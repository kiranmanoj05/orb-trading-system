"""Correction review is evidence, never a hold-clear or order capability."""
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.paper_evidence import EvidenceError
from orb.paper_reviews import PaperReviews, ReviewConflict, basis, capture_preflight, record_hold
from orb.paper_trading import PaperEngine, PaperError
from orb.presets import PresetStore
from test_paper_evidence import seed, capture, closed, DAY
from test_paper_trading import Broker, NOW

AT=(NOW+timedelta(minutes=1)).isoformat()


class ReadOnly(Broker):
    def __init__(self):super().__init__();self.account['currency']='USD';self.reads=[];self.account_reads=0;self.change=False
    def request(self,method,path,payload=None,**kw):
        assert method=='GET'
        self.reads.append(path)
        if path=='/v2/account':
            self.account_reads+=1
            if self.change and self.account_reads==2:return dict(self.account,cash='99999')
        return super().request(method,path,payload,**kw)


def setup(tmp_path):
    path=tmp_path/'p.sqlite';e=PaperEngine(path,ReadOnly(),review_clock=lambda:NOW+timedelta(minutes=1))
    row=seed(e);capture(e,row,closed())
    p=closed();p['filled_avg_price']='99';capture(e,row,p,1)
    return e,PaperReviews(e.db),path


def latest(e):return e.evidence.report()['attempts'][0]['correction_review']['cases'][0]


def test_hold_history_records_later_reasons_and_deduplicates_same_basis(tmp_path):
    e,j,_=setup(tmp_path)
    e.db.execute('BEGIN IMMEDIATE')
    record_hold(e.db,DAY,'later_disagreement',AT,'fill_activity')
    record_hold(e.db,DAY,'later_disagreement',AT,'fill_activity')
    e.db.execute('COMMIT')
    h=j.hold_history(DAY)
    assert [v['reason'] for v in h['events']]==['average_changed_without_fill','later_disagreement']
    assert e.evidence.holds()[0]['reason']=='average_changed_without_fill'
    assert not h['legacy_history'] and not h['incomplete'];e.close()


def test_prepare_and_record_are_durable_read_only_and_keep_all_risk_state(tmp_path):
    e,j,path=setup(tmp_path);e.meta('loss-blocked:'+DAY,'yes')
    original_attempt=dict(e.db.execute('SELECT * FROM paper_attempts').fetchone())
    old_holds=deepcopy(e.evidence.holds())
    e.prepare_review(DAY);c=latest(e)
    assert c['preflight']['state']=='flat_two_passes' and len(e.client.reads)==6 and not c['stale']
    assert c['fees_usd'] is c['net_pnl_usd'] is None
    e.record_review(c['case_id'],c['fingerprint'],'keep_blocked')
    assert latest(e)['decision']['disposition']=='keep_blocked'
    assert not e.enabled and not e.client.posts and not e.client.deletes
    assert e.evidence.holds()==old_holds and e.meta('loss-blocked:'+DAY)=='yes'
    assert dict(e.db.execute('SELECT * FROM paper_attempts').fetchone())==original_attempt
    e.close();e=PaperEngine(path,review_clock=lambda:NOW+timedelta(minutes=1))
    assert latest(e)['decision']['disposition']=='keep_blocked'
    with pytest.raises(PaperError,match='correction hold'):e.start(PresetStore.paper('paper-balanced'))
    assert e.thread is None;e.close()


@pytest.mark.parametrize('kind,state,budget', [('positions','inventory_present',3),('orders','inventory_present',3),
    ('boundary','inventory_truncated',3),('blocked','account_blocked',3),('changed','account_changed',6),
    ('scope','account_scope_mismatch',1),('bad_number','preflight_read_failed',1),('malformed','preflight_read_failed',3)])
def test_preflight_refuses_incomplete_or_nonflat_inventory(tmp_path,kind,state,budget):
    e,j,_=setup(tmp_path);b=e.client
    if kind=='positions':b.positions=[{'private':'not-exported'}]
    if kind=='orders':b.open_orders=[{'private':'not-exported'}]
    if kind=='boundary':b.open_orders=[{}]*500
    if kind=='blocked':b.account['trading_blocked']=True
    if kind=='changed':b.change=True
    if kind=='scope':b.account['id']='other-private-id'
    if kind=='bad_number':b.account['cash']='NaN'
    if kind=='malformed':b.open_orders={'secret':'not-exported'}
    e.prepare_review(DAY);c=latest(e)
    assert c['preflight']['state']==state and len(b.reads)==budget
    assert 'not-exported' not in json.dumps(c) and 'other-private-id' not in json.dumps(c)
    assert e.evidence.holds();e.close()


