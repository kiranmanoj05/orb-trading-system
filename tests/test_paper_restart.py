"""Startup reads are evidence, never permission to replay an unknown command."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.paper_evidence import EvidenceError, digest
from orb.paper_restart import PaperRestart, basis, collect
from orb.paper_trading import PaperEngine, PaperError
from orb.presets import PresetStore
from test_paper_trading import Broker, NOW
from test_paper_evidence import seed, snapshot, capture, closed

DAY=str(NOW.date());AT=NOW.isoformat();SCOPE=sha256(b'fixture-paper-account').hexdigest()


class InventoryBroker(Broker):
    def __init__(self):super().__init__();self.reads=[];self.hook=None;self.exact_open=None
    def request(self,method,path,payload=None,**kw):
        if method=='GET':
            self.reads.append(path)
            if self.hook:self.hook(path)
        if method=='GET' and path.startswith('/v2/orders?') and self.exact_open is None:
            orders=[]
            if self.parent:
                for o in [self.parent,*(self.parent.get('legs') or []),self.closing]:
                    if o and o['status'] not in ('held','filled','canceled','expired','rejected','replaced'):
                        orders.append(dict(id=o['id'],symbol=o['symbol'],side=o['side'],status=o['status'],qty=o.get('qty',self.parent['qty']),filled_qty=o['filled_qty']))
            return orders+self.open_orders
        if method=='GET' and path.startswith('/v2/orders?') and self.exact_open is not None:return self.exact_open
        return super().request(method,path,payload,**kw)
    def find(self,key):
        self.reads.append('find:closing' if key.endswith('-x') else 'find:entry')
        return deepcopy(super().find(key))
    def order(self,key):self.reads.append('nested:entry');return deepcopy(self.parent)


def running(tmp_path,b=None):
    b=b or InventoryBroker();e=PaperEngine(tmp_path/'paper.sqlite',b,observation_clock=lambda:0,review_clock=lambda:NOW)
    p=PresetStore.paper('paper-balanced');e.settings=e.validate(p,'iex');e.record=p;e.enabled=True
    return e,b


def saved(tmp_path,parent=None):
    b=InventoryBroker();e=PaperEngine(tmp_path/'paper.sqlite',b,observation_clock=lambda:0,review_clock=lambda:NOW)
    row=seed(e);b.parent=parent or snapshot();return e,b,row


def check(e):return e.check_restart(NOW,activate=True)


def test_default_startup_gate_commits_scope_then_allows_actual_entry_path(tmp_path):
    e,b=running(tmp_path);assert e.restart_required and not PaperRestart(e.db).export()['events']
    original=b.request
    def verify(method,path,payload=None,**kw):
        if method=='POST':
            latest=PaperRestart(e.db).report()['latest']
            assert latest['state']=='flat_inventory_agrees' and not e.restart_required and not e.db.in_transaction
        return original(method,path,payload,**kw)
    b.request=verify;e.tick(NOW)
    assert len(b.posts)==1 and e.restart_seq==1 and e.meta('account')==SCOPE
    assert e.status()['restart']['basis_matches_current'] is False # New entry supersedes the saved startup basis.
    e.close()


def test_reopen_never_reuses_previous_green_checkpoint_or_auto_starts(tmp_path):
    e,b=running(tmp_path);check(e);assert not e.restart_required;e.close()
    e=PaperEngine(tmp_path/'paper.sqlite',b,observation_clock=lambda:0)
    r=e.status()['restart'];assert r['worker_check_required'] and r['latest']['can_continue'] and not r['latest_matches_worker_epoch']
    assert not e.enabled and e.thread is None;e.close()


def test_unknown_entry_flat_inventory_is_not_a_known_no_fill_and_does_not_poll_loop(tmp_path):
    e,b=running(tmp_path);e.tick(NOW);e.close();b.parent=None
    e,b=running(tmp_path,b);e.tick(NOW)
    assert e.restart_blocked and e.restart_required and not e.enabled and len(b.posts)==1
    result=e.status();assert result['restart']['latest']['state']=='entry_lookup_unknown'
    assert result['admission']['active_count']==1 and result['attempts'][0]['state']=='reserved'
    reads=len(b.reads);e.tick(NOW+timedelta(seconds=15));assert len(b.reads)==reads
    e.close()


def test_matched_pending_entry_resumes_observation_without_resubmission(tmp_path):
    e,b=running(tmp_path);b.fail_post=True
    with pytest.raises(PaperError):e.tick(NOW)
    e.close();b.fail_post=False;e,b=running(tmp_path,b);e.tick(NOW)
    r=e.status();assert r['restart']['latest']['state']=='owned_inventory_agrees' and len(b.posts)==1
    assert r['admission']['active_count']==1 and not e.restart_required;e.close()


def test_unknown_closing_lookup_keeps_reservation_and_does_not_resend(tmp_path):
    e,b=running(tmp_path);e.tick(NOW);b.parent.update(status='canceled',filled_qty='4')
    for leg in b.parent['legs']:leg['status']='canceled'
    b.positions=[dict(symbol='SPY',qty='4')];b.fail_post=True
    with pytest.raises(PaperError):e.tick(NOW)
    e.close();b.closing=None;b.fail_post=False;e,b=running(tmp_path,b);e.tick(NOW)
    r=e.status();assert r['restart']['latest']['state']=='closing_lookup_unknown' and len(b.posts)==2
    assert r['admission']['active_count']==1 and r['attempts'][0]['state']=='reserved';e.close()


def test_observed_filled_close_can_reach_existing_proof_bound_release_without_replay(tmp_path):
    e,b=running(tmp_path);e.tick(NOW);b.parent.update(status='canceled',filled_qty='4')
    for leg in b.parent['legs']:leg['status']='canceled'
    b.positions=[dict(symbol='SPY',qty='4')];e.tick(NOW);e.close()
    b.closing.update(status='filled',filled_qty='4');b.positions=[]
    later=NOW+timedelta(seconds=1);b.clock['timestamp']=later.isoformat();e,b=running(tmp_path,b);e.tick(later)
    r=e.status();assert r['restart']['latest']['state']=='owned_inventory_agrees'
    assert len(b.posts)==2 and r['admission']['active_count']==0 and r['attempts'][0]['state']=='done'
    assert r['evidence']['attempts'][0]['net_pnl_usd'] is None;e.close()


def test_terminal_known_no_fill_consumes_attempt_and_does_not_manufacture_net_profit(tmp_path):
    e,b,row=saved(tmp_path,snapshot('canceled','0'));b.parent['legs']=[]
    result=check(e);assert result['can_continue']
    assert e.status()['evidence']['attempts'][0]['state']=='known_no_entry'
    assert e.status()['attempts'][0]['state']=='reserved' # Checkpoint itself does not release risk.
    assert not b.posts and not b.deletes;e.close()


@pytest.mark.parametrize('case,state', [('foreign','unattributed_inventory'),('scope','close_account_scope_mismatch'),
    ('currency','close_account_unavailable'),('clock','close_clock_unavailable'),('shape','close_inventory_truncated_or_invalid'),
    ('boundary','close_inventory_truncated_or_invalid'),('error','snapshot_read_failed'),('evidence_error','snapshot_read_failed')])
def test_flat_checks_block_unknown_or_incomplete_inputs_without_orders(tmp_path,case,state):
    e,b=running(tmp_path)
    if case=='foreign':b.positions=[dict(symbol='QQQ',qty='1')]
    if case=='scope':e.meta('account','different')
    if case=='currency':b.account.pop('currency')
    if case=='clock':b.clock['timestamp']=(NOW-timedelta(seconds=31)).isoformat()
    if case=='shape':b.positions={}
    if case=='boundary':b.exact_open=[{}]*500
    if case=='error':b.hook=lambda _:(_ for _ in ()).throw(RuntimeError('SECRET provider body'))
    if case=='evidence_error':b.hook=lambda _:(_ for _ in ()).throw(EvidenceError('SECRET provider body'))
    result=check(e);assert not result['can_continue'] and result['state']==state
    assert e.restart_blocked and not b.posts and not b.deletes
    assert 'SECRET' not in json.dumps(PaperRestart(e.db).export())
    if case=='scope':assert len(b.reads)==2
    e.close()


@pytest.mark.parametrize('case,state', [('position','position_disagreement'),('working_missing','working_order_not_listed'),
    ('unknown_order','unattributed_spy_order'),('fill_disagrees','open_order_disagreement'),('qty_incomplete','owned_evidence_incomplete')])
def test_owned_quantity_and_open_inventory_must_match(tmp_path,case,state):
    e,b,row=saved(tmp_path,snapshot('partially_filled','4','100'));b.positions=[dict(symbol='SPY',qty='4')]
    if case=='position':b.positions[0]['qty']='5'
    if case=='working_missing':b.exact_open=[]
    if case=='unknown_order':b.open_orders=[dict(id='foreign-private',symbol='SPY',side='buy',status='new',qty='1',filled_qty='0')]
    if case=='fill_disagrees':b.exact_open=[dict(id='entry',symbol='SPY',side='buy',status='partially_filled',qty='10',filled_qty='3')]
    if case=='qty_incomplete':b.parent['status']='filled'
    result=check(e);assert not result['can_continue'] and result['state']==state
    assert e.status()['attempts'][0]['state']=='reserved' and not b.posts and not b.deletes;e.close()


def test_closed_market_checkpoint_can_inspect_consistent_owned_state_without_actions(tmp_path):
    e,b,row=saved(tmp_path);b.clock['is_open']=False
    assert check(e)['can_continue']
    assert not b.posts and not b.deletes and e.status()['restart']['latest']['snapshot']['passes'][0]['market_open'] is False;e.close()


def test_foreign_context_does_not_authorise_entries_or_touch_unrelated_positions(tmp_path):
    e,b,row=saved(tmp_path,snapshot('partially_filled','4','100'))
    b.positions=[dict(symbol='SPY',qty='4'),dict(symbol='QQQ',qty='-2',description='SECRET')]
    assert check(e)['state']=='owned_inventory_agrees'
    data=PaperRestart(e.db).export();assert 'SECRET' not in json.dumps(data) and 'fixture-paper-account' not in json.dumps(data)
    assert not b.posts and not b.deletes;e.close()


def test_two_pass_change_blocks_but_retains_both_cumulative_observations(tmp_path):
    e,b,row=saved(tmp_path,snapshot('partially_filled','4','100'));b.positions=[dict(symbol='SPY',qty='4')]
    def change(path):
        if path=='/v2/clock' and b.reads.count(path)==2:
            b.parent['filled_qty']='5';b.positions[0]['qty']='5'
    b.hook=change;result=check(e)
    assert result['state']=='inventory_changed' and not result['can_continue']
    values=[json.loads(r[0])['orders'][0]['filled_quantity'] for r in e.db.execute('SELECT payload FROM paper_observations ORDER BY seq')]
    assert values==['4','5'] and not e.evidence.holds();e.close()


def test_marked_equity_moves_are_context_not_quantity_corrections(tmp_path):
    e,b,row=saved(tmp_path,snapshot('partially_filled','4','100'));b.positions=[dict(symbol='SPY',qty='4')]
    def change(path):
        if path=='/v2/account' and b.reads.count(path)==2:b.account['equity']='99999.25'
    b.hook=change;assert check(e)['can_continue']
    passes=e.status()['restart']['latest']['snapshot']['passes']
    assert passes[0]['observed_equity_usd']!=passes[1]['observed_equity_usd'];e.close()


def test_detected_correction_and_both_observations_commit_with_blocked_checkpoint(tmp_path):
    e,b,row=saved(tmp_path,snapshot('canceled','4','100'))
    for leg in b.parent['legs']:leg['status']='canceled'
    capture(e,row,b.parent);b.parent['filled_qty']='3';b.positions=[dict(symbol='SPY',qty='3')]
    result=check(e);assert result['state']=='correction_hold' and not result['can_continue']
    assert e.evidence.holds() and e.status()['restart']['latest']['snapshot']['passes'][0]['owned']['orders'][0]['filled_quantity']=='3'
    assert e.db.execute('SELECT COUNT(*) FROM paper_observations').fetchone()[0]==2
    e.close();e=PaperEngine(tmp_path/'paper.sqlite');assert e.evidence.holds() and e.restart_required;e.close()


def test_malformed_owned_response_creates_a_persistent_hold_without_raw_body(tmp_path):
    e,b,row=saved(tmp_path);b.parent['symbol']='QQQ';b.parent['secret']='SECRET'
    result=check(e);assert result['state']=='correction_hold' and e.evidence.holds()
    assert 'SECRET' not in json.dumps(PaperRestart(e.db).export()) and not b.posts;e.close()


def test_existing_hold_is_never_cleared_by_matching_checkpoint(tmp_path):
    e,b,row=saved(tmp_path);capture(e,row,b.parent)
    e.db.execute('INSERT INTO paper_evidence_holds VALUES (?,?,?)',(DAY,'fixture_hold',AT))
    assert not check(e)['can_continue'] and e.evidence.holds()
    assert not b.posts and not b.deletes;e.close()


def test_unbound_or_multiple_pending_legacy_rows_block_before_client_access(tmp_path):
    e,b,row=saved(tmp_path);e.db.execute("DELETE FROM paper_meta WHERE key='account'")
    assert check(e)['state']=='missing_saved_scope' and not b.reads
    e.meta('account',SCOPE)
    r=dict(row);r['day']='2026-11-03';r['client_id']='other'
    e.db.execute('INSERT INTO paper_attempts VALUES (?,?,?,?,?,?)',tuple(r[k] for k in ('day','client_id','request','preset','closing','state')))
    assert check(e)['state']=='multiple_pending_attempts' and not b.reads;e.close()


def test_manual_read_is_read_only_and_does_not_rearm_blocked_worker(tmp_path):
    e,b=running(tmp_path);b.positions=[dict(symbol='QQQ',qty='1')];check(e)
    b.positions=[];report=e.refresh_restart()
    assert report['restart']['latest']['can_continue'] and report['restart']['worker_check_required'] and report['restart']['explicit_restart_required']
    assert not e.enabled and not b.posts and not b.deletes;e.close()


def test_network_reads_release_local_lock_and_lease_but_recheck_basis(tmp_path):
    e,b=running(tmp_path);seen=[]
    def hook(path):
        if seen:return
        seen.append(True)
        def read():
            with e.lock:return e.db.execute('SELECT COUNT(*) FROM paper_attempts').fetchone()[0]
        with ThreadPoolExecutor() as pool:assert pool.submit(read).result(timeout=1)==0
        e.meta('changed-during-read','yes')
    b.hook=hook
    with pytest.raises(EvidenceError,match='basis_changed'):check(e)
    assert not PaperRestart(e.db).export()['events'] and not b.posts;e.close()


def test_elapsed_budget_stops_additional_reads_and_never_admits(tmp_path):
    e,b=running(tmp_path);elapsed=lambda:23
    snap,frames=collect(b,[],None,NOW,elapsed)
    assert snap['state']=='restart_snapshot_timeout' and not b.reads and not frames;e.close()


def test_total_checkpoint_expiry_cannot_become_permission(tmp_path):
    e,b=running(tmp_path);expected=basis(e.db);snap,frames=collect(b,[],None,NOW,lambda:0)
    r=PaperRestart(e.db).save(expected,snap,frames,AT,(NOW+timedelta(seconds=31)).isoformat(),e.restart_epoch,'startup')
    assert not r['can_continue'] and r['state']=='checkpoint_expired';e.close()


def test_capacity_is_sticky_and_never_evicts_or_resumes(tmp_path,monkeypatch):
    e,b=running(tmp_path);monkeypatch.setattr('orb.paper_restart.MAX_EVENTS',0)
    result=check(e);assert result['state']=='restart_capacity_reached' and e.evidence.holds()
    assert not PaperRestart(e.db).export()['events'] and not b.posts;e.close()
    e=PaperEngine(tmp_path/'paper.sqlite');assert e.evidence.holds()
    with pytest.raises(PaperError,match='correction hold'):e.start(PresetStore.paper('paper-balanced'))
    e.close()


def test_checkpoint_snapshot_and_owned_heads_are_atomic_on_real_process_exit(tmp_path):
    e,b,row=saved(tmp_path,snapshot('canceled','0'));b.parent['legs']=[]
    expected=basis(e.db);snap,frames=collect(b,[dict(row)],SCOPE,NOW,lambda:0)
    e.close()
    code='''import os,sqlite3,json,sys
from orb.paper_restart import PaperRestart
path,expected,snap,frames,at=sys.argv[1:]
db=sqlite3.connect(path,isolation_level=None);db.row_factory=sqlite3.Row
def crash(sql):
 if sql.startswith('INSERT INTO paper_restart_log'):os._exit(86)
db.set_trace_callback(crash)
PaperRestart(db).save(expected,json.loads(snap),json.loads(frames),at,at,'test-epoch','startup')
'''
    r=subprocess.run([sys.executable,'-c',code,str(tmp_path/'paper.sqlite'),expected,json.dumps(snap),json.dumps(frames),AT],capture_output=True)
    assert r.returncode==86,r.stderr
    e=PaperEngine(tmp_path/'paper.sqlite');assert not PaperRestart(e.db).export()['events']
    assert e.db.execute('SELECT COUNT(*) FROM paper_observations').fetchone()[0]==0
    assert e.db.execute('SELECT COUNT(*) FROM paper_observation_heads').fetchone()[0]==0;e.close()


def test_initial_scope_binding_rolls_back_with_receipt_on_actual_process_exit(tmp_path):
    e,b=running(tmp_path);expected=basis(e.db);snap,frames=collect(b,[],None,NOW,lambda:0);e.close()
    code='''import os,sqlite3,json,sys
from orb.paper_restart import PaperRestart
path,expected,snap,at=sys.argv[1:]
db=sqlite3.connect(path,isolation_level=None);db.row_factory=sqlite3.Row
def crash(sql):
 if sql.startswith('INSERT INTO paper_restart_log'):os._exit(87)
db.set_trace_callback(crash)
PaperRestart(db).save(expected,json.loads(snap),[],at,at,'test','startup')
'''
    r=subprocess.run([sys.executable,'-c',code,str(tmp_path/'paper.sqlite'),expected,json.dumps(snap),AT],capture_output=True)
    assert r.returncode==87,r.stderr
    e=PaperEngine(tmp_path/'paper.sqlite');assert e.meta('account') is None and not PaperRestart(e.db).export()['events'];e.close()


def test_stale_concurrent_check_is_rejected_without_overwriting_latest(tmp_path):
    e,b=running(tmp_path);expected=basis(e.db);snap,frames=collect(b,[],None,NOW,lambda:0)
    j=PaperRestart(e.db);j.save(expected,snap,frames,AT,AT,None,'manual_read')
    later=NOW+timedelta(seconds=1);b.clock['timestamp']=later.isoformat();new=basis(e.db)
    proof,frames=collect(b,[],SCOPE,later,lambda:0);j.save(new,proof,frames,later.isoformat(),later.isoformat(),None,'manual_read')
    # Even if its saved risk basis remains unchanged, a late older scan is not current.
    with pytest.raises(EvidenceError,match='stale_restart'):j.save(basis(e.db),snap,[],AT,AT,None,'manual_read')
    assert j.report()['latest']['started_at']==later.isoformat();e.close()


def test_new_checkpoint_supersedes_review_basis_without_clearing_any_hold(tmp_path):
    from test_paper_reviews import setup,latest
    e,_,_=setup(tmp_path);e.prepare_review(DAY);assert not latest(e)['stale']
    e.client=InventoryBroker();e.client.parent=snapshot();e.refresh_restart()
    assert latest(e)['stale'] and e.evidence.holds();e.close()


def test_api_exact_empty_body_origin_read_only_result_and_export(tmp_path):
    path=tmp_path/'paper.sqlite'
    with TestClient(create_app(paper_db=path),base_url='http://127.0.0.1:8765') as c:
        e=c.app.state.paper_engine;e.client=InventoryBroker();e.observation_clock=lambda:0;e.review_clock=lambda:NOW
        assert c.post('/api/paper/restart/check',json={'resume':True}).status_code==422
        assert c.post('/api/paper/restart/check',headers={'Origin':'https://evil.invalid'}).status_code==403
        r=c.post('/api/paper/restart/check');assert r.status_code==200
        assert r.json()['restart']['latest']['can_continue'] and not r.json()['enabled']
        assert r.json()['restart']['worker_check_required']
        assert len(c.get('/api/paper/restart').json()['events'])==1
        assert c.get('/api/paper/restart?after=-1').status_code==422
        assert e.thread is None and not e.client.posts and not e.client.deletes


def test_schema7_migration_is_atomic_and_does_not_invent_checkpoint(tmp_path,monkeypatch):
    e,b,row=saved(tmp_path);e.meta('loss-blocked:'+DAY,'yes');e.db.execute('DROP TABLE paper_restart_log');e.db.execute('PRAGMA user_version=7');e.close()
    import orb.paper_trading as m
    original=m.install_restart
    def fail(db):original(db);raise RuntimeError('migration interrupted')
    monkeypatch.setattr(m,'install_restart',fail)
    with pytest.raises(RuntimeError):PaperEngine(tmp_path/'paper.sqlite')
    db=sqlite3.connect(tmp_path/'paper.sqlite');assert db.execute('PRAGMA user_version').fetchone()[0]==7
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='paper_restart_log'").fetchone();db.close()
    monkeypatch.setattr(m,'install_restart',original);e=PaperEngine(tmp_path/'paper.sqlite')
    assert e.db.execute('PRAGMA user_version').fetchone()[0]==8 and e.meta('loss-blocked:'+DAY)=='yes'
    assert e.restart_required and not PaperRestart(e.db).export()['events'] and not e.enabled;e.close()


def test_append_only_corruption_and_fixed_export_watermark(tmp_path):
    e,b=running(tmp_path);check(e);j=PaperRestart(e.db);first=j.export()
    e.refresh_restart();assert not j.export(first['next_after'],first['through'])['events'] and len(j.export()['events'])==2
    with pytest.raises(sqlite3.IntegrityError,match='append-only'):e.db.execute('DELETE FROM paper_restart_log')
    e.db.execute('DROP TRIGGER paper_restart_log_no_update');e.db.execute("UPDATE paper_restart_log SET fingerprint='bad'")
    with pytest.raises(EvidenceError,match='integrity'):e.status()
    e.close()


@pytest.mark.parametrize('fault',[PaperError('broker read failed'),RuntimeError('fixture failure')])
def test_worker_exception_requires_explicit_restart_without_repeat_inventory_reads(tmp_path,fault):
    e,b=running(tmp_path)
    class OneIteration:
        def is_set(self):return False
        def wait(self,seconds):return True
        def set(self):pass
    e.quit=OneIteration()
    original=e.tick
    def fail(now):raise fault
    e.tick=fail;e._loop();e.tick=original
    assert not e.enabled and e.restart_required and e.restart_blocked
    e.tick(NOW)
    assert not b.reads and not b.posts and not b.deletes
    # Start may rearm the existing management loop, but must discard its old gate.
    class Alive:
        def is_alive(self):return True
    old=e.restart_epoch;e.thread=Alive()
    e.start(PresetStore.paper('paper-balanced'))
    assert e.restart_required and not e.restart_blocked and e.restart_epoch!=old
    e.thread=None;e.close()
