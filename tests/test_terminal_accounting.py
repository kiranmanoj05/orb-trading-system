"""Reviewed gross reconstruction uses synthetic evidence, never broker orders."""
from datetime import timedelta
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.paper_activities import PaperActivities
from orb.paper_evidence import digest, encoded
from orb.paper_reviews import PaperReviews, ReviewConflict, record_hold
from orb.paper_trading import PaperEngine, PaperError
from orb.presets import PresetStore
from test_paper_reviews import setup, latest, ReadOnly, AT
from test_paper_evidence import capture, closed, DAY, seed
from test_paper_trading import NOW
from test_paper_activities import activity
from orb.paper_activities import normalize


def save_events(e,items,seconds=20,finished=True):
    j=PaperActivities(e.db);at=(NOW+timedelta(seconds=seconds)).isoformat()
    events=[normalize(v,j.known_orders(DAY),at) for v in items]
    j.save(DAY,events,dict(after='2026-11-01T05:00:00+00:00',until=at,scan_finished=finished,
        pages=1,received=len(items),matched=len(events),failure=None if finished else 'activity_page_limit'),at)


def ready(tmp_path):
    e,j,path=setup(tmp_path)
    save_events(e,[activity(),activity('sell-1','target','sell','10','102')],seconds=10)
    save_events(e,[activity(price='99'),activity('sell-1','target','sell','10','102')])
    return e,j,path


def assessment(e):return latest(e)['terminal_accounting']


def accept(e):
    e.prepare_review(DAY);case=latest(e)
    e.record_review(case['case_id'],case['fingerprint'],'accept_terminal_accounting')
    return case


def test_price_correction_acceptance_reconstructs_gross_once_and_preserves_hold_and_latch(tmp_path):
    e,j,path=ready(tmp_path);e.meta('loss-blocked:'+DAY,'yes');e.meta('loss-budget:'+DAY,'500')
    before=dict(e.db.execute('SELECT * FROM paper_attempts').fetchone());holds=e.evidence.holds()
    case=accept(e);a=case['terminal_accounting']
    assert a['eligible'] and a['gross_price_pnl_usd']=='30' and a['fees_usd'] is a['net_pnl_usd'] is None
    assert len(a['activities'])==2 and len(a['selected_versions'])==2
    assert a['context']['risk_metadata']['loss-budget:'+DAY]=='500'
    text=json.dumps(a);assert 'orbp-fixture' not in text and 'fixture-paper-account' not in text
    report=e.evidence.report()['attempts'][0]
    assert report['gross_price_pnl_usd'] is report['net_pnl_usd'] is None
    reviewed=report['correction_review']['reviewed_accounting']
    assert reviewed['gross_price_pnl_usd']=='30' and reviewed['state']=='accepted_current_basis'
    e.record_review(case['case_id'],case['fingerprint'],'accept_terminal_accounting')
    assert [r['kind'] for r in j.export()['events']]==['case','accounting','decision']
    assert before==dict(e.db.execute('SELECT * FROM paper_attempts').fetchone())
    assert e.evidence.holds()==holds and e.meta('loss-blocked:'+DAY)=='yes'
    assert not e.enabled and not e.client.posts and not e.client.deletes
    e.close();e=PaperEngine(path)
    assert e.evidence.report()['attempts'][0]['correction_review']['reviewed_accounting']['gross_price_pnl_usd']=='30'
    with pytest.raises(PaperError,match='correction hold'):e.start(PresetStore.paper('paper-balanced'))
    e.close()


@pytest.mark.parametrize('kind',['unknown_hold','legacy_hold','incomplete_history','pending','replaced','quantity_reduction',
    'missing_legs','missing_time','missing_price','price_mismatch','tiny_rounding_mismatch','partial_scan','missing_member',
    'legacy_membership','source_stale','scan_before_order','activity_quantity_revision','scope_currency'])
