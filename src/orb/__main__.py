"""Run a deterministic synthetic demonstration; no network or orders."""

import argparse
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from orb.breakout import BreakoutDetector
from orb.domain import Bar
from orb.strategy import NEW_YORK, calculate_opening_range
from orb.replay import load_bars, replay


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
    run = commands.add_parser("replay", help="replay a canonical chronological CSV")
    run.add_argument("csv", type=Path)
    run.add_argument("--output", type=Path, help="write signal/session JSON")
    args = parser.parse_args()
    if args.command != "replay":
        demo()
        return
    try:
        result = replay(load_bars(args.csv))
        payload = json.dumps(result.to_dict(), indent=2) + "\n"
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
