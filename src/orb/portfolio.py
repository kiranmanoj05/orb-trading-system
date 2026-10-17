"""Offline chronological account replay using quantity-independent fill paths."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable

from orb.account import AccountLedger
from orb.domain import Bar
from orb.daily_loss import DailyLossGuard
from orb.risk import MarketMark, RiskLimits, _decimal
from orb.session import exchange_session
from orb.strategy import NEW_YORK
from orb.simulation import ExecutionConfig, simulate


def _json(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return value


def _marked_state(account, stamp, closes, entry_marks):
    marks, missing = {}, []
    equity = account.cash
    for item in account.allocations:
        if item.state not in ("open", "unresolved"):
            continue
        mark = entry_marks.get(item.order_id)
        if mark is None or mark.available_at != stamp:
            mark = closes.get((item.symbol, stamp))
        if mark is None:
            missing.append(item.symbol)
        else:
            marks[item.symbol] = mark
            equity += mark.price * item.quantity
    return (None if missing else equity), marks, sorted(missing)


def replay_portfolio(bars: Iterable[Bar], initial_cash: Decimal = Decimal("10000"),
                     limits: RiskLimits | None = None, *,
                     slippage_bps: Decimal = Decimal("0"),
                     fee_per_share: Decimal = Decimal("0"),
                     daily_loss_fraction: Decimal = Decimal("0.02")) -> dict:
    """Apply simulated events to one shared account without future funding.

    Fill paths are quantity-independent under the existing no-market-impact
    model. Precomputing them does not admit candidates using their eventual
    outcome: sizing reads only exact-time marked holdings, committed initial
    stop risk, current cash and the entry open. Missing held-symbol marks reject
    admission; there is no entry-cost fallback. Existing
    exits at a timestamp precede new entries; simultaneous entries sort by
    symbol then signal ID. Intrabar exits release resources only at completion.
    A new position's same-open exit follows all entries at that timestamp.
    Unresolved positions retain resources across sessions and dataset end.
    """
    limits = limits or RiskLimits()
    _decimal("daily loss fraction", daily_loss_fraction, positive=True)
    if daily_loss_fraction > 1:
        raise ValueError("daily loss fraction must be at most 1")
    account = AccountLedger(initial_cash, limits)
    config = ExecutionConfig(1, slippage_bps, fee_per_share)
    data = list(bars)
    paths = simulate(data, config)
    observed = {s.signal_id: s.observed_at for s in paths.replay_result.signals}
    paths = replace(paths,
                    replay_result=replace(paths.replay_result,
                        signals=tuple(sorted(paths.replay_result.signals,
                                             key=lambda s: (s.observed_at, s.symbol, s.signal_id))),
                        sessions=tuple(sorted(paths.replay_result.sessions,
                                              key=lambda s: (s.session, s.symbol)))),
                    trades=tuple(sorted(paths.trades,
                                        key=lambda t: (observed[t.signal_id], t.symbol, t.signal_id))))
    opens = {(b.symbol, b.start): b.open for b in data}
    closes = {}
    for bar in data:
        session = exchange_session(bar.start.astimezone(NEW_YORK).date())
        if session and session.contains_bar(bar.start):
            closes[(bar.symbol, bar.start + timedelta(minutes=1))] = MarketMark(
                bar.close, bar.start + timedelta(minutes=1))
    entry_marks = {}
    events = []
    records = {}
    trace = []
    loss_observations = []
    guards = {}
    # A risk clock samples every session minute, not only order events. This
    # catches a temporary loss or missing mark even without a new candidate.
    dates = [b.start.astimezone(NEW_YORK).date() for b in data]
    day = min(dates)
    end_day = max(dates)
    while day <= end_day:
        session = exchange_session(day)
        if session:
            stamp = session.opens_at
            while stamp <= session.closes_at:
                events.append((stamp, 0.5, "", "", "risk", None))
                stamp += timedelta(minutes=1)
        day += timedelta(days=1)

    def observe_loss(stamp, phase):
        equity, _, missing = _marked_state(account, stamp, closes, entry_marks)
        guard = guards[stamp.astimezone(NEW_YORK).date()]
        observation = guard.observe(stamp, equity)
        loss_observations.append({"at": stamp, "phase": phase,
                                  "missing_mark_symbols": missing, **observation})
    for trade in paths.trades:
        if trade.entry_at is None:
            records[trade.signal_id] = replace(trade, quantity=0)
            continue
        events.append((trade.entry_at, 1, trade.symbol, trade.signal_id, "entry", trade))
        if trade.exit_at is not None:
            phase = 2 if trade.exit_at == trade.entry_at else 0
            events.append((trade.exit_at, phase, trade.symbol, trade.signal_id, "exit", trade))
    for stamp, phase, symbol, key, kind, path in sorted(events):
        day = stamp.astimezone(NEW_YORK).date()
        if day not in guards:
            session = exchange_session(day)
            # The synthetic opening clock runs before any entry this day.
            equity, _, _ = _marked_state(account, stamp, closes, entry_marks)
            guards[day] = DailyLossGuard(session, daily_loss_fraction,
                                        equity if equity is not None and equity > 0 else None)
        guard = guards[day]
        if kind == "risk":
            observe_loss(stamp, "before_entries")
        elif kind == "entry":
            raw = opens[(symbol, stamp)]
            # Only exact completed-close availability, or a fill already applied
            # at this instant, may value existing holdings. Never use this
            # candle's future close or carry a previous minute across a gap.
            equity, marks, missing_marks = _marked_state(account, stamp, closes, entry_marks)
            snapshot = (account.marked_snapshot(stamp, marks)
                        if not missing_marks and equity > 0 else None)
            if any(a.symbol == symbol and a.state in ("reserved", "open", "unresolved")
                   for a in account.allocations):
                reason = "active_symbol_allocation"
                decision = None
            elif raw <= path.stop:
                reason = "pre_slippage_entry_at_or_below_stop"
                decision = None
            elif missing_marks:
                reason = "missing_or_stale_position_mark"
                decision = None
            elif snapshot is None:
                reason = "account_depleted"
                decision = None
            elif guard.block_reason:
                reason = guard.block_reason
                decision = None
            else:
                decision = account.reserve(key, symbol, raw, path.stop,
                                           slippage_bps=slippage_bps,
                                           fee_per_share=fee_per_share, at=stamp, marks=marks)
                reason = decision.reason
            quantity = decision.quantity if decision else 0
            if quantity:
                account.fill(key, path.entry_price)
                entry_marks[key] = MarketMark(path.entry_price, stamp, "entry_fill")
                observe_loss(stamp, "after_fill")
                records[key] = replace(path, quantity=quantity,
                                       gross_pnl=path.gross_pnl * quantity if path.gross_pnl is not None else None,
                                       fees=path.fees * quantity if path.fees is not None else None,
                                       net_pnl=path.net_pnl * quantity if path.net_pnl is not None else None)
            else:
                # A rejected candidate has no hypothetical entry, exit or P&L.
                records[key] = replace(path, quantity=0, status="rejected", reason=reason,
                                       entry_at=None, entry_price=None, stop=None, target=None,
                                       exit_at=None, exit_price=None, gross_pnl=None, fees=None,
                                       net_pnl=None, ambiguous_exit=False)
            trace.append({"at": stamp, "kind": "entry" if quantity else "rejected",
                          "signal_id": key, "symbol": symbol,
                          "sizing": decision.to_dict() if decision else None,
                          "admission_snapshot": asdict(snapshot) if snapshot is not None else None,
                          "marks": {s: asdict(m) for s, m in sorted(marks.items())},
                          "missing_mark_symbols": sorted(missing_marks),
                          "daily_loss": guard.to_dict(),
                          "reason": reason, "account": account.to_dict()})
        elif kind == "exit" and records[key].quantity:
            account.close(key, path.exit_price)
            if phase == 2:
                observe_loss(stamp, "after_same_entry_exit")
            trace.append({"at": stamp, "kind": "exit", "signal_id": key,
                          "symbol": symbol, "reason": path.reason, "account": account.to_dict()})
    # Mark unknown positions at the end of this offline evaluation. They have
    # held cash/exposure throughout, so this does not change earlier admission.
    for item in account.allocations:
        if item.state == "open":
            account.mark_unresolved(item.order_id)
    return _json({"model_version": "portfolio-session-loss-v3",
                  "config": {"initial_cash": initial_cash, "limits": asdict(limits),
                             "slippage_bps": slippage_bps, "fee_per_share": fee_per_share,
                             "daily_loss_fraction": daily_loss_fraction,
                             "daily_loss_policy": "fixed opening equity; loss/data-gap latch until next session",
                             "admission_valuation": "exact-time completed closes; same-instant fills",
                             "aggregate_risk": "initial cost-aware stop risk; release only on cancel/exit",
                             "tie_policy": "existing exits, symbol-ordered entries, same-entry exits"},
                  "replay": paths.replay_result.to_dict(),
                  "trades": [asdict(records[t.signal_id]) for t in paths.trades],
                  "daily_loss_sessions": [g.to_dict() for _, g in sorted(guards.items())],
                  "daily_loss_observations": loss_observations,
                  "events": trace, "account": account.to_dict()})
