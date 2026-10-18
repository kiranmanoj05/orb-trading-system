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
from orb.evaluation import evaluate, render_report
from orb.ingestion import evaluate_bundle, import_capture


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
    ingest = commands.add_parser("import-alpaca", help="validate saved response pages and archive a new bundle")
    ingest.add_argument("capture", type=Path)
    ingest.add_argument("--out-dir", type=Path, required=True)
    sizing = commands.add_parser("size", help="size a hypothetical long from an account snapshot")
    for name in ("equity", "cash", "entry", "stop"):
        sizing.add_argument(f"--{name}", type=Decimal, required=True)
    for name, default in (("gross-exposure", "0"), ("risk-fraction", "0.005"),
                          ("max-position-fraction", "0.25"), ("max-gross-fraction", "1"),
                          ("max-total-risk-fraction", "0.01"), ("committed-stop-risk", "0"),
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
    for command in ("portfolio", "evaluate", "evaluate-bundle"):
        portfolio = commands.add_parser(command, help="shared account replay or reproducible evaluation")
        portfolio.add_argument("csv", type=Path,
                               metavar="BUNDLE" if command == "evaluate-bundle" else "CSV")
        portfolio.add_argument("--output", type=Path)
        portfolio.add_argument("--daily-loss-fraction", type=Decimal, default=Decimal("0.02"))
        if command in ("evaluate", "evaluate-bundle"):
            portfolio.add_argument("--report", type=Path, help="write Markdown companion")
            portfolio.add_argument("--data-label", default="unspecified")
            portfolio.add_argument("--start-session", type=date.fromisoformat)
            portfolio.add_argument("--end-session", type=date.fromisoformat)
        for name, default in (("initial-cash", "10000"), ("risk-fraction", "0.005"),
                          ("max-position-fraction", "0.25"), ("max-gross-fraction", "1"),
                          ("max-total-risk-fraction", "0.01"),
                          ("slippage-bps", "0"), ("fee-per-share", "0")):
            portfolio.add_argument(f"--{name}", type=Decimal, default=Decimal(default))
    args = parser.parse_args()
    if args.command == "import-alpaca":
        try:
            print(json.dumps(import_capture(args.capture, args.out_dir), indent=2))
        except (ValueError, OSError) as exc:
            parser.exit(2, f"Import error: {exc}\n")
        return
    if args.command == "account-demo":
        print(json.dumps(ledger_demo(), indent=2))
        return
    if args.command == "size":
        try:
            decision = size_long(AccountSnapshot(args.equity, args.cash, args.gross_exposure,
                                                args.committed_stop_risk),
                                 args.entry, args.stop,
                                 RiskLimits(args.risk_fraction, args.max_position_fraction,
                                            args.max_gross_fraction, args.max_total_risk_fraction),
                                 slippage_bps=args.slippage_bps, fee_per_share=args.fee_per_share)
            print(json.dumps({"model_version": "snapshot-long-v2",
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
    if args.command not in ("replay", "simulate", "portfolio", "evaluate", "evaluate-bundle"):
        demo()
        return
    try:
        bars = args.csv if args.command == "evaluate-bundle" else load_bars(args.csv)
        if args.command in ("portfolio", "evaluate", "evaluate-bundle"):
            extra = ({"data_label": args.data_label, "start_session": args.start_session,
                      "end_session": args.end_session} if args.command in ("evaluate", "evaluate-bundle") else {})
            engine = {"evaluate": evaluate, "evaluate-bundle": evaluate_bundle,
                      "portfolio": replay_portfolio}[args.command]
            result_dict = engine(
                bars, args.initial_cash,
                RiskLimits(args.risk_fraction, args.max_position_fraction, args.max_gross_fraction,
                           args.max_total_risk_fraction),
                slippage_bps=args.slippage_bps, fee_per_share=args.fee_per_share,
                daily_loss_fraction=args.daily_loss_fraction, **extra)
        else:
            result_dict = (simulate(bars, ExecutionConfig(args.quantity, args.slippage_bps, args.fee_per_share))
                  if args.command == "simulate" else replay(bars)).to_dict()
        payload = json.dumps(result_dict, indent=2) + "\n"
        report_path = args.report if args.command in ("evaluate", "evaluate-bundle") else None
        destinations = [p.resolve() for p in (args.output, report_path) if p is not None]
        if args.csv.resolve() in destinations:
            raise ValueError("output must not overwrite input CSV")
        if args.command == "evaluate-bundle" and any(args.csv.resolve() in p.parents for p in destinations):
            raise ValueError("outputs must remain outside the source bundle")
        if len(set(destinations)) != len(destinations):
            raise ValueError("JSON output and report must use different paths")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(render_report(result_dict), encoding="utf-8")
        print(payload, end="")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Replay error: {exc}\n")


if __name__ == "__main__":
    main()