def test_unsupported_or_uncertain_terminal_evidence_cannot_be_accepted(tmp_path,kind):
    e,j,_=ready(tmp_path);row=e.db.execute('SELECT * FROM paper_attempts').fetchone()
    if kind=='unknown_hold':record_hold(e.db,DAY,'unexplained_correction',AT,'order_observation')
    elif kind=='legacy_hold':record_hold(e.db,DAY,'fill_activity_changed',AT,'legacy_first_reason_only')
    elif kind=='incomplete_history':e.meta('hold-history-incomplete','yes')
    elif kind in ('pending','replaced','quantity_reduction','missing_legs','missing_time','missing_price','scan_before_order'):
        p=closed();p['filled_avg_price']='99'
        if kind=='pending':p['legs'][1]['status']='held'
        if kind=='replaced':p['replaced_by']='changed-order'
        if kind=='quantity_reduction':p['filled_qty']='9';p['legs'][0]['filled_qty']='9'
        if kind=='missing_legs':p['legs']=[]
        if kind=='missing_time':p['filled_at']=None
        if kind=='missing_price':p['filled_avg_price']=None
        capture(e,row,p,30)
        if kind!='scan_before_order':save_events(e,[activity(price='99'),activity('sell-1','target','sell','10','102')],seconds=40)
    elif kind in ('price_mismatch','tiny_rounding_mismatch'):
        save_events(e,[activity(price='98' if kind=='price_mismatch' else '99.000000000001'),activity('sell-1','target','sell','10','102')],seconds=40)
    elif kind=='partial_scan':save_events(e,[activity(price='99'),activity('sell-1','target','sell','10','102')],seconds=40,finished=False)
    elif kind=='activity_quantity_revision':
        save_events(e,[activity(price='99',qty='9'),activity('sell-1','target','sell','10','102')],seconds=40)
    elif kind in ('missing_member','legacy_membership'):
        r=e.db.execute('SELECT * FROM paper_activity_scans ORDER BY seq DESC LIMIT 1').fetchone()
        v=json.loads(r['payload'])
        if kind=='legacy_membership':v.pop('matched_versions')
        else:v['matched_versions'][0]['fingerprint']='unknown'
        e.db.execute('INSERT INTO paper_activity_scans(day,observed_at,fingerprint,payload) VALUES(?,?,?,?)',(DAY,r['observed_at'],digest(v),encoded(v)))
    elif kind=='source_stale':e.review_clock=lambda:NOW+timedelta(minutes=5);e.evidence.clock=e.review_clock
    elif kind=='scope_currency':e.client.account['currency']='GBP'
    e.prepare_review(DAY);case=latest(e);assert not case['terminal_accounting']['eligible'],kind
    with pytest.raises(ReviewConflict,match='does not support'):e.record_review(case['case_id'],case['fingerprint'],'accept_terminal_accounting')
    assert len(j.export()['events'])==1 and e.evidence.holds() and not e.enabled;e.close()


def test_selected_version_quantities_are_not_double_counted_and_context_is_immutable(tmp_path):
    e,j,_=ready(tmp_path);e.prepare_review(DAY);case=latest(e)
    assert e.db.execute('SELECT COUNT(*) FROM paper_fill_activities').fetchone()[0]==3
    assert len(case['terminal_accounting']['activities'])==2
    original=case['terminal_accounting']['context']['preset']
    row=e.db.execute('SELECT * FROM paper_attempts').fetchone();preset=json.loads(row['preset']);preset['name']='later edited'
    e.db.execute('UPDATE paper_attempts SET preset=?',(json.dumps(preset),))
    with pytest.raises(ReviewConflict,match='changed or expired'):e.record_review(case['case_id'],case['fingerprint'],'accept_terminal_accounting')
    saved=j.export()['events'][0]['record']['terminal_accounting']['context']['preset']
    assert saved==original and saved['name']!='later edited';e.close()


