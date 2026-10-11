"""Deterministic long-only minute-bar fills; no broker or account model."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable

from orb.domain import Bar
from orb.replay import ReplayResult, replay
from orb.session import exchange_session
from orb.strategy import NEW_YORK


@dataclass(frozen=True)
class ExecutionConfig:
    quantity: int = 1
    slippage_bps: Decimal = Decimal("0")
    fee_per_share: Decimal = Decimal("0")

    def __post_init__(self):
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        for value in (self.slippage_bps, self.fee_per_share):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("costs must be finite non-negative Decimal values")
        if self.slippage_bps >= 10000:
            raise ValueError("slippage must be below 10000 bps")


@dataclass(frozen=True)
class SimulatedTrade:
    signal_id: str
    symbol: str
    status: str
    reason: str
    quantity: int
    entry_at: datetime | None = None
    entry_price: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    exit_at: datetime | None = None
    exit_price: Decimal | None = None
    gross_pnl: Decimal | None = None
    fees: Decimal | None = None
    net_pnl: Decimal | None = None
    ambiguous_exit: bool = False


@dataclass(frozen=True)
class SimulationResult:
    replay_result: ReplayResult
    config: ExecutionConfig
    trades: tuple[SimulatedTrade, ...]

    def to_dict(self) -> dict:
        def convert(value):
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, (tuple, list)):
                return [convert(v) for v in value]
            return value
        return {"model_version": "minute-long-v1", "replay": self.replay_result.to_dict(),
                "config": convert(asdict(self.config)),
                "trades": convert([asdict(t) for t in self.trades])}


def simulate(bars: Iterable[Bar], config: ExecutionConfig | None = None) -> SimulationResult:
    """Replay signals, then apply causal fills to each symbol/session.

    Signals are produced by the existing chronological replay. Later bars may
    determine fills/exits but never alter earlier signals. Entry requires the
    exact next minute, before the entry cutoff. No fill is invented across gaps.
    Intrabar exits are timestamped at bar completion (observation time), since
    their actual second and ordering are unknown. Stops win ambiguous candles.
    Limit targets have no adverse slippage; market buys/sells do. Fixed share
    counts are scenario inputs, not account risk sizing or liquidity modelling.
    """
    config = config or ExecutionConfig()
    data = list(bars)
    result = replay(data)  # Also validates global chronology and duplicate keys.
    grouped = {}
    for bar in data:
        day = bar.start.astimezone(NEW_YORK).date()
        schedule = exchange_session(day)
        if schedule is not None and schedule.contains_bar(bar.start):
            grouped.setdefault((bar.symbol, day), []).append(bar)
    trades = []
    slip = config.slippage_bps / Decimal("10000")
    for signal in result.signals:
        day = signal.bar_start.astimezone(NEW_YORK).date()
        schedule = exchange_session(day)
        assert schedule is not None
        future = [b for b in grouped[(signal.symbol, day)] if b.start >= signal.observed_at]
        fields = dict(signal_id=signal.signal_id, symbol=signal.symbol, quantity=config.quantity)
        if signal.observed_at >= schedule.entry_cutoff:
            trades.append(SimulatedTrade(**fields, status="unfilled", reason="entry_cutoff"))
            continue
        if not future or future[0].start != signal.observed_at:
            trades.append(SimulatedTrade(**fields, status="unfilled",
                                         reason="missing_entry_bar" if future else "dataset_ended_before_entry"))
            continue
        entry = future[0].open * (1 + slip)
        if entry <= signal.stop:
            trades.append(SimulatedTrade(**fields, status="unfilled", reason="entry_at_or_below_stop"))
            continue
        target = entry + 2 * (entry - signal.stop)
        fields.update(entry_at=future[0].start, entry_price=entry, stop=signal.stop, target=target)
        expected = signal.observed_at
        for bar in future:
            if bar.start != expected:
                trades.append(SimulatedTrade(**fields, status="unresolved", reason="missing_position_bar"))
                break
            expected += timedelta(minutes=1)
            ambiguous = False
            # At an open, flatten precedes intrabar movement; gap stops precede targets.
            if bar.start >= schedule.flatten_at:
                price, reason, stamp = bar.open * (1 - slip), "session_flatten", bar.start
            elif bar.open <= signal.stop:
                price, reason, stamp = bar.open * (1 - slip), "stop_gap", bar.start
            elif bar.open >= target:
                price, reason, stamp = bar.open, "target_gap", bar.start
            elif bar.low <= signal.stop:
                price, reason, stamp = signal.stop * (1 - slip), "stop", expected
                ambiguous = bar.high >= target
            elif bar.high >= target:
                price, reason, stamp = target, "target", expected
            else:
                continue
            gross = (price - entry) * config.quantity
            fees = 2 * config.fee_per_share * config.quantity
            trades.append(SimulatedTrade(**fields, status="closed", reason=reason,
                                         exit_at=stamp, exit_price=price, gross_pnl=gross,
                                         fees=fees, net_pnl=gross - fees, ambiguous_exit=ambiguous))
            break
        else:
            trades.append(SimulatedTrade(**fields, status="unresolved", reason="dataset_ended_with_position"))
    return SimulationResult(result, config, tuple(trades))
