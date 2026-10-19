from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from orb.breakout import BreakoutDetector
from orb.domain import BreakoutSignal
from orb.journal import EntryIntent, IntentJournal, JournalError, recovery_demo
from orb.replay import load_bars, replay


AT = datetime(2026, 10, 9, 13, 46, tzinfo=timezone.utc)
SCOPE = "paper:test-account"
CONTEXT = sha256(b"declared-synthetic-config").hexdigest()


def signal(symbol="SPY", at=AT):
    identity = f"{BreakoutDetector.VERSION}|{symbol}|{at.date()}|1"
    return BreakoutSignal(sha256(identity.encode()).hexdigest()[:32], BreakoutDetector.VERSION,
                          symbol, at - timedelta(minutes=1), at, D("100.20"), D("99"), D("102.60"))


def intent(symbol="SPY", quantity=10, at=AT):
    return EntryIntent.from_signal(signal(symbol, at), quantity, CONTEXT)


@pytest.fixture
def journal(tmp_path):
    with IntentJournal(tmp_path / "state.sqlite", SCOPE) as j:
        yield j


def prepare_claim(j, i=None):
    i = i or intent()
    j.prepare(i, AT)
    return j.claim(i.signal_id, expected_revision=0, at=AT)


def observe(j, *, state="accepted", qty=0, notional="0", revision=1, event="event-1", broker="broker-1", at=AT):
    return j.observe(intent().signal_id, event_id=event, state=state, filled_quantity=qty,
                     filled_notional=D(notional), broker_order_id=broker, expected_revision=revision, at=at)


def test_replay_candidate_can_be_persisted_without_changing_strategy(journal):
    result = replay(load_bars(Path("fixtures/orb_sample.csv")))
    candidate = result.signals[0]
    i = EntryIntent.from_signal(candidate, 3, CONTEXT)
    saved = journal.prepare(i, candidate.observed_at)
    assert saved["state"] == "prepared"
    assert json.loads(saved["payload"])["signal_id"] == candidate.signal_id


def test_prepare_and_exact_retry_survive_restart_without_extra_event(tmp_path):
    path = tmp_path / "state.sqlite"
    i = intent()
    with IntentJournal(path, SCOPE) as j:
        first = j.prepare(i, AT)
        before = j.snapshot()
    with IntentJournal(path, SCOPE, create=False) as j:
        assert j.prepare(i, AT + timedelta(seconds=1)) == first
        assert j.snapshot() == before
    assert first["revision"] == 0 and first["filled_quantity"] == 0
    assert i.client_order_id == "orb-" + i.signal_id


@pytest.mark.parametrize("change", [
    {"quantity": 11}, {"context_id": "f" * 64},
    {"entry_reference": "100.30", "target": "102.90"},
])
def test_session_reservation_rejects_changed_payload_atomically(journal, change):
    journal.prepare(intent(), AT)
    before = journal.snapshot()
    with pytest.raises(JournalError):
        journal.prepare(replace(intent(), **change), AT)
    assert journal.snapshot() == before


def test_claim_is_committed_before_external_send_and_cannot_be_repeated(tmp_path):
    path = tmp_path / "state.sqlite"
    with IntentJournal(path, SCOPE) as j:
        prepare_claim(j)
    with IntentJournal(path, SCOPE) as j:
        assert j.get(intent().signal_id)["state"] == "dispatching"
        with pytest.raises(JournalError, match="not dispatchable"):
            j.claim(intent().signal_id, expected_revision=1, at=AT)
        result = j.recover(AT + timedelta(seconds=1))
        assert result["reconciliation_required"] == [intent().signal_id]
        assert result["intents"][0]["state"] == "unknown"
        before = j.snapshot()
        assert j.recover(AT + timedelta(seconds=2)) == before
        with pytest.raises(JournalError):
            j.claim(intent().signal_id, expected_revision=2, at=AT + timedelta(seconds=2))