def test_network_failure_is_saved_as_review_evidence_without_private_error(tmp_path):
    e,j,_=setup(tmp_path)
    class Broken(ReadOnly):
        def request(self,*a,**k):raise RuntimeError('private exposed credential body')
    e.client=Broken();e.prepare_review(DAY);c=latest(e)
    assert c['preflight']['state']=='preflight_read_failed'
    assert 'credential' not in json.dumps(c)
    e.record_review(c['case_id'],c['fingerprint'],'request_provider_review')
    assert e.evidence.holds() and not e.enabled;e.close()


@pytest.mark.parametrize('mutation',['order','activity','hold','attempt','loss','account'])
def test_any_bound_evidence_change_refuses_stale_review(tmp_path,mutation):
    e,j,_=setup(tmp_path);e.prepare_review(DAY);c=latest(e)
    if mutation=='order':
        p=closed();p['filled_avg_price']='98';capture(e,e.db.execute('SELECT * FROM paper_attempts').fetchone(),p,2)
    if mutation=='activity':
        from orb.paper_activities import PaperActivities
        PaperActivities(e.db).save(DAY,[],dict(after=NOW.isoformat(),until=AT,scan_finished=False,pages=0,received=0,matched=0,failure='activity_read_failed'),AT)
    if mutation=='hold':record_hold(e.db,DAY,'another_reason',AT,'order_observation')
    if mutation=='attempt':e.db.execute("UPDATE paper_attempts SET state='changed'")
    if mutation=='loss':e.meta('loss-blocked:'+DAY,'yes')
    if mutation=='account':e.meta('account','another-scope')
    with pytest.raises(ReviewConflict,match='changed or expired'):j.record(c['case_id'],c['fingerprint'],'keep_blocked',AT)
    assert latest(e)['stale'] and not latest(e)['decision'];e.close()


@pytest.mark.parametrize('seconds',[121,-1])
def test_expired_or_future_case_cannot_be_recorded(tmp_path,seconds):
    e,j,_=setup(tmp_path);e.prepare_review(DAY);c=latest(e)
    with pytest.raises(ReviewConflict,match='expired'):j.record(c['case_id'],c['fingerprint'],'keep_blocked',(NOW+timedelta(minutes=1,seconds=seconds)).isoformat())
    assert not j.case(c['case_id'],AT)['decision'];e.close()


def test_idempotent_decision_retry_is_historical_and_conflicts_do_not_rewrite(tmp_path):
    e,j,_=setup(tmp_path);e.prepare_review(DAY);c=latest(e)
    j.record(c['case_id'],c['fingerprint'],'keep_blocked',AT)
    count=len(j.export()['events'])
    replay=j.record(c['case_id'],c['fingerprint'],'keep_blocked',(NOW+timedelta(days=1)).isoformat())
    assert replay['stale'] and replay['decision']['recorded_at']==AT
    assert len(j.export()['events'])==count==2
    with pytest.raises(ReviewConflict,match='different'):j.record(c['case_id'],c['fingerprint'],'request_provider_review',AT)
    with pytest.raises(ReviewConflict,match='fingerprint'):j.record(c['case_id'],'bad','keep_blocked',AT)
    with pytest.raises(ReviewConflict,match='cannot be cleared'):j.record(c['case_id'],c['fingerprint'],'clear_hold',AT)
    e.close()


