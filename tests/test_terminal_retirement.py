"""Atomic reviewed retirement of tracked intents; all broker actions stay held."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
import fcntl
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.paper_admission import PaperAdmission
from orb.paper_evidence import EvidenceError, digest, encoded
from orb.paper_management import PaperManagement
from orb.paper_reviews import PaperReviews, ReviewConflict, record_hold
from orb.paper_trading import PaperEngine, PaperError
from orb.presets import PresetStore
from test_paper_admission import reserve, terms
from test_paper_evidence import capture, closed, DAY
from test_paper_reviews import ReadOnly, latest, AT
from test_paper_trading import NOW
from test_terminal_accounting import save_events, ready as legacy_ready
from test_paper_activities import activity


def ready(tmp_path, *, quantity='10'):
    path=tmp_path/'paper.sqlite'
    e=PaperEngine(path,ReadOnly(),review_clock=lambda:NOW+timedelta(minutes=1))
    reserve(e,client_id='orbp-fixture',request=terms(client_order_id='orbp-fixture'))
    row=e.db.execute('SELECT * FROM paper_attempts').fetchone()
    parent=closed()
    if quantity!='10':
        parent.update(status='canceled',filled_qty=quantity)
        parent['legs'][0].update(status='canceled',filled_qty=quantity)
    capture(e,row,parent)
    parent['filled_avg_price']='99';capture(e,row,parent,1)
    save_events(e,[activity(qty=quantity),activity('sell-1','target','sell',quantity,'102')],seconds=10)
    save_events(e,[activity(qty=quantity,price='99'),activity('sell-1','target','sell',quantity,'102')])
    return e,path


def prepare(e):
    e.prepare_review(DAY)
    return latest(e)


def retire(e,c): return e.record_review(c['case_id'],c['fingerprint'],'retire_terminal_intent')


def frozen(e):
    return dict(meta=[tuple(r) for r in e.db.execute('SELECT * FROM paper_meta ORDER BY key')],
        holds=e.evidence.holds(),observations=e.evidence.events(),
        request=dict(e.db.execute('SELECT * FROM paper_attempts').fetchone()))


@pytest.mark.parametrize('quantity',['10','4'])
def test_retirement_atomically_accepts_gross_releases_headroom_but_not_holds(tmp_path,quantity):
    e,path=ready(tmp_path,quantity=quantity)
    e.meta('loss-blocked:'+DAY,'yes');before=frozen(e);a=PaperAdmission(e.db)
    assert a.report()['active_count']==1
    c=prepare(e);assert c['terminal_retirement']['eligible']
    reads=len(e.client.reads);report=retire(e,c)
    assert len(e.client.reads)==reads and not e.client.posts and not e.client.deletes
    assert e.db.execute('SELECT state FROM paper_attempts').fetchone()[0]=='done'
    after=frozen(e)
    assert after['meta']==before['meta'] and after['holds']==before['holds'] and after['observations']==before['observations']
    assert after['request']==before['request']|{'state':'done'}
    r=a.report()
    assert r['active_count']==0 and r['planned_stop_risk_usd']=='0' and r['cash_headroom_usd']=='0'
    assert r['daily_loss_blocked'] and r['session_attempt_count']==1
    assert [v['kind'] for v in a.export()['events']]==['reserved','terminal_release']
    reviews=PaperReviews(e.db).export()['events']
    assert [v['kind'] for v in reviews]==['case','accounting','retirement','decision']
    release=a.records()[-1]; assert release['review_case_id']==c['case_id'] and release['reservation_seq']==1
    review=report['attempts'][0]['correction_review']
    assert review['reviewed_accounting']['state']=='accepted_at_terminal_retirement'
    assert review['reviewed_accounting']['gross_price_pnl_usd']==str(int(quantity)*3)
    assert review['terminal_retirement']['state']=='applied_holds_remain'
    assert not review['terminal_retirement']['correction_clearance'] and not review['terminal_retirement']['restart_authority']
    assert report['entries_blocked'] and report['attempts'][0]['net_pnl_usd'] is None
    assert not e.enabled and e.restart_required and e.restart_blocked
    e.close();e=PaperEngine(path,review_clock=lambda:NOW+timedelta(minutes=1))
    assert PaperAdmission(e.db).report()==r and e.evidence.holds()==before['holds']
    with pytest.raises(PaperError,match='correction hold'):e.start(PresetStore.paper('paper-balanced'))
    e.close()


def test_exact_retry_after_expiry_and_restart_never_releases_twice(tmp_path):
    e,path=ready(tmp_path);c=prepare(e);retire(e,c);e.close()
    e=PaperEngine(path,ReadOnly(),review_clock=lambda:NOW+timedelta(days=1))
    changes=e.db.total_changes;retire(e,c)
    assert e.db.total_changes==changes and len(PaperAdmission(e.db).records())==2
    assert not e.client.reads and not e.enabled and e.evidence.holds()
    with pytest.raises(ReviewConflict,match='different'):e.record_review(c['case_id'],c['fingerprint'],'keep_blocked')
    e.close()


@pytest.mark.parametrize('kind',['metadata','order_seen','new_hold','future','expired','fingerprint'])
def test_stale_or_wrong_case_cannot_change_state_or_release_headroom(tmp_path,kind):
    e,_=ready(tmp_path);c=prepare(e)
    if kind=='metadata':e.meta('loss-blocked:'+DAY,'yes')
    elif kind=='order_seen':
        row=e.db.execute('SELECT * FROM paper_attempts').fetchone();p=closed();p['filled_avg_price']='99';capture(e,row,p,70)
    elif kind=='new_hold':record_hold(e.db,DAY,'unexplained_correction',AT,'order_observation')
    elif kind=='future':e.review_clock=lambda:NOW
    elif kind=='expired':e.review_clock=lambda:NOW+timedelta(minutes=4)
    elif kind=='fingerprint':c['fingerprint']='0'*64
    before=frozen(e);counts=e.db.total_changes
    with pytest.raises(ReviewConflict):retire(e,c)
    assert frozen(e)==before and e.db.total_changes==counts
    assert PaperAdmission(e.db).report()['active_count']==1
    assert len(PaperReviews(e.db).export()['events'])==1;e.close()


@pytest.mark.parametrize('kind',['pending','quantity_bust','unknown_hold','missing_scan','inventory','currency','legacy','management','already_done'])
def test_unsupported_recovery_remains_explicit_and_cannot_retire(tmp_path,kind):
    e,path=ready(tmp_path)
    if kind in ('pending','quantity_bust'):
        p=closed();p['filled_avg_price']='99'
        if kind=='pending':p['legs'][1]['status']='held'
        else:p['filled_qty']='9';p['legs'][0]['filled_qty']='9'
        capture(e,e.db.execute('SELECT * FROM paper_attempts').fetchone(),p,30)
    elif kind=='unknown_hold':record_hold(e.db,DAY,'unexplained_correction',AT,'order_observation')
    elif kind=='missing_scan':save_events(e,[activity(price='99')],seconds=30,finished=False)
    elif kind=='inventory':e.client.positions=[{'symbol':'SPY','qty':'1'}]
    elif kind=='currency':e.client.account['currency']='GBP'
    elif kind=='legacy':
        e.close();e,_,path=legacy_ready(tmp_path/'legacy')
    elif kind=='management':
        row=e.db.execute('SELECT * FROM paper_attempts').fetchone()
        PaperManagement(e.db)._append(DAY,AT,dict(kind='close_check',session=DAY,preflight={'state':'inventory_read_failed'},
            request_sha256=digest(json.loads(row['request'])),preset_sha256=digest(json.loads(row['preset']))))
    elif kind=='already_done':retire(e,prepare(e))
    c=prepare(e);assert not c['terminal_retirement']['eligible']
    if kind=='legacy':assert c['terminal_retirement']['reason']=='legacy_untracked_reservation'
    if kind=='management':assert c['terminal_retirement']['reason']=='management_history_requires_separate_recovery'
    if kind=='already_done':assert c['terminal_retirement']['reason']=='intent_already_terminal'
    before=frozen(e)
    with pytest.raises(ReviewConflict,match='cannot retire'):retire(e,c)
    assert frozen(e)==before and e.evidence.holds();e.close()


@pytest.mark.parametrize('slots',[0,1,2])
def test_review_capacity_failure_rolls_back_every_write_including_release(tmp_path,monkeypatch,slots):
    import orb.paper_reviews as reviews
    e,_=ready(tmp_path);c=prepare(e);monkeypatch.setattr(reviews,'MAX_REVIEWS',1+slots)
    with pytest.raises(ReviewConflict,match='capacity'):retire(e,c)
    assert e.db.execute('SELECT state FROM paper_attempts').fetchone()[0]=='reserved'
    assert len(PaperAdmission(e.db).records())==1
    assert len(PaperReviews(e.db).export()['events'])==1;e.close()


def test_admission_capacity_failure_leaves_no_accounting_or_decision(tmp_path,monkeypatch):
    import orb.paper_admission as admission
    e,_=ready(tmp_path);c=prepare(e);monkeypatch.setattr(admission,'MAX_EVENTS',1)
    with pytest.raises(EvidenceError,match='capacity'):retire(e,c)
    assert len(PaperReviews(e.db).export()['events'])==1
    assert PaperAdmission(e.db).report()['active_count']==1;e.close()


@pytest.mark.parametrize('sql_start',["INSERT INTO paper_admission_log","UPDATE paper_attempts","INSERT INTO paper_review_log"])
def test_process_exit_mid_transaction_cannot_leave_partial_retirement(tmp_path,sql_start):
    e,path=ready(tmp_path);c=prepare(e);e.close()
    code='''import os,sqlite3,sys
from orb.paper_reviews import PaperReviews
db=sqlite3.connect(sys.argv[1],isolation_level=None);db.row_factory=sqlite3.Row
def die(sql):
 if sql.startswith(sys.argv[5]) and (sys.argv[5]!='INSERT INTO paper_review_log' or "'decision'" in sql):os._exit(29)
db.set_trace_callback(die)
PaperReviews(db).record(sys.argv[2],sys.argv[3],'retire_terminal_intent',sys.argv[4])
'''
    result=subprocess.run([sys.executable,'-c',code,str(path),c['case_id'],c['fingerprint'],AT,sql_start])
    assert result.returncode==29
    e=PaperEngine(path,ReadOnly(),review_clock=lambda:NOW+timedelta(minutes=1))
    assert PaperAdmission(e.db).report()['active_count']==1
    assert len(PaperReviews(e.db).export()['events'])==1
    retire(e,c);assert PaperAdmission(e.db).report()['active_count']==0;e.close()


def test_concurrent_exact_decisions_commit_only_one_release(tmp_path):
    e,path=ready(tmp_path);c=prepare(e)
    def decide(_):
        db=sqlite3.connect(path,isolation_level=None);db.row_factory=sqlite3.Row
        try:return PaperReviews(db).record(c['case_id'],c['fingerprint'],'retire_terminal_intent',AT)['decision']
        finally:db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(decide,range(2)))
    assert results[0]==results[1] and len(PaperAdmission(e.db).records())==2
    assert len(PaperReviews(e.db).export()['events'])==4 and e.evidence.holds();e.close()


def test_fresh_case_can_retire_after_prior_accounting_only_acceptance(tmp_path):
    e,_=ready(tmp_path);c=prepare(e)
    e.record_review(c['case_id'],c['fingerprint'],'accept_terminal_accounting')
    assert PaperAdmission(e.db).report()['active_count']==1
    with pytest.raises(ReviewConflict,match='different'):retire(e,c)
    e.review_clock=lambda:NOW+timedelta(seconds=61)
    fresh=prepare(e);assert fresh['case_id']!=c['case_id']
    retire(e,fresh)
    assert PaperAdmission(e.db).report()['active_count']==0
    assert len(PaperReviews(e.db).export()['events'])==7 and e.evidence.holds();e.close()


def test_process_exit_after_commit_retains_the_entire_retirement(tmp_path):
    e,path=ready(tmp_path);c=prepare(e);e.close()
    code='''import os,sqlite3,sys
from orb.paper_reviews import PaperReviews
db=sqlite3.connect(sys.argv[1],isolation_level=None);db.row_factory=sqlite3.Row
PaperReviews(db).record(sys.argv[2],sys.argv[3],'retire_terminal_intent',sys.argv[4])
os._exit(31)
'''
    result=subprocess.run([sys.executable,'-c',code,str(path),c['case_id'],c['fingerprint'],AT])
    assert result.returncode==31
    e=PaperEngine(path,ReadOnly(),review_clock=lambda:NOW+timedelta(minutes=1))
    assert PaperAdmission(e.db).report()['active_count']==0
    assert len(PaperReviews(e.db).export()['events'])==4
    changes=e.db.total_changes;retire(e,c);assert e.db.total_changes==changes
    assert e.evidence.holds() and not e.enabled;e.close()


def test_later_evidence_supersedes_gross_but_does_not_erase_retirement(tmp_path):
    e,_=ready(tmp_path);c=prepare(e);retire(e,c)
    row=e.db.execute('SELECT * FROM paper_attempts').fetchone();p=closed();p['filled_avg_price']='98';capture(e,row,p,90)
    review=e.evidence.report()['attempts'][0]['correction_review']
    assert review['reviewed_accounting']['gross_price_pnl_usd'] is None
    assert review['terminal_retirement']['state']=='applied_basis_changed'
    assert PaperAdmission(e.db).report()['active_count']==0
    assert e.evidence.holds() and not e.enabled;e.close()


def test_release_binding_detects_missing_retirement_or_wrong_decision(tmp_path):
    e,_=ready(tmp_path);c=prepare(e);retire(e,c)
    e.db.execute('DROP TRIGGER paper_review_log_no_delete')
    e.db.execute("DELETE FROM paper_review_log WHERE kind='retirement'")
    with pytest.raises(EvidenceError,match='retirement_review_integrity'):PaperAdmission(e.db).report()
    e.close()


def test_identical_receipt_during_preflight_invalidates_the_prepared_case(tmp_path):
    e,_=ready(tmp_path);original=e.client.request;changed=False
    def read(*a,**kw):
        nonlocal changed
        if not changed:
            changed=True;row=e.db.execute('SELECT * FROM paper_attempts').fetchone()
            p=closed();p['filled_avg_price']='99';capture(e,row,p,50)
        return original(*a,**kw)
    e.client.request=read;c=prepare(e)
    assert c['stale_on_arrival'] and c['terminal_retirement']['reason']=='basis_changed_during_preflight'
    with pytest.raises(ReviewConflict,match='expired'):retire(e,c)
    assert PaperAdmission(e.db).report()['active_count']==1;e.close()


def test_old_case_schema_cannot_acquire_retirement_authority(tmp_path):
    e,_=ready(tmp_path);c=prepare(e)
    row=e.db.execute("SELECT * FROM paper_review_log WHERE kind='case'").fetchone()
    value=json.loads(row['payload']);value['schema_version']='orb-paper-review-case-v2'
    key=digest(value)
    e.db.execute('INSERT INTO paper_review_log(day,kind,case_id,observed_at,fingerprint,payload) VALUES(?,?,?,?,?,?)',
        (DAY,'case',key,row['observed_at'],key,encoded(value)))
    with pytest.raises(ReviewConflict,match='cannot retire'):
        retire(e,dict(case_id=key,fingerprint=key))
    assert PaperAdmission(e.db).report()['active_count']==1;e.close()


def test_competing_worker_lease_prevents_the_transition(tmp_path):
    e,path=ready(tmp_path);c=prepare(e)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(PaperError):retire(e,c)
        assert PaperAdmission(e.db).report()['active_count']==1
    retire(e,c);assert PaperAdmission(e.db).report()['active_count']==0;e.close()


def test_api_requires_exact_case_and_retains_all_evidence_in_exports(tmp_path):
    e,path=ready(tmp_path);e.close();app=create_app(paper_db=path)
    engine=app.state.paper_engine;engine.client=ReadOnly();engine.review_clock=lambda:NOW+timedelta(minutes=1)
    with TestClient(app,base_url='http://127.0.0.1:8765') as client:
        response=client.post('/api/paper/reviews/prepare',json={'session':DAY});assert response.status_code==200
        c=response.json()['attempts'][0]['correction_review']['cases'][0]
        body=dict(case_id=c['case_id'],fingerprint=c['fingerprint'],disposition='retire_terminal_intent')
        assert client.post('/api/paper/reviews/record',json=body|{'clear_holds':True}).status_code==422
        response=client.post('/api/paper/reviews/record',json=body)
        assert response.status_code==200 and response.json()['entries_blocked']
        assert len(client.get('/api/paper/reviews').json()['events'])==4
        assert len(client.get('/api/paper/admissions').json()['events'])==2
        assert client.post('/api/paper/start',json={'preset_id':'paper-balanced','version':1,'feed':'iex'}).status_code==409
        assert not engine.client.posts and not engine.client.deletes
