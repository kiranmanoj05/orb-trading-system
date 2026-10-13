"""Run a deterministic synthetic demonstration; no network or orders."""

import argparse
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from orb.breakout import BreakoutDetector
from orb.domain import Bar
from orb.strategy import NEW_YORK, calculate_opening_range
from orb.replay import load_bars, replay
from orb.session import exchange_session
from orb.simulation import ExecutionConfig, simulate
from orb.risk import AccountSnapshot, RiskLimits, size_long
from orb.account import ledger_demo
from orb.portfolio import replay_portfolio


def demo() -> None:
    start = datetime(2026, 10, 9, 9, 30, tzinfo=NEW_YORK)
    bars = [Bar("SPY", start + timedelta(minutes=i), Decimal("99"),
                Decimal("100"), Decimal("98"), Decimal("99.5"), 100)
            for i in range(15)]
    ready = start + timedelta(minutes=15)
    opening_range = calculate_opening_range(bars, "SPY", start.date(), ready)
    detector = BreakoutDetector(opening_range)
    breakout = Bar("SPY", ready, Decimal("99.5"), Decimal("100.5"),
                   Decimal("99.4"), Decimal("100.20"), 200)
    signal = detector.on_bar(breakout, ready + timedelta(minutes=1))
    assert signal is not None
    print("Synthetic ORB demonstration — no orders submitted")
    print(f"Range: {opening_range.low}–{opening_range.high}")
    print(f"Candidate: {signal.symbol} long at reference {signal.entry_reference}")
    print(f"Stop: {signal.stop}; planned target: {signal.target}")
    print(f"Signal ID: {signal.signal_id}; state: {detector.state.value}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline ORB signal research")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("demo", help="run the synthetic single-signal demonstration")
    commands.add_parser("account-demo", help="trace synthetic cash reservations and a full exit")
    sizing = commands.add_parser("size", help="size a hypothetical long from an account snapshot")
    for name in ("equity", "cash", "entry", "stop"):
        sizing.add_argument(f"--{name}", type=Decimal, required=True)
    for name, default in (("gross-exposure", "0"), ("risk-fraction", "0.005"),
                          ("max-position-fraction", "0.25"), ("max-gross-fraction", "1"),
                          ("slippage-bps", "0"), ("fee-per-share", "0")):
        sizing.add_argument(f"--{name}", type=Decimal, default=Decimal(default))
    schedule = commands.add_parser("session", help="inspect a US equity session date")
    schedule.add_argument("date", type=date.fromisoformat)
    run = commands.add_parser("replay", help="replay a canonical chronological CSV")
    run.add_argument("csv", type=Path)
    run.add_argument("--output", type=Path, help="write signal/session JSON")
    backtest = commands.add_parser("simulate", help="simulate fixed-share fills and exits")
    backtest.add_argument("csv", type=Path)
    backtest.add_argument("--output", type=Path)
    backtest.add_argument("--quantity", type=int, default=1)
    backtest.add_argument("--slippage-bps", type=Decimal, default=Decimal("0"))
    backtest.add_argument("--fee-per-share", type=Decimal, default=Decimal("0"))
    portfolio = commands.add_parser("portfolio", help="replay fills through one shared account")
    portfolio.add_argument("csv", type=Path)
    portfolio.add_argument("--output", type=Path)
    for name, default in (("initial-cash", "10000"), ("risk-fraction", "0.005"),
                          ("max-position-fraction", "0.25"), ("max-gross-fraction", "1"),
                          ("slippage-bps", "0"), ("fee-per-share", "0")):
        portfolio.add_argument(f"--{name}", type=Decimal, default=Decimal(default))
    args = parser.parse_args()
    if args.command == "account-demo":
        print(json.dumps(ledger_demo(), indent=2))
        return
    if args.command == "size":
        try:
            decision = size_long(AccountSnapshot(args.equity, args.cash, args.gross_exposure),
                                 args.entry, args.stop,
                                 RiskLimits(args.risk_fraction, args.max_position_fraction,
                                            args.max_gross_fraction),
                                 slippage_bps=args.slippage_bps, fee_per_share=args.fee_per_share)
            print(json.dumps({"model_version": "snapshot-long-v1",
                              "decision": decision.to_dict()}, indent=2))
        except ValueError as exc:
            parser.exit(2, f"Sizing error: {exc}\n")
        return
    if args.command == "session":
        try:
            session = exchange_session(args.date)
            payload = {"date": str(args.date), "state": "open" if session else "closed"}
            if session is not None:
                payload.update({name: getattr(session, name).isoformat() for name in
                                ("opens_at", "range_end", "entry_cutoff", "flatten_at", "closes_at")})
            print(json.dumps(payload, indent=2))
        except ValueError as exc:
            parser.exit(2, f"Session error: {exc}\n")
        return
    if args.command not in ("replay", "simulate", "portfolio"):
        demo()
        return
    try:
        bars = load_bars(args.csv)
        if args.command == "portfolio":
            result_dict = replay_portfolio(
                bars, args.initial_cash,
                RiskLimits(args.risk_fraction, args.max_position_fraction, args.max_gross_fraction),
                slippage_bps=args.slippage_bps, fee_per_share=args.fee_per_share)
        else:
            result_dict = (simulate(bars, ExecutionConfig(args.quantity, args.slippage_bps, args.fee_per_share))
                  if args.command == "simulate" else replay(bars)).to_dict()
        payload = json.dumps(result_dict, indent=2) + "\n"
        if args.output:
            if args.output.resolve() == args.csv.resolve():
                raise ValueError("output must not overwrite input CSV")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        print(payload, end="")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Replay error: {exc}\n")


if __name__ == "__main__":
    main()