def test_evidence_change_during_slow_get_saves_stale_case_without_holding_management_lock(tmp_path):
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    e,j,_=setup(tmp_path);entered,release=Event(),Event()
    class Slow(ReadOnly):
        def request(self,method,path,*args,**kwargs):
            if path=='/v2/account' and not entered.is_set():entered.set();assert release.wait(5)
            return super().request(method,path,*args,**kwargs)
    e.client=Slow()
    with ThreadPoolExecutor() as pool:
        future=pool.submit(e.prepare_review,DAY);assert entered.wait(5)
        assert e.lock.acquire(timeout=1)
        try:e.meta('loss-blocked:'+DAY,'yes')
        finally:e.lock.release();release.set()
        future.result(timeout=5)
    c=latest(e);assert c['stale_on_arrival'] and c['stale']
    with pytest.raises(ReviewConflict):e.record_review(c['case_id'],c['fingerprint'],'keep_blocked')
    e.close()


@pytest.mark.parametrize('offset,state',[(31,'preflight_too_slow'),(-1,'preflight_clock_reversed')])
def test_preflight_timing_is_explicit(tmp_path,offset,state):
    e,j,_=setup(tmp_path);key=basis(e.db,DAY)
    c=j.prepare(DAY,key,{'state':'flat_two_passes','passes':[]},AT,(NOW+timedelta(minutes=1,seconds=offset)).isoformat())
    assert c['preflight']['state']==state;e.close()


def test_schema3_migration_backfills_only_honest_legacy_reason(tmp_path):
    e,j,path=setup(tmp_path)
    for table in ('paper_fee_log','paper_review_log','paper_hold_events'):e.db.execute(f'DROP TABLE {table}')
    e.db.execute('PRAGMA user_version=3');e.close();e=PaperEngine(path)
    h=PaperReviews(e.db).hold_history(DAY)
    assert h['legacy_history'] and h['event_count']==1
    assert h['events'][0]['source']=='legacy_first_reason_only'
    assert e.db.execute('PRAGMA user_version').fetchone()[0]==5;e.close()


def test_migration_failure_rolls_back_and_active_old_worker_refuses_upgrade(tmp_path,monkeypatch):
    import fcntl
    e,j,path=setup(tmp_path)
    for table in ('paper_fee_log','paper_review_log','paper_hold_events'):e.db.execute(f'DROP TABLE {table}')
    e.db.execute('PRAGMA user_version=3');e.close()
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(PaperError,match='existing paper worker'):PaperEngine(path)
    def fail(db):db.execute('CREATE TABLE partial_upgrade(x)');raise RuntimeError('interrupt')
    monkeypatch.setattr('orb.paper_trading.install_reviews',fail)
    with pytest.raises(RuntimeError):PaperEngine(path)
    db=sqlite3.connect(path);assert db.execute('PRAGMA user_version').fetchone()[0]==3
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='partial_upgrade'").fetchone();db.close()


def test_append_only_integrity_export_and_capacity_keep_hold(tmp_path,monkeypatch):
    import orb.paper_reviews as m
    e,j,_=setup(tmp_path);e.prepare_review(DAY);c=latest(e);first=j.export()
    j.record(c['case_id'],c['fingerprint'],'keep_blocked',AT)
    assert not j.export(first['next_after'],first['through'])['events']
    assert len(j.export()['events'])==2
    for table in ('paper_hold_events','paper_review_log'):
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):e.db.execute(f'DELETE FROM {table}')
    monkeypatch.setattr(m,'MAX_REVIEWS',2)
    with pytest.raises(ReviewConflict,match='capacity'):j.prepare(DAY,basis(e.db,DAY),{'state':'flat_two_passes','passes':[]},AT,(NOW+timedelta(minutes=1,seconds=1)).isoformat())
    monkeypatch.setattr(m,'MAX_HOLDS',1);record_hold(e.db,DAY,'new_failure',AT,'fill_activity')
    assert j.hold_history(DAY)['incomplete'] and e.evidence.holds()
    e.db.execute('DROP TRIGGER paper_review_log_no_update')
    e.db.execute("UPDATE paper_review_log SET fingerprint='bad'")
    with pytest.raises(EvidenceError,match='integrity'):e.evidence.report()
    with pytest.raises(EvidenceError,match='integrity'):j.export()
    e.close()


