from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, localcontext
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from orb.breakout import BreakoutDetector
from orb.domain import BreakoutSignal
from orb.journal import EntryIntent
from orb.reconciliation import (BrokerPosition, BrokerSnapshot, ReconciliationError,
                                ReconciliationLedger, reconciliation_demo)


AT = datetime(2026, 10, 9, 13, 46, tzinfo=timezone.utc)
SCOPE = "paper:test-reconciliation"


def intent(symbol="SPY", quantity=4):
    identity = f"{BreakoutDetector.VERSION}|{symbol}|2026-10-09|1"
    signal = BreakoutSignal(sha256(identity.encode()).hexdigest()[:32], BreakoutDetector.VERSION,
                            symbol, AT - timedelta(minutes=1), AT,
                            D("100.20"), D("99"), D("102.60"))
    return EntryIntent.from_signal(signal, quantity, sha256(b"test-context").hexdigest())


@pytest.fixture
def ledger(tmp_path):
    with ReconciliationLedger(tmp_path / "ledger.sqlite", SCOPE, D("10000")) as value:
        yield value


def register(ledger, item=None, at=AT):
    item = item or intent()
    ledger.register("register-" + item.symbol, item, reserved_cash=D("400.84"),
                    planned_stop_risk=D("4.88"), at=at)
    return item


def partial_entry(ledger, item=None):
    item = item or register(ledger)
    ledger.observe_entry("entry-partial", item.signal_id, state="partial", filled_quantity=2,
                         filled_notional=D("200.40"), fees=D("0.02"),
                         broker_order_id="entry-broker", at=AT)
    return item


def cancel_entry(ledger, item=None):
    item = item or partial_entry(ledger)
    ledger.observe_entry("entry-cancel", item.signal_id, state="cancelled", filled_quantity=2,
                         filled_notional=D("200.40"), fees=D("0.02"),
                         broker_order_id="entry-broker", at=AT)
    return item


def test_partial_entry_spends_delta_and_retains_proportional_reservation(ledger):
    item = partial_entry(ledger)
    snap = ledger.snapshot()
    assert snap["cash"] == "9799.58"
    assert snap["reserved_cash"] == "200.42"
    assert snap["available_cash"] == "9599.16"
    assert snap["positions"] == {"SPY": 2}
    assert snap["open_client_order_ids"] == [item.client_order_id]
    assert snap["committed_stop_risk"] == "4.88"


def test_partial_cancel_releases_unfilled_reservation_but_keeps_position(ledger):
    cancel_entry(ledger)
    snap = ledger.snapshot()
    assert snap["cash"] == snap["available_cash"] == "9799.58"
    assert snap["reserved_cash"] == "0"
    assert snap["positions"] == {"SPY": 2}
    assert snap["gross_exposure"] == "200.40"
    assert snap["committed_stop_risk"] == "2.44"


def test_partial_exit_credits_net_proceeds_and_realizes_allocated_basis(ledger):
    item = cancel_entry(ledger)
    ledger.open_exit("exit-open", item.signal_id, client_order_id="exit-1", quantity=2, at=AT)
    ledger.observe_exit("exit-fill", item.signal_id, client_order_id="exit-1", state="partial",
                        filled_quantity=1, filled_notional=D("101"), fees=D("0.01"),
                        broker_order_id="exit-broker", at=AT)
    snap = ledger.snapshot()
    assert snap["cash"] == "9900.57"
    assert snap["positions"] == {"SPY": 1}
    assert snap["gross_exposure"] == "100.20"
    assert snap["committed_stop_risk"] == "1.22"
    assert snap["realized_pnl"] == "0.78"
    assert snap["open_client_order_ids"] == ["exit-1"]


def test_cancelled_partial_exit_can_be_replaced(ledger):
    item = cancel_entry(ledger)
    ledger.open_exit("exit-open", item.signal_id, client_order_id="exit-1", quantity=2, at=AT)
    ledger.observe_exit("exit-cancel", item.signal_id, client_order_id="exit-1", state="cancelled",
                        filled_quantity=1, filled_notional=D("101"), fees=D("0.01"),
                        broker_order_id="exit-broker", at=AT)
    ledger.open_exit("exit-open-2", item.signal_id, client_order_id="exit-2", quantity=1, at=AT)
    ledger.observe_exit("exit-fill-2", item.signal_id, client_order_id="exit-2", state="filled",
                        filled_quantity=1, filled_notional=D("102"), fees=D("0.01"),
                        broker_order_id="exit-broker-2", at=AT)
    snap = ledger.snapshot()
    assert snap["positions"] == {}
    assert D(snap["gross_exposure"]) == D(snap["committed_stop_risk"]) == 0
    assert snap["cash"] == "10002.56"
    assert snap["realized_pnl"] == "2.56"


