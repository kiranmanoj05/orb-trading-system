from decimal import Decimal as D
import json
import subprocess
import sys

import pytest

from orb.account import AccountLedger, ledger_demo
from orb.risk import RiskLimits


def test_pending_orders_share_cash_and_gross_capacity():
    account = AccountLedger(D("1000"), RiskLimits(D("1"), D("1"), D("1")))
    assert account.reserve("a", "SPY", D("100"), D("99")).quantity == 10
    snapshot = account.snapshot()
    assert snapshot.available_cash == 0
    assert snapshot.gross_exposure == snapshot.equity == D("1000")
    second = account.reserve("b", "QQQ", D("100"), D("99"))
    assert second.quantity == 0
    assert account.cash == D("1000")
    account.cancel("a")
    assert account.reserve("b", "QQQ", D("100"), D("99")).quantity == 10


def test_cost_aware_fill_and_exit_reconcile_cash_equity_and_pnl():
    account = AccountLedger(D("10000"))
    decision = account.reserve("a", "SPY", D("100"), D("98"),
                               slippage_bps=D("10"), fee_per_share=D("0.01"))
    assert decision.quantity == 22
    assert account.snapshot().available_cash == D("7797.58")
    account.fill("a", D("100.10"))  # Already slipped; do not apply twice.
    assert account.cash == D("7797.58")
    assert account.snapshot().equity == D("9999.78")
    assert account.snapshot().gross_exposure == D("2202.2")
    account.close("a", D("97.902"))
    assert account.cash == account.snapshot().equity == D("9951.204")
    assert account.realized_pnl == D("-48.796")
    assert account.snapshot().gross_exposure == 0
    assert account.allocations[0].net_pnl == D("-48.796")


def test_better_fill_releases_unused_reservation_without_spending_other_cash():
    account = AccountLedger(D("10000"))
    account.reserve("a", "SPY", D("100"), D("98"))
    account.reserve("b", "QQQ", D("100"), D("98"))
    account.fill("a", D("99"))
    assert account.cash == D("7525")
    assert account.snapshot().available_cash == D("5025")
    assert account.snapshot().gross_exposure == D("4975")
    assert account.snapshot().equity == D("10000")


def test_unresolved_position_retains_resources_until_explicit_exit():
    account = AccountLedger(D("1000"), RiskLimits(D("1"), D("1"), D("1")))
    account.reserve("a", "SPY", D("100"), D("99"))
    account.fill("a", D("100"))
    account.mark_unresolved("a")
    assert account.reserve("b", "QQQ", D("100"), D("99")).quantity == 0
    with pytest.raises(ValueError):
        account.cancel("a")
    assert account.snapshot().gross_exposure == D("1000")
    assert account.allocations[0].net_pnl is None
    account.close("a", D("90"))  # Explicit resolution, including a gap loss.
    assert account.cash == D("900")
    assert account.realized_pnl == D("-100")
    assert account.reserve("b", "QQQ", D("100"), D("99")).quantity == 9


@pytest.mark.parametrize("price", [D("101"), D("98"), D("0"), D("NaN"), 100.0])
def test_invalid_or_worse_fill_keeps_reservation_intact(price):
    account = AccountLedger(D("10000"))
    account.reserve("a", "SPY", D("100"), D("98"))
    before = account.to_dict()
    with pytest.raises(ValueError):
        account.fill("a", price)
    assert account.to_dict() == before


@pytest.mark.parametrize("method", ["cancel", "fill", "close", "mark_unresolved"])
def test_unknown_order_does_not_mutate_state(method):
    account = AccountLedger(D("10000"))
    before = account.to_dict()
    args = ("unknown", D("100")) if method in ("fill", "close") else ("unknown",)
    with pytest.raises(ValueError):
        getattr(account, method)(*args)
    assert account.to_dict() == before


@pytest.mark.parametrize("terminal", ["cancelled", "closed"])
def test_duplicate_and_terminal_events_cannot_double_spend_or_credit(terminal):
    account = AccountLedger(D("10000"))
    account.reserve("a", "SPY", D("100"), D("98"))
    if terminal == "closed":
        account.fill("a", D("100"))
        with pytest.raises(ValueError):
            account.fill("a", D("100"))
        account.close("a", D("102"))
    else:
        account.cancel("a")
    before = account.to_dict()
    for action in (lambda: account.reserve("a", "SPY", D("100"), D("98")),
                   lambda: account.cancel("a"), lambda: account.close("a", D("102"))):
        with pytest.raises(ValueError):
            action()
        assert account.to_dict() == before


@pytest.mark.parametrize("state", ["reserved", "open", "unresolved"])
def test_existing_symbol_cannot_bypass_position_cap(state):
    account = AccountLedger(D("10000"))
    account.reserve("a", "SPY", D("100"), D("98"))
    if state != "reserved":
        account.fill("a", D("100"))
    if state == "unresolved":
        account.mark_unresolved("a")
    with pytest.raises(ValueError, match="active allocation"):
        account.reserve("b", "SPY", D("100"), D("98"))


@pytest.mark.parametrize("cash", [D("0"), D("-1"), D("NaN"), D("Infinity"), 100.0])
def test_invalid_initial_cash(cash):
    with pytest.raises(ValueError):
        AccountLedger(cash)


@pytest.mark.parametrize("order_id,symbol", [("", "SPY"), (None, "SPY"),
                                           ("a", "spy"), ("a", " SPY"), ("a", None)])
def test_invalid_identity_is_rejected(order_id, symbol):
    account = AccountLedger(D("10000"))
    with pytest.raises(ValueError):
        account.reserve(order_id, symbol, D("100"), D("98"))
    assert account.allocations == ()


def test_invalid_exit_does_not_release_position():
    account = AccountLedger(D("10000"))
    account.reserve("a", "SPY", D("100"), D("98"))
    account.fill("a", D("100"))
    before = account.to_dict()
    with pytest.raises(ValueError):
        account.close("a", D("NaN"))
    assert account.to_dict() == before


def test_gross_reservation_binds_before_cash_and_equity_updates_after_exit():
    account = AccountLedger(D("10000"), RiskLimits(D("1"), D("0.25"), D("0.3")))
    assert account.reserve("a", "SPY", D("100"), D("98")).quantity == 25
    assert account.reserve("b", "QQQ", D("100"), D("98")).quantity == 5
    account.fill("a", D("100"))
    account.close("a", D("104"))
    assert account.realized_pnl == D("100")
    assert account.snapshot().equity == D("10100")
    assert account.snapshot().gross_exposure == D("500")
    assert account.snapshot().available_cash == D("9600")


def test_installed_cli_traces_synthetic_lifecycle():
    run = subprocess.run([sys.executable, "-m", "orb", "account-demo"],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    data = json.loads(run.stdout)
    assert data == ledger_demo()
    assert data["first_quantity"] == 24
    assert D(data["after_reservations"]["available_cash"]) == D("5185.60")
    assert D(data["after_exit"]["cash"]) == D("10062.40")
    assert D(data["after_exit"]["realized_pnl"]) == D("62.40")
