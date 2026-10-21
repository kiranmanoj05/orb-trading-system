from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D, localcontext
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

import orb.coordinator as module
from orb.coordinator import CoordinatorConfig, DurableCoordinator
from orb.domain import BreakoutSignal
from orb.journal import EntryIntent, IntentJournal, JournalError
from orb.risk import MarketMark, RiskLimits


DAY = date(2026, 10, 9)
AT = datetime(2026, 10, 9, 13, 46, tzinfo=timezone.utc)
SCOPE = "paper:coordinator-test"


def signal(symbol="SPY", at=AT):
    identity = f"orb-long-v1|{symbol}|{at.date()}|1"
    return BreakoutSignal(sha256(identity.encode()).hexdigest()[:32], "orb-long-v1", symbol,
                          at - timedelta(minutes=1), at, D("100.20"), D("99"), D("102.60"))


def marks(at=AT, price="100.20", symbol="SPY"):
    return {symbol: MarketMark(D(price), at)}


def ready(c):
    c.start_session("session", DAY)
    c.tick("first-minute", AT)


@pytest.fixture
def coordinator(tmp_path):
    with DurableCoordinator(tmp_path / "coordinator.sqlite", SCOPE) as c:
        ready(c)
        yield c


def test_accepted_candidate_and_shared_reservations_survive_restart(tmp_path):
    path = tmp_path / "state.sqlite"
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
        a = c.admit("spy", signal())
        b = c.admit("qqq", signal("QQQ"))
        before = c.snapshot()
        assert a["quantity"] == b["quantity"] == 24
        assert D(before["account"]["available_cash"]) == D("5190.40")
        assert D(before["account"]["committed_stop_risk"]) == D("57.60")
        assert len(c.journal.snapshot()["intents"]) == 2
    with DurableCoordinator(path, SCOPE, create=False) as c:
        assert c.snapshot() == before
        assert c.admit("spy", signal()) == a
        assert c.snapshot() == before
        assert c.admit("same-candidate-new-request", signal()) == a
        assert len(c.snapshot()["account"]["allocations"]) == 2
        assert len(c.journal.snapshot()["intents"]) == 2


def test_rejected_risk_candidate_remains_consumed_after_restart(tmp_path):
    path = tmp_path / "state.sqlite"
    config = CoordinatorConfig(initial_cash=D("1"))
    with DurableCoordinator(path, SCOPE, config) as c:
        ready(c)
        result = c.admit("rejected", signal())
        assert result["quantity"] == 0
        assert result["reason"].startswith("below_one_share:")
        before = c.snapshot()
    with DurableCoordinator(path, SCOPE, config) as c:
        assert c.admit("rejected", signal()) == result
        assert c.snapshot() == before
        changed = replace(signal(), entry_reference=D("100.30"), target=D("102.90"))
        with pytest.raises(JournalError, match="already consumed"):
            c.admit("changed-candidate", changed)
        assert c.snapshot() == before


def test_cost_aware_full_fill_exit_and_loss_latch_survive_recovery(tmp_path):
    path = tmp_path / "state.sqlite"
    config = CoordinatorConfig(daily_loss_fraction=D("0.001"), fee_per_share=D("0.01"))
    with DurableCoordinator(path, SCOPE, config) as c:
        ready(c)
        result = c.admit("spy", signal())
        qty = result["quantity"]
        c.account_event("fill", "fill", signal().signal_id, AT, price=D("100.20"))
        c.tick("down", AT + timedelta(minutes=1), marks(AT + timedelta(minutes=1), "99"))
        c.account_event("exit", "close", signal().signal_id, AT + timedelta(minutes=1), price=D("99"))
        before = c.snapshot()
        assert before["daily_loss"]["reason"] == "daily_loss_limit"
        assert D(before["account"]["cash"]) == D("10000") - qty * D("1.22")
        assert before["account"]["committed_stop_risk"] == "0"
    with DurableCoordinator(path, SCOPE, config) as c:
        assert c.snapshot() == before
        c.recover(AT + timedelta(minutes=1))
        assert c.snapshot() == before
        rejected = c.admit("qqq-rejected", signal("QQQ", AT + timedelta(minutes=1)))
        assert rejected["reason"] == "daily_loss_limit" and rejected["quantity"] == 0
        assert not c.snapshot()["paper_execution_enabled"]
        assert not c.snapshot()["broker_reconciled"]


