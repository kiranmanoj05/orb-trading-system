"""Sequential, in-memory cash ledger for offline long-only research."""

from dataclasses import asdict, dataclass, replace
from decimal import Decimal

from orb.risk import AccountSnapshot, RiskLimits, SizingDecision, _decimal, size_long


ZERO = Decimal("0")


@dataclass(frozen=True)
class Allocation:
    order_id: str
    symbol: str
    state: str
    quantity: int
    stop: Decimal
    fee_per_share: Decimal
    reserved_cash: Decimal
    reserved_notional: Decimal
    entry_price: Decimal | None = None
    entry_fee: Decimal = ZERO
    exit_price: Decimal | None = None
    exit_fee: Decimal = ZERO
    net_pnl: Decimal | None = None


class AccountLedger:
    """Own shared resources; callers serialize all operations.

    Equity carries open positions at entry cost, not a current market mark.
    Pending orders reserve cash and entry notional without spending cash.
    Only full fills/exits are supported. IDs remain consumed after closure or
    cancellation. State is not durable or safe across concurrent workers.
    """

    def __init__(self, cash: Decimal, limits: RiskLimits | None = None):
        _decimal("cash", cash, positive=True)
        self._cash = cash
        self._realized_pnl = ZERO
        self.limits = limits or RiskLimits()
        self._allocations: dict[str, Allocation] = {}

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def realized_pnl(self) -> Decimal:
        return self._realized_pnl

    @property
    def allocations(self) -> tuple[Allocation, ...]:
        return tuple(self._allocations.values())

    def _totals(self) -> tuple[Decimal, Decimal, Decimal]:
        reserved_cash = reserved_notional = position_notional = ZERO
        for item in self.allocations:
            if item.state == "reserved":
                reserved_cash += item.reserved_cash
                reserved_notional += item.reserved_notional
            elif item.state in ("open", "unresolved"):
                position_notional += item.entry_price * item.quantity
        return reserved_cash, reserved_notional, position_notional

    def snapshot(self) -> AccountSnapshot:
        reserved_cash, reserved_notional, position_notional = self._totals()
        return AccountSnapshot(self.cash + position_notional,
                               self.cash - reserved_cash,
                               position_notional + reserved_notional)

    def reserve(self, order_id: str, symbol: str, entry: Decimal, stop: Decimal,
                *, slippage_bps: Decimal = ZERO,
                fee_per_share: Decimal = ZERO) -> SizingDecision:
        if not isinstance(order_id, str) or not order_id.strip():
            raise ValueError("order ID must be non-empty")
        if order_id in self._allocations:
            raise ValueError("order ID already consumed")
        if not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper():
            raise ValueError("symbol must be non-empty, uppercase and trimmed")
        if any(a.symbol == symbol and a.state in ("reserved", "open", "unresolved")
               for a in self.allocations):
            raise ValueError("symbol already has an active allocation")
        decision = size_long(self.snapshot(), entry, stop, self.limits,
                             slippage_bps=slippage_bps, fee_per_share=fee_per_share)
        if decision.quantity:
            self._allocations[order_id] = Allocation(
                order_id, symbol, "reserved", decision.quantity, stop,
                fee_per_share, decision.cash_required, decision.entry_notional)
        return decision

    def _require(self, order_id: str, *states: str) -> Allocation:
        item = self._allocations.get(order_id)
        if item is None or item.state not in states:
            raise ValueError(f"order must be in state {states}")
        return item

    def cancel(self, order_id: str) -> None:
        item = self._require(order_id, "reserved")
        self._allocations[order_id] = replace(item, state="cancelled")

    def fill(self, order_id: str, price: Decimal) -> None:
        """Apply an already-slipped fill, retaining reservation on rejection.

        A worse fill must be re-sized by a future execution controller. It is
        never accepted using capacity reserved for other orders.
        """
        item = self._require(order_id, "reserved")
        _decimal("fill price", price, positive=True)
        notional = price * item.quantity
        fee = item.fee_per_share * item.quantity
        if price <= item.stop:
            raise ValueError("fill must be above stop")
        if notional > item.reserved_notional or notional + fee > item.reserved_cash:
            raise ValueError("fill exceeds reservation; resize or cancel explicitly")
        self._cash -= notional + fee
        self._allocations[order_id] = replace(item, state="open", entry_price=price,
                                               entry_fee=fee)

    def mark_unresolved(self, order_id: str) -> None:
        item = self._require(order_id, "open")
        self._allocations[order_id] = replace(item, state="unresolved")

    def close(self, order_id: str, price: Decimal) -> None:
        """Apply an explicit full exit, including resolution of an unknown exit.

        Missing-data alone is never an exit. Caller must supply the confirmed
        hypothetical exit price. Per-side fees are fixed at reservation time.
        """
        item = self._require(order_id, "open", "unresolved")
        _decimal("exit price", price, positive=True)
        fee = item.fee_per_share * item.quantity
        pnl = (price - item.entry_price) * item.quantity - item.entry_fee - fee
        self._cash += price * item.quantity - fee
        self._realized_pnl += pnl
        self._allocations[order_id] = replace(item, state="closed", exit_price=price,
                                               exit_fee=fee, net_pnl=pnl)

    def to_dict(self) -> dict:
        def convert(value):
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, list):
                return [convert(v) for v in value]
            return value
        reserved_cash, reserved_notional, positions = self._totals()
        return convert({"model_version": "entry-cost-ledger-v1", "cash": self.cash,
                        "available_cash": self.cash - reserved_cash,
                        "carrying_equity": self.cash + positions,
                        "reserved_cash": reserved_cash,
                        "gross_exposure": positions + reserved_notional,
                        "realized_pnl": self.realized_pnl,
                        "allocations": [asdict(a) for a in self.allocations]})


def ledger_demo() -> dict:
    """Hand-calculated resource lifecycle, unrelated to historical returns."""
    account = AccountLedger(Decimal("10000"))
    first = account.reserve("demo-spy", "SPY", Decimal("100.30"), Decimal("99"))
    account.reserve("demo-qqq", "QQQ", Decimal("100.30"), Decimal("99"))
    reserved = account.to_dict()
    account.fill("demo-spy", Decimal("100.30"))
    account.cancel("demo-qqq")
    account.close("demo-spy", Decimal("102.90"))
    return {"synthetic": True, "first_quantity": first.quantity,
            "after_reservations": reserved, "after_exit": account.to_dict()}
