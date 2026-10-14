"""Deterministic research evidence, including absent sessions and unknown exits."""

from collections import Counter
from dataclasses import replace
from datetime import date, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import platform
from typing import Iterable

from orb.breakout import BreakoutDetector
from orb.domain import Bar
from orb.portfolio import replay_portfolio
from orb.risk import RiskLimits
from orb.session import exchange_session
from orb.strategy import NEW_YORK
from orb.valuation import market_valuation


def _decimal(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def engine_fingerprint() -> str:
    """Hash shipped Python sources rather than relying on an editable version."""
    return _digest({p.name: sha256(p.read_bytes()).hexdigest()
                    for p in sorted(Path(__file__).parent.glob("*.py"))})


def evaluate(bars: Iterable[Bar], initial_cash: Decimal = Decimal("10000"),
             limits: RiskLimits | None = None, *,
             slippage_bps: Decimal = Decimal("0"), fee_per_share: Decimal = Decimal("0"),
             data_label: str = "unspecified",
             start_session: date | None = None, end_session: date | None = None) -> dict:
    """Evaluate the fixed SPY/QQQ baseline; never treat absent bars as no trades.

    Without explicit date bounds, coverage spans observed New York dates only.
    Calendar session coverage checks every regular-session minute for BOTH
    symbols, independently of whether a candidate or a closed trade exists.
    """
    data = list(bars)
    if not data:
        raise ValueError("evaluation input contains no bars")
    if any(b.symbol not in ("SPY", "QQQ") for b in data):
        raise ValueError("baseline evaluation supports only SPY and QQQ")
    if not data_label.strip():
        raise ValueError("data label must not be blank")
    if (start_session is None) != (end_session is None):
        raise ValueError("provide both start-session and end-session")
    observed_dates = [b.start.astimezone(NEW_YORK).date() for b in data]
    explicit = start_session is not None
    start = start_session or min(observed_dates)
    end = end_session or max(observed_dates)
    if start > end:
        raise ValueError("start-session must not follow end-session")
    if min(observed_dates) < start or max(observed_dates) > end:
        raise ValueError("input observations fall outside evaluation session bounds")
    # Validate chronological input BEFORE canonical tie ordering. Sorting must
    # never silently repair malformed source ordering or duplicate revisions.
    previous = None
    for bar in data:
        stamp = bar.start.astimezone(timezone.utc)
        if previous is not None and stamp < previous:
            raise ValueError("evaluation input must be chronological")
        previous = stamp
    canonical = sorted((replace(b, start=b.start.astimezone(timezone.utc),
                                **{name: Decimal(_decimal(getattr(b, name)))
                                   for name in ("open", "high", "low", "close")}) for b in data),
                       key=lambda b: (b.start, b.symbol))
    limits = limits or RiskLimits()
    initial_cash = Decimal(_decimal(initial_cash))
    slippage_bps = Decimal(_decimal(slippage_bps))
    fee_per_share = Decimal(_decimal(fee_per_share))
    limits = RiskLimits(*(Decimal(_decimal(getattr(limits, name))) for name in
                          ("risk_fraction", "max_position_fraction", "max_gross_fraction")))
    result = replay_portfolio(canonical, initial_cash, limits,
                              slippage_bps=slippage_bps, fee_per_share=fee_per_share)
    dataset_rows = [[b.symbol, b.start.isoformat(),
                     *(_decimal(p) for p in (b.open, b.high, b.low, b.close)), b.volume]
                    for b in canonical]
    keys = {(b.symbol, b.start) for b in canonical}
    coverage = []
    day = start
    while day <= end:
        session = exchange_session(day)
        if session is not None:
            expected = int((session.closes_at - session.opens_at).total_seconds() // 60)
            for symbol in ("SPY", "QQQ"):
                stamps = [session.opens_at + timedelta(minutes=i) for i in range(expected)]
                missing = [t for t in stamps if (symbol, t) not in keys]
                coverage.append({"session": str(day), "symbol": symbol,
                                 "expected_minutes": expected,
                                 "observed_minutes": expected - len(missing),
                                 "missing_minutes": len(missing),
                                 "first_missing_at": missing[0].isoformat() if missing else None,
                                 "state": "missing" if len(missing) == expected else
                                          "partial" if missing else "complete"})
        day += timedelta(days=1)
    trades = result["trades"]
    counts = {state: sum(t["status"] == state for t in trades)
              for state in ("closed", "unresolved", "unfilled", "rejected")}
    closed = [t for t in trades if t["status"] == "closed"]
    pnls = [Decimal(t["net_pnl"]) for t in closed]
    realised = sum(pnls, Decimal("0"))
    if realised != Decimal(result["account"]["realized_pnl"]):
        raise ValueError("closed trade P&L disagrees with account ledger")
    incomplete_sessions = sum(c["state"] != "complete" for c in coverage)
    complete = bool(coverage) and not incomplete_sessions and not (
        counts["unresolved"] or counts["unfilled"])
    valuation = market_valuation(canonical, result, coverage)
    manifest = {
        "schema_version": "orb-evaluation-v2",
        "dataset": {"sha256": _digest(dataset_rows), "bars": len(canonical),
                    "first_bar_at": canonical[0].start.isoformat(),
                    "last_bar_at": canonical[-1].start.isoformat(),
                    "label": data_label.strip(), "provenance_verified": False},
        "config": {"initial_cash": _decimal(initial_cash),
                   "risk_fraction": _decimal(limits.risk_fraction),
                   "max_position_fraction": _decimal(limits.max_position_fraction),
                   "max_gross_fraction": _decimal(limits.max_gross_fraction),
                   "slippage_bps": _decimal(slippage_bps),
                   "fee_per_share": _decimal(fee_per_share),
                   "start_session": str(start), "end_session": str(end),
                   "bounds_source": "explicit" if explicit else "observed_dates",
                   "symbols": ["SPY", "QQQ"]},
        "engine": {"source_sha256": engine_fingerprint(),
                   "strategy_version": BreakoutDetector.VERSION,
                   "portfolio_model": result["model_version"],
                   "calendar": "XNYS", "exchange_calendars": version("exchange-calendars"),
                   "dependencies": {name: version(name) for name in
                                    ("numpy", "pandas", "tzdata", "pyluach",
                                     "korean-lunar-calendar", "toolz", "python-dateutil")}},
    }
    summary = {
        "complete": complete, "coverage_scope": "regular-session minutes within declared dates",
        "expected_symbol_sessions": len(coverage),
        "incomplete_symbol_sessions": incomplete_sessions,
        "candidate_count": len(trades), "trade_status_counts": counts,
        "trade_reasons": dict(sorted(Counter(t["reason"] for t in trades).items())),
        "session_state_counts": dict(sorted(Counter(s["state"] for s in result["replay"]["sessions"]).items())),
        "ambiguous_closed_exits": sum(t["ambiguous_exit"] for t in closed),
        "closed_subset": {"count": len(closed), "net_pnl": str(realised),
                          "wins": sum(p > 0 for p in pnls), "losses": sum(p < 0 for p in pnls),
                          "flat": sum(p == 0 for p in pnls),
                          "win_fraction": str(Decimal(sum(p > 0 for p in pnls)) / len(pnls)) if pnls else None},
        "unresolved_signal_ids": [t["signal_id"] for t in trades if t["status"] == "unresolved"],
        "total_return": valuation["total_return"], "max_drawdown": valuation["max_drawdown"],
        "limitations": [
            "Realised P&L and win fraction describe closed trades only; unknown exits are not zero.",
            "Completeness checks declared dates only; omitted dates outside those bounds are unknown.",
            "Data labels are user assertions; no provider provenance or historical performance is verified.",
            "Market valuation is reporting-only; sizing still uses entry-cost equity/exposure.",
            "Return and sampled drawdown are not calculated for incomplete coverage or outcomes.",
            "Instantaneous full fills, stop-first intrabar ambiguity and no volume or settlement model.",
        ],
    }
    return {"evaluation_id": _digest(manifest), "manifest": manifest,
            "environment": {"python": platform.python_version()},
            "summary": summary, "coverage": coverage, "portfolio": result, "valuation": valuation}


def render_report(result: dict) -> str:
    """Human-readable companion; the full JSON retains all evidence and paths."""
    s = result["summary"]
    m = result["manifest"]
    lines = ["# ORB evaluation", "", f"Evaluation ID: {result['evaluation_id']}", "",
             f"Data label: {json.dumps(m['dataset']['label'])} (unverified)",
             f"Sessions: {m['config']['start_session']} to {m['config']['end_session']}",
             f"Dataset SHA-256: {m['dataset']['sha256']}",
             f"Engine SHA-256: {m['engine']['source_sha256']}", "",
             f"Complete within declared coverage: {s['complete']}",
             f"Incomplete symbol/sessions: {s['incomplete_symbol_sessions']} / {s['expected_symbol_sessions']}", "",
             "| Candidate status | Count |", "| --- | ---: |"]
    lines += [f"| {state} | {count} |" for state, count in s["trade_status_counts"].items()]
    lines += ["", f"Closed-trade net P&L only: {s['closed_subset']['net_pnl']}",
              f"Closed-trade win fraction: {s['closed_subset']['win_fraction']}",
              f"Ambiguous closed exits: {s['ambiguous_closed_exits']}",
              f"Total return: {s['total_return']} (not calculated when incomplete)",
              f"Minute-sampled maximum drawdown: {s['max_drawdown']}",
              f"Missing-mark samples: {result['valuation']['missing_mark_samples']}",
              "Valuation is reporting-only; sizing remains at entry cost.", "",
              "## Coverage", "", "| Session | Symbol | State | Missing / expected minutes |",
              "| --- | --- | --- | ---: |"]
    lines += [f"| {c['session']} | {c['symbol']} | {c['state']} | {c['missing_minutes']} / {c['expected_minutes']} |"
              for c in result["coverage"]]
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in s["limitations"]]
    return "\n".join(lines) + "\n"
