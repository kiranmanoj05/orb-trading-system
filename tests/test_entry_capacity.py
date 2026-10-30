"""Capacity tests exercise actual admissions, transactions and frozen evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
import json
import os
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from orb.coordinator import CoordinatorConfig, DurableCoordinator
from orb.dashboard import build_dashboard_run
from orb.dashboard_fixtures import scenarios
from orb.dashboard_import import import_dashboard
from orb.entry_capacity import EntryCaps
from orb.evaluation import evaluate, _digest, render_report
from orb.journal import JournalError
from orb.presets import PresetStore, REFERENCE
from orb.profile_runs import apply_preset
from orb.research_settings import ResearchSettings, CAPS_SCHEMA, SCHEMA
from test_coordinator import AT, DAY, SCOPE, signal, ready, marks
from test_dashboard_import import csv_text


def v3(**changes):
    return {**ResearchSettings(schema_version=CAPS_SCHEMA).to_dict(), **changes}


def two_symbols(stagger=False):
    # Same invented setup on both ETFs; optionally let SPY exit before QQQ entry.
    spy = [b for b in scenarios()['complete_trade'] if b.symbol == 'SPY']
    qqq = [replace(b, symbol='QQQ', start=b.start + timedelta(minutes=2 if stagger else 0)) for b in spy]
    if stagger:
        qqq = [replace(b, symbol='QQQ') for b in spy[:2]] + qqq[:-2]
    return sorted(spy + qqq, key=lambda b: (b.start, b.symbol))


@pytest.mark.parametrize('value', [0, 3, -1, True, '1', 1.0, None])
def test_caps_are_strict_integers(value):
    with pytest.raises(ValueError): EntryCaps(value, 2)
    with pytest.raises(ValueError): EntryCaps(2, value)


@pytest.mark.parametrize('value', ['0', '3', '1.0', '01', ' 1', '1e0', 1, True, None])
def test_settings_reject_ambiguous_cap_strings(value):
    with pytest.raises(ValueError): ResearchSettings.from_dict(v3(max_session_entries=value))


def test_old_saved_values_hashes_and_reference_do_not_upgrade_in_place(tmp_path):
    with PresetStore(tmp_path/'presets.sqlite') as store:
        ref = store.get(REFERENCE)
        old = store.save('v2', ResearchSettings(schema_version=SCHEMA).to_dict())
        new = store.save('v3', v3(max_session_entries='1'))
    with PresetStore(tmp_path/'presets.sqlite') as store:
        assert store.get(REFERENCE) == ref
        assert store.get(old['id']) == old
        assert store.get(new['id']) == new
        assert ResearchSettings.from_dict(ref['settings']).to_dict() == ref['settings']
        assert 'entry_caps' not in ResearchSettings.from_dict(old['settings']).engine_kwargs()
        assert ResearchSettings.from_dict(new['settings']).engine_kwargs()['entry_caps'] == EntryCaps(1, 2)


@pytest.mark.parametrize('stagger', [False, True])
def test_session_cap_is_not_refunded_by_exit_and_does_not_hide_candidates(stagger):
    bars = two_symbols(stagger)
    reference = evaluate(bars)
    limited = evaluate(bars, entry_caps=EntryCaps(1, 2))
    assert reference['summary']['trade_status_counts']['closed'] == 2
    assert limited['summary']['trade_status_counts'] == dict(closed=1, unresolved=0, unfilled=0, rejected=1)
    assert limited['summary']['trade_reasons']['session_entry_limit'] == 1
    assert limited['summary']['complete']
    assert limited['manifest']['schema_version'] == 'orb-evaluation-v7'
    assert limited['manifest']['dataset'] == reference['manifest']['dataset']
    assert limited['coverage'] == reference['coverage']
    blocked = next(t for t in limited['portfolio']['trades'] if t['status'] == 'rejected')
    assert blocked['entry_at'] is blocked['net_pnl'] is None
    event = next(e for e in limited['portfolio']['events'] if e['kind'] == 'rejected')
    assert event['entry_capacity']['filled_entries'] == 1
    assert event['entry_capacity']['concurrent_slots_used'] == (0 if stagger else 1)


def test_concurrency_releases_on_known_exit_not_future_exit():
    same = evaluate(two_symbols(), entry_caps=EntryCaps(2, 1))
    later = evaluate(two_symbols(True), entry_caps=EntryCaps(2, 1))
    assert same['summary']['trade_reasons']['concurrent_position_limit'] == 1
    assert later['summary']['trade_status_counts']['closed'] == 2
    # A future exit/price cannot fund the simultaneous admission.
    at = next(e['at'] for e in same['portfolio']['events'] if e['kind'] == 'rejected')
    changed = [replace(b, high=D('1000'), close=D('500')) if b.start.isoformat() > at else b for b in two_symbols()]
    prefix = lambda r: [e for e in r['portfolio']['events'] if e['at'] <= at]
    assert prefix(evaluate(changed, entry_caps=EntryCaps(2, 1))) == prefix(same)


def test_cap_count_resets_by_exchange_session_and_missing_data_stays_unknown():
    friday = two_symbols(True)
    monday = [replace(b, start=b.start+timedelta(days=3)) for b in friday]
    result = evaluate(friday+monday, entry_caps=EntryCaps(1, 1))
    assert result['summary']['trade_status_counts']['closed'] == 2
    assert result['summary']['trade_reasons']['session_entry_limit'] == 2
    missing = evaluate(scenarios()['missing_entry'], entry_caps=EntryCaps(1, 1))
    assert not missing['summary']['complete']
    assert missing['summary']['total_return'] is None
    assert missing['portfolio']['entry_capacity_final']['filled_entries'] == 0


def config(caps=EntryCaps(1, 1)):
    return CoordinatorConfig(entry_caps=caps)


def test_pending_slot_duplicate_and_restart_do_not_create_capacity(tmp_path):
    path = tmp_path/'state.sqlite'
    with DurableCoordinator(path, SCOPE, config()) as c:
        ready(c)
        accepted = c.admit('spy', signal())
        snapshot = c.snapshot()
        assert snapshot['entry_capacity']['pending_entry_slots'] == 1
        assert snapshot['entry_capacity']['filled_entries'] == 0
    with DurableCoordinator(path, SCOPE, config()) as c:
        assert c.snapshot() == snapshot
        assert c.admit('spy', signal()) == accepted
        assert c.admit('retry-same-signal', signal()) == accepted
        assert c.admit('qqq', signal('QQQ'))['reason'] == 'session_entry_limit'
        assert c.snapshot()['entry_capacity']['session_slots_used'] == 1
        assert len(c.journal.snapshot()['intents']) == 1


def test_no_fill_withdrawal_releases_slots_but_never_rearms_consumed_candidate(tmp_path):
    with DurableCoordinator(tmp_path/'state.sqlite', SCOPE, config()) as c:
        ready(c)
        original = c.admit('spy', signal())
        c.account_event('withdraw', 'cancel', signal().signal_id, AT)
        assert c.snapshot()['entry_capacity']['session_slots_used'] == 0
        assert c.admit('same', signal()) == original
        assert c.snapshot()['entry_capacity']['session_slots_used'] == 0
        assert c.admit('qqq', signal('QQQ'))['quantity'] > 0


def test_filled_entry_count_survives_close_restart_and_resets_next_session(tmp_path):
    path = tmp_path/'state.sqlite'
    with DurableCoordinator(path, SCOPE, config()) as c:
        ready(c)
        c.admit('spy', signal())
        c.account_event('fill', 'fill', signal().signal_id, AT, price=D('100.2'))
        c.account_event('close', 'close', signal().signal_id, AT, price=D('100.2'))
        assert c.snapshot()['entry_capacity']['concurrent_slots_used'] == 0
    with DurableCoordinator(path, SCOPE, config()) as c:
        assert c.snapshot()['entry_capacity']['filled_entries'] == 1
        assert c.admit('qqq', signal('QQQ'))['reason'] == 'session_entry_limit'
        c.start_session('monday', date(2026,10,12))
        later = AT + timedelta(days=3)
        c.tick('monday-minute', later)
        assert c.snapshot()['entry_capacity']['session_slots_used'] == 0
        assert c.admit('monday-spy', signal(at=later))['quantity'] > 0


def test_carried_pending_reservation_holds_today_slot_without_inventing_a_fill(tmp_path):
    with DurableCoordinator(tmp_path/'state.sqlite', SCOPE, config()) as c:
        ready(c); c.admit('spy', signal())
        c.start_session('monday', date(2026,10,12))
        later = AT+timedelta(days=3)
        c.tick('monday-minute', later)
        cap = c.snapshot()['entry_capacity']
        assert cap['filled_entries'] == 0 and cap['pending_entry_slots'] == 1
        assert c.admit('monday-qqq', signal('QQQ', later))['reason'] == 'session_entry_limit'


def test_unresolved_holding_retains_concurrency_on_later_session(tmp_path):
    with DurableCoordinator(tmp_path/'state.sqlite', SCOPE, config(EntryCaps(2,1))) as c:
        ready(c); c.admit('spy', signal())
        c.account_event('fill', 'fill', signal().signal_id, AT, price=D('100.2'))
        c.account_event('unknown-exit', 'unresolved', signal().signal_id, AT)
        day = date(2026,10,12)
        opening = AT.replace(hour=13,minute=30)+timedelta(days=3)
        c.start_session('monday',day,marks(opening))
        later = AT+timedelta(days=3)
        for i in range(1,17):
            stamp=opening+timedelta(minutes=i)
            c.tick('monday-'+str(i),stamp,marks(stamp))
        assert c.admit('monday-qqq',signal('QQQ',later))['reason']=='concurrent_position_limit'
        assert c.snapshot()['entry_capacity']['filled_entries']==0


@pytest.mark.parametrize('terminal',['partial','cancelled'])
def test_external_partial_or_cancelled_remainder_never_frees_slots(tmp_path, terminal):
    path=tmp_path/'state.sqlite'
    with DurableCoordinator(path,SCOPE,config()) as c:
        ready(c);c.admit('spy',signal())
        c.journal.claim(signal().signal_id,expected_revision=0,at=AT)
        c.journal.observe(signal().signal_id,event_id='external',state=terminal,filled_quantity=2,
            filled_notional=D('200.4'),broker_order_id='observed',expected_revision=1,at=AT)
    with DurableCoordinator(path,SCOPE,config()) as c:
        snapshot=c.snapshot()
        assert snapshot['reconciliation_required']
        assert snapshot['entry_capacity']['pending_entry_slots']==1
        assert c.admit('qqq',signal('QQQ'))['reason']=='reconciliation_required'
        with pytest.raises(JournalError,match='requires reconciliation'):
            c.account_event('bad-cancel','cancel',signal().signal_id,AT)
        assert c.snapshot()['entry_capacity']==snapshot['entry_capacity']
        assert c.snapshot()['account']==snapshot['account']


def test_two_connections_cannot_both_spend_last_slot(tmp_path):
    path=tmp_path/'state.sqlite'
    with DurableCoordinator(path,SCOPE,config()) as c:ready(c)
    barrier=Barrier(2)
    def work(symbol):
        with DurableCoordinator(path,SCOPE,config()) as c:
            barrier.wait()
            return c.admit(symbol,signal(symbol))
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(work,['SPY','QQQ']))
    assert sum(r['quantity']>0 for r in results)==1
    assert sum(r['reason']=='session_entry_limit' for r in results)==1
    with DurableCoordinator(path,SCOPE,config()) as c:
        assert c.snapshot()['entry_capacity']['session_slots_used']==1
        assert len(c.journal.snapshot()['intents'])==1


def test_transaction_failure_rolls_back_counter_and_reservation(tmp_path):
    with DurableCoordinator(tmp_path/'state.sqlite',SCOPE,config()) as c:
        ready(c);before=c.snapshot()
        c._db.execute("CREATE TEMP TRIGGER fail_event BEFORE INSERT ON coordinator_events BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(sqlite3.IntegrityError):c.admit('spy',signal())
        assert c.snapshot()==before
        assert not c.journal.snapshot()['intents']


@pytest.mark.parametrize('commit',[False,True])
def test_abrupt_exit_preserves_only_committed_capacity(tmp_path,commit):
    path=tmp_path/'state.sqlite'
    with DurableCoordinator(path,SCOPE,config()) as c:ready(c)
    code='''
import os,sys
from pathlib import Path
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from hashlib import sha256
from orb.coordinator import DurableCoordinator,CoordinatorConfig
from orb.domain import BreakoutSignal
from orb.entry_capacity import EntryCaps
at=datetime(2026,10,9,13,46,tzinfo=timezone.utc)
s=BreakoutSignal(sha256(b'orb-long-v1|SPY|2026-10-09|1').hexdigest()[:32],'orb-long-v1','SPY',at-timedelta(minutes=1),at,D('100.2'),D('99'),D('102.6'))
c=DurableCoordinator(Path(sys.argv[1]),'paper:coordinator-test',CoordinatorConfig(entry_caps=EntryCaps(1,1)))
if sys.argv[2]=='False':
 old=c._effects
 def stop(*args):
  old(*args)
  os._exit(23)
 c._effects=stop
c.admit('child',s)
os._exit(23)
'''
    assert subprocess.run([sys.executable,'-c',code,str(path),str(commit)]).returncode==23
    with DurableCoordinator(path,SCOPE,config()) as c:
        assert c.snapshot()['entry_capacity']['session_slots_used']==int(commit)
        assert len(c.journal.snapshot()['intents'])==int(commit)


def test_config_change_cannot_reset_consumed_capacity(tmp_path):
    path=tmp_path/'state.sqlite'
    with DurableCoordinator(path,SCOPE,config()) as c:ready(c);c.admit('spy',signal())
    with pytest.raises(JournalError,match='configuration or executable model changed'):
        DurableCoordinator(path,SCOPE,config(EntryCaps(2,2)))


def test_v3_profile_apply_import_and_tamper_rejection():
    bars=two_symbols(True)
    original=build_dashboard_run(bars);original['import_context']={'synthetic':True}
    with PresetStore() as store:p=store.save('One entry',v3(max_session_entries='1'))
    run=apply_preset(original,p)
    assert run['evidence']['summary']['trade_reasons']['session_entry_limit']==1
    assert run['evidence']['manifest']['base_schema_version']=='orb-evaluation-v7'
    imported=import_dashboard({'kind':'csv','source':csv_text(bars),'report':run['evidence']})
    assert imported['evidence']['summary']==run['evidence']['summary']
    bad=deepcopy(run['evidence']);bad['manifest']['config']['entry_caps']['max_session_entries']=2
    bad['evaluation_id']=_digest(bad['manifest'])
    with pytest.raises(ValueError,match='snapshot disagrees'):
        import_dashboard({'kind':'csv','source':csv_text(bars),'report':bad})
    assert 'Entry capacity' in render_report(run['evidence'])


def test_api_and_cli_use_same_saved_v3_caps(tmp_path):
    from fastapi.testclient import TestClient
    from orb.dashboard_api import create_app
    app=create_app(preset_db=tmp_path/'presets.sqlite')
    bars=two_symbols(True)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"origin":"http://127.0.0.1:8765"}) as client:
        r=client.post('/api/import',json={'kind':'csv','source':csv_text(bars)})
        assert r.status_code==201,r.text
        source=r.json()['id']
        invalid=client.post('/api/presets',json={'name':'invalid','settings':v3(max_concurrent_positions='1.5')})
        assert invalid.status_code==422
        saved=client.post('/api/presets',json={'name':'limited','settings':v3(max_session_entries='1')}).json()
        applied=client.post(f'/api/runs/{source}/apply-preset',json={'preset_id':saved['id'],'version':1})
        assert applied.status_code==201,applied.text
        run=client.get('/api/runs/'+applied.json()['id']).json()
        assert run['evidence']['summary']['trade_reasons']['session_entry_limit']==1
    csv=tmp_path/'bars.csv';csv.write_text(csv_text(bars))
    output=tmp_path/'limited.json'
    subprocess.run([sys.executable,'-m','orb.presets','--db',str(tmp_path/'presets.sqlite'),'run',
                    '--id',saved['id'],'--version','1','--csv',str(csv),'--output',str(output)],check=True,capture_output=True)
    cli=json.loads(output.read_text())
    assert cli['evidence']['summary']==run['evidence']['summary']
    assert cli['applied_settings']['preset']==run['applied_settings']['preset']


def test_installed_inspection_flags_do_not_ignore_or_change_caps(tmp_path):
    path=tmp_path/'state.sqlite'
    with DurableCoordinator(path,SCOPE,config()) as c:
        ready(c);c.admit('spy',signal());expected=c.snapshot()
    cmd=[sys.executable,'-m','orb','coordinator-inspect','--db',str(path),'--account-scope',SCOPE]
    r=subprocess.run(cmd+['--max-session-entries','1','--max-concurrent-positions','1'],capture_output=True,text=True,check=True)
    assert json.loads(r.stdout)==expected
    for flags in ([],['--max-session-entries','1'],['--max-session-entries','2','--max-concurrent-positions','2']):
        r=subprocess.run(cmd+flags,capture_output=True,text=True)
        assert r.returncode==2
    with DurableCoordinator(path,SCOPE,config()) as c:assert c.snapshot()==expected


def test_failed_fill_and_failed_cancel_do_not_release_or_double_count_slots(tmp_path):
    with DurableCoordinator(tmp_path/'state.sqlite',SCOPE,config()) as c:
        ready(c);c.admit('spy',signal());before=c.snapshot()
        with pytest.raises(ValueError,match='exceeds reservation'):
            c.account_event('bad-fill','fill',signal().signal_id,AT,price=D('101'))
        assert c.snapshot()==before
        c._db.execute("CREATE TEMP TRIGGER fail_event BEFORE INSERT ON coordinator_events BEGIN SELECT RAISE(ABORT,'injected'); END")
        for op in ('fill','cancel'):
            with pytest.raises(sqlite3.IntegrityError):
                c.account_event('failed-'+op,op,signal().signal_id,AT,**({'price':D('100.2')} if op=='fill' else {}))
            assert c.snapshot()==before


def test_unknown_submission_retains_slot_after_recovery(tmp_path):
    with DurableCoordinator(tmp_path/'state.sqlite',SCOPE,config()) as c:
        ready(c);c.admit('spy',signal())
        c.journal.claim(signal().signal_id,expected_revision=0,at=AT)
        state=c.recover(AT)
        assert state['reconciliation_required']
        assert state['entry_capacity']['session_slots_used']==1
        assert c.journal.get(signal().signal_id)['state']=='unknown'
        assert c.admit('qqq',signal('QQQ'))['reason']=='reconciliation_required'


def test_falsey_invalid_caps_never_silently_default():
    for bad in (False,0,[],{}):
        with pytest.raises(ValueError):evaluate(scenarios()['complete_trade'],entry_caps=bad)
        with pytest.raises(ValueError):CoordinatorConfig(entry_caps=bad)
