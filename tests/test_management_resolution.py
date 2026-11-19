"""Native cancel/close history may retire only from exact terminal evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.paper_admission import PaperAdmission
from orb.paper_evidence import EvidenceError, digest, encoded, verified
from orb.paper_management import PaperManagement, capture_close_inventory
from orb.paper_reviews import PaperReviews, ReviewConflict
from orb.paper_trading import PaperEngine, PaperError
from orb.presets import PresetStore
from test_paper_admission import reserve, terms, SCOPE
from test_paper_evidence import snapshot, closed, capture, DAY
from test_paper_reviews import ReadOnly, latest, AT
from test_paper_trading import NOW
from test_terminal_accounting import save_events
from test_paper_activities import activity
from test_terminal_retirement import prepare, retire, frozen, ready as no_management


def ready(tmp_path, kind='close', receipt='unknown'):
    path=tmp_path/'paper.sqlite';b=ReadOnly()
    e=PaperEngine(path,b,review_clock=lambda:NOW+timedelta(minutes=1))
    reserve(e,client_id='orbp-fixture',request=terms(client_order_id='orbp-fixture'))
    row=e.db.execute('SELECT * FROM paper_attempts').fetchone();j=PaperManagement(e.db)
    parent=snapshot('partially_filled','4','100');capture(e,row,parent)
    claim=j.claim_cancel(DAY,['entry','target','stop'],'flatten_deadline',NOW.isoformat(),SCOPE)
    if receipt!='absent':
        j.dispatched(DAY,claim['seq'],'outcome_unknown' if receipt in ('unknown','mixed') else 'request_returned',
            (NOW+timedelta(seconds=1)).isoformat(),results=[dict(id=t['id'],
                outcome=('outcome_unknown','not_attempted','request_returned')[i] if receipt=='mixed' else
                        'outcome_unknown' if receipt=='unknown' else 'request_returned')
            for i,t in enumerate(j.records()[0]['targets'])])
    if kind=='close':
        parent['status']='canceled'
        for leg in parent['legs']:leg['status']='canceled'
        capture(e,row,parent,2);b.positions=[dict(symbol='SPY',qty='4')]
        when=NOW+timedelta(seconds=3)
        proof=capture_close_inventory(b,SCOPE,'4',when,lambda:0)
        assert proof['state']=='two_agreeing_inventory_passes'
        claim=j.claim_close(DAY,j.basis(DAY),proof,when.isoformat())
        row=e.db.execute('SELECT * FROM paper_attempts').fetchone()
        closing=dict(id='closing',client_order_id='orbp-fixture-x',symbol='SPY',side='sell',qty='4',filled_qty='4',
            filled_avg_price='102',status='filled',filled_at=(NOW+timedelta(seconds=4)).isoformat())
        if receipt!='absent':
            j.dispatched(DAY,claim['seq'],'outcome_unknown' if receipt in ('unknown','mixed') else 'request_returned',
                (NOW+timedelta(seconds=4)).isoformat(),response=closing if receipt=='returned' else None)
        capture(e,row,parent,11,closing=closing)
        parent['filled_avg_price']='99';capture(e,row,parent,12,closing=closing)
        buy=activity(qty='4');sell=activity('sell-1','closing','sell','4','102')
        sell['transaction_time']=closing['filled_at']
    else:
        parent=closed();capture(e,row,parent,11)
        parent['filled_avg_price']='99';capture(e,row,parent,12)
        buy=activity();sell=activity('sell-1','target','sell','10','102')
    b.positions=[]
    save_events(e,[buy,sell],seconds=20);buy['price']='99';save_events(e,[buy,sell],seconds=25)
    return e,j,path


@pytest.mark.parametrize('kind',['cancel','close'])
@pytest.mark.parametrize('receipt',['unknown','returned','absent','mixed'])
def test_terminal_command_history_retires_atomically_without_rewriting_transport(tmp_path,kind,receipt):
    e,j,path=ready(tmp_path,kind,receipt);e.meta('loss-blocked:'+DAY,'yes')
    original=deepcopy(j.export());before=frozen(e);case=prepare(e)
    a=case['terminal_retirement'];assert a['eligible'] and case['schema_version']=='orb-paper-review-case-v4'
    resolution=a['management_resolution'];assert resolution['eligible']
    assert resolution['command_count']==(2 if kind=='close' else 1)
    assert resolution['unknown_transport_count']==(0 if receipt=='returned' else resolution['command_count'])
    if receipt=='mixed':
        assert {r['outcome'] for r in resolution['commands'][0]['target_outcomes']}=={'outcome_unknown','not_attempted','request_returned'}
    reads=len(e.client.reads);retire(e,case)
    assert len(e.client.reads)==reads and j.export()==original and not e.client.posts and not e.client.deletes
    assert [r['kind'] for r in PaperReviews(e.db).export()['events']]==['case','accounting','management_resolution','retirement','decision']
    assert PaperAdmission(e.db).report()['active_count']==0
    assert PaperAdmission(e.db).report()['daily_loss_blocked'] and PaperAdmission(e.db).report()['session_attempt_count']==1
    assert e.evidence.holds()==before['holds'] and dict(e.db.execute('SELECT * FROM paper_meta'))==dict(before['meta'])
    row=j.report()['attempts'][0]
    assert row['state']=='reviewed_terminal_history' and row['terminal_resolution']['historical_only']
    if receipt in ('unknown','absent','mixed'):assert 'unknown' in row['transport_state']
    assert not row['terminal_resolution']['retry_authority'] and not row['terminal_resolution']['correction_clearance']
    e.close();e=PaperEngine(path,ReadOnly(),review_clock=lambda:NOW+timedelta(minutes=1))
    assert PaperManagement(e.db).export()==original and PaperAdmission(e.db).report()['active_count']==0
    with pytest.raises(PaperError,match='correction hold'):e.start(PresetStore.paper('paper-balanced'))
    assert not e.enabled;e.close()


def test_passive_failed_check_can_be_preserved_without_inventing_a_command(tmp_path):
    e,_=no_management(tmp_path);row=e.db.execute('SELECT * FROM paper_attempts').fetchone();j=PaperManagement(e.db)
    j._append(DAY,NOW.isoformat(),dict(kind='close_check',session=DAY,preflight={'state':'inventory_read_failed'},
        request_sha256=digest(json.loads(row['request'])),preset_sha256=digest(json.loads(row['preset']))))
    case=prepare(e);a=case['terminal_retirement']['management_resolution']
    assert a['eligible'] and a['command_count']==0 and a['passive_check_seqs']==[1]
    retire(e,case);assert j.report()['attempts'][0]['transport_state']=='inventory_read_failed';e.close()


@pytest.mark.parametrize('kind',['late_receipt','legacy_closing','wrong_target_role','unknown_target',
    'duplicate_target','wrong_receipt_id','response_identity','response_quantity_bust','reversed_clock','record_bound'])
def test_unexplained_or_stale_command_evidence_cannot_retire(tmp_path,kind,monkeypatch):
    import orb.management_resolution as module
    e,j,_=ready(tmp_path)
    if kind=='record_bound':monkeypatch.setattr(module,'MAX_RECORDS',1)
    elif kind=='legacy_closing':
        # Simulate an older unattested closing claim with no native close intent.
        e.close();e,j,_=ready(tmp_path/'cancel',kind='cancel')
        row=e.db.execute('SELECT * FROM paper_attempts').fetchone()
        request=dict(symbol='SPY',side='sell',qty='1',type='market',time_in_force='day',client_order_id='orbp-fixture-x')
        e.db.execute('UPDATE paper_attempts SET closing=?',(json.dumps(request),))
        p=closed();p['filled_avg_price']='99'
        close=dict(id='other-close',client_order_id='orbp-fixture-x',symbol='SPY',side='sell',qty='1',filled_qty='0',status='canceled',filled_avg_price=None,filled_at=None)
        capture(e,e.db.execute('SELECT * FROM paper_attempts').fetchone(),p,15,closing=close)
    else:
        # Recompute a deliberately inconsistent local receipt fixture. All
        # provider-shaped input remains invented; production logs are immutable.
        record=e.db.execute("SELECT * FROM paper_management_log WHERE seq=?",(1 if kind in ('wrong_target_role','unknown_target','duplicate_target') else 4,)).fetchone()
        value=verified(record);at=record['observed_at']
        if kind=='late_receipt':at=(NOW+timedelta(seconds=30)).isoformat()
        elif kind=='reversed_clock':at=(NOW-timedelta(seconds=1)).isoformat()
        elif kind=='wrong_target_role':value['targets'][0]['role']='closing_exit'
        elif kind=='unknown_target':value['targets'][0]['id']='unattributed'
        elif kind=='duplicate_target':value['targets'][1]=value['targets'][0]
        elif kind=='wrong_receipt_id':
            record=e.db.execute('SELECT * FROM paper_management_log WHERE seq=2').fetchone();value=verified(record)
            value['results'][0]['id']='other-order';at=record['observed_at']
        elif kind in ('response_identity','response_quantity_bust'):
            value['outcome']='request_returned'
            source=verified(e.db.execute('SELECT * FROM paper_observations ORDER BY seq DESC LIMIT 1').fetchone())
            value['response']=next(o for o in source['orders'] if o['role']=='closing_exit')
            if kind=='response_identity':value['response']['id']='other-order'
            else:value['response']['filled_quantity']='5'
        e.db.execute('DROP TRIGGER paper_management_log_no_update')
        e.db.execute('UPDATE paper_management_log SET observed_at=?,payload=?,fingerprint=? WHERE seq=?',
            (at,encoded(value),digest(value),record['seq']))
    before=PaperAdmission(e.db).report()['active_count']
    case=prepare(e);assert not case['terminal_retirement']['eligible']
    with pytest.raises(ReviewConflict,match='cannot retire'):retire(e,case)
    assert PaperAdmission(e.db).report()['active_count']==before and e.evidence.holds();e.close()


def test_changed_management_after_preflight_invalidates_the_exact_case(tmp_path):
    e,j,_=ready(tmp_path);case=prepare(e);row=e.db.execute('SELECT * FROM paper_attempts').fetchone()
    j._append(DAY,(NOW+timedelta(seconds=50)).isoformat(),dict(kind='close_check',session=DAY,
        preflight={'state':'inventory_read_failed'},request_sha256=digest(json.loads(row['request'])),preset_sha256=digest(json.loads(row['preset']))))
    with pytest.raises(ReviewConflict,match='expired'):retire(e,case)
    assert PaperAdmission(e.db).report()['active_count']==1;e.close()


@pytest.mark.parametrize('remaining',[0,1,2,3])
def test_review_capacity_rolls_back_management_acceptance_and_release(tmp_path,monkeypatch,remaining):
    import orb.paper_reviews as module
    e,j,_=ready(tmp_path);case=prepare(e);monkeypatch.setattr(module,'MAX_REVIEWS',1+remaining)
    original=j.export()
    with pytest.raises(ReviewConflict,match='capacity'):retire(e,case)
    assert j.export()==original and PaperAdmission(e.db).report()['active_count']==1
    assert len(PaperReviews(e.db).export()['events'])==1;e.close()


@pytest.mark.parametrize('boundary',['management_resolution','release','decision','after_commit'])
def test_actual_process_exits_preserve_all_or_none_of_management_retirement(tmp_path,boundary):
    e,j,path=ready(tmp_path);case=prepare(e);original=j.export();e.close()
    code='''import os,sqlite3,sys
from orb.paper_reviews import PaperReviews
db=sqlite3.connect(sys.argv[1],isolation_level=None);db.row_factory=sqlite3.Row
def die(sql):
 if ((sys.argv[5]=='release' and sql.startswith('INSERT INTO paper_admission_log')) or
     (sys.argv[5] in ('management_resolution','decision') and sql.startswith('INSERT INTO paper_review_log') and "'"+sys.argv[5]+"'" in sql)):os._exit(29)
db.set_trace_callback(die)
PaperReviews(db).record(sys.argv[2],sys.argv[3],'retire_terminal_intent',sys.argv[4])
os._exit(31)
'''
    result=subprocess.run([sys.executable,'-c',code,str(path),case['case_id'],case['fingerprint'],AT,boundary])
    assert result.returncode==(31 if boundary=='after_commit' else 29)
    e=PaperEngine(path,ReadOnly(),review_clock=lambda:NOW+timedelta(minutes=1))
    assert PaperManagement(e.db).export()==original
    assert PaperAdmission(e.db).report()['active_count']==(0 if boundary=='after_commit' else 1)
    assert len(PaperReviews(e.db).export()['events'])==(5 if boundary=='after_commit' else 1)
    retire(e,case);assert PaperAdmission(e.db).report()['active_count']==0 and e.evidence.holds();e.close()


def test_concurrent_exact_acceptance_preserves_one_resolution_and_release(tmp_path):
    e,j,path=ready(tmp_path);case=prepare(e)
    def act(_):
        db=sqlite3.connect(path,isolation_level=None);db.row_factory=sqlite3.Row
        try:return PaperReviews(db).record(case['case_id'],case['fingerprint'],'retire_terminal_intent',AT)['decision']
        finally:db.close()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(act,range(2)))
    assert results[0]==results[1] and len(PaperReviews(e.db).export()['events'])==5
    assert len(PaperAdmission(e.db).records())==2 and e.evidence.holds();e.close()


def test_legacy_case_cannot_acquire_management_resolution_authority(tmp_path):
    e,j,_=ready(tmp_path);case=prepare(e)
    row=e.db.execute("SELECT * FROM paper_review_log WHERE kind='case'").fetchone();value=verified(row)
    value['schema_version']='orb-paper-review-case-v3';key=digest(value)
    e.db.execute('INSERT INTO paper_review_log(day,kind,case_id,observed_at,fingerprint,payload) VALUES(?,?,?,?,?,?)',
        (DAY,'case',key,row['observed_at'],key,encoded(value)))
    with pytest.raises(ReviewConflict,match='cannot retire'):retire(e,dict(case_id=key,fingerprint=key))
    assert PaperAdmission(e.db).report()['active_count']==1;e.close()


def test_missing_resolution_record_invalidates_both_inspection_paths(tmp_path):
    e,j,_=ready(tmp_path);retire(e,prepare(e))
    e.db.execute('DROP TRIGGER paper_review_log_no_delete')
    e.db.execute("DELETE FROM paper_review_log WHERE kind='management_resolution'")
    for read in (j.report,PaperAdmission(e.db).report):
        with pytest.raises(EvidenceError,match='management_resolution_integrity'):read()
    e.close()


def test_api_persists_resolution_and_linked_exports_without_replaying_commands(tmp_path):
    e,j,path=ready(tmp_path);e.close();app=create_app(paper_db=path)
    e=app.state.paper_engine;e.client=ReadOnly();e.review_clock=lambda:NOW+timedelta(minutes=1);e.evidence.clock=e.review_clock
    with TestClient(app,base_url='http://127.0.0.1:8765') as client:
        c=client.post('/api/paper/reviews/prepare',json={'session':DAY}).json()['attempts'][0]['correction_review']['cases'][0]
        body=dict(case_id=c['case_id'],fingerprint=c['fingerprint'],disposition='retire_terminal_intent')
        response=client.post('/api/paper/reviews/record',json=body)
        assert response.status_code==200 and response.json()['entries_blocked']
        assert len(client.get('/api/paper/reviews').json()['events'])==5
        status=client.get('/api/paper/status').json()
        assert status['management']['attempts'][0]['state']=='reviewed_terminal_history'
        assert status['admission']['active_count']==0 and not status['enabled']
        assert not e.client.posts and not e.client.deletes


def test_terminal_closing_quantity_must_match_the_saved_request(tmp_path):
    from orb.management_resolution import assess
    e,j,_=ready(tmp_path);case=prepare(e)
    terminal=deepcopy(case['terminal_accounting'])
    next(o for o in terminal['orders'] if o['role']=='closing_exit')['quantity']='5'
    result=assess(e.db,e.db.execute('SELECT * FROM paper_attempts').fetchone(),terminal,j.records())
    assert not result['eligible'] and result['reason']=='closing_terminal_quantity_mismatch'
    assert PaperAdmission(e.db).report()['active_count']==1;e.close()


def test_prior_case_v3_without_commands_keeps_its_existing_retirement_contract(tmp_path):
    e,_=no_management(tmp_path);prepare(e)
    row=e.db.execute("SELECT * FROM paper_review_log WHERE kind='case'").fetchone();value=verified(row)
    value['schema_version']='orb-paper-review-case-v3';key=digest(value)
    e.db.execute('INSERT INTO paper_review_log(day,kind,case_id,observed_at,fingerprint,payload) VALUES(?,?,?,?,?,?)',
        (DAY,'case',key,row['observed_at'],key,encoded(value)))
    retire(e,dict(case_id=key,fingerprint=key))
    assert PaperAdmission(e.db).report()['active_count']==0 and e.evidence.holds()
    assert not e.db.execute("SELECT 1 FROM paper_review_log WHERE kind='management_resolution'").fetchone();e.close()
