"""Run a deterministic synthetic demonstration; no network or orders."""

from datetime import datetime, timedelta
from decimal import Decimal

from orb.breakout import BreakoutDetector
from orb.domain import Bar
from orb.strategy import NEW_YORK, calculate_opening_range


def main() -> None:
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


if __name__ == "__main__":
    main()
