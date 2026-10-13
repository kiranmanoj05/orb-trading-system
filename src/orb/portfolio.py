"""Offline chronological account replay using quantity-independent fill paths."""

from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from orb.account import AccountLedger
from orb.domain import Bar
from orb.risk import RiskLimits
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


def replay_portfolio(bars: Iterable[Bar], initial_cash: Decimal = Decimal("10000"),
                     limits: RiskLimits | None = None, *,
                     slippage_bps: Decimal = Decimal("0"),
                     fee_per_share: Decimal = Decimal("0")) -> dict:
    """Apply simulated events to one shared account without future funding.

    Fill paths are quantity-independent under the existing no-market-impact
    model. Precomputing them does not admit candidates using their eventual
    outcome: sizing reads only the current ledger and the entry open. Existing
    exits at a timestamp precede new entries; simultaneous entries sort by
    symbol then signal ID. Intrabar exits release resources only at completion.
    A new position's same-open exit follows all entries at that timestamp.
    Unresolved positions retain resources across sessions and dataset end.
    """
    limits = limits or RiskLimits()
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
    events = []
    records = {}
    trace = []
    for trade in paths.trades:
        if trade.entry_at is None:
            records[trade.signal_id] = replace(trade, quantity=0)
            continue
        events.append((trade.entry_at, 1, trade.symbol, trade.signal_id, "entry", trade))
        if trade.exit_at is not None:
            phase = 2 if trade.exit_at == trade.entry_at else 0
            events.append((trade.exit_at, phase, trade.symbol, trade.signal_id, "exit", trade))
    for stamp, _, symbol, key, kind, path in sorted(events):
        if kind == "entry":
            raw = opens[(symbol, stamp)]
            if any(a.symbol == symbol and a.state in ("reserved", "open", "unresolved")
                   for a in account.allocations):
                reason = "active_symbol_allocation"
                decision = None
            elif raw <= path.stop:
                reason = "pre_slippage_entry_at_or_below_stop"
                decision = None
            elif account.cash + sum(a.entry_price * a.quantity for a in account.allocations
                                    if a.state in ("open", "unresolved")) <= 0:
                reason = "account_depleted"
                decision = None
            else:
                decision = account.reserve(key, symbol, raw, path.stop,
                                           slippage_bps=slippage_bps,
                                           fee_per_share=fee_per_share)
                reason = decision.reason
            quantity = decision.quantity if decision else 0
            if quantity:
                account.fill(key, path.entry_price)
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
                          "reason": reason, "account": account.to_dict()})
        elif records[key].quantity:
            account.close(key, path.exit_price)
            trace.append({"at": stamp, "kind": "exit", "signal_id": key,
                          "symbol": symbol, "reason": path.reason, "account": account.to_dict()})
    # Mark unknown positions at the end of this offline evaluation. They have
    # held cash/exposure throughout, so this does not change earlier admission.
    for item in account.allocations:
        if item.state == "open":
            account.mark_unresolved(item.order_id)
    return _json({"model_version": "portfolio-entry-cost-v1",
                  "config": {"initial_cash": initial_cash, "limits": asdict(limits),
                             "slippage_bps": slippage_bps, "fee_per_share": fee_per_share,
                             "tie_policy": "existing exits, symbol-ordered entries, same-entry exits"},
                  "replay": paths.replay_result.to_dict(),
                  "trades": [asdict(records[t.signal_id]) for t in paths.trades],
                  "events": trace, "account": account.to_dict()})