def test_recovery_does_not_submit_prepared_or_drop_known_pending(journal):
    journal.prepare(intent(), AT)
    qqq = intent("QQQ")
    journal.prepare(qqq, AT)
    journal.claim(qqq.signal_id, expected_revision=0, at=AT)
    journal.observe(qqq.signal_id, event_id="qqq-ack", state="accepted", filled_quantity=0,
                    filled_notional=D(0), broker_order_id="qqq-broker", expected_revision=1, at=AT)
    before = journal.snapshot()
    assert journal.recover(AT + timedelta(seconds=1)) == before
    assert journal.get(intent().signal_id)["state"] == "prepared"
    assert before["reconciliation_required"] == [qqq.signal_id]


@pytest.mark.parametrize("terminal", ["cancelled", "expired"])
def test_partial_terminal_order_retains_filled_inventory_after_restart(tmp_path, terminal):
    path = tmp_path / "state.sqlite"
    with IntentJournal(path, SCOPE) as j:
        prepare_claim(j)
        observe(j, state="partial", qty=3, notional="300.63")
        observe(j, state=terminal, qty=3, notional="300.63", revision=2, event="terminal")
    with IntentJournal(path, SCOPE) as j:
        result = j.recover(AT + timedelta(seconds=1))
        assert result["filled_shares"] == {intent().signal_id: 3}
        assert result["reconciliation_required"] == []
        assert j.get(intent().signal_id)["filled_notional"] == "300.63"
        assert result["cash_ledger_restored"] is False
        with pytest.raises(JournalError):
            j.claim(intent().signal_id, expected_revision=3, at=AT)


def test_cumulative_fills_are_exact_and_duplicate_old_event_cannot_add_shares(journal):
    prepare_claim(journal)
    observe(journal, state="partial", qty=3, notional="300.00000000000000000003")
    observe(journal, state="filled", qty=10, notional="1001.20000000000000000003", revision=2, event="filled")
    before = journal.snapshot()
    duplicate = observe(journal, state="partial", qty=3, notional="300.00000000000000000003", revision=1)
    assert duplicate["state"] == "filled"
    assert journal.snapshot() == before
    assert before["filled_shares"] == {intent().signal_id: 10}


def test_unknown_submission_can_be_reconciled_but_not_retried(journal):
    prepare_claim(journal)
    journal.recover(AT)
    observe(journal, state="filled", qty=10, notional="1002", revision=2)
    assert journal.get(intent().signal_id)["state"] == "filled"


@pytest.mark.parametrize("state", ["rejected", "cancelled", "expired"])
def test_confirmed_zero_fill_terminal_consumes_attempt(journal, state):
    prepare_claim(journal)
    observe(journal, state=state)
    assert journal.prepare(intent(), AT)["state"] == state
    with pytest.raises(JournalError):
        journal.prepare(intent(quantity=20), AT)


@pytest.mark.parametrize("kwargs", [
    {"state": "filled", "qty": 9, "notional": "900"},
    {"state": "partial", "qty": 10, "notional": "1000"},
    {"state": "partial", "qty": 0},
    {"state": "accepted", "qty": 1, "notional": "100"},
    {"state": "rejected", "qty": 1, "notional": "100"},
    {"state": "cancelled", "qty": 10, "notional": "1000"},
    {"qty": -1}, {"qty": True}, {"notional": "1"}, {"notional": "NaN"},
    {"notional": "Infinity"}, {"notional": "-1"}, {"state": "not_found"},
    {"revision": 0}, {"revision": True}, {"at": AT - timedelta(seconds=1)},
])
def test_invalid_observation_does_not_mutate_state_or_append_audit_event(journal, kwargs):
    prepare_claim(journal)
    before = journal.snapshot()
    with pytest.raises(JournalError):
        observe(journal, **kwargs)
    assert journal.snapshot() == before