def test_later_evidence_supersedes_current_projection_without_erasing_accepted_amount(tmp_path):
    e,j,_=ready(tmp_path);case=accept(e)
    e.meta('loss-blocked:'+DAY,'yes')
    reviewed=e.evidence.report()['attempts'][0]['correction_review']['reviewed_accounting']
    assert reviewed['state']=='superseded_by_new_evidence' and reviewed['gross_price_pnl_usd'] is None
    record=[r for r in j.export()['events'] if r['kind']=='accounting'][0]['record']
    assert record['assessment']['gross_price_pnl_usd']=='30'
    e.record_review(case['case_id'],case['fingerprint'],'accept_terminal_accounting')
    assert len(j.export()['events'])==3;e.close()


def test_accounting_and_decision_rollback_together_when_only_one_capacity_slot_remains(tmp_path,monkeypatch):
    import orb.paper_reviews as module
    e,j,_=ready(tmp_path);e.prepare_review(DAY);c=latest(e)
    monkeypatch.setattr(module,'MAX_REVIEWS',2)
    with pytest.raises(ReviewConflict,match='capacity'):e.record_review(c['case_id'],c['fingerprint'],'accept_terminal_accounting')
    assert [r['kind'] for r in j.export()['events']]==['case']
    assert not latest(e)['decision'] and e.evidence.holds();e.close()


def test_actual_process_exit_after_accounting_insert_cannot_leave_half_acceptance(tmp_path):
    import subprocess,sys
    e,j,path=ready(tmp_path);e.prepare_review(DAY);c=latest(e);e.close()
    code='''import os,sys,sqlite3
from orb.paper_reviews import PaperReviews
db=sqlite3.connect(sys.argv[1],isolation_level=None);db.row_factory=sqlite3.Row
def trace(sql):
 if sql.startswith('INSERT INTO paper_review_log') and "'decision'" in sql:os._exit(29)
db.set_trace_callback(trace)
PaperReviews(db).record(sys.argv[2],sys.argv[3],'accept_terminal_accounting',sys.argv[4])
'''
    result=__import__('subprocess').run([sys.executable,'-c',code,str(path),c['case_id'],c['fingerprint'],AT])
    assert result.returncode==29
    e=PaperEngine(path,review_clock=lambda:NOW+timedelta(minutes=1));j=PaperReviews(e.db)
    assert len(j.export()['events'])==1 and not latest(e)['decision']
    e.record_review(c['case_id'],c['fingerprint'],'accept_terminal_accounting')
    assert len(j.export()['events'])==3;e.close()


def test_legacy_review_case_cannot_acquire_new_accounting_authority(tmp_path):
    e,j,_=ready(tmp_path);e.prepare_review(DAY);row=e.db.execute('SELECT * FROM paper_review_log').fetchone()
    value=json.loads(row['payload']);value.pop('terminal_accounting');value['schema_version']='orb-paper-review-case-v1'
    # Construct the older immutable contract in a separate legacy case fixture.
    key=digest(value)
    e.db.execute('INSERT INTO paper_review_log(day,kind,case_id,observed_at,fingerprint,payload) VALUES(?,?,?,?,?,?)',
        (DAY,'case',key,row['observed_at'],digest(value),encoded(value)))
    with pytest.raises(ReviewConflict,match='does not support'):j.record(key,digest(value),'accept_terminal_accounting',AT)
    assert e.evidence.holds();e.close()


def test_api_acceptance_exports_both_records_and_keeps_start_blocked(tmp_path):
    e,j,path=ready(tmp_path);e.close()
    with TestClient(create_app(paper_db=path),base_url='http://127.0.0.1:8765') as c:
        engine=c.app.state.paper_engine;engine.client=ReadOnly()
        engine.review_clock=lambda:NOW+timedelta(minutes=1);engine.evidence.clock=engine.review_clock
        r=c.post('/api/paper/reviews/prepare',json={'session':DAY});assert r.status_code==200
        case=r.json()['attempts'][0]['correction_review']['cases'][0]
        assert case['terminal_accounting']['eligible']
        body=dict(case_id=case['case_id'],fingerprint=case['fingerprint'],disposition='accept_terminal_accounting')
        r=c.post('/api/paper/reviews/record',json=body);assert r.status_code==200
        assert r.json()['entries_blocked'] and r.json()['attempts'][0]['gross_price_pnl_usd'] is None
        assert c.post('/api/paper/start',json=dict(preset_id='paper-balanced',version=1,feed='iex')).status_code==409
        assert [v['kind'] for v in c.get('/api/paper/reviews').json()['events']]==['case','accounting','decision']
        assert not engine.enabled and not engine.client.posts and not engine.client.deletes