def test_api_review_validation_privacy_restart_and_hold_never_clear(tmp_path):
    e,j,path=setup(tmp_path);e.close()
    with TestClient(create_app(paper_db=path),base_url='http://127.0.0.1:8765') as c:
        engine=c.app.state.paper_engine;engine.client=ReadOnly()
        assert c.post('/api/paper/reviews/prepare',json={'session':DAY,'clear':True}).status_code==422
        assert c.post('/api/paper/reviews/prepare',json={'session':DAY},headers={'Origin':'http://evil.invalid'}).status_code==403
        r=c.post('/api/paper/reviews/prepare',json={'session':DAY});assert r.status_code==200
        case=r.json()['attempts'][0]['correction_review']['cases'][0]
        body=dict(case_id=case['case_id'],fingerprint=case['fingerprint'],disposition='keep_blocked')
        assert c.post('/api/paper/reviews/record',json=body).status_code==200
        assert c.post('/api/paper/reviews/record',json=dict(body,disposition='clear_hold')).status_code==409
        assert c.post('/api/paper/reviews/record',json=dict(body,notes='private')).status_code==422
        export=c.get('/api/paper/reviews').json();assert len(export['events'])==2
        text=json.dumps(export);assert 'fixture-paper-account' not in text and 'orbp-fixture' not in text
        assert c.get('/api/paper/reviews?after=-1').status_code==422
        assert c.post('/api/paper/start',json=dict(preset_id='paper-balanced',version=1,feed='iex')).status_code==409
        assert not engine.enabled and engine.thread is None and not engine.client.posts
    e=PaperEngine(path);assert latest(e)['decision']['disposition']=='keep_blocked';e.close()


def test_prepare_requires_hold_and_exclusive_same_path_lease(tmp_path):
    import fcntl
    e=PaperEngine(tmp_path/'empty.sqlite',ReadOnly());seed(e)
    with pytest.raises(ReviewConflict,match='No correction hold'):e.prepare_review(DAY)
    assert not e.client.reads;e.close()
    e,j,path=setup(tmp_path)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(PaperError,match='Another paper worker'):e.prepare_review(DAY)
    assert not e.client.reads;e.close()


def test_actual_process_exit_rolls_back_review_record(tmp_path):
    import subprocess,sys
    e,j,path=setup(tmp_path);e.prepare_review(DAY);c=latest(e);e.close()
    code='''import os,sys,sqlite3
from orb.paper_reviews import PaperReviews
db=sqlite3.connect(sys.argv[1],isolation_level=None);db.row_factory=sqlite3.Row
def trace(sql):
 if sql=='COMMIT':os._exit(23)
db.set_trace_callback(trace)
PaperReviews(db).record(sys.argv[2],sys.argv[3],'keep_blocked',sys.argv[4])
'''
    r=subprocess.run([sys.executable,'-c',code,str(path),c['case_id'],c['fingerprint'],AT])
    assert r.returncode==23
    e=PaperEngine(path,review_clock=lambda:NOW+timedelta(minutes=1))
    assert not latest(e)['decision'] and e.evidence.holds()
    e.record_review(c['case_id'],c['fingerprint'],'keep_blocked');assert latest(e)['decision'];e.close()


def test_full_hold_export_uses_frozen_pages_without_losing_bounded_ui_history(tmp_path):
    e,j,_=setup(tmp_path)
    for i in range(105):record_hold(e.db,DAY,'reason_'+chr(97+i//26)+chr(97+i%26),AT,'order_observation')
    history=j.hold_history(DAY)
    assert history['event_count']==106 and history['truncated'] and len(history['events'])==100
    first=j.export(holds=True);assert len(first['events'])==100 and first['has_more']
    record_hold(e.db,DAY,'later_reason',AT,'fill_activity')
    second=j.export(first['next_after'],first['through'],holds=True)
    assert len(second['events'])==6 and not second['has_more']
    assert all(v['kind']=='hold' and v['case_id'] is None for v in first['events'])
    assert len(j.export(first['next_after'],holds=True)['events'])==7;e.close()


def test_recording_a_review_does_not_make_unknown_gross_available(tmp_path):
    e,j,_=setup(tmp_path);e.prepare_review(DAY);c=latest(e)
    e.record_review(c['case_id'],c['fingerprint'],'request_provider_review')
    a=e.evidence.report()['attempts'][0]
    assert a['state']=='correction_hold' and a['gross_price_pnl_usd'] is a['net_pnl_usd'] is None
    assert a['fill_activities']['gross_price_pnl_usd'] is None
    e.close()