@pytest.mark.parametrize("kwargs", [
    {"qty": 2, "notional": "200"}, {"qty": 3, "notional": "299"},
    {"qty": 3, "notional": "301"}, {"qty": 4, "notional": "300"},
    {"qty": 11, "notional": "1100"}, {"broker": "different-order"},
    {"state": "accepted", "qty": 0, "notional": "0"},
])
def test_cumulative_regression_and_identity_changes_fail_closed(journal, kwargs):
    prepare_claim(journal)
    observe(journal, state="partial", qty=3, notional="300")
    before = journal.snapshot()
    args = {"state": "partial", "qty": 3, "notional": "300", "revision": 2, "event": "next", **kwargs}
    with pytest.raises(JournalError):
        observe(journal, **args)
    assert journal.snapshot() == before


def test_conflicting_duplicate_and_terminal_amendment_require_reconciliation(journal):
    prepare_claim(journal)
    observe(journal)
    before = journal.snapshot()
    with pytest.raises(JournalError, match="conflicting duplicate"):
        observe(journal, state="rejected")
    assert journal.snapshot() == before
    observe(journal, state="filled", qty=10, notional="1002", revision=2, event="fill")
    before = journal.snapshot()
    with pytest.raises(JournalError, match="lifecycle"):
        observe(journal, state="cancelled", revision=3, event="late-cancel")
    assert journal.snapshot() == before


def test_broker_identity_collision_rolls_back_update_and_event(journal):
    prepare_claim(journal)
    observe(journal)
    qqq = intent("QQQ")
    prepare_claim(journal, qqq)
    before = journal.snapshot()
    with pytest.raises(sqlite3.IntegrityError):
        journal.observe(qqq.signal_id, event_id="qqq", state="accepted", filled_quantity=0,
                        filled_notional=D(0), broker_order_id="broker-1", expected_revision=1, at=AT)
    assert journal.snapshot() == before


def test_observation_before_claim_and_unknown_intent_fail_without_mutation(journal):
    journal.prepare(intent(), AT)
    before = journal.snapshot()
    with pytest.raises(JournalError, match="lifecycle"):
        observe(journal)
    with pytest.raises(JournalError, match="unknown"):
        journal.claim("missing", expected_revision=0, at=AT)
    assert journal.snapshot() == before


@pytest.mark.parametrize("change", [
    {"symbol": "AAPL"}, {"signal_id": "other"}, {"strategy_version": "different"},
    {"stop": D("100.20")}, {"target": D("102.61")}, {"stop": D("NaN")},
    {"entry_reference": 100.2}, {"bar_start": AT},
    {"observed_at": AT.replace(tzinfo=None)},
    {"observed_at": AT + timedelta(seconds=91)},
    {"observed_at": AT.replace(hour=15)},
    {"bar_start": AT.replace(second=1)},
])
def test_invalid_or_future_informed_signal_cannot_be_reserved(change):
    with pytest.raises(JournalError):
        EntryIntent.from_signal(replace(signal(), **change), 10, CONTEXT)


@pytest.mark.parametrize("quantity,context", [(0, CONTEXT), (True, CONTEXT), (1.5, CONTEXT),
    (2**31, CONTEXT), (10, "missing-evidence"), (10, "F" * 64)])
def test_invalid_quantity_or_context(quantity, context):
    with pytest.raises(JournalError):
        EntryIntent.from_signal(signal(), quantity, context)


def test_direct_intent_constructor_cannot_bypass_validation(journal):
    with pytest.raises(JournalError):
        journal.prepare(replace(intent(), session="2026-10-08"), AT)
    with pytest.raises(JournalError):
        journal.prepare(replace(intent(), quantity=0), AT)
    with pytest.raises(JournalError):
        journal.prepare(intent(), AT - timedelta(seconds=1))
    assert journal.snapshot()["intents"] == []


def test_next_session_gets_new_attempt_without_forgetting_old_order(journal):
    prepare_claim(journal)
    monday = AT + timedelta(days=3)
    newer = intent(at=monday)
    journal.prepare(newer, monday)
    assert newer.signal_id != intent().signal_id
    assert len(journal.snapshot()["intents"]) == 2
    assert journal.snapshot()["reconciliation_required"] == [intent().signal_id]


