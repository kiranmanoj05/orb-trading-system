"""Deterministic long-only minute-bar fills; no broker or account model."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable

from orb.domain import Bar
from orb.replay import ReplayResult, replay
from orb.session import exchange_session
from orb.strategy import NEW_YORK
from orb.entry_policy import EntryPolicy, resolve_policy
from orb.fill_assumptions import FillAssumptions, validate_fill_assumptions


@dataclass(frozen=True)
class ExecutionConfig:
    quantity: int = 1
    slippage_bps: Decimal = Decimal("0")
    fee_per_share: Decimal = Decimal("0")
    fill_assumptions: FillAssumptions | None = None

    def __post_init__(self):
        assumptions = validate_fill_assumptions(self.fill_assumptions)
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        for value in (self.slippage_bps, self.fee_per_share):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("costs must be finite non-negative Decimal values")
        if self.slippage_bps >= 10000:
            raise ValueError("slippage must be below 10000 bps")
        if assumptions is not None:
            assumptions.market_side_bps(self.slippage_bps)


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
    entry_policy: EntryPolicy | None = None

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
        policy = resolve_policy(self.entry_policy)
        config = asdict(self.config)
        config.pop('fill_assumptions')
        if self.config.fill_assumptions is not None:
            config['fill_assumptions'] = self.config.fill_assumptions.to_dict()
        return {"model_version": ("minute-long-fill-assumptions-v1" if self.config.fill_assumptions is not None
                                  else "minute-long-v1" if policy.is_reference else "minute-long-policy-v1"),
                **({'entry_policy': policy.to_dict()} if not policy.is_reference else {}), "replay": self.replay_result.to_dict(),
                "config": convert(config),
                "trades": convert([asdict(t) for t in self.trades])}


def simulate(bars: Iterable[Bar], config: ExecutionConfig | None = None, *,
             entry_policy: EntryPolicy | None = None) -> SimulationResult:
    """Replay signals, then apply causal fills to each symbol/session.

    Signals are produced by the existing chronological replay. Later bars may
    determine fills/exits but never alter earlier signals. Entry requires the
    exact next minute by default, before the entry cutoff. Explicit historical
    fill assumptions may delay this boundary 0–5 minutes and add half-spread to
    each market side. All waiting minutes must exist; no order/reservation or
    stop/target is active until the delayed open. No fill is invented across gaps.
    Intrabar exits are timestamped at bar completion (observation time), since
    their actual second and ordering are unknown. Stops win ambiguous candles.
    Limit targets have no adverse slippage; market buys/sells do. Fixed share
    counts are scenario inputs, not account risk sizing or liquidity modelling.
    """
    config = config or ExecutionConfig()
    data = list(bars)
    policy = resolve_policy(entry_policy)
    result = replay(data, entry_policy=policy)  # Also validates global chronology and duplicate keys.
    grouped = {}
    for bar in data:
        day = bar.start.astimezone(NEW_YORK).date()
        schedule = exchange_session(day)
        if schedule is not None and schedule.contains_bar(bar.start):
            grouped.setdefault((bar.symbol, day), []).append(bar)
    trades = []
    assumptions = config.fill_assumptions or FillAssumptions()
    slip = assumptions.market_side_bps(config.slippage_bps) / Decimal("10000")
    for signal in result.signals:
        day = signal.bar_start.astimezone(NEW_YORK).date()
        schedule = exchange_session(day)
        assert schedule is not None
        future = [b for b in grouped[(signal.symbol, day)] if b.start >= signal.observed_at]
        fields = dict(signal_id=signal.signal_id, symbol=signal.symbol, quantity=config.quantity)
        entry_at = signal.observed_at + timedelta(minutes=assumptions.entry_delay_minutes)
        if entry_at >= policy.entry_cutoff(schedule):
            trades.append(SimulatedTrade(**fields, status="unfilled", reason="entry_cutoff"))
            continue
        # Every waiting minute must exist. Checking presence never uses future
        # high/low/close to decide whether a fixed signal would have been sent.
        required = [signal.observed_at + timedelta(minutes=i)
                    for i in range(assumptions.entry_delay_minutes + 1)]
        if len(future) < len(required) or [b.start for b in future[:len(required)]] != required:
            trades.append(SimulatedTrade(**fields, status="unfilled",
                                         reason="missing_entry_bar" if future else "dataset_ended_before_entry"))
            continue
        future = future[assumptions.entry_delay_minutes:]
        entry = future[0].open * (1 + slip)
        if entry <= signal.stop:
            trades.append(SimulatedTrade(**fields, status="unfilled", reason="entry_at_or_below_stop"))
            continue
        target = entry + policy.target_r * (entry - signal.stop)
        fields.update(entry_at=future[0].start, entry_price=entry, stop=signal.stop, target=target)
        expected = entry_at
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
    return SimulationResult(result, config, tuple(trades), policy)
