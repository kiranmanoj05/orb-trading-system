"""Native commands are durable; no test contacts a provider."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.paper_evidence import EvidenceError, digest
from orb.paper_management import PaperManagement, capture_close_inventory
from orb.paper_trading import PaperEngine, PaperError
from orb.presets import PresetStore
from test_paper_trading import Broker, NOW, engine
from test_paper_evidence import seed, snapshot, capture

DAY=str(NOW.date());AT=NOW.isoformat();SCOPE=sha256(b'fixture-paper-account').hexdigest()


class InventoryBroker(Broker):
    def __init__(self):super().__init__();self.reads=[];self.hook=None
    def request(self,method,path,payload=None,**kw):
        if method=='GET':
            self.reads.append(path)
            if self.hook:self.hook(path)
        return super().request(method,path,payload,**kw)


def ready(tmp_path,terminal=True):
    path=tmp_path/'paper.sqlite';b=InventoryBroker();e=PaperEngine(path,b,observation_clock=lambda:0)
    row=seed(e);p=snapshot('canceled' if terminal else 'partially_filled','4','100')
    if terminal:
        for leg in p['legs']:leg['status']='canceled'
    b.parent=p;b.positions=[dict(symbol='SPY',qty='4')]
    capture(e,row,p)
    return e,PaperManagement(e.db),b,path


def preflight(b):return capture_close_inventory(b,SCOPE,'4',NOW,lambda:0)


def close(e,j,b,**kw):
    basis=j.basis(DAY);proof=preflight(b)
    return j.claim_close(DAY,kw.get('basis',basis),kw.get('proof',proof),kw.get('at',AT))


def test_worker_cancel_once_then_close_retains_full_reservation_and_freezes_commands(tmp_path):
    e,b=engine(tmp_path);e.tick(NOW);risk=e.status()['admission']['planned_stop_risk_usd']
    b.parent.update(status='partially_filled',filled_qty='4');e.tick(NOW)
    assert len(b.deletes)==3
    records=PaperManagement(e.db).export()['events'];assert [r['kind'] for r in records]==['cancel_intent','dispatch']
    assert all(r['outcome']=='request_returned' for r in records[-1]['results'])
    e.tick(NOW);assert len(b.deletes)==3 and len(b.posts)==1
    b.parent['status']='canceled'
    for leg in b.parent['legs']:leg['status']='canceled'
    b.positions=[dict(symbol='SPY',qty='4')];e.tick(NOW)
    assert len(b.posts)==2 and b.posts[-1]['side']=='sell' and b.posts[-1]['qty']=='4'
    r=e.status();assert r['admission']['planned_stop_risk_usd']==risk and r['management']['attempts'][0]['state']=='closing_response_observed'
    assert r['attempts'][0]['preset']==e.record
    assert len(PaperManagement(e.db).export()['events'])==4;e.close()


def test_cancel_timeout_records_unknown_and_unattempted_then_never_resends(tmp_path):
    e,b=engine(tmp_path);e.tick(NOW);b.parent.update(status='partially_filled',filled_qty='4');original=b.request
    def fail(method,path,payload=None,**kw):
        result=original(method,path,payload,**kw)
        if method=='DELETE':raise RuntimeError('PRIVATE provider credential text')
        return result
    b.request=fail
    with pytest.raises(PaperError,match='unknown'):e.tick(NOW)
    receipt=PaperManagement(e.db).export()['events'][-1]
    assert [r['outcome'] for r in receipt['results']]==['outcome_unknown','not_attempted','not_attempted']
    assert 'PRIVATE' not in json.dumps(receipt)
    assert e.status()['management']['attempts'][0]['state']=='cancellation_outcome_unknown'
    e.close();e,b=engine(tmp_path,b);e.tick(NOW)
    assert len(b.deletes)==1 and len(b.posts)==1 and e.status()['admission']['active_count']==1;e.close()


def test_close_timeout_is_durable_and_never_resubmits_after_restart(tmp_path):
    e,b=engine(tmp_path);e.tick(NOW);b.parent.update(status='canceled',filled_qty='4')
    for leg in b.parent['legs']:leg['status']='canceled'
    b.positions=[dict(symbol='SPY',qty='4')];b.fail_post=True
    with pytest.raises(PaperError,match='Closing outcome unknown'):e.tick(NOW)
    assert len(b.posts)==2 and e.status()['management']['attempts'][0]['state']=='closing_outcome_unknown'
    e.close();e,b=engine(tmp_path,b);b.closing=None;b.fail_post=False;e.tick(NOW)
    assert len(b.posts)==2 and 'not resent' in e.message and e.status()['admission']['active_count']==1;e.close()


def test_two_pass_inventory_records_context_and_scrubs_private_fields(tmp_path):
    e,j,b,_=ready(tmp_path);b.account['description']='SECRET';b.positions.append(dict(symbol='QQQ',qty='-2',description='SECRET'))
    b.open_orders=[dict(id='private-qqq-order',symbol='QQQ',qty='1',filled_qty='0',side='buy',status='new',description='SECRET')]
    proof=preflight(b)
    assert proof['state']=='two_agreeing_inventory_passes' and len(b.reads)==8
    assert proof['passes'][0]['stable']['holdings']=={'SPY':'4','QQQ':'-2'}
    text=json.dumps(proof);assert 'private-qqq-order' not in text and 'fixture-paper-account' not in text and 'SECRET' not in text
    assert not proof['recovery_clearance'] and not proof['fee_or_cash_reconciliation']
    c=j.claim_close(DAY,j.basis(DAY),proof,AT)
    assert c['request']['qty']=='4' and c['request']['symbol']=='SPY'
    assert not b.posts and not b.deletes
    assert 'orbp-fixture' not in json.dumps(j.export());e.close()


def test_changing_marked_equity_does_not_masquerade_as_quantity_change(tmp_path):
    e,j,b,_=ready(tmp_path)
    def change(path):
        if path=='/v2/account' and b.reads.count(path)==2:b.account.update(equity='99999.25',cash='99900')
    b.hook=change;proof=preflight(b)
    assert proof['state']=='two_agreeing_inventory_passes'
    assert proof['passes'][0]['observed_equity_usd']!=proof['passes'][1]['observed_equity_usd']
    assert j.claim_close(DAY,j.basis(DAY),proof,AT);e.close()


@pytest.mark.parametrize('case,state', [
    ('scope','close_account_scope_mismatch'),('currency','close_account_unavailable'),('blocked','close_account_unavailable'),
    ('missing_account','close_account_scope_mismatch'),('clock','close_clock_unavailable'),('closed','close_clock_unavailable'),
    ('wrong_quantity','close_position_order_mismatch'),('missing_spy','close_position_order_mismatch'),
    ('spy_order','close_position_order_mismatch'),('boundary','close_inventory_truncated_or_invalid'),
    ('duplicate_position','duplicate_close_position'),('bad_position','invalid_close_inventory'),
    ('bad_order','invalid_close_inventory'),('nan','invalid_order_number'),('read_failure','inventory_read_failed'),('read_evidence_error','inventory_read_failed')])
def test_incomplete_or_mismatched_inventory_never_authorises_close(tmp_path,case,state):
    e,j,b,_=ready(tmp_path)
    if case=='scope':b.account['id']='OTHER PRIVATE ACCOUNT'
    if case=='currency':b.account.pop('currency')
    if case=='blocked':b.account['trading_blocked']=True
    if case=='missing_account':b.account={}
    if case=='clock':b.clock['timestamp']=(NOW-timedelta(seconds=31)).isoformat()
    if case=='closed':b.clock['is_open']=False
    if case=='wrong_quantity':b.positions[0]['qty']='5'
    if case=='missing_spy':b.positions=[]
    if case=='spy_order':b.open_orders=[dict(id='other',symbol='SPY',qty='1',filled_qty='0',side='sell',status='new')]
    if case=='boundary':b.open_orders=[{}]*500
    if case=='duplicate_position':b.positions*=2
    if case=='bad_position':b.positions=[dict(symbol='PRIVATE!',qty='4')]
    if case=='bad_order':b.open_orders=[dict(id='other',symbol='QQQ',qty='1',filled_qty='0',side='buy',status='unknown')]
    if case=='nan':b.account['equity']='NaN'
    if case=='read_failure':b.hook=lambda _:(_ for _ in ()).throw(RuntimeError('SECRET credential body'))
    if case=='read_evidence_error':b.hook=lambda _:(_ for _ in ()).throw(EvidenceError('SECRET credential body'))
    proof=preflight(b);assert proof['state']==state and len(b.reads)<=8
    assert not j.claim_close(DAY,j.basis(DAY),proof,AT)
    assert e.status()['attempts'][0]['closing'] is None and not b.posts
    assert j.export()['events'][0]['kind']=='close_check' and 'SECRET' not in json.dumps(j.export())
    if case=='scope':assert len(b.reads)==2
    e.close()


def test_two_pass_quantity_or_order_inventory_change_is_refused(tmp_path):
    e,j,b,_=ready(tmp_path)
    def change(path):
        if path=='/v2/positions' and b.reads.count(path)==2:b.positions.append(dict(symbol='QQQ',qty='1'))
    b.hook=change;proof=preflight(b)
    assert proof['state']=='close_inventory_changed' and len(proof['passes'])==2
    assert not j.claim_close(DAY,j.basis(DAY),proof,AT);e.close()


@pytest.mark.parametrize('case',['basis','hold','expiry','clock','market_close','proof_quantity','closing_changed'])
def test_late_change_expired_or_conflicting_evidence_never_commits_close(tmp_path,case):
    e,j,b,_=ready(tmp_path);basis=j.basis(DAY);proof=preflight(b);at=AT
    if case=='basis':e.meta('new-risk-evidence','changed')
    if case=='hold':e.meta('fee-hold-reason','invalid_fee_evidence');e.meta('fee-hold-at',AT)
    if case=='expiry':at=(NOW+timedelta(seconds=31)).isoformat()
    if case=='clock':proof['passes'][-1]['broker_clock']=(NOW-timedelta(seconds=31)).isoformat()
    if case=='market_close':at=NOW.replace(hour=21,minute=0,second=0).isoformat()
    if case=='proof_quantity':proof['passes'][0]['stable']['holdings']['SPY']='5'
    if case=='closing_changed':e.db.execute('UPDATE paper_attempts SET closing=?',(json.dumps(dict(client_order_id='other')),))
    with pytest.raises(EvidenceError):j.claim_close(DAY,basis,proof,at)
    assert not j.export()['events'] and not b.posts;e.close()


def test_claim_is_unique_across_connections_and_preserves_frozen_watermark(tmp_path):
    e,j,b,path=ready(tmp_path);basis=j.basis(DAY);proof=preflight(b);e.close()
    def claim(_):
        e=PaperEngine(path)
        try:return PaperManagement(e.db).claim_close(DAY,basis,proof,AT) is not None
        except EvidenceError:return False
        finally:e.close()
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(claim,range(2)))
    assert sorted(results)==[False,True]
    e=PaperEngine(path);j=PaperManagement(e.db);first=j.export();intent=first['events'][0]
    j.dispatched(DAY,intent['seq'],'outcome_unknown',AT)
    assert not j.export(first['next_after'],first['through'])['events'] and len(j.export()['events'])==2
    assert j.report()['attempts'][0]['state']=='closing_outcome_unknown';e.close()


def test_cancel_targets_are_owned_deduplicated_and_scope_bound(tmp_path):
    e,j,b,_=ready(tmp_path,terminal=False)
    with pytest.raises(EvidenceError,match='not_owned'):j.claim_cancel(DAY,['foreign-id'],'partial_entry',AT,SCOPE)
    with pytest.raises(EvidenceError,match='scope_changed'):j.claim_cancel(DAY,['entry'],'partial_entry',AT,'other')
    c=j.claim_cancel(DAY,['entry','target','stop'],'partial_entry',AT,SCOPE)
    assert c['targets']==['entry','target','stop'] and len(j.export()['events'])==1
    assert j.claim_cancel(DAY,['target','stop'],'flatten_deadline',AT,SCOPE) is None
    assert len(j.export()['events'])==1 and not b.deletes;e.close()


def test_close_response_is_whitelisted_and_not_cancellation_or_fill_confirmation(tmp_path):
    e,j,b,_=ready(tmp_path);claim=close(e,j,b);request=claim['request']
    response=dict(id='private-close-id',client_order_id=request['client_order_id'],symbol='SPY',side='sell',qty='4',filled_qty='0',status='new',secret='SECRET')
    j.dispatched(DAY,claim['seq'],'request_returned',AT,response=response)
    assert j.report()['attempts'][0]['state']=='closing_response_observed'
    assert 'SECRET' not in json.dumps(j.export()) and 'private-close-id' not in json.dumps(j.export())
    assert e.status()['attempts'][0]['state']=='reserved' and e.status()['evidence']['attempts'][0]['net_pnl_usd'] is None
    j.dispatched(DAY,claim['seq'],'request_returned',AT,response=response)
    assert len(j.export()['events'])==2
    with pytest.raises(EvidenceError,match='conflict'):j.dispatched(DAY,claim['seq'],'outcome_unknown',AT)
    e.close()


def test_unverified_returned_body_is_never_labelled_a_verified_order(tmp_path):
    e,j,b,_=ready(tmp_path);claim=close(e,j,b)
    j.dispatched(DAY,claim['seq'],'request_returned',AT,response={'secret':'SECRET'})
    assert j.report()['attempts'][0]['state']=='closing_returned_unverified'
    assert j.export()['events'][-1]['response'] is None;e.close()


def test_capacity_blocks_intents_and_keeps_closing_column_atomic(tmp_path,monkeypatch):
    e,j,b,_=ready(tmp_path);monkeypatch.setattr('orb.paper_management.MAX_EVENTS',1)
    with pytest.raises(EvidenceError,match='capacity'):close(e,j,b)
    assert not j.export()['events'] and e.status()['attempts'][0]['closing'] is None
    monkeypatch.setattr('orb.paper_management.MAX_EVENTS',2);claim=close(e,j,b);j.dispatched(DAY,claim['seq'],'outcome_unknown',AT)
    assert len(j.export()['events'])==2;e.close()


@pytest.mark.parametrize('stage',['cancel_before_commit','close_before_column','close_before_commit','dispatch_before_commit'])
def test_actual_process_exit_rolls_back_partial_transition(tmp_path,stage):
    e,j,b,path=ready(tmp_path,terminal=not stage.startswith('cancel'))
    proof=preflight(b);basis=j.basis(DAY);intent=None
    if stage=='dispatch_before_commit':intent=j.claim_close(DAY,basis,proof,AT)['seq']
    e.close()
    code='''import os,sqlite3,json,sys
from orb.paper_management import PaperManagement
path,stage,day,at,scope,basis,raw,intent=sys.argv[1:]
db=sqlite3.connect(path,isolation_level=None);db.row_factory=sqlite3.Row
j=PaperManagement(db)
def crash(sql):
 if stage=='close_before_column' and sql.startswith('UPDATE paper_attempts SET closing='):os._exit(84)
 if stage!='close_before_column' and sql=='COMMIT':os._exit(84)
db.set_trace_callback(crash)
if stage.startswith('cancel'):j.claim_cancel(day,['entry','target','stop'],'partial_entry',at,scope)
elif stage=='dispatch_before_commit':j.dispatched(day,int(intent),'outcome_unknown',at)
else:j.claim_close(day,basis,json.loads(raw),at)
'''
    r=subprocess.run([sys.executable,'-c',code,str(path),stage,DAY,AT,SCOPE,basis,json.dumps(proof),str(intent)],capture_output=True)
    assert r.returncode==84,r.stderr
    e=PaperEngine(path);j=PaperManagement(e.db)
    if stage=='dispatch_before_commit':assert len(j.export()['events'])==1 and j.report()['attempts'][0]['state']=='closing_outcome_unknown'
    else:assert not j.export()['events'] and e.status()['attempts'][0]['closing'] is None
    e.close()


def test_committed_close_with_crash_before_post_is_never_automatically_dispatched(tmp_path):
    e,j,b,path=ready(tmp_path);close(e,j,b);e.close()
    e,b=engine(tmp_path,b);e.tick(NOW)
    assert not b.posts and not b.deletes and 'not resent' in e.message
    assert e.status()['management']['attempts'][0]['state']=='closing_outcome_unknown';e.close()


def test_changed_log_or_closing_intent_blocks_before_broker_read(tmp_path):
    e,j,b,path=ready(tmp_path);close(e,j,b);e.close()
    e,b=engine(tmp_path,b);b.reads=[];original=b.request
    def observe(*a,**k):b.reads.append(a);return original(*a,**k)
    b.request=observe;e.db.execute("UPDATE paper_attempts SET closing='{}'")
    with pytest.raises(EvidenceError,match='closing_intent_changed'):e.tick(NOW)
    assert not b.reads and not b.posts
    e.enabled=False
    with pytest.raises(EvidenceError):e.start(PresetStore.paper('paper-balanced'))
    e.close()


def test_append_only_and_corrupt_proof_are_detected(tmp_path):
    e,j,b,_=ready(tmp_path);close(e,j,b)
    with pytest.raises(sqlite3.IntegrityError,match='append-only'):e.db.execute('DELETE FROM paper_management_log')
    e.db.execute('DROP TRIGGER paper_observations_no_update');e.db.execute("UPDATE paper_observations SET fingerprint='bad'")
    with pytest.raises(EvidenceError,match='proof_changed'):j.report()
    e.close()


def test_schema6_upgrade_preserves_closing_latches_and_rolls_back_interruption(tmp_path,monkeypatch):
    e,j,b,path=ready(tmp_path);e.db.execute('DROP TABLE paper_restart_log');e.db.execute('DROP TABLE paper_management_log');e.db.execute('PRAGMA user_version=6')
    e.meta('loss-blocked:'+DAY,'yes');e.db.execute('UPDATE paper_attempts SET closing=?',(json.dumps(dict(client_order_id='legacy-close')),));e.close()
    import orb.paper_trading as m
    original=m.install_management
    def fail(db):original(db);raise RuntimeError('interrupted')
    monkeypatch.setattr(m,'install_management',fail)
    with pytest.raises(RuntimeError):PaperEngine(path)
    db=sqlite3.connect(path);assert db.execute('PRAGMA user_version').fetchone()[0]==6
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='paper_management_log'").fetchone();db.close()
    monkeypatch.setattr(m,'install_management',original);e=PaperEngine(path)
    assert e.db.execute('PRAGMA user_version').fetchone()[0]==8 and e.meta('loss-blocked:'+DAY)=='yes'
    assert e.status()['management']['legacy_closing_sessions']==[DAY] and not e.enabled;e.close()


def test_api_is_read_only_and_exports_bounded_fixed_cursor(tmp_path):
    e,j,b,path=ready(tmp_path);close(e,j,b);e.close()
    with TestClient(create_app(paper_db=path),base_url='http://127.0.0.1:8765') as c:
        r=c.get('/api/paper/management');assert r.status_code==200 and len(r.json()['events'])==1
        assert c.get('/api/paper/management?after=-1').status_code==422
        assert c.post('/api/paper/management',json={'retry':True}).status_code==405
        assert c.get('/api/paper/status').json()['management']['attempts'][0]['state']=='closing_outcome_unknown'


@pytest.mark.parametrize('command',['cancel','close'])
def test_real_crash_after_committed_claim_before_dispatch_never_replays(tmp_path,command):
    e,j,b,path=ready(tmp_path,terminal=command=='close');proof=preflight(b);basis=j.basis(DAY);e.close()
    code='''import os,sqlite3,json,sys
from orb.paper_management import PaperManagement
path,command,day,at,scope,basis,proof=sys.argv[1:]
db=sqlite3.connect(path,isolation_level=None);db.row_factory=sqlite3.Row
j=PaperManagement(db)
if command=='cancel':j.claim_cancel(day,['entry','target','stop'],'partial_entry',at,scope)
else:j.claim_close(day,basis,json.loads(proof),at)
os._exit(85)
'''
    r=subprocess.run([sys.executable,'-c',code,str(path),command,DAY,AT,SCOPE,basis,json.dumps(proof)],capture_output=True)
    assert r.returncode==85,r.stderr
    e,b=engine(tmp_path,b);e.tick(NOW)
    assert not b.posts and not b.deletes and len(PaperManagement(e.db).export()['events'])==1;e.close()


def test_stop_and_weekday_settings_gate_entries_without_skipping_management(tmp_path):
    e,b=engine(tmp_path);e.tick(NOW);e.stop()
    p=deepcopy(PresetStore.paper('paper-balanced'));p['settings']['weekdays']=[1,3]
    from orb.research_settings import ResearchSettings
    e.settings=ResearchSettings.from_dict(p['settings'])
    b.parent.update(status='partially_filled',filled_qty='4');e.tick(NOW)
    assert len(b.deletes)==3 and len(b.posts)==1 and not e.enabled
    assert e.status()['attempts'][0]['preset']['settings']['weekdays']==[0,1,2,3,4];e.close()


def test_cancel_proof_must_be_fresh(tmp_path):
    e,j,b,_=ready(tmp_path,terminal=False)
    with pytest.raises(EvidenceError,match='proof_expired'):
        j.claim_cancel(DAY,['entry'],'partial_entry',(NOW+timedelta(seconds=31)).isoformat(),SCOPE)
    assert not j.export()['events'] and not b.deletes;e.close()


def test_management_export_paginates_beyond_100_with_frozen_cutoff(tmp_path):
    e,j,b,_=ready(tmp_path);basis=j.basis(DAY);failed=dict(state='inventory_read_failed',passes=[])
    for _ in range(101):assert j.claim_close(DAY,basis,failed,AT) is None
    first=j.export();assert len(first['events'])==100 and first['has_more'] and first['through']==101
    j.claim_close(DAY,basis,failed,AT)
    tail=j.export(first['next_after'],first['through']);assert len(tail['events'])==1 and not tail['has_more']
    assert tail['events'][0]['seq']==101;e.close()