@pytest.mark.parametrize("scope", ["live:account", "paper:", "paper:has spaces", "paper:\nsecret"])
def test_invalid_scope_rejected_before_creation(tmp_path, scope):
    path = tmp_path / "state.sqlite"
    with pytest.raises(JournalError):
        IntentJournal(path, scope)
    assert not path.exists()


def test_other_account_or_unknown_schema_version_is_not_silently_reset(tmp_path):
    path = tmp_path / "state.sqlite"
    with IntentJournal(path, SCOPE) as j:
        j.prepare(intent(), AT)
    before = path.read_bytes()
    with pytest.raises(JournalError, match="scope"):
        IntentJournal(path, "paper:other")
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        db.execute("UPDATE metadata SET version=999")
    before = path.read_bytes()
    with pytest.raises(JournalError, match="version"):
        IntentJournal(path, SCOPE)
    assert path.read_bytes() == before


def test_foreign_corrupt_and_missing_files_fail_without_replacement(tmp_path):
    path = tmp_path / "foreign.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
    before = path.read_bytes()
    with pytest.raises(JournalError, match="schema"):
        IntentJournal(path, SCOPE)
    assert path.read_bytes() == before
    path.write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        IntentJournal(path, SCOPE)
    assert path.read_bytes() == b"not a database"
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        IntentJournal(missing, SCOPE, create=False)
    assert not missing.exists()


def test_memory_symlink_and_wal_mode_are_not_accepted(tmp_path):
    with pytest.raises(JournalError):
        IntentJournal(Path(":memory:"), SCOPE)
    path = tmp_path / "state.sqlite"
    path.touch()
    link = tmp_path / "link.sqlite"
    link.symlink_to(path)
    with pytest.raises(JournalError):
        IntentJournal(link, SCOPE)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
    with pytest.raises(JournalError, match="rollback-journal"):
        IntentJournal(path, SCOPE)


def test_two_workers_reserve_one_attempt_and_only_one_can_claim(tmp_path):
    path = tmp_path / "state.sqlite"
    with IntentJournal(path, SCOPE):
        pass
    barrier = Barrier(2)
    def worker(_):
        with IntentJournal(path, SCOPE) as j:
            barrier.wait(timeout=5)
            j.prepare(intent(), AT)
            barrier.wait(timeout=5)
            try:
                j.claim(intent().signal_id, expected_revision=0, at=AT)
                return "claimed"
            except JournalError:
                return "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, range(2))) == ["blocked", "claimed"]
    with IntentJournal(path, SCOPE) as j:
        result = j.snapshot()
        assert len(result["intents"]) == 1
        assert len(result["events"]) == 2
        assert result["intents"][0]["revision"] == 1


@pytest.mark.parametrize("committed", [False, True])
def test_abrupt_process_exit_preserves_committed_claim_and_rolls_back_uncommitted_claim(tmp_path, committed):
    path = tmp_path / "state.sqlite"
    with IntentJournal(path, SCOPE) as j:
        j.prepare(intent(), AT)
    code = """
import os, sqlite3, sys
from datetime import datetime
from pathlib import Path
from orb.journal import IntentJournal
path, signal_id, committed = sys.argv[1:]
if committed == 'True':
    journal = IntentJournal(Path(path), 'paper:test-account')
    journal.claim(signal_id, expected_revision=0, at=datetime.fromisoformat('2026-10-09T13:46:00+00:00'))
else:
    db = sqlite3.connect(path, isolation_level=None)
    db.execute('BEGIN IMMEDIATE')
    db.execute("UPDATE intents SET state='dispatching',revision=1")
    db.execute("INSERT INTO events(event_id,signal_id,payload,observed_at) VALUES ('uncommitted',?,'{}','test')", (signal_id,))
os._exit(23)
"""
    process = subprocess.run([sys.executable, "-c", code, str(path), intent().signal_id, str(committed)])
    assert process.returncode == 23
    with IntentJournal(path, SCOPE) as j:
        result = j.recover(AT + timedelta(seconds=1))
        assert result["intents"][0]["state"] == ("unknown" if committed else "prepared")
        assert len(result["events"]) == (3 if committed else 1)