def test_partial_external_evidence_gates_admissions_and_retains_all_reservations(tmp_path):
    path = tmp_path / "state.sqlite"
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
        c.admit("spy", signal())
        initial = c.snapshot()["account"]
        c.journal.claim(signal().signal_id, expected_revision=0, at=AT)
        c.journal.observe(signal().signal_id, event_id="partial", state="partial", filled_quantity=2,
            filled_notional=D("200.40"), broker_order_id="external-paper-order", expected_revision=1, at=AT)
    with DurableCoordinator(path, SCOPE) as c:
        assert c.snapshot()["reconciliation_required"]
        assert c.snapshot()["account"] == initial
        result = c.admit("qqq", signal("QQQ"))
        assert result["reason"] == "reconciliation_required"
        for op in ("fill", "cancel", "close", "unresolved"):
            with pytest.raises(JournalError, match="requires reconciliation"):
                c.account_event("blocked-" + op, op, signal().signal_id, AT,
                                price=D("100") if op in ("fill", "close") else None)
        assert c.snapshot()["account"] == initial


def test_interrupted_external_claim_recovers_unknown_without_freeing_cash(coordinator):
    c = coordinator
    c.admit("spy", signal())
    before = c.snapshot()["account"]
    c.journal.claim(signal().signal_id, expected_revision=0, at=AT)
    result = c.recover(AT)
    assert result["reconciliation_required"]
    assert result["account"] == before
    assert c.journal.get(signal().signal_id)["state"] == "unknown"


def test_local_withdrawal_releases_reservation_but_not_session_attempt(coordinator):
    c = coordinator
    original = c.admit("spy", signal())
    c.account_event("withdraw", "cancel", signal().signal_id, AT)
    assert c.snapshot()["account"]["available_cash"] == "10000"
    assert c.journal.get(signal().signal_id)["state"] == "withdrawn"
    assert c.admit("same-attempt", signal()) == original
    assert c.snapshot()["account"]["allocations"][0]["state"] == "cancelled"
    assert c.snapshot()["account"]["available_cash"] == "10000"


