"""Terminal correction permission: atomic, account-wide, restart-safe and bounded."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3
import os

import pytest
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.paper_admission import PaperAdmission
from orb.paper_evidence import EvidenceError, digest, encoded
from orb.paper_recovery import PaperRecovery, account_basis
from orb.paper_reviews import PaperReviews, ReviewConflict, record_hold
from orb.paper_trading import PaperEngine
from test_terminal_retirement import ready, prepare, retire
from test_paper_evidence import capture, closed, DAY
from test_paper_reviews import ReadOnly
from test_paper_trading import NOW


def setup(tmp_path,quantity='10'):
    e,path=ready(tmp_path,quantity=quantity);retire(e,prepare(e))
    e.prepare_recovery();return e,path,e.status()['recovery']['case']


def accept(e,c):return e.accept_recovery(c['case_id'],c['fingerprint'])


@pytest.mark.parametrize('quantity',['10','4'])
def test_acceptance_clears_only_terminal_holds_preserves_history_risk_and_requires_start(tmp_path,quantity):
    e,path,c=setup(tmp_path,quantity)
    assert c['assessment']['eligible'] and not c['stale']
    e.meta('loss-blocked:'+DAY,'yes')
    e.prepare_recovery();c=e.status()['recovery']['case']
    before=dict(holds=[dict(r) for r in e.db.execute('SELECT * FROM paper_evidence_holds')],
                meta=[tuple(r) for r in e.db.execute('SELECT * FROM paper_meta')],
                commands=e.management_events(),admission=e.admission_events())
    reads=len(e.client.reads);s=accept(e,c)
    assert len(e.client.reads)==reads and not e.client.posts and not e.client.deletes
    assert not s['evidence']['entries_blocked'] and not s['enabled']
    assert e.restart_required and not e.restart_blocked and e.restart_seq is None
    assert s['admission']['daily_loss_blocked'] and s['admission']['session_attempt_count']==1
    assert e.meta('loss-blocked:'+DAY)=='yes'
    assert before['holds']==[dict(r) for r in e.db.execute('SELECT * FROM paper_evidence_holds')]
    assert before['meta']==[tuple(r) for r in e.db.execute('SELECT * FROM paper_meta')]
    assert before['commands']==e.management_events() and before['admission']==e.admission_events()
    assert s['recovery']['case']['accepted'] and not s['recovery']['clearances'][0]['retry_authority']
    assert s['recovery']['clearances'][0]['broker_net_pnl_usd'] is None
    e.close();e=PaperEngine(path,ReadOnly(),review_clock=lambda:NOW+timedelta(days=1))
    assert not e.evidence.holds() and not e.enabled and e.restart_required
    changes=e.db.total_changes;accept(e,c);assert e.db.total_changes==changes
    assert not e.client.reads;e.close()


@pytest.mark.parametrize('change',['metadata','order_seen','new_hold','expired','future','fingerprint'])
def test_stale_or_changed_case_never_clears(tmp_path,change):
    e,_,c=setup(tmp_path)
    if change=='metadata':e.meta('new-gate','yes')
    if change=='order_seen':e.db.execute('UPDATE paper_observation_heads SET last_seen_at=?',((NOW+timedelta(seconds=40)).isoformat(),))
    if change=='new_hold':record_hold(e.db,DAY,'unsupported',NOW.isoformat(),'test')
    if change=='expired':e.review_clock=lambda:NOW+timedelta(minutes=4)
    if change=='future':e.review_clock=lambda:NOW
    if change=='fingerprint':c['fingerprint']='0'*64
    with pytest.raises(ReviewConflict):accept(e,c)
    assert e.evidence.holds() and not PaperRecovery(e.db).report(e.review_clock().isoformat())['clearances'];e.close()


@pytest.mark.parametrize('kind',['fee','cash','restart','pending','unsupported','stale_source','inventory','changed_account','scope','legacy','bound'])
def test_account_wide_refusals_are_explicit(tmp_path,kind,monkeypatch):
    e,_,_=setup(tmp_path)
    if kind=='fee':e.meta('fee-hold-reason','fee_record_changed');e.meta('fee-hold-at',NOW.isoformat())
    if kind=='cash':e.meta('cash-hold','cash_activity_changed');e.meta('cash-hold-at',NOW.isoformat())
    if kind=='restart':e.meta('restart-hold','restart_capacity_reached');e.meta('restart-hold-at',NOW.isoformat())
    if kind=='pending':
        row=dict(e.db.execute('SELECT * FROM paper_attempts').fetchone())
        e.db.execute('INSERT INTO paper_attempts VALUES (?,?,?,?,?,?)',
                     ('2026-11-03','other-intent',row['request'],row['preset'],None,'pending'))
    if kind=='unsupported':record_hold(e.db,DAY,'replaced_order',NOW.isoformat(),'test')
    if kind=='stale_source':e.review_clock=lambda:NOW+timedelta(minutes=4)
    if kind=='inventory':e.client.positions=[dict(symbol='SPY',qty='1')]
    if kind=='changed_account':e.client.change=True;e.client.account_reads=0
    if kind=='scope':e.client.account['id']='different-account'
    if kind=='legacy':e.meta('hold-history-incomplete','yes')
    if kind=='bound':monkeypatch.setattr('orb.paper_recovery.MAX_SESSIONS',0)
    e.prepare_recovery();c=e.status()['recovery']['case']
    assert not c['assessment']['eligible']
    with pytest.raises(ReviewConflict):accept(e,c)
    assert e.evidence.holds() and not e.enabled;e.close()


def test_new_source_and_repeated_identical_hold_revoke_clearance(tmp_path):
    e,_,c=setup(tmp_path);accept(e,c)
    row=e.db.execute('SELECT * FROM paper_attempts').fetchone()
    # An identical later receipt is benign, but a new hold occurrence is not.
    p=closed();p['filled_avg_price']='99';capture(e,row,p,45)
    assert not e.evidence.holds()
    record_hold(e.db,DAY,'average_changed_without_fill',(NOW+timedelta(seconds=46)).isoformat(),'order_observation')
    assert e.evidence.holds()
    assert e.status()['recovery']['case']['accepted']  # historical, never an active-green claim
    assert accept(e,c)['evidence']['entries_blocked'] and e.restart_blocked
    e.close()


def test_capacity_exhaustion_after_clearance_cannot_deduplicate_a_new_hold(tmp_path,monkeypatch):
    e,_,c=setup(tmp_path);accept(e,c)
    monkeypatch.setattr('orb.paper_reviews.MAX_HOLDS',0)
    record_hold(e.db,DAY,'average_changed_without_fill',NOW.isoformat(),'order_observation')
    assert e.meta('hold-history-incomplete')=='yes' and e.evidence.holds();e.close()


def test_new_payload_blocks_even_without_a_new_hold_event(tmp_path):
    e,_,c=setup(tmp_path);accept(e,c)
    row=e.db.execute('SELECT * FROM paper_attempts').fetchone()
    e.evidence.capture(row,None,None,(NOW+timedelta(seconds=45)).isoformat())
    assert e.evidence.holds();e.close()


@pytest.mark.parametrize('capacity',[0,1])
def test_capacity_failure_rolls_back_permission_and_decision(tmp_path,monkeypatch,capacity):
    e,_,c=setup(tmp_path);before=e.review_events()
    count=e.db.execute('SELECT COUNT(*) FROM paper_review_log').fetchone()[0]
    monkeypatch.setattr('orb.paper_reviews.MAX_REVIEWS',count+capacity)
    with pytest.raises(ReviewConflict,match='capacity'):accept(e,c)
    assert e.review_events()==before and e.evidence.holds();e.close()


def test_concurrent_exact_acceptance_commits_once(tmp_path):
    e,_,c=setup(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:accept(e,c),range(2)))
    assert all(not r['evidence']['entries_blocked'] for r in results)
    assert len(e.status()['recovery']['clearances'])==1
    assert len([r for r in e.review_events()['events'] if r['kind']=='recovery_decision'])==1;e.close()


def test_independent_connections_accept_once(tmp_path):
    e,path,c=setup(tmp_path)
    def worker(_):
        other=PaperEngine(path,review_clock=lambda:NOW+timedelta(minutes=1))
        try:
            PaperRecovery(other.db).accept(c['case_id'],c['fingerprint'],other.review_clock().isoformat())
            return not other.evidence.holds()
        finally:other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:assert all(pool.map(worker,range(2)))
    assert len(e.status()['recovery']['clearances'])==1;e.close()


@pytest.mark.parametrize('point',['recovery_clearance','recovery_decision','after_commit'])
def test_real_process_exit_rolls_back_or_retains_one_complete_clearance(tmp_path,point):
    e,path,c=setup(tmp_path);before=e.review_events();e.close()
    pid=os.fork()
    if pid==0:
        try:
            child=PaperEngine(path)
            original=PaperReviews._append
            def append(self,day,kind,case_id,value,at):
                if kind==point:os._exit(23)
                return original(self,day,kind,case_id,value,at)
            PaperReviews._append=append
            PaperRecovery(child.db).accept(c['case_id'],c['fingerprint'],(NOW+timedelta(minutes=1)).isoformat())
            os._exit(23)
        except BaseException:os._exit(24)
    _,status=os.waitpid(pid,0);assert os.waitstatus_to_exitcode(status)==23
    e=PaperEngine(path,review_clock=lambda:NOW+timedelta(minutes=1))
    if point=='after_commit':
        assert not e.evidence.holds() and len(e.status()['recovery']['clearances'])==1
    else:assert e.evidence.holds() and e.review_events()==before
    assert not e.enabled and e.restart_required;e.close()


def test_unlocked_preflight_cannot_accept_changed_saved_state(tmp_path):
    e,_,_=setup(tmp_path);original=e.client.request;changed=False
    def request(*args,**kwargs):
        nonlocal changed
        if not changed:e.meta('changed-during-read','yes');changed=True
        return original(*args,**kwargs)
    e.client.request=request;e.prepare_recovery();c=e.status()['recovery']['case']
    assert c['assessment']['reason']=='basis_changed_during_preflight'
    with pytest.raises(ReviewConflict):accept(e,c)
    assert e.evidence.holds();e.close()


def test_manual_restart_check_after_clearance_does_not_start_worker(tmp_path):
    e,_,c=setup(tmp_path);accept(e,c)
    s=e.refresh_restart()
    assert not s['enabled'] and e.restart_required
    assert not e.client.posts and not e.client.deletes;e.close()


def test_unknown_management_receipts_survive_clearance_without_resend(tmp_path):
    from test_management_resolution import ready as managed
    e,_,_=managed(tmp_path);retire(e,prepare(e))
    original=e.management_events();e.prepare_recovery();c=e.status()['recovery']['case']
    assert c['assessment']['eligible'];s=accept(e,c)
    assert not s['evidence']['entries_blocked'] and not e.enabled
    assert s['management']['attempts'][0]['transport_state']=='closing_outcome_unknown'
    assert original==e.management_events() and not e.client.posts and not e.client.deletes;e.close()


def test_cleared_price_review_enables_only_conditional_cost_estimate(tmp_path):
    e,_,c=setup(tmp_path)
    assert not e.performance()['all_saved_attempts_estimated']
    accept(e,c);r=e.performance()
    assert r['all_saved_attempts_estimated'] and r['totals']['estimated_net_pnl_usd']=='30'
    assert r['broker_net_pnl_usd'] is None and not r['fee_coverage_verified'] and not r['recovery_clearance']
    fills=e.evidence.report()['attempts'][0]['fill_activities']
    assert fills['version_event_count']==3 and fills['event_count']==2
    assert fills['selection']=='reviewed_terminal_versions' and len(e.activity_events()['events'])==3
    assert not e.enabled;e.close()


@pytest.mark.parametrize('kind',['legacy','no_retirement','new_accounting'])
def test_unreviewed_or_changed_terminal_sources_never_gain_permission(tmp_path,kind):
    e,_,_=setup(tmp_path)
    if kind=='legacy':
        row=dict(e.db.execute('SELECT * FROM paper_attempts').fetchone())
        e.db.execute('INSERT INTO paper_attempts VALUES (?,?,?,?,?,?)',
                     ('2026-11-03','legacy-done',row['request'],row['preset'],None,'done'))
    if kind=='no_retirement':
        # Fully accounted evidence without reviewed retirement is insufficient.
        # Remove that reviewed linkage only in the fixture; integrity must fail closed.
        e.db.execute('DROP TRIGGER paper_admission_log_no_delete')
        e.db.execute('DELETE FROM paper_admission_log WHERE seq=2')
        e.db.execute("UPDATE paper_attempts SET state='reserved'")
    if kind=='new_accounting':
        row=e.db.execute('SELECT * FROM paper_attempts').fetchone()
        parent=closed();parent['filled_avg_price']='98';capture(e,row,parent,30)
    e.prepare_recovery();c=e.status()['recovery']['case']
    assert not c['assessment']['eligible']
    with pytest.raises(ReviewConflict):accept(e,c)
    assert e.evidence.holds();e.close()


@pytest.mark.parametrize('kind',['recovery_case','recovery_decision','accounting'])
def test_missing_authority_links_fail_closed(tmp_path,kind):
    e,_,c=setup(tmp_path);accept(e,c)
    e.db.execute('DROP TRIGGER paper_review_log_no_delete')
    e.db.execute('DELETE FROM paper_review_log WHERE kind=?',(kind,))
    with pytest.raises(EvidenceError):e.evidence.holds()
    e.close()


def test_api_requires_exact_case_and_never_submits(tmp_path):
    e,path,_=setup(tmp_path);e.close()
    app=create_app(paper_db=path);engine=app.state.paper_engine
    engine.client=ReadOnly();engine.review_clock=lambda:NOW+timedelta(minutes=1)
    with TestClient(app,base_url='http://127.0.0.1:8765') as client:
        assert client.post('/api/paper/recovery/prepare',json={'eligible':True}).status_code==422
        r=client.post('/api/paper/recovery/prepare',json={});assert r.status_code==200
        c=r.json()['recovery']['case'];body=dict(case_id=c['case_id'],fingerprint=c['fingerprint'])
        assert client.post('/api/paper/recovery/accept',json=dict(body,start=True)).status_code==422
        r=client.post('/api/paper/recovery/accept',json=body);assert r.status_code==200,r.text
        assert not r.json()['enabled'] and not r.json()['evidence']['entries_blocked']
        assert not engine.client.posts and not engine.client.deletes
        assert client.post('/api/paper/recovery/accept',json=body).status_code==200
        events=client.get('/api/paper/reviews').json()['events']
        assert any(v['kind']=='recovery_clearance' for v in events)
