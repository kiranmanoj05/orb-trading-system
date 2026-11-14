"""Account-wide cash arithmetic is evidence, never a restart/hold-clearance token."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.paper_cash import PaperCash, bridge, collect, fetch, normalize
from orb.paper_evidence import EvidenceError
from orb.paper_restart import basis
from orb.paper_trading import PaperEngine, PaperError
from orb.presets import PresetStore
from test_paper_trading import NOW
from test_paper_restart import InventoryBroker


class CashBroker(InventoryBroker):
    def __init__(self):super().__init__();self.pages=[[]];self.page_calls=[];self.now=NOW
    def request(self,method,path,payload=None,**kw):
        if path.startswith('/v2/account/activities?'):
            assert method=='GET';self.page_calls.append(path)
            page=self.pages[min(len(self.page_calls)-1,len(self.pages)-1)]
            if isinstance(page,Exception):raise page
            return deepcopy(page)
        return super().request(method,path,payload,**kw)


def setup(tmp_path):
    b=CashBroker();e=PaperEngine(tmp_path/'p.sqlite',b,review_clock=lambda:b.now,observation_clock=lambda:0)
    return e,b


def advance(b,seconds=60):
    b.now=NOW+timedelta(seconds=seconds);b.clock['timestamp']=b.now.isoformat();b.page_calls=[]


def fill(key='fill',symbol='SPY',side='buy',qty='2',price='100',seconds=30,order='external'):
    return dict(id=key,activity_type='FILL',symbol=symbol,side=side,qty=qty,price=price,
                transaction_time=(NOW+timedelta(seconds=seconds)).isoformat(),order_id=order,type='fill')


def latest(e):return PaperCash(e.db).report()['latest']


def two(tmp_path,events=None,cash='99800',positions=None):
    e,b=setup(tmp_path);e.refresh_cash();advance(b);b.pages=[events if events is not None else [fill()]]
    b.account['cash']=cash;b.positions=positions if positions is not None else [dict(symbol='SPY',qty='2')]
    e.refresh_cash();return e,b


def test_first_read_persists_only_opening_anchor_without_history_orders_or_worker_start(tmp_path):
    e,b=setup(tmp_path);s=e.refresh_cash();r=latest(e)
    assert r['result']['state']=='opening_anchor_saved' and s['cash_bridge']['anchor_seq']==1
    assert e.meta('account') and not b.page_calls and not b.posts and not b.deletes and e.thread is None and not e.enabled
    assert not r['recovery_cleared'] and not r['settled_cash_verified'] and r['net_pnl_usd'] is None
    e.close()


def test_account_wide_buy_cash_and_position_deltas_agree_without_orb_attribution(tmp_path):
    e,b=two(tmp_path);r=latest(e);v=r['result']
    assert v['state']=='observed_deltas_agree' and v['observed_cash_delta_usd']==v['modeled_gross_fill_cash_usd']=='-200'
    assert v['unexplained_cash_usd']=='0' and v['positions'][0]['residual_quantity']=='0'
    assert r['capture']['events'][0]['attribution']=='account_only_unattributed'
    assert r['anchor_seq']==1 and not r['fee_coverage_verified'] and not e.restart_blocked and not b.posts;e.close()


def test_account_wide_round_trip_and_foreign_short_are_separate_from_strategy_profit(tmp_path):
    events=[fill(),fill('exit',side='sell',price='102',seconds=35),fill('qqq',symbol='QQQ',side='sell',qty='1',price='400',seconds=40)]
    e,b=two(tmp_path,events,cash='100404',positions=[dict(symbol='QQQ',qty='-1')]);v=latest(e)['result']
    assert v['state']=='observed_deltas_agree' and v['modeled_gross_fill_cash_usd']=='404' and v['included_fills']==3
    assert latest(e)['net_pnl_usd'] is None and e.status()['attempts']==[];e.close()


def test_observed_empty_interval_is_zero_arithmetic_not_a_fee_completeness_claim(tmp_path):
    e,b=two(tmp_path,[],cash='100000',positions=[]);r=latest(e)
    assert r['result']['state']=='observed_deltas_agree' and r['result']['modeled_gross_fill_cash_usd']=='0'
    assert not r['settled_cash_verified'] and not r['fee_coverage_verified'];e.close()


@pytest.mark.parametrize('cash,positions,amount',[('99799.9',[dict(symbol='SPY',qty='2')],'-0.1'),('99800',[], '0')])
def test_unexplained_cash_or_quantity_is_not_silently_balanced_or_a_correction_clearance(tmp_path,cash,positions,amount):
    e,b=two(tmp_path,cash=cash,positions=positions);v=latest(e)['result']
    assert v['state']=='unexplained_difference' and v['unexplained_cash_usd']==amount
    # A diagnostic mismatch is not proof of a revision: late postings can explain it.
    assert not e.evidence.holds() and latest(e)['net_pnl_usd'] is None;e.close()


@pytest.mark.parametrize('kind',['FEE','CSD','DIV','JNLS','OPASN','NEWTYPE'])
def test_nontrade_date_and_creation_time_never_become_exact_cash_booking_or_zero_unknown(tmp_path,kind):
    event=dict(id='n',activity_type=kind,net_amount='-0.1',currency='USD',date='2026-11-02',
               created_at=(NOW+timedelta(seconds=20)).isoformat(),status='executed',secret='SECRET')
    e,b=two(tmp_path,[fill(),event],cash='99799.9');r=latest(e)
    assert r['result']['state']=='activity_timing_or_effect_unknown' and r['result']['uncertain_records']==1
    assert r['result']['unexplained_cash_usd'] is None and not r['result']['arithmetic_eligible']
    assert r['capture']['events'][1]['signed_cash_amount']=='-0.1' and 'SECRET' not in json.dumps(r)
    assert not e.evidence.holds() and r['net_pnl_usd'] is None;e.close()


@pytest.mark.parametrize('seconds',[-1,0,60])
def test_pre_anchor_and_boundary_fills_are_not_assumed_inside_cash_snapshot(tmp_path,seconds):
    e,b=two(tmp_path,[fill(seconds=seconds)],cash='100000',positions=[]);r=latest(e)['result']
    assert r['included_fills']==0
    assert r['state']==('observed_deltas_agree' if seconds<0 else 'activity_timing_or_effect_unknown');e.close()


@pytest.mark.parametrize('symbol',['BTC/USD','SPY261120C00600000','ES1'])
def test_unsupported_instrument_cash_multiplier_stays_unknown(tmp_path,symbol):
    e,b=two(tmp_path,[fill(symbol=symbol)],cash='100000',positions=[])
    assert latest(e)['result']['state']=='activity_timing_or_effect_unknown' and latest(e)['capture']['events'][0]['gross_fill_cash_usd'] is None;e.close()


def test_creation_query_frozen_bounds_and_raw_cursor_with_sanitised_ids(tmp_path,monkeypatch):
    from urllib.parse import urlparse,parse_qs
    monkeypatch.setattr('orb.paper_cash.PAGE_SIZE',1)
    e,b=setup(tmp_path);e.refresh_cash();advance(b);b.pages=[[fill('first')],[fill('second',side='sell',seconds=40)],[]]
    e.refresh_cash();r=latest(e);queries=[parse_qs(urlparse(p).query) for p in b.page_calls]
    assert len(queries)==3 and queries[1]['page_token']==['first'] and queries[2]['page_token']==['second']
    assert len({q['until'][0] for q in queries})==1 and all('activity_types' not in q and 'order_id' not in q for q in queries)
    assert 'first' not in json.dumps(r['capture']['events']) and r['capture']['scan']['scan_finished'];e.close()


@pytest.mark.parametrize('kind',['read','repeat','limit','shape','invalid','future'])
def test_partial_or_invalid_scans_preserve_explicit_unavailability_and_no_broker_mutation(tmp_path,monkeypatch,kind):
    e,b=setup(tmp_path);e.refresh_cash();advance(b)
    if kind=='read':b.pages=[PaperError('SECRET transport')]
    if kind=='repeat':
        monkeypatch.setattr('orb.paper_cash.PAGE_SIZE',1);b.pages=[[fill()],[fill()]]
    if kind=='limit':
        monkeypatch.setattr('orb.paper_cash.PAGE_SIZE',1);monkeypatch.setattr('orb.paper_cash.MAX_PAGES',1);b.pages=[[fill()]]
    if kind=='shape':b.pages=[{'secret':'SECRET'}]
    if kind=='invalid':b.pages=[[dict(fill(),price='NaN',secret='SECRET')]]
    if kind=='future':b.pages=[[fill(seconds=61)]]
    e.refresh_cash();r=latest(e)
    assert r['result']['state'] in ('activity_scan_incomplete','correction_hold') and not r['capture']['scan']['scan_finished']
    assert 'SECRET' not in json.dumps(r) and not b.posts and not b.deletes and r['net_pnl_usd'] is None;e.close()


def test_revised_activity_creates_sticky_account_hold_and_supersedes_restart_and_review_basis(tmp_path):
    e,b=two(tmp_path);before=basis(e.db);advance(b,120);b.pages=[[fill(price='101')]];e.enabled=True;e.refresh_cash()
    assert latest(e)['result']['state']=='correction_hold' and e.evidence.holds()[0]['reason']=='cash_activity_changed'
    assert not e.enabled and e.restart_blocked and basis(e.db)!=before
    with pytest.raises(PaperError,match='correction hold'):e.start(PresetStore.paper('paper-balanced'))
    e.close();e=PaperEngine(tmp_path/'p.sqlite');assert e.evidence.holds() and not e.enabled;e.close()


def test_missing_record_after_an_intervening_failed_scan_retains_history_and_hold(tmp_path):
    e,b=two(tmp_path);advance(b,120);b.pages=[PaperError('offline')];e.refresh_cash()
    advance(b,180);b.pages=[[]];e.refresh_cash()
    assert e.evidence.holds()[0]['reason']=='cash_activity_missing' and len(PaperCash(e.db).export()['events'])==4;e.close()


def test_changing_attribution_alone_is_not_a_broker_activity_revision(tmp_path):
    from orb.paper_cash import event_fingerprint
    a=normalize(fill(),{},(NOW+timedelta(seconds=60)).isoformat())
    b=normalize(fill(),{a['order_id']:dict(symbol='SPY',side='buy')},(NOW+timedelta(seconds=60)).isoformat())
    assert a['attribution']!=b['attribution'] and event_fingerprint(a)==event_fingerprint(b)


def test_moving_cash_during_anchor_read_is_not_saved_as_valid_anchor(tmp_path):
    e,b=setup(tmp_path);count=[]
    def hook(path):
        if path=='/v2/account':
            count.append(1);b.account['cash']=str(100000-len(count))
    b.hook=hook;e.refresh_cash()
    assert latest(e)['result']['state']=='inventory_unavailable' and PaperCash(e.db).anchor() is None and not b.page_calls;e.close()


def test_marked_equity_changes_do_not_masquerade_as_cash_or_quantity_changes(tmp_path):
    e,b=setup(tmp_path)
    def hook(path):
        if path=='/v2/account':b.account['equity']=str(Decimal(b.account['equity'])+1)
    b.hook=hook;e.refresh_cash();assert latest(e)['result']['state']=='opening_anchor_saved';e.close()


def test_scope_change_is_never_bound_as_success_or_allowed_to_start(tmp_path):
    e,b=setup(tmp_path);e.refresh_cash();saved=e.meta('account');advance(b);b.account['id']='other';e.refresh_cash()
    assert e.meta('account')==saved and latest(e)['result']['state']=='inventory_unavailable' and not b.page_calls and not e.enabled;e.close()


def test_existing_hold_is_never_cleared_and_manual_read_does_not_rearm(tmp_path):
    e,b=setup(tmp_path)
    from test_paper_evidence import seed
    seed(e);e.db.execute('INSERT INTO paper_evidence_holds VALUES (?,?,?)',('2026-11-02','fixture',NOW.isoformat()))
    e.refresh_cash();advance(b);e.refresh_cash()
    assert e.evidence.holds() and not latest(e)['recovery_cleared'] and not e.enabled and e.restart_required
    assert latest(e)['anchor_seq']==1 and PaperCash(e.db).anchor()['seq']==1;e.close()


def test_network_has_no_writer_lease_and_concurrent_saved_changes_abort_commit(tmp_path):
    e,b=setup(tmp_path);seen=[]
    def hook(path):
        if seen:return
        seen.append(True)
        def other():
            with e.lock:
                with e.evidence_write_lease():e.meta('concurrent','change')
        with ThreadPoolExecutor() as pool:pool.submit(other).result(timeout=2)
    b.hook=hook
    with pytest.raises(EvidenceError,match='cash_basis_changed'):e.refresh_cash()
    assert not PaperCash(e.db).export()['events'] and not b.posts;e.close()


def test_capacity_is_sticky_without_eviction_or_reusable_permission(tmp_path,monkeypatch):
    e,b=setup(tmp_path);e.refresh_cash();monkeypatch.setattr('orb.paper_cash.MAX_CHECKS',1)
    advance(b);e.refresh_cash();assert len(PaperCash(e.db).export()['events'])==1
    assert e.evidence.holds()[0]['reason']=='cash_capacity_reached';e.close()


def test_atomic_initial_scope_and_anchor_rollback_on_actual_process_exit(tmp_path):
    e,b=setup(tmp_path);expected=basis(e.db);capture=collect(b,None,None,{},NOW,lambda:0);e.close()
    script='''import os,sqlite3,json,sys
from orb.paper_cash import PaperCash
p,b,c,at=sys.argv[1:]
db=sqlite3.connect(p,isolation_level=None);db.row_factory=sqlite3.Row
def crash(sql):
 if sql.startswith('INSERT INTO paper_cash_log'):os._exit(88)
db.set_trace_callback(crash)
PaperCash(db).save(b,json.loads(c),at)
'''
    r=subprocess.run([sys.executable,'-c',script,str(tmp_path/'p.sqlite'),expected,json.dumps(capture),NOW.isoformat()],capture_output=True)
    assert r.returncode==88,r.stderr
    e=PaperEngine(tmp_path/'p.sqlite');assert not PaperCash(e.db).export()['events'] and e.meta('account') is None;e.close()


def test_bounded_time_expiry_and_invalid_windows_cannot_be_agreement(tmp_path):
    e,b=setup(tmp_path);expected=basis(e.db);capture=collect(b,None,None,{},NOW,lambda:0)
    PaperCash(e.db).save(expected,capture,(NOW+timedelta(seconds=61)).isoformat())
    assert latest(e)['result']['state']=='cash_capture_expired' and PaperCash(e.db).anchor() is None;e.close()
    e,b=setup(tmp_path/'other');e.refresh_cash();e.refresh_cash();assert latest(e)['result']['state']=='invalid_cash_window';e.close()


def test_api_empty_body_origin_export_and_read_never_starts_or_releases_risk(tmp_path):
    with TestClient(create_app(paper_db=tmp_path/'api.sqlite'),base_url='http://127.0.0.1:8765') as c:
        e=c.app.state.paper_engine;b=CashBroker();e.client=b;e.review_clock=lambda:b.now;e.observation_clock=lambda:0
        assert c.post('/api/paper/cash/check',json={'clear_holds':True}).status_code==422
        assert c.post('/api/paper/cash/check',headers={'Origin':'https://evil.invalid'}).status_code==403
        r=c.post('/api/paper/cash/check');assert r.status_code==200 and r.json()['cash_bridge']['anchor_seq']==1 and not r.json()['enabled']
        assert c.get('/api/paper/cash?after=-1').status_code==422 and len(c.get('/api/paper/cash').json()['events'])==1
        assert not b.posts and not b.deletes and e.thread is None


def test_schema8_migration_is_atomic_preserves_latches_and_never_invents_anchor(tmp_path,monkeypatch):
    e,b=setup(tmp_path);e.meta('loss-blocked:2026-11-02','yes');e.db.execute('DROP TABLE paper_cash_log');e.db.execute('PRAGMA user_version=8');e.close()
    import orb.paper_trading as module
    install=module.install_cash
    def fail(db):install(db);raise RuntimeError('crash')
    monkeypatch.setattr(module,'install_cash',fail)
    with pytest.raises(RuntimeError):PaperEngine(tmp_path/'p.sqlite')
    db=sqlite3.connect(tmp_path/'p.sqlite');assert db.execute('PRAGMA user_version').fetchone()[0]==8
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='paper_cash_log'").fetchone();db.close()
    monkeypatch.setattr(module,'install_cash',install);e=PaperEngine(tmp_path/'p.sqlite')
    assert e.meta('loss-blocked:2026-11-02')=='yes' and e.db.execute('PRAGMA user_version').fetchone()[0]==9 and PaperCash(e.db).anchor() is None;e.close()


def test_append_only_frozen_watermark_and_corrupt_record_fail_closed(tmp_path):
    e,b=setup(tmp_path);e.refresh_cash();j=PaperCash(e.db);first=j.export();advance(b);e.refresh_cash()
    assert not j.export(first['next_after'],first['through'])['events']
    with pytest.raises(sqlite3.IntegrityError,match='append-only'):e.db.execute('DELETE FROM paper_cash_log')
    e.db.execute('DROP TRIGGER paper_cash_log_no_update');e.db.execute("UPDATE paper_cash_log SET fingerprint='wrong'")
    with pytest.raises(EvidenceError,match='integrity'):e.status()
    e.close()


def test_activity_read_budget_preserves_incomplete_scan_without_network(tmp_path):
    e,b=setup(tmp_path)
    events,scan=fetch(b,NOW.isoformat(),(NOW+timedelta(seconds=60)).isoformat(),{},lambda:53)
    assert not events and scan['failure']=='cash_read_timeout' and not b.page_calls;e.close()


def test_changed_duplicate_in_one_page_is_retained_and_held(tmp_path):
    e,b=two(tmp_path,[fill(),fill(price='101')]);r=latest(e)
    assert r['result']['state']=='correction_hold' and len(r['capture']['events'])==2
    assert e.evidence.holds()[0]['reason']=='cash_activity_changed';e.close()


def test_fixed_anchor_does_not_roll_forward_or_erase_unexplained_intervals(tmp_path):
    e,b=two(tmp_path,cash='99799');assert latest(e)['result']['state']=='unexplained_difference'
    advance(b,120);b.account['cash']='99798';e.refresh_cash()
    assert latest(e)['anchor_seq']==1 and latest(e)['result']['unexplained_cash_usd']=='-2';e.close()


def test_clock_reversal_and_stale_check_never_replace_newer_receipt(tmp_path):
    e,b=setup(tmp_path);expected=basis(e.db);capture=collect(b,None,None,{},NOW,lambda:0)
    with pytest.raises(EvidenceError,match='clock_reversed'):PaperCash(e.db).save(expected,capture,(NOW-timedelta(seconds=1)).isoformat())
    e.refresh_cash();advance(b);e.refresh_cash()
    with pytest.raises(EvidenceError,match='stale_cash_scan'):PaperCash(e.db).save(basis(e.db),capture,b.now.isoformat())
    assert len(PaperCash(e.db).export()['events'])==2;e.close()


def test_cash_checkpoint_supersedes_prepared_review_without_clearing_hold(tmp_path):
    from test_paper_reviews import setup as review_setup,latest as review_latest
    e,_,_=review_setup(tmp_path);e.prepare_review('2026-11-02');assert not review_latest(e)['stale']
    e.client=CashBroker();e.review_clock=lambda:NOW;e.observation_clock=lambda:0;e.refresh_cash()
    assert review_latest(e)['stale'] and e.evidence.holds() and not latest(e)['recovery_cleared'];e.close()


def test_large_export_has_frozen_second_page_and_no_silent_eviction(tmp_path):
    e,b=setup(tmp_path);e.refresh_cash()
    for i in range(1,102):advance(b,i);e.refresh_cash()
    j=PaperCash(e.db);first=j.export();assert len(first['events'])==100 and first['has_more']
    advance(b,120);e.refresh_cash();second=j.export(first['next_after'],first['through'])
    assert [r['seq'] for r in second['events']]==[101,102] and not second['has_more'] and first['through']==102;e.close()


def test_detected_revision_hold_and_receipt_rollback_together_on_actual_exit(tmp_path):
    e,b=two(tmp_path);advance(b,120);b.pages=[[fill(price='101')]]
    expected=basis(e.db);capture=collect(b,e.meta('account'),PaperCash(e.db).anchor(),{},b.now,lambda:0);at=b.now.isoformat();e.close()
    code="""import os,sqlite3,json,sys
from orb.paper_cash import PaperCash
p,b,c,at=sys.argv[1:]
db=sqlite3.connect(p,isolation_level=None);db.row_factory=sqlite3.Row
def crash(sql):
 if sql.startswith('INSERT INTO paper_cash_log'):os._exit(89)
db.set_trace_callback(crash)
PaperCash(db).save(b,json.loads(c),at)
"""
    r=subprocess.run([sys.executable,'-c',code,str(tmp_path/'p.sqlite'),expected,json.dumps(capture),at],capture_output=True)
    assert r.returncode==89,r.stderr
    e=PaperEngine(tmp_path/'p.sqlite');assert not e.evidence.holds() and len(PaperCash(e.db).export()['events'])==2
    assert latest(e)['result']['state']=='observed_deltas_agree';e.close()
