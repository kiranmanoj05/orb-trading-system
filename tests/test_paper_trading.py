"""Paper-only dispatcher regression: never contact any broker in tests."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from orb.acquisition import Credentials
from orb.domain import Bar
from orb.paper_trading import PaperClient, PaperEngine, PaperError, BROKER
from orb.presets import PresetStore
from orb.session import exchange_session
from orb.dashboard_api import create_app

NOW = datetime(2026, 11, 2, 14, 46, 15, tzinfo=timezone.utc)
SESSION = exchange_session(NOW.date())
D = Decimal


class Broker:
    def __init__(self):
        self.clock = {'timestamp': NOW.isoformat(), 'is_open': True}
        self.account = dict(id='fixture-paper-account', equity='100000', cash='100000', status='ACTIVE',
                            trading_blocked=False, account_blocked=False)
        self.positions, self.open_orders = [], []
        self.posts, self.deletes = [], []
        self.parent = self.closing = None
        self.fail_post = False
        self.quote = dict(t=NOW.isoformat(), ap='101.02', bp='101.01')
        self.series = [Bar('SPY', SESSION.opens_at + timedelta(minutes=i), D('99'), D('100'), D('98'), D('99'), 100)
                       for i in range(15)]
        self.series.append(Bar('SPY', SESSION.range_end, D('100'), D('101.03'), D('100'), D('101.02'), 300))

    def request(self, method, path, payload=None, **kwargs):
        if method == 'POST':
            self.posts.append(deepcopy(payload))
            if payload['side'] == 'buy':
                self.parent = dict(id='parent', symbol='SPY', side='buy', qty=payload['qty'], client_order_id=payload['client_order_id'], status='new', filled_qty='0', submitted_at=NOW.isoformat(),
                                   legs=[dict(symbol='SPY',side='sell',id='tp', status='held', filled_qty='0'), dict(symbol='SPY',side='sell',id='sl', status='held', filled_qty='0')])
            else:
                self.closing = dict(id='closing', symbol='SPY', side='sell', qty=payload['qty'], client_order_id=payload['client_order_id'], status='new', filled_qty='0')
            if self.fail_post: raise PaperError('Timeout after broker accepted order.')
            return self.parent if payload['side'] == 'buy' else self.closing
        if method == 'DELETE': self.deletes.append(path); return None
        if path == '/v2/clock': return self.clock
        if path == '/v2/account': return self.account
        if path == '/v2/positions': return self.positions
        if path.startswith('/v2/orders?'): return self.open_orders
        if path.startswith('/v2/stocks/SPY/quotes'): return {'quote': self.quote}
        if path == '/v2/assets/SPY': return {'tradable': True}
        raise AssertionError((method, path))

    def bars(self, *args): return self.series
    def find(self, client_id): return self.closing if client_id.endswith('-x') else self.parent
    def order(self, _): return self.parent


def engine(tmp_path, broker=None):
    b = broker or Broker()
    e = PaperEngine(tmp_path / 'paper.sqlite', b)
    e.settings = e.validate(PresetStore.paper('paper-balanced'), 'iex')
    e.record = PresetStore.paper('paper-balanced')
    e.enabled = True
    return e, b


def test_fresh_signal_posts_capped_bracket_with_durable_exact_preset(tmp_path):
    e,b=engine(tmp_path)
    e.tick(NOW)
    assert len(b.posts)==1
    p=b.posts[0]
    assert p['symbol']=='SPY' and p['order_class']=='bracket' and p['type']=='limit'
    assert p['extended_hours'] is False and p['time_in_force']=='gtc'
    q, entry, stop = D(p['qty']), D(p['limit_price']), D(p['stop_loss']['stop_price'])
    assert q * entry <= D('75000')
    assert q * (entry-stop) <= D('500')
    assert D(p['take_profit']['limit_price']) >= entry + 2*(entry-stop)
    assert e.status()['attempts'][0]['preset']['settings_sha256'] == e.record['settings_sha256']
    e.tick(NOW)
    assert len(b.posts)==1
    e.close()


def test_uncertain_post_is_recovered_without_resubmission_across_restart(tmp_path):
    e,b=engine(tmp_path); b.fail_post=True
    with pytest.raises(PaperError): e.tick(NOW)
    e.close()
    e,b=engine(tmp_path,b); b.fail_post=False
    e.tick(NOW)
    assert len(b.posts)==1 and 'Managing' in e.message
    e.close()


def test_unknown_post_not_found_blocks_future_entries(tmp_path):
    e,b=engine(tmp_path);e.tick(NOW);b.parent=None
    e.tick(NOW)
    assert len(b.posts)==1 and 'not found' in e.message
    e.close()


def test_no_order_from_old_signal_weekend_or_closed_market(tmp_path):
    e,b=engine(tmp_path)
    later=NOW+timedelta(minutes=3);b.clock['timestamp']=later.isoformat();e.tick(later)
    assert 'missed' in e.message and not b.posts
    sunday=NOW-timedelta(days=1);b.clock.update(timestamp=sunday.isoformat(),is_open=False);e.tick(sunday)
    assert not b.posts and 'Waiting' in e.message
    e.close()


@pytest.mark.parametrize('change', ['positions','orders','missing_range','gap','stale_quote','crossed_quote','spread','blocked','clock'])
def test_invalid_or_external_evidence_never_submits(tmp_path,change):
    e,b=engine(tmp_path)
    if change=='positions': b.positions=[dict(symbol='QQQ',qty='1')]
    if change=='orders': b.open_orders=[dict(symbol='SPY')]
    if change=='missing_range': b.series.pop(2)
    if change=='gap':
        last=b.series[-1]
        b.series[-1]=Bar('SPY',last.start+timedelta(minutes=1),last.open,last.high,last.low,last.close,last.volume)
    if change=='stale_quote': b.quote['t']=(NOW-timedelta(seconds=16)).isoformat()
    if change=='crossed_quote': b.quote['bp']='102'
    if change=='spread': b.quote['bp']='100'
    if change=='blocked': b.account['trading_blocked']=True
    if change=='clock': b.clock['timestamp']=(NOW-timedelta(minutes=2)).isoformat()
    try: e.tick(NOW)
    except ValueError: pass
    assert not b.posts
    e.close()


def test_day_attempt_remains_consumed_after_target_and_preset_change(tmp_path):
    e,b=engine(tmp_path);e.tick(NOW)
    b.parent.update(status='filled',filled_qty='10')
    b.parent['legs']=[dict(symbol='SPY',side='sell',id='tp',status='filled',filled_qty='10'), dict(symbol='SPY',side='sell',id='sl',status='canceled',filled_qty='0')]
    e.record=PresetStore.paper('paper-active')
    e.tick(NOW)
    assert len(b.posts)==1 and e.status()['attempts'][0]['state']=='done'
    e.close()


def test_stop_prevents_entries_without_canceling_protection(tmp_path):
    e,b=engine(tmp_path);e.stop();e.tick(NOW)
    assert not b.posts and not b.deletes
    e.enabled=True;e.tick(NOW);e.stop()
    b.parent.update(status='filled',filled_qty='10')
    e.tick(NOW)
    assert len(b.posts)==1 and not b.deletes
    e.close()


def test_partial_fill_cancellation_confirmed_before_residual_close(tmp_path):
    e,b=engine(tmp_path);e.tick(NOW)
    b.parent.update(status='partially_filled',filled_qty='4')
    e.tick(NOW)
    assert b.deletes and len(b.posts)==1
    b.parent['status']='canceled'
    for leg in b.parent['legs']:leg['status']='canceled'
    b.positions=[dict(symbol='SPY',qty='4')]
    e.tick(NOW)
    assert len(b.posts)==2 and b.posts[-1]['qty']=='4' and b.posts[-1]['side']=='sell'
    e.tick(NOW)
    assert len(b.posts)==2
    b.closing.update(status='filled',filled_qty='4');b.positions=[]
    e.tick(NOW)
    assert e.status()['attempts'][0]['state']=='done'
    e.close()


def test_eod_waits_for_exit_cancellation_then_checks_actual_residual(tmp_path):
    e,b=engine(tmp_path);e.tick(NOW)
    b.parent.update(status='filled',filled_qty='10')
    end=SESSION.flatten_at+timedelta(seconds=1);b.clock['timestamp']=end.isoformat()
    e.tick(end)
    assert len(b.posts)==1 and b.deletes
    b.parent['legs'][0].update(status='filled',filled_qty='3')
    b.parent['legs'][1]['status']='canceled'
    b.positions=[dict(symbol='SPY',qty='7')]
    e.tick(end)
    assert b.posts[-1]['side']=='sell' and b.posts[-1]['qty']=='7'
    e.close()


def test_reconciliation_mismatch_never_flattens_external_quantity(tmp_path):
    e,b=engine(tmp_path);e.tick(NOW)
    b.parent.update(status='canceled',filled_qty='4')
    for leg in b.parent['legs']:leg['status']='canceled'
    b.positions=[dict(symbol='SPY',qty='5')]
    with pytest.raises(PaperError,match='reconciliation'):e.tick(NOW)
    assert len(b.posts)==1
    e.close()


def test_account_identity_cannot_change(tmp_path):
    e,b=engine(tmp_path);e.tick(NOW);b.account['id']='other'
    with pytest.raises(PaperError,match='different'): e.tick(NOW)
    assert len(b.posts)==1
    e.close()


def test_three_starter_presets_are_versioned_and_enforce_worker_caps():
    with PresetStore() as store:
        for id in ('paper-calm','paper-balanced','paper-active'):
            p=store.get(id,1);s=PaperEngine.validate(p,'iex')
            assert p['readonly'] and s.max_session_entries==1
            assert store.history(id)==[p]
            dup=store.duplicate(id,1,'my experiment')
            assert dup['settings']==p['settings']
        with pytest.raises(PaperError): PaperEngine.validate(store.reference(),'iex')


@pytest.mark.parametrize('status',[301,401,403,422,429,500])
def test_http_client_never_redirects_or_exposes_broker_body(status):
    calls=[]
    def transport(method,url,headers,payload):
        calls.append(url);return status,b'private broker body'
    c=PaperClient(Credentials('fixture-key-abcdef','fixture-secret-abcdef'),transport)
    with pytest.raises(PaperError) as exc:c.request('POST','/v2/orders',{'side':'buy'})
    assert calls==[BROKER+'/v2/orders'] and 'private' not in str(exc.value)


def test_local_api_requires_storage_exact_preset_and_origin(tmp_path,monkeypatch):
    monkeypatch.delenv('APCA_API_KEY_ID',raising=False)
    app=create_app(paper_db=tmp_path/'p.sqlite')
    with TestClient(app,base_url='http://127.0.0.1:8765') as c:
        assert c.get('/api/paper/status').json()['available']
        assert not c.get('/api/paper/status').json()['enabled']
        assert c.post('/api/paper/start',json={'preset_id':'paper-balanced','version':1,'feed':'iex'},headers={'origin':'https://evil.test'}).status_code==403
        assert c.post('/api/paper/start',json={'preset_id':'paper-balanced','version':1,'feed':'iex'}).status_code==409
        assert c.post('/api/paper/start',json={'preset_id':'orb-reference','version':1,'feed':'iex'}).status_code==409
        assert c.post('/api/paper/start',json={'preset_id':'paper-balanced','version':True,'feed':'iex'}).status_code==422
        assert c.post('/api/paper/stop').status_code==200


def test_second_worker_cannot_own_same_state_and_start_freezes_settings(tmp_path):
    first,b=engine(tmp_path)
    first.enabled=False
    record=PresetStore.paper('paper-balanced')
    first.start(record)
    record['settings']['risk_fraction']='0.99'
    assert first.record['settings']['risk_fraction']=='0.005'
    second=PaperEngine(tmp_path/'paper.sqlite', Broker())
    with pytest.raises(PaperError,match='Another paper worker'): second.start(PresetStore.paper('paper-balanced'))
    second.close();first.close()


def test_unknown_closing_post_is_never_resent_after_restart(tmp_path):
    e,b=engine(tmp_path);e.tick(NOW)
    b.parent.update(status='canceled',filled_qty='4')
    for leg in b.parent['legs']:leg['status']='canceled'
    b.positions=[dict(symbol='SPY',qty='4')];b.fail_post=True
    with pytest.raises(PaperError):e.tick(NOW)
    e.close();e,b=engine(tmp_path,b);b.closing=None
    e.tick(NOW)
    assert len(b.posts)==2 and 'Uncertain closing' in e.message
    e.close()


def test_state_path_cannot_be_preset_database_or_symlink(tmp_path):
    path=tmp_path/'p.sqlite'
    with PresetStore(path):pass
    original=path.read_bytes()
    with pytest.raises(PaperError,match='Unrecognised'):PaperEngine(path)
    assert path.read_bytes()==original
    link=tmp_path/'link.sqlite';link.symlink_to(path)
    with pytest.raises(PaperError,match='symlinks'):PaperEngine(link)


def test_replaced_or_mismatched_order_never_canceled_or_closed(tmp_path):
    e,b=engine(tmp_path);e.tick(NOW)
    b.parent['client_order_id']='external-order'
    with pytest.raises(PaperError,match='identity'):e.tick(NOW)
    assert len(b.posts)==1 and not b.deletes
    e.close()


def test_market_open_does_not_request_inverted_bar_window(tmp_path):
    e,b=engine(tmp_path)
    now=SESSION.opens_at+timedelta(seconds=5);b.clock['timestamp']=now.isoformat()
    def no_bars(*args):raise AssertionError('No minute has completed yet.')
    b.bars=no_bars
    e.tick(now)
    assert e.market['candles']==[] and e.enabled and not b.posts
    e.close()


def test_real_json_adapter_handles_decimal_bars_quotes_and_posts_paper_bracket(tmp_path):
    b=Broker();calls=[]
    def transport(method,url,headers,payload):
        calls.append((method,url))
        path=urlsplit(url).path
        if path=='/v2/stocks/SPY/bars':
            value={'bars':[dict(t=x.start.isoformat(),o=float(x.open),h=float(x.high),
                               l=float(x.low),c=float(x.close),v=x.volume) for x in b.series],
                   'next_page_token':None}
        elif path=='/v2/stocks/SPY/quotes/latest':
            value={'quote':{**b.quote,'ap':float(b.quote['ap']),'bp':float(b.quote['bp'])}}
        else:
            parsed=urlsplit(url)
            route=parsed.path+('?' + parsed.query if parsed.query else '')
            value=b.request(method,route,payload)
        return 200,json.dumps(value).encode()
    c=PaperClient(Credentials('fixture-key-abcdef','fixture-secret-abcdef'),transport)
    e,b=engine(tmp_path,b);e.client=c
    e.tick(NOW)
    assert len(b.posts)==1
    assert all(urlsplit(u).netloc in ('data.alpaca.markets','paper-api.alpaca.markets') for _,u in calls)
    assert calls[-1]==('POST',BROKER+'/v2/orders')
    e.close()


@pytest.mark.parametrize('legs', [None, [], [dict(symbol='SPY', side='sell', id='sl', status='canceled', filled_qty='0')]])
def test_incomplete_bracket_evidence_never_proves_safe_to_sell(tmp_path, legs):
    e, b = engine(tmp_path)
    e.tick(NOW)
    b.parent.update(status='filled', filled_qty='4', legs=legs)
    b.positions = [dict(symbol='SPY', qty='4')]
    end = SESSION.flatten_at + timedelta(seconds=1)
    b.clock['timestamp'] = end.isoformat()
    with pytest.raises(PaperError, match='Incomplete bracket'):
        e.tick(end)
    assert len(b.posts) == 1 and not b.deletes
    assert e.status()['attempts'][0]['state'] != 'done'
    e.close()


def test_duplicate_exit_identity_cannot_double_count_fills(tmp_path):
    e, b = engine(tmp_path)
    e.tick(NOW)
    b.parent.update(status='filled', filled_qty='4')
    leg = dict(symbol='SPY', side='sell', id='tp', status='filled', filled_qty='2')
    b.parent['legs'] = [leg, dict(leg)]
    with pytest.raises(PaperError, match='duplicate'):
        e.tick(NOW)
    assert e.status()['attempts'][0]['state'] != 'done' and len(b.posts) == 1
    e.close()


def test_loss_of_one_exit_cancels_remaining_exit_before_close(tmp_path):
    e, b = engine(tmp_path)
    e.tick(NOW)
    b.parent.update(status='filled', filled_qty='4')
    b.parent['legs'][1]['status'] = 'rejected'
    b.positions = [dict(symbol='SPY', qty='4')]
    e.tick(NOW)
    assert b.deletes == ['/v2/orders/tp'] and len(b.posts) == 1
    b.parent['legs'][0]['status'] = 'canceled'
    e.tick(NOW)
    assert b.posts[-1]['side'] == 'sell' and b.posts[-1]['qty'] == '4'
    e.close()


@pytest.mark.parametrize('change', ['rejected', 'canceled_partial', 'replaced', 'wrong_qty', 'overfill'])
def test_closing_failures_or_changed_identity_never_resubmit(tmp_path, change):
    e, b = engine(tmp_path)
    e.tick(NOW)
    b.parent.update(status='canceled', filled_qty='4')
    for leg in b.parent['legs']:
        leg['status'] = 'canceled'
    b.positions = [dict(symbol='SPY', qty='4')]
    e.tick(NOW)
    assert len(b.posts) == 2
    if change == 'rejected': b.closing['status'] = 'rejected'
    if change == 'canceled_partial': b.closing.update(status='canceled', filled_qty='2')
    if change == 'replaced': b.closing['replaced_by'] = 'unknown-replacement'
    if change == 'wrong_qty': b.closing['qty'] = '5'
    if change == 'overfill': b.closing['filled_qty'] = '5'
    with pytest.raises(PaperError, match='Closing order'):
        e.tick(NOW)
    e.close()
    e, b = engine(tmp_path, b)
    with pytest.raises(PaperError, match='Closing order'):
        e.tick(NOW)
    assert len(b.posts) == 2 and e.status()['attempts'][0]['state'] != 'done'
    e.close()


@pytest.mark.parametrize('elapsed', [16, 120])
def test_slow_provider_requests_cannot_use_tick_start_freshness(tmp_path, monkeypatch, elapsed):
    e, b = engine(tmp_path)
    times = iter([0, elapsed])
    monkeypatch.setattr('orb.paper_trading.monotonic', lambda: next(times))
    with pytest.raises(PaperError, match='expired during requests'):
        e.tick(NOW)
    assert not b.posts and not e.status()['attempts']
    e.close()


def test_request_crossing_entry_cutoff_cannot_submit(tmp_path, monkeypatch):
    e, b = engine(tmp_path)
    now = SESSION.entry_cutoff - timedelta(seconds=1)
    # First breakout at 10:58 (available 10:59), all prior minutes intact.
    last = b.series.pop()
    b.series.extend(Bar('SPY', SESSION.range_end + timedelta(minutes=i), D('99'), D('100'), D('98'), D('99'), 100)
                    for i in range(73))
    b.series.append(Bar('SPY', SESSION.entry_cutoff - timedelta(minutes=2), last.open,
                        last.high, last.low, last.close, last.volume))
    b.clock['timestamp'] = b.quote['t'] = now.isoformat()
    times = iter([0, 2])
    monkeypatch.setattr('orb.paper_trading.monotonic', lambda: next(times))
    with pytest.raises(PaperError, match='expired during requests'):
        e.tick(now)
    assert not b.posts and not e.status()['attempts']
    e.close()


def test_daily_loss_block_survives_equity_recovery_restart_and_looser_preset(tmp_path):
    e, b = engine(tmp_path)
    # Record the opening observation without emitting a candidate.
    last = b.series[-1]
    b.series[-1] = Bar('SPY', last.start, D('99'), D('100'), D('98'), D('99'), 300)
    e.tick(NOW)
    b.account['equity'] = b.account['cash'] = '98400'
    e.tick(NOW)
    assert 'Daily loss' in e.message and not b.posts
    e.close()
    e, b = engine(tmp_path, b)
    e.settings = e.validate(PresetStore.paper('paper-active'), 'iex')
    b.account['equity'] = b.account['cash'] = '100000'
    b.series[-1] = last
    e.tick(NOW)
    assert 'Daily loss' in e.message and not b.posts
    assert e.meta('loss-budget:' + str(NOW.date())) == '1500.000'
    e.close()


def test_day_loss_budget_can_tighten_but_never_widen_before_breach(tmp_path):
    e, b = engine(tmp_path)
    last = b.series[-1]
    b.series[-1] = Bar('SPY', last.start, D('99'), D('100'), D('98'), D('99'), 300)
    e.tick(NOW)
    e.settings = e.validate(PresetStore.paper('paper-calm'), 'iex')
    e.tick(NOW)
    e.settings = e.validate(PresetStore.paper('paper-active'), 'iex')
    b.account['equity'] = b.account['cash'] = '98900'
    b.series[-1] = last
    e.tick(NOW)
    assert 'Daily loss' in e.message and not b.posts
    assert D(e.meta('loss-budget:' + str(NOW.date()))) == D('1000')
    e.close()


def test_loss_block_is_ny_session_scoped_and_never_blocks_existing_management(tmp_path):
    e, b = engine(tmp_path)
    e.tick(NOW)
    e.meta('loss-blocked:' + str(NOW.date()), 'yes')
    b.parent.update(status='partially_filled', filled_qty='4')
    e.tick(NOW)
    assert b.deletes and len(b.posts) == 1
    # A separate clean state has yesterday's latch but today's new session.
    e.close()
    fresh = tmp_path / 'next-state'
    fresh.mkdir()
    e, b = engine(fresh)
    e.meta('loss-blocked:' + str(NOW.date()), 'yes')
    tomorrow = NOW + timedelta(days=1)
    b.clock['timestamp'] = b.quote['t'] = tomorrow.isoformat()
    b.series = [Bar(x.symbol, x.start + timedelta(days=1), x.open, x.high, x.low, x.close, x.volume)
                for x in b.series]
    e.tick(tomorrow)
    assert len(b.posts) == 1
    e.close()
