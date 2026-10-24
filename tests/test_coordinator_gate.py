"""Safety contracts at the snapshot/coordinator boundary; no provider calls."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
import sqlite3
import subprocess
import sys

import pytest

from orb.coordinator import DurableCoordinator, coordinator_gate_demo
from orb.journal import JournalError
from orb.paper_snapshot import PaperSnapshotCapture
from orb.reconciliation import BrokerPosition, BrokerSnapshot
from test_coordinator import AT, DAY, SCOPE, ready, signal


def broker(**changes):
    return replace(BrokerSnapshot(SCOPE, AT - timedelta(seconds=10), D('10000'), (), (), True), **changes)


@pytest.fixture
def c(tmp_path):
    with DurableCoordinator(tmp_path / 'coordinator.sqlite', SCOPE) as instance:
        ready(instance)
        yield instance


def test_match_is_durable_but_never_dispatch_permission(tmp_path):
    path = tmp_path / 'state.sqlite'
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
        result = c.observe_broker_snapshot('match', broker(), at=AT)
        assert result['broker_reconciled']
        assert not result['paper_execution_enabled'] and not result['orders_submitted']
        before = c.snapshot()
    with DurableCoordinator(path, SCOPE, create=False) as c:
        assert c.snapshot() == before
        assert c.snapshot()['broker_reconciled']
        assert not c.snapshot()['paper_execution_enabled']
        assert c.observe_broker_snapshot('match', broker(), at=AT) == result
        assert c.snapshot() == before


@pytest.mark.parametrize('change,reason', [
    ({'cash': D('9999')}, 'cash_mismatch'),
    ({'account_scope': 'paper:wrong'}, 'account_scope_mismatch'),
    ({'complete': False}, 'snapshot_incomplete'),
    ({'positions': (BrokerPosition('SPY', 1),)}, 'position_mismatch'),
    ({'open_client_order_ids': ('foreign-order',)}, 'unexpected_open_orders'),
    ({'as_of': AT + timedelta(microseconds=1)}, 'snapshot_time_mismatch'),
    ({'as_of': AT - timedelta(seconds=60, microseconds=1)}, 'snapshot_time_mismatch'),
])
def test_mismatch_blocks_candidate_and_retains_cash(c, change, reason):
    before = c.snapshot()['account']
    result = c.observe_broker_snapshot('bad', broker(**change), at=AT)
    assert reason in result['comparison']['reasons']
    assert not result['broker_reconciled']
    assert c.snapshot()['reconciliation_required']
    admission = c.admit('blocked', signal())
    assert admission['quantity'] == 0 and admission['reason'] == 'reconciliation_required'
    assert c.snapshot()['account'] == before


@pytest.mark.parametrize('age', [0, 1, 59, 60])
def test_bounded_nonfuture_snapshot_age(c, age):
    assert c.observe_broker_snapshot('age', broker(as_of=AT-timedelta(seconds=age)), at=AT)['broker_reconciled']


def test_rejected_attempt_stays_rejected_after_match(c):
    c.observe_broker_snapshot('bad', broker(cash=D('1')), at=AT)
    rejected = c.admit('spy', signal())
    c.observe_broker_snapshot('good', broker(), at=AT)
    assert c.admit('new-request-same-attempt', signal()) == rejected
    assert not c.snapshot()['account']['allocations']


def test_new_candidate_invalidates_comparison_and_old_request_does_not_refresh(c):
    result = c.observe_broker_snapshot('good', broker(), at=AT)
    assert c.admit('spy', signal())['quantity'] == 24
    assert not c.snapshot()['broker_reconciled']
    assert 'snapshot_stale' in c.snapshot()['reconciliation_reasons']
    assert c.observe_broker_snapshot('good', broker(), at=AT) == result
    assert not c.snapshot()['broker_reconciled']
    before = c.snapshot()['account']
    assert c.admit('qqq', signal('QQQ'))['quantity'] == 0
    assert c.snapshot()['account'] == before


def test_pending_unsent_reservation_does_not_require_external_open_order(c):
    c.admit('spy', signal())
    before = c.snapshot()['account']
    assert c.observe_broker_snapshot('good', broker(), at=AT)['broker_reconciled']
    assert c.snapshot()['account'] == before
    assert c.snapshot()['account']['reserved_cash'] != '0'


def test_minute_clock_invalidates_snapshot(c):
    c.observe_broker_snapshot('good', broker(), at=AT)
    later = AT + timedelta(minutes=1)
    c.tick('later', later)
    assert 'snapshot_stale' in c.snapshot()['reconciliation_reasons']
    assert c.observe_broker_snapshot('fresh', broker(as_of=later), at=later)['broker_reconciled']


@pytest.mark.parametrize('operation', ['fill', 'cancel', 'close', 'unresolved'])
def test_failed_comparison_blocks_modeled_operations_without_releasing_resources(c, operation):
    c.admit('spy', signal())
    before = c.snapshot()['account']
    c.observe_broker_snapshot('bad', broker(cash=D('9999')), at=AT)
    with pytest.raises(JournalError, match='requires reconciliation'):
        c.account_event('operation', operation, signal().signal_id, AT,
                        price=D('100.20') if operation in ('fill', 'close') else None)
    assert c.snapshot()['account'] == before


@pytest.mark.parametrize('reason', ['order_status_unknown', 'terminal_correction', 'fill_bust', 'external_activity'])
def test_hold_survives_matching_snapshot_restart_and_new_session(tmp_path, reason):
    path = tmp_path / 'state.sqlite'
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
        c.hold_recovery('hold', reason, at=AT)
        assert not c.observe_broker_snapshot('match', broker(), at=AT)['broker_reconciled']
        before = c.snapshot()
    with DurableCoordinator(path, SCOPE, create=False) as c:
        assert c.snapshot() == before
        assert reason in c.snapshot()['reconciliation_reasons']
        # Oct 12 is the next exchange session; holds do not reset overnight.
        c.start_session('next-session', DAY + timedelta(days=3))
        assert reason in c.snapshot()['recovery_holds']
        assert not c.snapshot()['broker_reconciled']


@pytest.mark.parametrize('reason', ['capture_unavailable', 'unsupported_positions'])
def test_capture_failure_is_durable_and_can_be_superseded(c, reason):
    c.observe_broker_snapshot('match', broker(), at=AT)
    c.record_capture_failure('failure', reason, at=AT)
    assert reason in c.snapshot()['reconciliation_reasons']
    assert not c.snapshot()['broker_reconciled']
    assert c.observe_broker_snapshot('rechecked', broker(), at=AT)['broker_reconciled']


@pytest.mark.parametrize('status', ['claimed', 'partial', 'unknown'])
def test_external_journal_evidence_cannot_be_cleared_by_matching_cash(c, status):
    c.admit('spy', signal())
    c.observe_broker_snapshot('before', broker(), at=AT)
    before = c.snapshot()['account']
    c.journal.claim(signal().signal_id, expected_revision=0, at=AT)
    if status == 'unknown':
        c.journal.recover(AT)
    elif status != 'claimed':
        c.journal.observe(signal().signal_id, event_id='external', state=status,
            filled_quantity=2 if status == 'partial' else 0,
            filled_notional=D('200.40') if status == 'partial' else D('0'),
            broker_order_id='external-order', expected_revision=1, at=AT)
    assert 'journal_changed' in c.snapshot()['reconciliation_reasons']
    result = c.observe_broker_snapshot('after', broker(), at=AT)
    assert not result['broker_reconciled']
    assert 'external_lifecycle_unsupported' in result['comparison']['reasons']
    assert c.snapshot()['account'] == before


def test_synthetic_fills_cannot_be_blessed_as_actual_account_truth(c):
    c.admit('spy', signal())
    c.account_event('fill', 'fill', signal().signal_id, AT, price=D('100.20'))
    state = c.snapshot()
    result = c.observe_broker_snapshot('match', broker(cash=D(state['account']['cash']),
        positions=(BrokerPosition('SPY', 24),)), at=AT)
    assert result['comparison']['reasons'] == ['synthetic_lifecycle']
    assert not result['broker_reconciled']


def test_conflicting_request_leaves_evidence_unchanged(c):
    c.observe_broker_snapshot('same', broker(), at=AT)
    before = c.snapshot()
    with pytest.raises(JournalError, match='conflicting duplicate'):
        c.observe_broker_snapshot('same', broker(cash=D('9999')), at=AT)
    assert c.snapshot() == before


def test_failed_atomic_evidence_write_preserves_previous_state(c):
    c.record_capture_failure('failure', 'capture_unavailable', at=AT)
    before = c.snapshot()
    c._db.execute("CREATE TEMP TRIGGER fail_gate BEFORE INSERT ON coordinator_events BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        c.observe_broker_snapshot('good', broker(), at=AT)
    assert c.snapshot() == before


def test_second_connection_sees_new_hold_instead_of_old_matching_view(tmp_path):
    path = tmp_path / 'state.sqlite'
    with DurableCoordinator(path, SCOPE) as a, DurableCoordinator(path, SCOPE) as b:
        ready(a)
        a.observe_broker_snapshot('match', broker(), at=AT)
        assert b.snapshot()['broker_reconciled']
        b.hold_recovery('hold', 'terminal_correction', at=AT)
        assert a.admit('spy', signal())['quantity'] == 0
        assert not a.snapshot()['broker_reconciled']


def test_competing_candidate_commands_cannot_both_consume_one_comparison(tmp_path):
    path = tmp_path / 'state.sqlite'
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
        c.observe_broker_snapshot('match', broker(), at=AT)
    def admit(symbol):
        with DurableCoordinator(path, SCOPE, create=False) as c:
            return c.admit(symbol, signal(symbol))['quantity']
    with ThreadPoolExecutor(max_workers=2) as pool:
        quantities = list(pool.map(admit, ['SPY', 'QQQ']))
    assert sorted(quantities) == [0, 24]


@pytest.mark.parametrize('reason', ['secret-value', '', None])
def test_only_fixed_reason_codes_are_persisted(c, reason):
    before = c.snapshot()
    for method in [c.hold_recovery, c.record_capture_failure]:
        with pytest.raises(JournalError, match='unsupported'):
            method('invalid', reason, at=AT)
    assert c.snapshot() == before


def test_paper_capture_shared_contract_preserves_incomplete_outcome(c):
    capture = PaperSnapshotCapture(AT-timedelta(seconds=15), AT-timedelta(seconds=10),
                                   broker(), ('unstable_capture',), ())
    result = c.observe_paper_capture('capture', capture, at=AT)
    assert 'snapshot_incomplete' in result['comparison']['reasons']
    capture = replace(capture, reasons=())
    assert c.observe_paper_capture('stable', capture, at=AT)['broker_reconciled']


def test_unsupported_capture_does_not_retain_previous_green_state(c):
    c.observe_broker_snapshot('match', broker(), at=AT)
    capture = PaperSnapshotCapture(AT, AT, None, ('unsupported_position',), ())
    c.observe_paper_capture('unsupported', capture, at=AT)
    assert 'unsupported_positions' in c.snapshot()['reconciliation_reasons']


def test_invalid_snapshot_and_capture_types_are_rejected(c):
    for method in [c.observe_broker_snapshot, c.observe_paper_capture]:
        with pytest.raises(JournalError, match='expected'):
            method('invalid', {}, at=AT)


def test_gate_demo_is_repeatable_and_cli_inspection_agrees(tmp_path):
    result = coordinator_gate_demo(tmp_path / 'a.sqlite')
    assert result == coordinator_gate_demo(tmp_path / 'b.sqlite')
    assert result['restored_identically']
    assert result['matching']['broker_reconciled']
    assert not result['after_correction_hold']['broker_reconciled']
    completed = subprocess.run([sys.executable, '-m', 'orb', 'coordinator-gate-demo',
                               '--db', str(tmp_path / 'cli.sqlite')], capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout) == result
    inspected = subprocess.run([sys.executable, '-m', 'orb', 'coordinator-inspect',
        '--db', str(tmp_path/'cli.sqlite'), '--account-scope', 'paper:synthetic-gate'],
        capture_output=True, text=True, check=True)
    assert json.loads(inspected.stdout) == result['state']


@pytest.mark.parametrize('error_type', ['adapter', 'credentials', 'transport'])
def test_capture_errors_replace_green_state_without_persisting_secrets(c, monkeypatch, error_type):
    from orb.acquisition import AcquisitionError
    from orb.paper_snapshot import PaperSnapshotError
    import orb.paper_snapshot as adapter
    c.observe_broker_snapshot('match', broker(), at=AT)
    cls = {'adapter': PaperSnapshotError, 'credentials': AcquisitionError, 'transport': OSError}[error_type]
    def fail():
        raise cls('SENSITIVE_PROVIDER_BODY_OR_SECRET')
    monkeypatch.setattr(adapter, 'capture_paper_snapshot', fail)
    result = c.check_paper_account('network-failure', at=AT)
    assert result['comparison']['reasons'] == ['capture_unavailable']
    assert not c.snapshot()['broker_reconciled']
    assert 'SENSITIVE' not in json.dumps(c.snapshot())
    rows = list(c._db.execute('SELECT command,outcome FROM coordinator_events'))
    assert 'SENSITIVE' not in str([tuple(row) for row in rows])


def test_real_capture_function_missing_credentials_records_failure_without_network(c, monkeypatch):
    monkeypatch.delenv('APCA_API_KEY_ID', raising=False)
    monkeypatch.delenv('APCA_API_SECRET_KEY', raising=False)
    assert not c.check_paper_account('no-credentials', at=AT)['broker_reconciled']
    assert 'capture_unavailable' in c.snapshot()['reconciliation_reasons']


def test_capture_retry_never_requeries_or_revives_historical_success(c, monkeypatch):
    import orb.paper_snapshot as adapter
    calls = []
    def capture():
        calls.append(1)
        # Network work must not hold the SQLite writer transaction.
        assert not c._db.in_transaction
        assert "capture_in_progress" in c.snapshot()["reconciliation_reasons"]
        return PaperSnapshotCapture(AT-timedelta(seconds=15), AT-timedelta(seconds=10), broker(), (), ())
    monkeypatch.setattr(adapter, 'capture_paper_snapshot', capture)
    first = c.check_paper_account('capture', at=AT)
    c.hold_recovery('hold', 'terminal_correction', at=AT)
    assert c.check_paper_account('capture', at=AT) == first
    assert len(calls) == 1
    assert not c.snapshot()['broker_reconciled']
    with pytest.raises(JournalError, match='conflicting duplicate'):
        c.check_paper_account('capture', at=AT+timedelta(minutes=1))
    assert len(calls) == 1


def test_long_capture_window_is_incomplete_even_if_ending_now(c):
    capture = PaperSnapshotCapture(AT-timedelta(minutes=2), AT-timedelta(seconds=10), broker(), (), ())
    assert not c.observe_paper_capture('old-start', capture, at=AT)['broker_reconciled']
    assert 'snapshot_incomplete' in c.snapshot()['reconciliation_reasons']


@pytest.mark.parametrize('change', [
    {'observed_from': AT}, {'observed_to': AT-timedelta(seconds=11)},
])
def test_inconsistent_capture_timestamps_are_rejected(c, change):
    capture = PaperSnapshotCapture(AT-timedelta(seconds=15), AT-timedelta(seconds=10), broker(), (), ())
    with pytest.raises(JournalError, match='clock|disagrees'):
        c.observe_paper_capture('inconsistent', replace(capture, **change), at=AT)


@pytest.mark.parametrize('committed', [False, True])
def test_process_exit_preserves_only_committed_gate_evidence(tmp_path, committed):
    path = tmp_path / 'crash.sqlite'
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
        c.record_capture_failure('blocked', 'capture_unavailable', at=AT)
        before = c.snapshot()
    script = '''
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from orb.coordinator import DurableCoordinator
from orb.reconciliation import BrokerSnapshot
c = DurableCoordinator(Path(PATH), SCOPE, create=False)
at = datetime(2026,10,9,13,46,tzinfo=timezone.utc)
if not COMMITTED:
    c._db.create_function('crash_gate', 0, lambda: os._exit(73))
    c._db.execute("CREATE TEMP TRIGGER crash BEFORE INSERT ON coordinator_events BEGIN SELECT crash_gate(); END")
c.observe_broker_snapshot('matching', BrokerSnapshot(SCOPE, at, Decimal('10000'), (), (), True), at=at)
os._exit(73)
'''
    script = script.replace('PATH', repr(str(path))).replace('SCOPE', repr(SCOPE)).replace('COMMITTED', repr(committed))
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    assert result.returncode == 73, result.stderr
    with DurableCoordinator(path, SCOPE, create=False) as c:
        if committed:
            assert c.snapshot()['broker_reconciled']
            assert c.snapshot()['events'] == before['events'] + 1
        else:
            assert c.snapshot() == before


def test_reconciliation_failure_survives_process_reopen_with_reservations(tmp_path):
    path = tmp_path / 'state.sqlite'
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
        c.admit('spy', signal())
        account = c.snapshot()['account']
        c.observe_broker_snapshot('bad', broker(open_client_order_ids=('duplicate-foreign',)), at=AT)
        before = c.snapshot()
    with DurableCoordinator(path, SCOPE, create=False) as c:
        assert c.snapshot() == before
        assert c.snapshot()['account'] == account
        assert c.admit('qqq', signal('QQQ'))['quantity'] == 0


def test_capture_interruption_leaves_durable_unknown_gate(tmp_path):
    path = tmp_path / 'capture-crash.sqlite'
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
        c.observe_broker_snapshot('green', broker(), at=AT)
    script = '''
import os
from datetime import datetime, timezone
from pathlib import Path
import orb.paper_snapshot as adapter
from orb.coordinator import DurableCoordinator
adapter.capture_paper_snapshot = lambda: os._exit(74)
with DurableCoordinator(Path(PATH), SCOPE, create=False) as c:
    c.check_paper_account('network', at=datetime(2026,10,9,13,46,tzinfo=timezone.utc))
'''.replace('PATH', repr(str(path))).replace('SCOPE', repr(SCOPE))
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    assert result.returncode == 74, result.stderr
    with DurableCoordinator(path, SCOPE, create=False) as c:
        assert not c.snapshot()['broker_reconciled']
        assert 'capture_in_progress' in c.snapshot()['reconciliation_reasons']
        assert c.admit('spy', signal())['quantity'] == 0


def test_malformed_capture_leaves_started_gate_blocked(c, monkeypatch):
    import orb.paper_snapshot as adapter
    c.observe_broker_snapshot('green', broker(), at=AT)
    monkeypatch.setattr(adapter, 'capture_paper_snapshot', lambda: {})
    with pytest.raises(JournalError, match='expected'):
        c.check_paper_account('malformed', at=AT)
    assert 'capture_in_progress' in c.snapshot()['reconciliation_reasons']
    assert not c.snapshot()['broker_reconciled']