def test_review_bound_is_explicit_and_no_tolerance_is_guessed(tmp_path,monkeypatch):
    import orb.terminal_accounting as module
    e,j,_=ready(tmp_path);monkeypatch.setattr(module,'MAX_SELECTED',1)
    e.prepare_review(DAY);assert assessment(e)['reason']=='activity_review_bound'
    e.close()


@pytest.mark.parametrize('currency',[None,'GBP','usd'])
def test_preflight_currency_must_be_explicit_usd(tmp_path,currency):
    e,j,_=ready(tmp_path)
    if currency is None:e.client.account.pop('currency')
    else:e.client.account['currency']=currency
    e.prepare_review(DAY)
    assert latest(e)['preflight']['state']=='account_currency_unverified'
    assert not assessment(e)['eligible'] and len(e.client.reads)==1;e.close()


def test_timestamp_corrections_require_matching_activity_and_order_times(tmp_path):
    e,j,_=ready(tmp_path);p=closed();p['filled_avg_price']='99'
    p['filled_at']=(NOW+timedelta(seconds=5)).isoformat()
    p['legs'][0]['filled_at']=(NOW+timedelta(seconds=10)).isoformat()
    capture(e,e.db.execute('SELECT * FROM paper_attempts').fetchone(),p,30)
    buy=activity(price='99');buy['transaction_time']=p['filled_at']
    sell=activity('sell-1','target','sell','10','102');sell['transaction_time']=p['legs'][0]['filled_at']
    save_events(e,[buy,sell],40);e.prepare_review(DAY)
    assert assessment(e)['eligible'] and assessment(e)['gross_price_pnl_usd']=='30';e.close()


@pytest.mark.parametrize('time_kind',['order_differs','exit_before_entry'])
def test_unknown_event_order_chronology_is_refused(tmp_path,time_kind):
    e,j,_=ready(tmp_path);buy=activity(price='99');sell=activity('sell-1','target','sell','10','102')
    if time_kind=='order_differs':sell['transaction_time']=(NOW+timedelta(seconds=10)).isoformat()
    else:sell['transaction_time']=(NOW-timedelta(seconds=1)).isoformat()
    save_events(e,[buy,sell],40);e.prepare_review(DAY)
    assert assessment(e)['reason']==('activity_order_time_mismatch' if time_kind=='order_differs' else 'entry_exit_chronology_unverified')
    e.close()


def test_canceled_partial_entry_uses_actual_fill_quantities(tmp_path):
    path=tmp_path/'partial.sqlite';e=PaperEngine(path,ReadOnly(),review_clock=lambda:NOW+timedelta(minutes=1));row=seed(e)
    p=closed();p.update(status='canceled',filled_qty='4');p['legs'][0].update(status='canceled',filled_qty='4')
    capture(e,row,p);p['filled_avg_price']='99';capture(e,row,p,1)
    save_events(e,[activity(qty='4'),activity('sell-1','target','sell','4','102')],10)
    save_events(e,[activity(qty='4',price='99'),activity('sell-1','target','sell','4','102')],20)
    e.prepare_review(DAY)
    assert assessment(e)['eligible'] and assessment(e)['gross_price_pnl_usd']=='12';e.close()


def test_negative_reviewed_gross_is_not_clamped_or_promoted_to_net(tmp_path):
    e,j,_=ready(tmp_path);p=closed();p['filled_avg_price']='103';capture(e,e.db.execute('SELECT * FROM paper_attempts').fetchone(),p,30)
    save_events(e,[activity(price='103'),activity('sell-1','target','sell','10','102')],40)
    case=accept(e)
    assert case['terminal_accounting']['gross_price_pnl_usd']=='-10'
    assert case['terminal_accounting']['net_pnl_usd'] is None;e.close()