def test_complete_matching_snapshot_clears_gate_but_never_enables_execution(ledger):
    item = cancel_entry(ledger)
    result = ledger.reconcile("reconcile", BrokerSnapshot(SCOPE, AT, D("9799.58"),
        (BrokerPosition("SPY", 2),), (), True))
    assert result == {"broker_reconciled": True, "paper_execution_enabled": False,
                      "reasons": [], "snapshot_as_of": AT.isoformat()}
    snap = ledger.snapshot()
    assert snap["broker_reconciled"] is True
    assert snap["paper_execution_enabled"] is snap["orders_submitted"] is False
    assert snap["records"][0]["signal_id"] == item.signal_id


@pytest.mark.parametrize(("snapshot", "reason"), [
    (BrokerSnapshot(SCOPE, AT, D("9799.57"), (BrokerPosition("SPY", 2),), (), True), "cash_mismatch"),
    (BrokerSnapshot(SCOPE, AT, D("9799.58"), (BrokerPosition("SPY", 1),), (), True), "position_mismatch"),
    (BrokerSnapshot(SCOPE, AT, D("9799.58"), (BrokerPosition("SPY", 2),), ("foreign",), True), "open_order_mismatch"),
    (BrokerSnapshot(SCOPE, AT, D("9799.58"), (BrokerPosition("SPY", 2),), (), False), "snapshot_incomplete"),
    (BrokerSnapshot("paper:other", AT, D("9799.58"), (BrokerPosition("SPY", 2),), (), True), "account_scope_mismatch"),
])
def test_snapshot_mismatches_remain_explicitly_gated(ledger, snapshot, reason):
    cancel_entry(ledger)
    result = ledger.reconcile("reconcile", snapshot)
    assert result["broker_reconciled"] is False
    assert reason in result["reasons"]
    assert ledger.snapshot()["broker_reconciled"] is False


def test_active_entry_requires_matching_client_order_identity(ledger):
    item = partial_entry(ledger)
    exact = BrokerSnapshot(SCOPE, AT, D("9799.58"), (BrokerPosition("SPY", 2),),
                           (item.client_order_id,), True)
    assert ledger.reconcile("exact", exact)["broker_reconciled"] is True


def test_unknown_lifecycle_never_inferred_from_missing_open_order(ledger):
    item = register(ledger)
    ledger.observe_entry("unknown", item.signal_id, state="unknown", filled_quantity=0,
                         filled_notional=D("0"), fees=D("0"), broker_order_id="uncertain", at=AT)
    result = ledger.reconcile("snapshot", BrokerSnapshot(SCOPE, AT, D("10000"), (), (), True))
    assert result["reasons"] == ["unknown_lifecycle"]


def test_later_command_invalidates_a_previous_matching_snapshot(ledger):
    item = cancel_entry(ledger)
    ledger.reconcile("snapshot", BrokerSnapshot(SCOPE, AT, D("9799.58"),
                                                (BrokerPosition("SPY", 2),), (), True))
    ledger.open_exit("exit", item.signal_id, client_order_id="exit-1", quantity=2, at=AT)
    assert ledger.snapshot()["broker_reconciled"] is False
    assert ledger.snapshot()["last_reconciliation"] is None


