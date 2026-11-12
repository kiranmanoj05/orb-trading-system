"""Native paper admission is one durable transition, never an order retry."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import fcntl
import json
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.paper_admission import PaperAdmission
from orb.paper_evidence import EvidenceError
from orb.paper_trading import PaperEngine, PaperError
from orb.presets import PresetStore
from orb.research_settings import ResearchSettings
from test_paper_trading import engine, NOW
from test_paper_evidence import seed, closed, capture

DAY=str(NOW.date());AT=NOW.isoformat();SCOPE=sha256(b'fixture-paper-account').hexdigest()


def terms(qty='10',entry='100',stop='99',**changes):
    return dict(dict(symbol='SPY',qty=qty,side='buy',type='limit',limit_price=entry,time_in_force='gtc',
        extended_hours=False,order_class='bracket',client_order_id='orbp-test',
        take_profit={'limit_price':'102.00'},stop_loss={'stop_price':stop}),**changes)


def reserve(e,**changes):
    p=PresetStore.paper('paper-balanced')
    args=dict(day=DAY,client_id='orbp-test',request=terms(),preset=p,settings=ResearchSettings.from_dict(p['settings']),
        scope=SCOPE,equity=Decimal('100000'),cash=Decimal('100000'),at=AT,feed='iex')
    args.update(changes)
    return PaperAdmission(e.db).reserve(**args)


def new(tmp_path):
    path=tmp_path/'paper.sqlite';e=PaperEngine(path)
    return e,PaperAdmission(e.db),path


def terminal(e, *, qty='10', filled='10', status='filled'):
    p=closed();p['client_order_id']='orbp-test';p['qty']=qty;p['filled_qty']=filled;p['status']=status
    for leg in p['legs']:leg['qty']=qty
    p['legs'][0]['filled_qty']=filled
    row=e.db.execute('SELECT * FROM paper_attempts WHERE day=?',(DAY,)).fetchone()
    capture(e,row,p,seconds=1)


def test_worker_commits_intent_risk_and_preset_before_post_and_retains_uncertainty(tmp_path):
    e,b=engine(tmp_path);original=b.request
    def read(method,path,payload=None,**kw):
        if method=='POST':
            r=PaperAdmission(e.db).report();assert r['active_count']==1
            assert r['planned_stop_risk_usd'] is not None and not e.db.in_transaction
            assert r['active_reservations'][0]['preset_version']==e.record['version']
        return original(method,path,payload,**kw)
    b.request=read;b.fail_post=True
    with pytest.raises(PaperError):e.tick(NOW)
    saved=e.status()['admission'];assert saved['active_count']==1 and len(b.posts)==1
    assert Decimal(saved['planned_stop_risk_usd'])<=500
    e.close();e,b=engine(tmp_path,b);b.parent=None;e.tick(NOW)
    assert len(b.posts)==1 and e.status()['admission']==saved;e.close()


def test_atomic_reserved_risk_exact_frozen_settings_and_restart(tmp_path):
    e,j,path=new(tmp_path);assert reserve(e)['accepted']
    r=j.report();a=r['active_reservations'][0]
    assert r['active_count']==r['session_attempt_count']==1
    assert r['cash_headroom_usd']=='1000' and r['exposure_headroom_usd']=='1000' and r['planned_stop_risk_usd']=='10'
    assert a['settings_sha256']==e.status()['attempts'][0]['preset']['settings_sha256']
    assert a['request_sha256'] and a['preset_sha256'] and not a['daily_loss_blocked']
    assert a['loss_budget_usd']=='1500.000'
    assert 'orbp-test' not in json.dumps(j.export())
    e.close();e=PaperEngine(path);assert e.status()['admission']==r;e.close()


def test_cost_headroom_is_exact_for_partial_risk_utilisation(tmp_path):
    e,j,_=new(tmp_path);p=deepcopy(PresetStore.paper('paper-balanced'));p['settings']['fee_per_share']='0.01';p['settings']['slippage_bps']='1'
    s=ResearchSettings.from_dict(p['settings']);p['settings_sha256']=s.fingerprint
    reserve(e,preset=p,settings=s,request=terms(qty='7'))
    a=j.report()['active_reservations'][0]
    assert a['cash_headroom_usd']=='700.14' and a['exposure_headroom_usd']=='700.07'
    assert a['planned_stop_risk_usd']=='7.2793';e.close()


def test_loss_checkpoint_tightens_atomically_and_latch_never_clears(tmp_path):
    e,j,path=new(tmp_path);s=ResearchSettings.from_dict(PresetStore.paper('paper-balanced')['settings'])
    j.checkpoint(DAY,s,Decimal('100000'),SCOPE)
    calm=ResearchSettings.from_dict(PresetStore.paper('paper-calm')['settings'])
    assert Decimal(j.checkpoint(DAY,calm,Decimal('100000'),SCOPE)['loss_budget_usd'])==1000
    assert j.checkpoint(DAY,s,Decimal('98900'),SCOPE)['daily_loss_blocked']
    assert not reserve(e)['accepted'] and not j.export()['events'] and not e.status()['attempts']
    e.close();e=PaperEngine(path);assert e.status()['admission']['daily_loss_blocked'];e.close()


def test_budget_checkpoint_half_written_legacy_state_is_not_guessed(tmp_path):
    e,j,_=new(tmp_path);e.meta('equity:'+DAY,'100000')
    with pytest.raises(EvidenceError,match='incomplete_loss'):reserve(e)
    assert e.meta('loss-budget:'+DAY) is None and e.meta('account') is None and not e.status()['attempts'];e.close()


@pytest.mark.parametrize('change', ['latch','tighten','scope','hold'])
def test_worker_rechecks_mutable_gates_after_sizing_requests_before_post(tmp_path,change):
    e,b=engine(tmp_path);original=b.request
    def hook(method,path,payload=None,**kw):
        if path=='/v2/assets/SPY':
            if change=='latch':e.meta('loss-blocked:'+DAY,'yes')
            if change=='tighten':e.meta('loss-budget:'+DAY,'1');b.account['equity']='99998'
            if change=='scope':e.meta('account','different-scope')
            if change=='hold':e.meta('fee-hold-reason','invalid_fee_evidence');e.meta('fee-hold-at',AT)
        return original(method,path,payload,**kw)
    # A persisted tighter budget does not imply a new equity sample. For that
    # case reserve() below receives the changed observation explicitly.
    if change=='tighten':
        j=PaperAdmission(e.db);j.checkpoint(DAY,e.settings,Decimal('100000'),SCOPE);e.meta('loss-budget:'+DAY,'1')
        assert not reserve(e,equity=Decimal('99998'))['accepted']
    else:
        b.request=hook
        try:e.tick(NOW)
        except EvidenceError:pass
    assert not b.posts and not e.status()['attempts'];e.close()


@pytest.mark.parametrize('order_terms', [terms(qty='0'),terms(qty='1.5'),terms(qty='10000'),terms(stop='101'),terms(side='sell'),
    terms(extended_hours=True),terms(take_profit={'limit_price':'103.00'}),terms(order_class='simple')])
def test_invalid_or_oversized_terms_never_claim_attempt(tmp_path,order_terms):
    e,j,_=new(tmp_path)
    with pytest.raises((EvidenceError,ValueError)):reserve(e,request=order_terms)
    assert not e.status()['attempts'] and not j.export()['events'] and e.meta('account') is None;e.close()


@pytest.mark.parametrize('which',['hash','applied','schedule','timing','cap','feed','time'])
def test_settings_and_session_mismatch_is_explicit(tmp_path,which):
    e,j,_=new(tmp_path);p=deepcopy(PresetStore.paper('paper-balanced'));s=ResearchSettings.from_dict(p['settings']);kwargs={}
    if which=='hash':p['settings_sha256']='wrong'
    if which=='applied':kwargs['settings']=ResearchSettings.from_dict(PresetStore.paper('paper-calm')['settings'])
    if which=='schedule':p['settings']['weekdays']=[1,3]
    if which=='timing':p['settings'].update(schema_version='orb-research-settings-v4',range_minutes='5',entry_delay_minutes='0',entry_cutoff_local='11:00',stop_policy='range_midpoint')
    if which=='cap':p['settings']['max_session_entries']='2'
    if which=='feed':kwargs['feed']='unknown'
    if which=='time':kwargs['at']=(NOW+timedelta(hours=2)).isoformat()
    if which in ('schedule','timing','cap'):
        s=ResearchSettings.from_dict(p['settings']);p['settings_sha256']=s.fingerprint
    kwargs.setdefault('settings',s)
    with pytest.raises((EvidenceError,ValueError)):reserve(e,preset=p,**kwargs)
    assert not e.status()['attempts'] and not j.export()['events'];e.close()


def test_duplicate_different_profile_or_day_cannot_multiply_pending_budget(tmp_path):
    e,j,_=new(tmp_path);reserve(e)
    with pytest.raises(EvidenceError,match='session_cap'):reserve(e)
    next_day=NOW+timedelta(days=1)
    with pytest.raises(EvidenceError,match='pending_account_attempt'):reserve(e,day=str(next_day.date()),at=next_day.isoformat())
    assert j.report()['active_count']==1 and len(j.export()['events'])==1;e.close()


def test_two_independent_connections_cannot_both_claim(tmp_path):
    e,j,path=new(tmp_path);e.close()
    def attempt(_):
        e=PaperEngine(path)
        try:return reserve(e)['accepted']
        except EvidenceError:return False
        finally:e.close()
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(attempt,range(2)))
    assert sorted(results)==[False,True]
    e=PaperEngine(path);assert len(e.status()['attempts'])==1 and len(PaperAdmission(e.db).export()['events'])==1;e.close()


def test_partial_fill_keeps_full_headroom_and_terminal_release_is_atomic(tmp_path):
    e,j,path=new(tmp_path);reserve(e);original=j.report()['planned_stop_risk_usd']
    terminal(e,filled='4',status='partially_filled')
    with pytest.raises(EvidenceError,match='terminal_proof'):j.finish(DAY,(NOW+timedelta(seconds=2)).isoformat())
    assert j.report()['planned_stop_risk_usd']==original
    # Use a clean terminal cancellation with four shares exited; a later full
    # status must not erase an earlier conflicting observation.
    terminal(e,filled='4',status='canceled')
    j.finish(DAY,(NOW+timedelta(seconds=2)).isoformat())
    assert j.report()['active_count']==0 and j.report()['planned_stop_risk_usd']=='0'
    assert j.report()['session_attempt_count']==1
    j.finish(DAY,(NOW+timedelta(seconds=3)).isoformat());assert len(j.export()['events'])==2
    e.close();e=PaperEngine(path);assert e.status()['admission']['active_count']==0;e.close()


def test_terminal_status_with_inconsistent_filled_quantity_stays_reserved(tmp_path):
    e,j,_=new(tmp_path);reserve(e);terminal(e,filled='4')
    with pytest.raises(EvidenceError,match='terminal_proof'):j.finish(DAY,(NOW+timedelta(seconds=2)).isoformat())
    assert j.report()['active_count']==1 and e.status()['attempts'][0]['state']=='reserved';e.close()


def test_missing_proof_or_clock_reversal_never_releases(tmp_path):
    e,j,_=new(tmp_path);reserve(e)
    with pytest.raises(EvidenceError,match='terminal_proof'):j.finish(DAY,AT)
    terminal(e)
    with pytest.raises(EvidenceError,match='clock_reversed'):j.finish(DAY,AT)
    assert j.report()['active_count']==1;e.close()


def test_legacy_pending_headroom_is_unknown_and_never_backfilled(tmp_path):
    e,j,_=new(tmp_path);row=seed(e)
    r=j.report();assert r['legacy_pending_sessions']==[DAY] and r['planned_stop_risk_usd'] is None
    capture(e,row,closed(),seconds=1);j.finish(DAY,(NOW+timedelta(seconds=2)).isoformat())
    r=j.report();assert r['active_count']==0 and r['legacy_completed_count']==1
    event=j.export()['events'][0];assert event['legacy_untracked'] and event['reservation_seq'] is None;e.close()


def test_correction_hold_prevents_release_and_never_changes_latches(tmp_path):
    e,j,_=new(tmp_path);reserve(e);terminal(e)
    e.meta('loss-blocked:'+DAY,'yes');e.db.execute('INSERT INTO paper_evidence_holds VALUES (?,?,?)',(DAY,'fixture_hold',AT))
    with pytest.raises(EvidenceError,match='correction_hold'):j.finish(DAY,(NOW+timedelta(seconds=2)).isoformat())
    assert j.report()['active_count']==1 and e.meta('loss-blocked:'+DAY)=='yes';e.close()


def test_capacity_rolls_back_whole_admission_and_terminal_transition(tmp_path,monkeypatch):
    e,j,_=new(tmp_path);monkeypatch.setattr('orb.paper_admission.MAX_EVENTS',0)
    with pytest.raises(EvidenceError,match='capacity'):reserve(e)
    assert not e.status()['attempts'] and not j.export()['events'] and e.meta('account') is None
    monkeypatch.setattr('orb.paper_admission.MAX_EVENTS',2);reserve(e);terminal(e)
    monkeypatch.setattr('orb.paper_admission.MAX_EVENTS',1)
    with pytest.raises(EvidenceError,match='capacity'):j.finish(DAY,(NOW+timedelta(seconds=2)).isoformat())
    assert e.status()['attempts'][0]['state']=='reserved' and j.report()['active_count']==1;e.close()


def test_frozen_export_and_tampered_intent_or_ledger_fail_closed(tmp_path):
    e,j,_=new(tmp_path);reserve(e);first=j.export();terminal(e);j.finish(DAY,(NOW+timedelta(seconds=2)).isoformat())
    assert not j.export(first['next_after'],first['through'])['events'] and len(j.export()['events'])==2
    with pytest.raises(sqlite3.IntegrityError,match='append-only'):e.db.execute('DELETE FROM paper_admission_log')
    e.db.execute("UPDATE paper_attempts SET request='{}'")
    with pytest.raises(EvidenceError,match='intent_changed'):e.status()
    e.close()


def test_manual_done_without_release_is_detected(tmp_path):
    e,j,_=new(tmp_path);reserve(e);e.db.execute("UPDATE paper_attempts SET state='done'")
    with pytest.raises(EvidenceError,match='unreleased_done'):j.report()
    e.close()


@pytest.mark.parametrize('transition',['reserve','release','checkpoint'])
def test_real_process_exit_rolls_back_each_complete_transition(tmp_path,transition):
    e,j,path=new(tmp_path)
    if transition=='release':reserve(e);terminal(e)
    e.close()
    code='''import os,sqlite3,json,sys
from datetime import datetime
from decimal import Decimal
from orb.paper_admission import PaperAdmission
from orb.presets import PresetStore
from orb.research_settings import ResearchSettings
path,kind,day,at,scope,request=sys.argv[1:]
db=sqlite3.connect(path,isolation_level=None);db.row_factory=sqlite3.Row
j=PaperAdmission(db);p=PresetStore.paper('paper-balanced');s=ResearchSettings.from_dict(p['settings'])
def crash(sql):
 if kind=='reserve' and 'INSERT INTO paper_admission_log' in sql:os._exit(83)
 if kind=='release' and "UPDATE paper_attempts SET state='done'" in sql:os._exit(83)
 if kind=='checkpoint' and 'loss-budget:' in sql and sql.startswith('INSERT'):os._exit(83)
db.set_trace_callback(crash)
if kind=='reserve':j.reserve(day,'orbp-test',json.loads(request),p,s,scope,Decimal('100000'),Decimal('100000'),at,'iex')
elif kind=='release':j.finish(day,at)
else:j.checkpoint(day,s,Decimal('100000'),scope)
'''
    r=subprocess.run([sys.executable,'-c',code,str(path),transition,DAY,(NOW+timedelta(seconds=2)).isoformat(),SCOPE,json.dumps(terms())],capture_output=True)
    assert r.returncode==83,r.stderr
    e=PaperEngine(path);j=PaperAdmission(e.db)
    if transition=='release':assert j.report()['active_count']==1 and len(j.export()['events'])==1 and e.status()['attempts'][0]['state']=='reserved'
    else:assert not e.status()['attempts'] and not j.export()['events'] and e.meta('account') is None and e.meta('equity:'+DAY) is None
    e.close()


def test_schema5_upgrade_is_transactional_and_retains_state(tmp_path,monkeypatch):
    e,j,path=new(tmp_path);e.meta('loss-blocked:'+DAY,'yes');e.db.execute('DROP TABLE paper_management_log');e.db.execute('DROP TABLE paper_admission_log');e.db.execute('PRAGMA user_version=5');e.close()
    import orb.paper_trading as m
    original=m.install_admission
    def fail(db):original(db);raise RuntimeError('interrupt migration')
    monkeypatch.setattr(m,'install_admission',fail)
    with pytest.raises(RuntimeError):PaperEngine(path)
    db=sqlite3.connect(path);assert db.execute('PRAGMA user_version').fetchone()[0]==5
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='paper_admission_log'").fetchone();db.close()
    monkeypatch.setattr(m,'install_admission',original)
    with path.with_suffix('.lock').open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(PaperError,match='Stop the existing'):PaperEngine(path)
    e=PaperEngine(path);assert e.db.execute('PRAGMA user_version').fetchone()[0]==7
    assert e.meta('loss-blocked:'+DAY)=='yes' and not e.enabled;e.close()


def test_api_read_only_export_cursor_and_backend_status(tmp_path):
    e,j,path=new(tmp_path);reserve(e);e.close()
    with TestClient(create_app(paper_db=path),base_url='http://127.0.0.1:8765') as c:
        status=c.get('/api/paper/status').json();assert status['admission']['active_count']==1
        rows=c.get('/api/paper/admissions').json();assert len(rows['events'])==1
        assert c.get('/api/paper/admissions?after=-1').status_code==422
        assert c.post('/api/paper/admissions',json={'release':True}).status_code==405
        assert 'orbp-test' not in json.dumps(rows)


@pytest.mark.parametrize('field', ['fingerprint','intent','scope'])
def test_invalid_admission_blocks_worker_before_any_broker_read(tmp_path,field):
    e,b=engine(tmp_path);reserve(e);b.reads=[];original=b.request
    def observe(*args,**kw):b.reads.append(args);return original(*args,**kw)
    b.request=observe
    if field=='fingerprint':
        e.db.execute('DROP TRIGGER paper_admission_log_no_update');e.db.execute("UPDATE paper_admission_log SET fingerprint='bad'")
    if field=='intent':e.db.execute("UPDATE paper_attempts SET preset='{}'")
    if field=='scope':e.meta('account','wrong')
    with pytest.raises(EvidenceError):e.tick(NOW)
    assert not b.reads and not b.posts and not b.deletes;e.enabled=False
    with pytest.raises(EvidenceError):e.start(PresetStore.paper('paper-balanced'))
    assert e.thread is None;e.close()


def test_terminal_release_proof_corruption_remains_detectable(tmp_path):
    e,j,_=new(tmp_path);reserve(e);terminal(e);j.finish(DAY,(NOW+timedelta(seconds=2)).isoformat())
    e.db.execute('DROP TRIGGER paper_observations_no_update');e.db.execute("UPDATE paper_observations SET fingerprint='bad'")
    with pytest.raises(EvidenceError,match='proof_changed'):j.report()
    e.close()


def test_last_row_is_reserved_for_terminal_release(tmp_path,monkeypatch):
    e,j,_=new(tmp_path);monkeypatch.setattr('orb.paper_admission.MAX_EVENTS',1)
    with pytest.raises(EvidenceError,match='capacity'):reserve(e)
    assert not e.status()['attempts'] and not j.export()['events'];e.close()


def test_known_no_fill_releases_headroom_but_consumes_attempt(tmp_path):
    e,j,_=new(tmp_path);reserve(e)
    row=e.db.execute('SELECT * FROM paper_attempts WHERE day=?',(DAY,)).fetchone()
    p=closed();p.update(client_order_id='orbp-test',status='canceled',filled_qty='0',filled_avg_price=None,filled_at=None,legs=[])
    capture(e,row,p,seconds=1);j.finish(DAY,(NOW+timedelta(seconds=2)).isoformat())
    assert j.report()['active_count']==0 and j.report()['session_attempt_count']==1
    assert e.evidence.report()['attempts'][0]['state']=='known_no_entry'
    with pytest.raises(EvidenceError,match='session_cap'):reserve(e)
    e.close()


def test_quantity_release_does_not_invent_priced_pnl(tmp_path):
    e,j,_=new(tmp_path);reserve(e)
    row=e.db.execute('SELECT * FROM paper_attempts WHERE day=?',(DAY,)).fetchone()
    p=closed();p['client_order_id']='orbp-test'
    for order in [p,*p['legs']]:order['filled_avg_price']=None
    capture(e,row,p,seconds=1);j.finish(DAY,(NOW+timedelta(seconds=2)).isoformat())
    assert j.report()['active_count']==0
    result=e.evidence.report()['attempts'][0]
    assert result['state']=='missing_fill_price' and result['gross_price_pnl_usd'] is result['net_pnl_usd'] is None;e.close()