def test_recovery_transaction_rolls_back_every_row_when_one_clock_is_invalid(journal):
    prepare_claim(journal)
    qqq = intent("QQQ")
    journal.prepare(qqq, AT)
    journal.claim(qqq.signal_id, expected_revision=0, at=AT + timedelta(seconds=2))
    before = journal.snapshot()
    with pytest.raises(JournalError, match="backwards"):
        journal.recover(AT + timedelta(seconds=1))
    assert journal.snapshot() == before


@pytest.mark.parametrize("delay", [91, 3 * 86400])
def test_stale_prepared_intent_never_dispatches_after_restart(tmp_path, delay):
    path = tmp_path / "state.sqlite"
    with IntentJournal(path, SCOPE) as j:
        j.prepare(intent(), AT)
    with IntentJournal(path, SCOPE) as j:
        before = j.snapshot()
        with pytest.raises(JournalError, match="stale"):
            j.claim(intent().signal_id, expected_revision=0, at=AT + timedelta(seconds=delay))
        assert j.snapshot() == before


def test_submission_at_cutoff_is_refused_even_when_fresh(journal):
    cutoff = AT.replace(hour=15, minute=0)
    late = intent(at=cutoff - timedelta(minutes=1))
    journal.prepare(late, cutoff - timedelta(minutes=1))
    before = journal.snapshot()
    with pytest.raises(JournalError, match="cutoff"):
        journal.claim(late.signal_id, expected_revision=0, at=cutoff)
    assert journal.snapshot() == before


def test_event_write_failure_rolls_back_claim_before_caller_can_send(journal):
    journal.prepare(intent(), AT)
    before = journal.snapshot()
    journal._db.execute("""CREATE TEMP TRIGGER fail_event BEFORE INSERT ON events
                         BEGIN SELECT RAISE(ABORT, 'injected event write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        journal.claim(intent().signal_id, expected_revision=0, at=AT)
    assert journal.snapshot() == before


def test_broker_event_id_cannot_be_reused_for_other_symbol(journal):
    prepare_claim(journal)
    observe(journal)
    qqq = intent("QQQ")
    prepare_claim(journal, qqq)
    before = journal.snapshot()
    with pytest.raises(JournalError, match="conflicting duplicate"):
        journal.observe(qqq.signal_id, event_id="event-1", state="accepted", filled_quantity=0,
                        filled_notional=D(0), broker_order_id="broker-2", expected_revision=1, at=AT)
    assert journal.snapshot() == before


def test_demo_cli_reopens_without_mutation_and_refuses_overwrite_or_missing_inspect(tmp_path):
    path = tmp_path / "demo.sqlite"
    command = [sys.executable, "-m", "orb"]
    demo = subprocess.run(command + ["recovery-demo", "--db", str(path)], capture_output=True, text=True, check=True)
    result = json.loads(demo.stdout)
    assert result["synthetic"] and not result["orders_submitted"]
    assert len(result["reconciliation_required"]) == 1
    assert list(result["filled_shares"].values()) == [2]
    before = path.read_bytes()
    inspected = subprocess.run(command + ["journal-inspect", "--db", str(path), "--account-scope", "paper:synthetic-demo"],
                               capture_output=True, text=True, check=True)
    assert json.loads(inspected.stdout) == {k: v for k, v in result.items() if k not in ("synthetic", "orders_submitted")}
    assert path.read_bytes() == before
    failed = subprocess.run(command + ["recovery-demo", "--db", str(path)], capture_output=True, text=True)
    assert failed.returncode == 2 and path.read_bytes() == before
    missing = tmp_path / "missing.sqlite"
    failed = subprocess.run(command + ["journal-inspect", "--db", str(missing), "--account-scope", SCOPE], capture_output=True)
    assert failed.returncode == 2 and not missing.exists()
