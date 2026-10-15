"""Pure, snapshot-based long sizing. No reservations or broker side effects."""

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal


def _decimal(name: str, value: Decimal, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'non-negative'} Decimal")


@dataclass(frozen=True)
class RiskLimits:
    risk_fraction: Decimal = Decimal("0.005")
    max_position_fraction: Decimal = Decimal("0.25")
    max_gross_fraction: Decimal = Decimal("1")
    max_total_risk_fraction: Decimal = Decimal("0.01")

    def __post_init__(self):
        for name, value in asdict(self).items():
            _decimal(name, value, positive=True)
            if value > 1:
                raise ValueError(f"{name} must be at most 1 (no leverage)")


@dataclass(frozen=True)
class AccountSnapshot:
    equity: Decimal
    available_cash: Decimal
    gross_exposure: Decimal = Decimal("0")
    committed_stop_risk: Decimal = Decimal("0")

    def __post_init__(self):
        for name, value in asdict(self).items():
            _decimal(name, value, positive=name == "equity")


@dataclass(frozen=True)
class MarketMark:
    """Price at its availability instant, not the candle's start label."""

    price: Decimal
    available_at: datetime
    source: str = "completed_close"

    def __post_init__(self):
        _decimal("mark price", self.price, positive=True)
        if (not isinstance(self.available_at, datetime) or self.available_at.tzinfo is None
                or self.available_at.utcoffset() is None):
            raise ValueError("mark availability must be timezone-aware")
        if self.source not in ("completed_close", "entry_fill"):
            raise ValueError("unsupported mark source")


@dataclass(frozen=True)
class SizingDecision:
    quantity: int
    reason: str
    risk_budget: Decimal
    risk_per_share: Decimal
    planned_risk: Decimal
    entry_notional: Decimal
    cash_required: Decimal

    def to_dict(self):
        return {key: str(value) if isinstance(value, Decimal) else value
                for key, value in asdict(self).items()}


def size_long(account: AccountSnapshot, entry: Decimal, stop: Decimal,
              limits: RiskLimits | None = None, *, slippage_bps: Decimal = Decimal("0"),
              fee_per_share: Decimal = Decimal("0")) -> SizingDecision:
    """Size at an assumed pre-slippage entry price against a declared snapshot.

    Stop risk includes adverse market slippage and round-trip per-share fees.
    Exposure uses the supplied snapshot; cash includes the entry fee. The caller must
    reserve/reconcile accepted orders and supply a fresh snapshot. Gap losses
    may exceed planned risk. This function neither caps losses nor uses bars.
    """
    limits = limits or RiskLimits()
    for name, value in (("entry", entry), ("stop", stop)):
        _decimal(name, value, positive=True)
    for name, value in (("slippage_bps", slippage_bps), ("fee_per_share", fee_per_share)):
        _decimal(name, value)
    if slippage_bps >= 10000:
        raise ValueError("slippage must be below 10000 bps")
    if entry <= stop:
        raise ValueError("long entry must be above stop")
    slip = slippage_bps / Decimal("10000")
    buy = entry * (1 + slip)
    risk = buy - stop * (1 - slip) + 2 * fee_per_share
    budget = account.equity * limits.risk_fraction
    caps = {
        "risk_budget": int(budget // risk),
        "position_cap": int((account.equity * limits.max_position_fraction) // buy),
        "gross_exposure_cap": int(max(Decimal("0"), account.equity * limits.max_gross_fraction
                                      - account.gross_exposure) // buy),
        "cash_cap": int(account.available_cash // (buy + fee_per_share)),
        "aggregate_risk_cap": int(max(Decimal("0"),
            account.equity * limits.max_total_risk_fraction - account.committed_stop_risk) // risk),
    }
    binding = min(caps, key=caps.get)  # Stable tie order is part of the contract.
    quantity = caps[binding]
    reason = f"below_one_share:{binding}" if quantity == 0 else f"sized:{binding}"
    return SizingDecision(quantity, reason, budget, risk, quantity * risk,
                          quantity * buy, quantity * (buy + fee_per_share))