def test_audit_write_failure_rolls_back_admission_reservation_and_intent(coordinator):
    c = coordinator
    before, journal_before = c.snapshot(), c.journal.snapshot()
    c._db.execute("""CREATE TEMP TRIGGER fail_command BEFORE INSERT ON coordinator_events
                   BEGIN SELECT RAISE(ABORT, 'injected command failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        c.admit("spy", signal())
    assert c.snapshot() == before
    assert c.journal.snapshot() == journal_before


def test_audit_write_failure_rolls_back_fill_cash_and_journal_events(coordinator):
    c = coordinator
    c.admit("spy", signal())
    before, journal_before = c.snapshot(), c.journal.snapshot()
    c._db.execute("""CREATE TEMP TRIGGER fail_fill BEFORE INSERT ON coordinator_events
                   BEGIN SELECT RAISE(ABORT, 'injected fill failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        c.account_event("fill", "fill", signal().signal_id, AT, price=D("100.20"))
    assert c.snapshot() == before
    assert c.journal.snapshot() == journal_before


@pytest.mark.parametrize("kind", ["missing", "stale", "gap"])
def test_held_equity_uncertainty_latches_across_restart(tmp_path, kind):
    path = tmp_path / "state.sqlite"
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
        c.admit("spy", signal())
        c.account_event("fill", "fill", signal().signal_id, AT, price=D("100.20"))
        later = AT + timedelta(minutes=2 if kind == "gap" else 1)
        evidence = {} if kind == "missing" else marks(AT if kind == "stale" else later)
        c.tick("unknown", later, evidence)
        assert c.snapshot()["daily_loss"]["reason"] == "daily_loss_equity_unavailable"
    with DurableCoordinator(path, SCOPE) as c:
        at = later + timedelta(minutes=1)
        c.tick("recovery", at, marks(at, "110"))
        assert c.snapshot()["daily_loss"]["reason"] == "daily_loss_equity_unavailable"
        assert c.admit("qqq", signal("QQQ", at))["quantity"] == 0


def test_next_session_resets_loss_reference_but_never_drops_carried_inventory(tmp_path):
    path = tmp_path / "state.sqlite"
    with DurableCoordinator(path, SCOPE, CoordinatorConfig(daily_loss_fraction=D("0.001"))) as c:
        ready(c)
        c.admit("spy", signal())
        c.account_event("fill", "fill", signal().signal_id, AT, price=D("100.20"))
        c.tick("loss", AT + timedelta(minutes=1), marks(AT + timedelta(minutes=1), "99"))
        assert c.snapshot()["daily_loss"]["blocked"]
        c.start_session("monday", date(2026, 10, 12))
        result = c.snapshot()
        assert result["daily_loss"]["reason"] == "daily_loss_baseline_unavailable"
        assert result["account"]["allocations"][0]["state"] == "open"
        assert D(result["account"]["committed_stop_risk"]) > 0


def test_flat_next_session_starts_from_then_current_cash(coordinator):
    c = coordinator
    c.admit("spy", signal())
    c.account_event("fill", "fill", signal().signal_id, AT, price=D("100.20"))
    c.account_event("exit", "close", signal().signal_id, AT, price=D("99"))
    cash = c.snapshot()["account"]["cash"]
    c.start_session("monday", date(2026, 10, 12))
    assert c.snapshot()["daily_loss"]["reference_equity"] == cash
    assert not c.snapshot()["daily_loss"]["blocked"]


@pytest.mark.parametrize("change", [{"initial_cash": D("9999")}, {"fee_per_share": D("0.01")},
    {"daily_loss_fraction": D("0.01")}, {"limits": RiskLimits(max_total_risk_fraction=D("0.02"))}])
def test_changed_configuration_never_reinterprets_persisted_history(tmp_path, change):
    path = tmp_path / "state.sqlite"
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
    before = path.read_bytes()
    with pytest.raises(JournalError, match="configuration"):
        DurableCoordinator(path, SCOPE, replace(CoordinatorConfig(), **change))
    assert path.read_bytes() == before


def test_changed_executable_identity_requires_explicit_migration(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite"
    with DurableCoordinator(path, SCOPE):
        pass
    monkeypatch.setattr(module, "_model_id", lambda: "changed")
    with pytest.raises(JournalError, match="model"):
        DurableCoordinator(path, SCOPE)


def test_existing_nonempty_v1_journal_is_not_adopted_or_overwritten(tmp_path):
    path = tmp_path / "state.sqlite"
    with IntentJournal(path, SCOPE) as j:
        j.prepare(EntryIntent.from_signal(signal(), 10, "f" * 64), AT)
    before = path.read_bytes()
    with pytest.raises(JournalError, match="cannot adopt"):
        DurableCoordinator(path, SCOPE)
    assert path.read_bytes() == before
    with IntentJournal(path, SCOPE) as j:
        assert j.snapshot()["schema_version"] == 1


def test_schema2_journal_can_be_inspected_without_coordinator(tmp_path):
    path = tmp_path / "state.sqlite"
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
        c.admit("spy", signal())
    with IntentJournal(path, SCOPE) as j:
        assert j.snapshot()["schema_version"] == 2
        assert len(j.snapshot()["intents"]) == 1


def test_conflicting_duplicate_request_rolls_back_but_exact_retry_returns_old_result(coordinator):
    c = coordinator
    first = c.admit("spy", signal())
    before = c.snapshot()
    assert c.admit("spy", signal()) == first
    with pytest.raises(JournalError, match="conflicting duplicate"):
        c.admit("spy", signal("QQQ"))
    assert c.snapshot() == before


@pytest.mark.parametrize("operation", ["cancel", "fill", "close", "unresolved"])
def test_unknown_order_never_mutates_coordinator(operation, coordinator):
    c = coordinator
    before = c.snapshot()
    with pytest.raises(ValueError):
        c.account_event("unknown", operation, "missing", AT, price=D(100) if operation in ("fill", "close") else None)
    assert c.snapshot() == before


@pytest.mark.parametrize("case", ["future-mark", "backwards-tick", "same-tick", "unclocked-candidate", "weekend"])
def test_invalid_clock_and_future_evidence_leave_no_event(coordinator, case):
    c = coordinator
    before = c.snapshot()
    with pytest.raises(ValueError):
        if case == "future-mark":
            c.tick("bad", AT + timedelta(minutes=1), marks(AT + timedelta(minutes=2)))
        elif case == "backwards-tick":
            c.tick("bad", AT - timedelta(minutes=1))
        elif case == "same-tick":
            c.tick("bad", AT)
        elif case == "weekend":
            c.start_session("bad", date(2026, 10, 10))
        else:
            c.admit("bad", signal("QQQ", AT + timedelta(minutes=1)))
    assert c.snapshot() == before


def test_replay_outcome_corruption_fails_closed(tmp_path):
    path = tmp_path / "state.sqlite"
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
        c.admit("spy", signal())
    with sqlite3.connect(path) as db:
        db.execute("UPDATE coordinator_events SET outcome='{}' WHERE request_id='spy'")
    with pytest.raises(JournalError, match="disagrees"):
        DurableCoordinator(path, SCOPE)


def test_competing_candidates_cannot_overspend_or_duplicate_attempts(tmp_path):
    path = tmp_path / "state.sqlite"
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
    barrier = Barrier(2)
    def worker(symbol):
        with DurableCoordinator(path, SCOPE) as c:
            barrier.wait(timeout=5)
            return c.admit(symbol.lower(), signal(symbol))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ("SPY", "QQQ")))
    assert all(r["quantity"] == 24 for r in results)
    with DurableCoordinator(path, SCOPE) as c:
        assert len(c.snapshot()["attempts"]) == 2
        assert D(c.snapshot()["account"]["available_cash"]) == D("5190.40")


def test_reconstruction_is_independent_of_callers_decimal_context(tmp_path):
    path = tmp_path / "state.sqlite"
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
        c.admit("spy", signal())
        before = c.snapshot()
    with localcontext() as context:
        context.prec = 5
        with DurableCoordinator(path, SCOPE) as c:
            assert c.snapshot() == before


@pytest.mark.parametrize("kwargs", [{"limits": None}, {"initial_cash": D(0)},
    {"daily_loss_fraction": D("NaN")}, {"fee_per_share": D("-0.01")}, {"slippage_bps": D("10000")}])
def test_invalid_coordinator_configuration_is_rejected_before_creating_state(kwargs):
    with pytest.raises(ValueError):
        CoordinatorConfig(**kwargs)


@pytest.mark.parametrize("commit", [False, True])
def test_abrupt_exit_during_or_after_admission_is_atomic(tmp_path, commit):
    path = tmp_path / "state.sqlite"
    with DurableCoordinator(path, SCOPE) as c:
        ready(c)
    code = """
import os, sys
from pathlib import Path
from datetime import datetime
from decimal import Decimal as D
from hashlib import sha256
from orb.coordinator import DurableCoordinator
from orb.domain import BreakoutSignal
from datetime import timedelta
at = datetime.fromisoformat('2026-10-09T13:46:00+00:00')
signal = BreakoutSignal(sha256(b'orb-long-v1|SPY|2026-10-09|1').hexdigest()[:32],
    'orb-long-v1','SPY',at-timedelta(minutes=1),at,D('100.20'),D('99'),D('102.60'))
c = DurableCoordinator(Path(sys.argv[1]),'paper:coordinator-test')
if sys.argv[2] == 'False':
    old = c._effects
    def stop_after_effects(state, command, outcome):
        old(state, command, outcome)
        os._exit(23)
    c._effects = stop_after_effects
c.admit('child',signal)
os._exit(23)
"""
    assert subprocess.run([sys.executable, "-c", code, str(path), str(commit)]).returncode == 23
    with DurableCoordinator(path, SCOPE) as c:
        assert len(c.snapshot()["attempts"]) == len(c.journal.snapshot()["intents"]) == int(commit)
        if commit:
            assert c.snapshot()["account"]["allocations"][0]["quantity"] == 24
        else:
            assert c.snapshot()["account"]["allocations"] == []


def test_aggregate_risk_capacity_is_not_released_by_restart(tmp_path):
    path = tmp_path / "state.sqlite"
    config = CoordinatorConfig(limits=RiskLimits(max_total_risk_fraction=D("0.004")))
    with DurableCoordinator(path, SCOPE, config) as c:
        ready(c)
        assert c.admit("spy", signal())["quantity"] == 24
    with DurableCoordinator(path, SCOPE, config) as c:
        assert c.admit("qqq", signal("QQQ"))["quantity"] == 9
        assert D(c.snapshot()["account"]["committed_stop_risk"]) == D("39.60")


def test_rejected_attempt_does_not_reopen_after_other_reservation_is_withdrawn(tmp_path):
    path = tmp_path / "state.sqlite"
    config = CoordinatorConfig(limits=RiskLimits(max_gross_fraction=D("0.25")))
    with DurableCoordinator(path, SCOPE, config) as c:
        ready(c)
        c.admit("spy", signal())
        result = c.admit("qqq", signal("QQQ"))
        assert result["quantity"] == 0
        c.account_event("cancel-spy", "cancel", signal().signal_id, AT)
        assert c.snapshot()["account"]["available_cash"] == "10000"
    with DurableCoordinator(path, SCOPE, config) as c:
        assert c.admit("qqq-again", signal("QQQ")) == result
        assert len(c.snapshot()["account"]["allocations"]) == 1


def test_entry_fees_can_latch_loss_before_simultaneous_second_candidate(tmp_path):
    path = tmp_path / "state.sqlite"
    config = CoordinatorConfig(daily_loss_fraction=D("0.000001"), fee_per_share=D("0.01"))
    with DurableCoordinator(path, SCOPE, config) as c:
        ready(c)
        c.admit("spy", signal())
        c.account_event("fill", "fill", signal().signal_id, AT, price=D("100.20"))
        assert c.admit("qqq", signal("QQQ"))["reason"] == "daily_loss_limit"


def test_stale_candidate_is_consumed_without_a_reservation(coordinator):
    c = coordinator
    later = AT + timedelta(minutes=2)
    c.tick("later", later)
    result = c.admit("stale", signal(), at=later)
    assert result["reason"] == "stale_candidate"
    assert c.journal.snapshot()["intents"] == []
    assert len(c.snapshot()["attempts"]) == 1


def test_worse_full_fill_cannot_exceed_another_order_reservation(coordinator):
    c = coordinator
    c.admit("spy", signal())
    c.admit("qqq", signal("QQQ"))
    before, journal_before = c.snapshot(), c.journal.snapshot()
    with pytest.raises(ValueError, match="exceeds reservation"):
        c.account_event("worse-fill", "fill", signal().signal_id, AT, price=D("101"))
    assert c.snapshot() == before
    assert c.journal.snapshot() == journal_before


def test_journal_effect_failure_cannot_commit_an_account_command(coordinator):
    c = coordinator
    before = c.snapshot()
    c._db.execute("""CREATE TEMP TRIGGER fail_intent BEFORE INSERT ON intents
                   BEGIN SELECT RAISE(ABORT, 'injected intent failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        c.admit("spy", signal())
    assert c.snapshot() == before
    assert c.journal.snapshot()["intents"] == []


def test_recovery_before_account_clock_is_rejected_without_mutation(coordinator):
    c = coordinator
    before = c.snapshot()
    with pytest.raises(JournalError, match="cannot precede"):
        c.recover(AT - timedelta(minutes=1))
    assert c.snapshot() == before


def test_new_cli_demo_reconstructs_exactly_and_refuses_overwrite_or_missing_inspect(tmp_path):
    path = tmp_path / "demo.sqlite"
    command = [sys.executable, "-m", "orb"]
    demo = subprocess.run(command + ["coordinator-demo", "--db", str(path)], capture_output=True, text=True, check=True)
    result = json.loads(demo.stdout)
    assert result["synthetic"] and not result["orders_submitted"] and result["restored_identically"]
    state = result["state"]
    assert state["daily_loss"]["reason"] == "daily_loss_limit"
    assert state["attempts"][0]["outcome"]["quantity"] == 0  # QQQ sorts first.
    assert D(state["account"]["cash"]) == D("9970.72")
    before = path.read_bytes()
    inspect = subprocess.run(command + ["coordinator-inspect", "--db", str(path), "--account-scope",
        "paper:synthetic-coordinator", "--daily-loss-fraction", "0.001", "--fee-per-share", "0.01"],
        capture_output=True, text=True, check=True)
    assert json.loads(inspect.stdout) == state
    assert path.read_bytes() == before
    failed = subprocess.run(command + ["coordinator-demo", "--db", str(path)], capture_output=True)
    assert failed.returncode == 2 and path.read_bytes() == before
    missing = tmp_path / "missing.sqlite"
    failed = subprocess.run(command + ["coordinator-inspect", "--db", str(missing), "--account-scope", SCOPE], capture_output=True)
    assert failed.returncode == 2 and not missing.exists()