def test_restart_replays_partial_entry_and_exit(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with ReconciliationLedger(path, SCOPE, D("10000")) as first:
        item = cancel_entry(first)
        first.open_exit("exit", item.signal_id, client_order_id="exit-1", quantity=2, at=AT)
        first.observe_exit("exit-partial", item.signal_id, client_order_id="exit-1", state="partial",
                           filled_quantity=1, filled_notional=D("101"), fees=D("0.01"),
                           broker_order_id="exit-broker", at=AT)
        expected = first.snapshot()
    with ReconciliationLedger(path, SCOPE, D("10000"), create=False) as second:
        assert second.snapshot() == expected


def test_exact_request_retry_returns_saved_outcome_after_newer_events(ledger):
    item = register(ledger)
    first = ledger.observe_entry("accepted", item.signal_id, state="accepted", filled_quantity=0,
                                 filled_notional=D("0"), fees=D("0"), broker_order_id="entry", at=AT)
    ledger.observe_entry("partial", item.signal_id, state="partial", filled_quantity=1,
                         filled_notional=D("100.20"), fees=D("0.01"), broker_order_id="entry", at=AT)
    again = ledger.observe_entry("accepted", item.signal_id, state="accepted", filled_quantity=0,
                                 filled_notional=D("0"), fees=D("0"), broker_order_id="entry", at=AT)
    assert again == first
    assert ledger.snapshot()["positions"] == {"SPY": 1}


def test_conflicting_request_retry_fails_closed(ledger):
    item = register(ledger)
    ledger.observe_entry("event", item.signal_id, state="accepted", filled_quantity=0,
                         filled_notional=D("0"), fees=D("0"), broker_order_id="entry", at=AT)
    with pytest.raises(ReconciliationError, match="request identity"):
        ledger.observe_entry("event", item.signal_id, state="partial", filled_quantity=1,
                             filled_notional=D("100.20"), fees=D("0.01"), broker_order_id="entry", at=AT)


@pytest.mark.parametrize("changes", [
    {"filled_quantity": -1}, {"filled_quantity": 5}, {"filled_quantity": 0, "filled_notional": D("1")},
    {"filled_quantity": 1, "filled_notional": D("0")}, {"fees": D("-0.01")},
])
def test_invalid_cumulative_entry_evidence_rolls_back(ledger, changes):
    item = register(ledger)
    kwargs = {"state": "partial", "filled_quantity": 1, "filled_notional": D("100.20"),
              "fees": D("0.01"), "broker_order_id": "entry", "at": AT}
    kwargs.update(changes)
    before = ledger.snapshot()
    with pytest.raises(ReconciliationError):
        ledger.observe_entry("bad", item.signal_id, **kwargs)
    assert ledger.snapshot() == before


@pytest.mark.parametrize(("state", "quantity"), [
    ("accepted", 1), ("partial", 0), ("partial", 4), ("filled", 3),
    ("rejected", 1), ("cancelled", 4), ("expired", 4), ("made-up", 0),
])
def test_entry_state_must_agree_with_cumulative_quantity(ledger, state, quantity):
    item = register(ledger)
    notional = D("0") if quantity == 0 else D("100") * quantity
    with pytest.raises(ReconciliationError):
        ledger.observe_entry("bad", item.signal_id, state=state, filled_quantity=quantity,
                             filled_notional=notional, fees=D("0"), broker_order_id="entry", at=AT)


def test_cumulative_regression_and_identity_change_fail(ledger):
    item = partial_entry(ledger)
    for request, q, notional, broker in (("q", 1, D("100.20"), "entry-broker"),
                                         ("n", 2, D("200"), "entry-broker"),
                                         ("id", 3, D("300.60"), "other")):
        with pytest.raises(ReconciliationError):
            ledger.observe_entry(request, item.signal_id, state="partial", filled_quantity=q,
                                 filled_notional=notional, fees=D("0.02"), broker_order_id=broker, at=AT)


def test_exit_requires_terminal_filled_entry_and_open_quantity(ledger):
    item = partial_entry(ledger)
    with pytest.raises(ReconciliationError, match="terminal entry"):
        ledger.open_exit("exit", item.signal_id, client_order_id="exit-1", quantity=1, at=AT)
    cancel_entry(ledger, item)
    with pytest.raises(ReconciliationError, match="exceeds"):
        ledger.open_exit("exit-too-large", item.signal_id, client_order_id="exit-2", quantity=3, at=AT)


def test_second_active_exit_and_reused_exit_id_fail(ledger):
    item = cancel_entry(ledger)
    ledger.open_exit("one", item.signal_id, client_order_id="exit-1", quantity=1, at=AT)
    with pytest.raises(ReconciliationError, match="already active"):
        ledger.open_exit("two", item.signal_id, client_order_id="exit-2", quantity=1, at=AT)
    ledger.observe_exit("cancel", item.signal_id, client_order_id="exit-1", state="cancelled",
                        filled_quantity=0, filled_notional=D("0"), fees=D("0"),
                        broker_order_id="broker-exit", at=AT)
    with pytest.raises(ReconciliationError, match="already consumed"):
        ledger.open_exit("reuse", item.signal_id, client_order_id="exit-1", quantity=1, at=AT)


def test_same_symbol_active_registration_is_rejected(ledger):
    register(ledger)
    later = datetime(2026, 10, 12, 13, 46, tzinfo=timezone.utc)
    identity = f"{BreakoutDetector.VERSION}|SPY|2026-10-12|1"
    signal = BreakoutSignal(sha256(identity.encode()).hexdigest()[:32], BreakoutDetector.VERSION, "SPY",
                            later - timedelta(minutes=1), later,
                            D("100.20"), D("99"), D("102.60"))
    second = EntryIntent.from_signal(signal, 4, sha256(b"two").hexdigest())
    with pytest.raises(ReconciliationError, match="symbol already"):
        ledger.register("second", second, reserved_cash=D("400.84"),
                        planned_stop_risk=D("4.88"), at=later)


def test_adverse_partial_fill_raises_committed_risk_above_plan(ledger):
    item = register(ledger)
    ledger.observe_entry("bad-price", item.signal_id, state="partial", filled_quantity=1,
                         filled_notional=D("110"), fees=D("0"), broker_order_id="entry", at=AT)
    assert ledger.snapshot()["committed_stop_risk"] == "11"


def test_event_clock_cannot_move_behind_complete_snapshot(ledger):
    item = cancel_entry(ledger)
    future = AT + timedelta(seconds=10)
    ledger.reconcile("snapshot", BrokerSnapshot(SCOPE, future, D("9799.58"),
                                                (BrokerPosition("SPY", 2),), (), True))
    with pytest.raises(ReconciliationError, match="clock moved backwards"):
        ledger.open_exit("late-arrival", item.signal_id, client_order_id="exit-1", quantity=1, at=AT)


@pytest.mark.parametrize("snapshot", [
    BrokerSnapshot(SCOPE, AT, D("10000"), (BrokerPosition("SPY", 1), BrokerPosition("SPY", 2)), (), True),
    BrokerSnapshot(SCOPE, AT, D("10000"), (BrokerPosition("AAPL", 1),), (), True),
    BrokerSnapshot(SCOPE, AT, D("10000"), (), ("same", "same"), True),
])
def test_snapshot_validation_rejects_duplicates_and_nonbaseline_symbols(ledger, snapshot):
    with pytest.raises(ReconciliationError):
        ledger.reconcile("snapshot", snapshot)


@pytest.mark.parametrize(("scope", "cash"), [("live:acct", D("10000")), ("paper:", D("10000")),
                                               (SCOPE, D("0")), (SCOPE, D("NaN"))])
def test_ledger_configuration_is_fail_closed(tmp_path, scope, cash):
    with pytest.raises((ReconciliationError, ValueError)):
        ReconciliationLedger(tmp_path / "bad.sqlite", scope, cash)


def test_missing_existing_file_and_symlink_are_rejected(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        ReconciliationLedger(tmp_path / "missing.sqlite", SCOPE, D("10000"), create=False)
    target = tmp_path / "target"
    target.touch()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ReconciliationError, match="symlink"):
        ReconciliationLedger(link, SCOPE, D("10000"))


def test_configuration_and_source_identity_are_checked(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    ReconciliationLedger(path, SCOPE, D("10000")).close()
    with pytest.raises(ReconciliationError, match="configuration"):
        ReconciliationLedger(path, SCOPE, D("9999"), create=False)
    import orb.reconciliation as module
    monkeypatch.setattr(module, "_model_id", lambda: "changed")
    with pytest.raises(ReconciliationError, match="model mismatch"):
        ReconciliationLedger(path, SCOPE, D("10000"), create=False)


def test_saved_outcome_tamper_is_detected(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with ReconciliationLedger(path, SCOPE, D("10000")) as value:
        register(value)
    db = sqlite3.connect(path)
    db.execute("UPDATE commands SET outcome='{}'")
    db.commit()
    db.close()
    with pytest.raises(ReconciliationError, match="deterministic replay"):
        ReconciliationLedger(path, SCOPE, D("10000"), create=False)


def test_decimal_result_is_independent_of_callers_context(ledger):
    with localcontext() as context:
        context.prec = 5
        item = partial_entry(ledger)
        ledger.observe_entry("terminal", item.signal_id, state="cancelled", filled_quantity=2,
                             filled_notional=D("200.40"), fees=D("0.02"),
                             broker_order_id="entry-broker", at=AT)
    assert ledger.snapshot()["committed_stop_risk"] == "2.44"


def test_demo_restart_and_cli_are_deterministic(tmp_path):
    direct = reconciliation_demo(tmp_path / "direct.sqlite")
    assert direct["broker_reconciled"] is True
    assert direct["paper_execution_enabled"] is direct["orders_submitted"] is False
    first_path, second_path = tmp_path / "first.sqlite", tmp_path / "second.sqlite"
    command = [sys.executable, "-m", "orb", "reconciliation-demo", "--db"]
    first = subprocess.run(command + [str(first_path)], check=True, text=True, capture_output=True).stdout
    second = subprocess.run(command + [str(second_path)], check=True, text=True, capture_output=True).stdout
    assert first == second
    payload = json.loads(first)
    assert payload["cash"] == "9900.57"
    inspected = subprocess.run([sys.executable, "-m", "orb", "reconciliation-inspect", "--db", str(first_path),
        "--account-scope", "paper:synthetic-reconciliation", "--initial-cash", "10000"],
        check=True, text=True, capture_output=True).stdout
    assert json.loads(inspected)["broker_reconciled"] is True


def test_demo_refuses_overwrite(tmp_path):
    path = tmp_path / "exists.sqlite"
    path.touch()
    with pytest.raises(FileExistsError):
        reconciliation_demo(path)
