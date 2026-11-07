"""Durable owned-order observations, correction holds and honest P&L eligibility."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.paper_trading import PaperEngine, PaperError
from orb.paper_evidence import EvidenceError
from orb.presets import PresetStore
from test_paper_trading import Broker, NOW, engine

DAY = str(NOW.date())


def seed(e):
    e.meta('account', sha256(b'fixture-paper-account').hexdigest())
    request = dict(symbol='SPY', qty='10', side='buy', client_order_id='orbp-fixture')
    e.db.execute('INSERT INTO paper_attempts VALUES (?,?,?,?,?,?)',
        (DAY, 'orbp-fixture', json.dumps(request), json.dumps(PresetStore.paper('paper-balanced')), None, 'reserved'))
    return e.db.execute('SELECT * FROM paper_attempts WHERE day=?', (DAY,)).fetchone()


def snapshot(status='new', qty='0', price=None):
    leg = lambda id: dict(id=id, symbol='SPY', side='sell', qty='10', filled_qty='0',
                         status='held', filled_avg_price=None, filled_at=None)
    return dict(id='entry', client_order_id='orbp-fixture', symbol='SPY', side='buy', qty='10',
        filled_qty=qty, filled_avg_price=price, status=status, filled_at=NOW.isoformat() if price else None,
        submitted_at=NOW.isoformat(), legs=[leg('target'), leg('stop')])


def closed():
    p = snapshot('filled', '10', '100')
    p['legs'][0].update(status='filled', filled_qty='10', filled_avg_price='102', filled_at=NOW.isoformat())
    p['legs'][1]['status'] = 'canceled'
    return p


def capture(e, row, parent, seconds=0, closing=None):
    e.evidence.capture(row, parent, closing, (NOW + timedelta(seconds=seconds)).isoformat())


def test_append_only_dedup_seen_time_and_decimal_normalisation_survive_reopen(tmp_path):
    path=tmp_path/'p.sqlite';e=PaperEngine(path);row=seed(e)
    p=snapshot();capture(e,row,p)
    p['qty']='10.0';p['filled_qty']='0.000';p['legs'].reverse()
    capture(e,row,p,1)
    assert len(e.evidence.events()['observations'])==1
    assert e.evidence.report()['attempts'][0]['observed_at']==(NOW+timedelta(seconds=1)).isoformat()
    for sql in ('DELETE FROM paper_observations', "UPDATE paper_observations SET payload='{}'"):
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):e.db.execute(sql)
    e.close();e=PaperEngine(path)
    assert len(e.evidence.events()['observations'])==1
    assert not e.enabled and not e.evidence.holds()
    e.close()


def test_closed_price_evidence_reports_gross_estimate_but_never_net_or_account_return(tmp_path):
    e=PaperEngine(tmp_path/'p.sqlite');row=seed(e);capture(e,row,closed())
    r=e.evidence.report();a=r['attempts'][0]
    assert a['gross_price_pnl_usd']=='20' and a['net_pnl_usd'] is None
    assert a['state']=='closed_price_evidence' and a['entry_filled']==a['exit_filled']=='10'
    assert 'total_return' not in r and 'equity' not in r
    assert all(o['attribution']=='matched_durable_intent' for o in r['orders'])
    assert 'orbp-fixture' not in json.dumps(r) and 'fixture-paper-account' not in json.dumps(r)
    e.close()


@pytest.mark.parametrize('condition,expected', [
    ('not_seen','not_observed'), ('not_found','order_not_found'), ('pending','open_or_pending'),
    ('open','open_or_pending'), ('partial_exit','open_or_pending'), ('no_entry','known_no_entry'),
    ('missing_price','missing_fill_price'), ('zero_placeholder','missing_fill_price'),
    ('missing_legs','incomplete_bracket'), ('terminal_mismatch','terminal_quantity_mismatch')])
def test_unknown_open_or_incomplete_evidence_never_becomes_zero_profit(tmp_path,condition,expected):
    e=PaperEngine(tmp_path/'p.sqlite');row=seed(e);p=closed()
    if condition=='not_seen':pass
    elif condition=='not_found':capture(e,row,None)
    else:
        if condition=='pending':p=snapshot()
        if condition=='open':p=snapshot('filled','10','100')
        if condition=='partial_exit':p['legs'][0].update(status='partially_filled',filled_qty='4')
        if condition=='no_entry':
            p=snapshot('canceled')
            for leg in p['legs']:leg['status']='canceled'
        if condition=='missing_price':p['legs'][0]['filled_avg_price']=None
        if condition=='zero_placeholder':p['filled_avg_price']='0'
        if condition=='missing_legs':p['legs']=[]
        if condition=='terminal_mismatch':p['qty']='11';request=json.loads(row['request']);request['qty']='11';e.db.execute('UPDATE paper_attempts SET request=?',(json.dumps(request),));row=e.db.execute('SELECT * FROM paper_attempts').fetchone()
        capture(e,row,p)
    a=e.evidence.report()['attempts'][0]
    assert a['state']==expected
    assert a['gross_price_pnl_usd']==('0' if condition=='no_entry' else None)
    assert a['net_pnl_usd'] is None
    e.close()


def test_partial_entry_then_reconciled_close_uses_actual_filled_quantity(tmp_path):
    e=PaperEngine(tmp_path/'p.sqlite');row=seed(e)
    p=snapshot('canceled','4','100')
    for leg in p['legs']:leg['status']='canceled'
    closing=dict(id='close', client_order_id='orbp-fixture-x',symbol='SPY',side='sell',qty='4',
                 filled_qty='4',filled_avg_price='99',status='filled',filled_at=NOW.isoformat())
    e.db.execute('UPDATE paper_attempts SET closing=?',(json.dumps(dict(qty='4',client_order_id='orbp-fixture-x')),))
    row=e.db.execute('SELECT * FROM paper_attempts').fetchone()
    capture(e,row,p,closing=closing)
    assert e.evidence.report()['attempts'][0]['gross_price_pnl_usd']=='-4'
    e.close()


@pytest.mark.parametrize('change,reason', [('regress','cumulative_quantity_decreased'),
    ('price','average_changed_without_fill'), ('terminal','terminal_order_changed'),
    ('entry_id','order_identity_changed'), ('exit_id','bracket_identity_changed'),
    ('replace','replaced_order')])
def test_corrections_preserve_history_and_hold_across_restart_even_after_good_refresh(tmp_path,change,reason):
    path=tmp_path/'p.sqlite';e=PaperEngine(path);row=seed(e);p=closed();capture(e,row,p)
    bad=deepcopy(p)
    if change=='regress':bad['legs'][0]['filled_qty']='9'
    if change=='price':bad['filled_avg_price']='101'
    if change=='terminal':bad['status']='new'
    if change=='entry_id':bad['id']='different'
    if change=='exit_id':bad['legs'][1]['id']='different'
    if change=='replace':bad['replaced_by']='replacement'
    capture(e,row,bad,1)
    assert e.evidence.report()['attempts'][0]['gross_price_pnl_usd'] is None
    assert e.evidence.holds()[0]['reason']==reason
    capture(e,row,p,2)
    assert e.evidence.holds() and len(e.evidence.events()['observations'])==3
    e.close();e=PaperEngine(path)
    assert e.evidence.holds()[0]['reason']==reason
    with pytest.raises(PaperError,match='correction'):e.start(PresetStore.paper('paper-balanced'))
    e.close()


def test_transient_missing_response_does_not_forget_cumulative_fill_or_leg_identity(tmp_path):
    e=PaperEngine(tmp_path/'p.sqlite');row=seed(e);p=closed();capture(e,row,p)
    capture(e,row,None,1)
    bad=deepcopy(p);bad['legs'][1]['id']='changed-after-gap'
    capture(e,row,bad,2)
    assert e.evidence.holds()[0]['reason']=='bracket_identity_changed'
    e.close()


@pytest.mark.parametrize('change', ['unknown_status','bad_price','duplicate','future_stamp','wrong_client'])
def test_invalid_broker_shape_never_persists_private_payload_and_latches_hold(tmp_path,change):
    e=PaperEngine(tmp_path/'p.sqlite');row=seed(e);p=snapshot()
    if change=='unknown_status':p['status']='arbitrary-private-status'
    if change=='bad_price':p['filled_avg_price']='private-invalid-number'
    if change=='duplicate':p['legs'][1]['id']=p['legs'][0]['id']
    if change=='future_stamp':p['filled_at']=(NOW+timedelta(seconds=30)).isoformat()
    if change=='wrong_client':p['client_order_id']='external-order'
    with pytest.raises(EvidenceError):capture(e,row,p)
    assert e.evidence.holds() and not e.evidence.events()['observations']
    assert 'private' not in json.dumps(e.evidence.holds())
    e.close()


def test_export_pagination_uses_fixed_watermark_without_duplicate_or_late_events(tmp_path):
    e=PaperEngine(tmp_path/'p.sqlite');row=seed(e);p=snapshot();capture(e,row,p)
    p['status']='accepted';capture(e,row,p,1)
    first=e.evidence.events(limit=1);assert first['has_more']
    p['status']='new';capture(e,row,p,2)
    rest=e.evidence.events(first['next_after'],limit=1,through=first['through'])
    assert not rest['has_more'] and len(rest['observations'])==1
    assert rest['observations'][0]['seq']!=first['observations'][0]['seq']
    assert e.evidence.events()['through']>first['through']
    with pytest.raises(EvidenceError):e.evidence.events(-1)
    e.close()


def test_capacity_and_clock_fail_closed_without_losing_last_good_evidence(tmp_path,monkeypatch):
    e=PaperEngine(tmp_path/'p.sqlite');row=seed(e);p=snapshot();capture(e,row,p)
    monkeypatch.setattr('orb.paper_evidence.MAX_EVENTS',1)
    p['status']='accepted';capture(e,row,p,1)
    assert e.evidence.holds()[0]['reason']=='paper_evidence_capacity_reached'
    assert len(e.evidence.events()['observations'])==1
    e.close()


def test_v1_migration_preserves_intents_account_and_loss_latch(tmp_path):
    path=tmp_path/'p.sqlite';db=sqlite3.connect(path)
    db.execute('CREATE TABLE paper_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    db.execute('CREATE TABLE paper_attempts (day TEXT PRIMARY KEY, client_id TEXT UNIQUE NOT NULL, request TEXT NOT NULL, preset TEXT NOT NULL, closing TEXT, state TEXT NOT NULL)')
    db.execute('PRAGMA application_id=1330795600');db.execute('PRAGMA user_version=1')
    db.execute('INSERT INTO paper_meta VALUES (?,?)',('loss-blocked:'+DAY,'yes'))
    db.execute('INSERT INTO paper_attempts VALUES (?,?,?,?,?,?)',(DAY,'legacy',json.dumps(dict(qty='10')),json.dumps(PresetStore.paper('paper-balanced')),None,'done'))
    db.commit();db.close()
    e=PaperEngine(path)
    assert e.meta('loss-blocked:'+DAY)=='yes' and e.db.execute('PRAGMA user_version').fetchone()[0]==4
    assert e.evidence.report()['attempts'][0]['state']=='not_observed'
    assert not e.enabled and not e.evidence.events()['observations']
    e.close()


def test_worker_captures_before_commands_and_persisted_regression_blocks_restart(tmp_path):
    e,b=engine(tmp_path);e.tick(NOW);e.tick(NOW)
    assert e.evidence.events()['observations']
    b.parent.update(status='partially_filled',filled_qty='4',filled_avg_price='100')
    e.tick(NOW)
    before=(len(b.posts),len(b.deletes))
    b.parent['filled_qty']='3'
    with pytest.raises(PaperError,match='correction'):e.tick(NOW)
    assert (len(b.posts),len(b.deletes))==before and e.evidence.holds()
    e.close();e,b=engine(tmp_path,b)
    with pytest.raises(PaperError,match='correction'):e.tick(NOW)
    assert (len(b.posts),len(b.deletes))==before
    e.close()


def test_explicit_refresh_is_get_only_scope_checked_and_survives_api_restart(tmp_path):
    path=tmp_path/'p.sqlite';e=PaperEngine(path);seed(e);e.close()
    def app():return create_app(paper_db=path)
    with TestClient(app(),base_url='http://127.0.0.1:8765') as c:
        b=Broker();b.parent=closed();c.app.state.paper_engine.client=b
        r=c.post('/api/paper/evidence/refresh',json={'session':DAY})
        assert r.status_code==200,r.text
        assert r.json()['attempts'][0]['gross_price_pnl_usd']=='20'
        assert not b.posts and not b.deletes and not c.get('/api/paper/status').json()['enabled']
        exported=c.get('/api/paper/evidence').json();assert len(exported['observations'])==1
        assert c.post('/api/paper/evidence/refresh',json={'session':DAY},headers={'Origin':'https://evil.example'}).status_code==403
        assert c.post('/api/paper/evidence/refresh',json={'session':'wrong'}).status_code==422
        assert c.get('/api/paper/evidence?after=-1').status_code==422
        b.account['id']='external-account'
        assert c.post('/api/paper/evidence/refresh',json={'session':DAY}).status_code==409
        assert len(c.get('/api/paper/evidence').json()['observations'])==1
    with TestClient(app(),base_url='http://127.0.0.1:8765') as c:
        assert c.get('/api/paper/status').json()['evidence']['attempts'][0]['gross_price_pnl_usd']=='20'
        assert c.get('/api/paper/evidence').json()==exported
        assert not c.get('/api/paper/status').json()['enabled']


def test_nonmonotonic_observation_clock_holds_without_replacing_newer_head(tmp_path):
    e=PaperEngine(tmp_path/'p.sqlite');row=seed(e);p=snapshot();capture(e,row,p,2)
    capture(e,row,p,1)
    assert e.evidence.holds()[0]['reason']=='observation_clock_reversed'
    assert e.evidence.report()['attempts'][0]['observed_at']==(NOW+timedelta(seconds=2)).isoformat()
    e.close()


@pytest.mark.parametrize('field,value', [('limit_price','103'),('stop_price','97'),('type','market')])
def test_changed_known_order_terms_create_a_durable_hold(tmp_path,field,value):
    e=PaperEngine(tmp_path/'p.sqlite');row=seed(e);p=snapshot()
    p.update(type='limit',limit_price='101',stop_price='98');capture(e,row,p)
    p[field]=value;capture(e,row,p,1)
    assert e.evidence.holds()[0]['reason']=='order_terms_changed'
    e.close()


def test_v1_migration_is_atomic_on_failure_and_refuses_active_old_worker(tmp_path,monkeypatch):
    import fcntl
    path=tmp_path/'old.sqlite';db=sqlite3.connect(path)
    db.execute('CREATE TABLE paper_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    db.execute('CREATE TABLE paper_attempts (day TEXT PRIMARY KEY, client_id TEXT UNIQUE NOT NULL, request TEXT NOT NULL, preset TEXT NOT NULL, closing TEXT, state TEXT NOT NULL)')
    db.execute('PRAGMA application_id=1330795600');db.execute('PRAGMA user_version=1');db.commit();db.close()
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(PaperError,match='existing paper worker'):PaperEngine(path)
    def fail(db):
        db.execute('CREATE TABLE partial_upgrade(x)')
        raise RuntimeError('Injected migration interruption')
    monkeypatch.setattr('orb.paper_trading.install_evidence',fail)
    with pytest.raises(RuntimeError):PaperEngine(path)
    db=sqlite3.connect(path)
    assert db.execute('PRAGMA user_version').fetchone()[0]==1
    assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}=={'paper_meta','paper_attempts'}
    db.close()


def test_head_corruption_returns_503_and_cannot_start_a_broker_worker(tmp_path):
    path=tmp_path/'p.sqlite';e=PaperEngine(path);row=seed(e);capture(e,row,closed());e.close()
    db=sqlite3.connect(path);db.execute('DROP TRIGGER paper_observations_no_update')
    db.execute("UPDATE paper_observations SET fingerprint='bad'");db.commit();db.close()
    with TestClient(create_app(paper_db=path),base_url='http://127.0.0.1:8765') as c:
        assert c.get('/api/paper/status').status_code==503
        assert c.get('/api/paper/evidence').status_code==503
        assert c.post('/api/paper/start',json=dict(preset_id='paper-balanced',version=1,feed='iex')).status_code==503
        assert c.app.state.paper_engine.thread is None


def test_same_quantity_timestamp_correction_never_silently_retimes_a_fill(tmp_path):
    e=PaperEngine(tmp_path/'p.sqlite');row=seed(e);p=closed();capture(e,row,p)
    p['filled_at']=(NOW-timedelta(seconds=1)).isoformat();capture(e,row,p,1)
    assert e.evidence.holds()[0]['reason']=='fill_timestamp_changed_without_fill'
    assert e.evidence.report()['attempts'][0]['gross_price_pnl_usd'] is None
    e.close()


def test_inflight_fill_uses_response_completion_clock_not_tick_start(tmp_path):
    e,b=engine(tmp_path);seed(e);b.parent=closed()
    b.parent['filled_at']=(NOW+timedelta(seconds=1)).isoformat()
    times=iter([0,2])
    e.observation_clock=lambda:next(times)
    e.tick(NOW)
    assert not e.evidence.holds()
    assert e.evidence.events()['observations'][0]['observed_at']==(NOW+timedelta(seconds=2)).isoformat()
    assert not b.posts and not b.deletes
    e.close()


def test_read_only_refresh_refuses_a_competing_worker_lease(tmp_path):
    import fcntl
    e=PaperEngine(tmp_path/'p.sqlite');seed(e);e.client=Broker();e.client.parent=closed()
    with e.path.with_suffix('.lock').open('a') as fd:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(PaperError,match='Another paper worker'):e.refresh_evidence(DAY)
    assert not e.evidence.events()['observations']
    e.refresh_evidence(DAY)
    assert e.evidence.report()['attempts'][0]['gross_price_pnl_usd']=='20'
    assert not e.client.posts and not e.client.deletes
    e.close()


def test_process_exit_between_observation_and_head_commit_rolls_back_atomically(tmp_path):
    import subprocess, sys
    path=tmp_path/'p.sqlite';e=PaperEngine(path);row=seed(e);p=snapshot();capture(e,row,p);e.close()
    p['status']='accepted'
    script='''
import json,os,sys
from orb.paper_trading import PaperEngine
engine=PaperEngine(sys.argv[1])
row=engine.db.execute('SELECT * FROM paper_attempts').fetchone()
def crash(sql):
    if sql.startswith('INSERT OR REPLACE INTO paper_observation_heads'):os._exit(17)
engine.db.set_trace_callback(crash)
engine.evidence.capture(row,json.loads(sys.argv[2]),None,sys.argv[3])
'''
    result=subprocess.run([sys.executable,'-c',script,str(path),json.dumps(p),(NOW+timedelta(seconds=1)).isoformat()],capture_output=True,text=True)
    assert result.returncode==17,result.stderr
    e=PaperEngine(path)
    assert len(e.evidence.events()['observations'])==1
    assert e.evidence.report()['attempts'][0]['orders'][0]['status']=='new'
    row=e.db.execute('SELECT * FROM paper_attempts').fetchone();capture(e,row,p,2)
    assert len(e.evidence.events()['observations'])==2 and not e.evidence.holds()
    e.close()


def test_malformed_json_evidence_returns_sanitised_503(tmp_path):
    path=tmp_path/'p.sqlite';e=PaperEngine(path);row=seed(e);capture(e,row,closed());e.close()
    db=sqlite3.connect(path);db.execute('DROP TRIGGER paper_observations_no_update')
    db.execute("UPDATE paper_observations SET payload='private invalid JSON'");db.commit();db.close()
    with TestClient(create_app(paper_db=path),base_url='http://127.0.0.1:8765') as c:
        r=c.get('/api/paper/status')
        assert r.status_code==503 and 'private' not in r.text
        assert c.get('/api/paper/evidence').status_code==503
