"""Offline chronological signal replay; no fills, returns or broker calls."""

import csv
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

from orb.breakout import BreakoutDetector
from orb.domain import Bar, BreakoutSignal
from orb.strategy import NEW_YORK, calculate_opening_range
from orb.session import exchange_session

CSV_FIELDS = ("symbol", "start", "open", "high", "low", "close", "volume")


def load_bars(path: Path) -> list[Bar]:
    """Parse canonical CSV with row-specific errors; never repair bad prices."""
    bars = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(CSV_FIELDS):
            raise ValueError("CSV header must be: " + ",".join(CSV_FIELDS))
        for line, row in enumerate(reader, 2):
            try:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError("unexpected column count")
                bars.append(Bar(
                    row["symbol"], datetime.fromisoformat(row["start"]),
                    *(Decimal(row[name]) for name in ("open", "high", "low", "close")),
                    int(row["volume"]),
                ))
            except (ValueError, InvalidOperation, TypeError) as exc:
                raise ValueError(f"CSV row {line}: {exc}") from exc
    if not bars:
        raise ValueError("CSV contains no bars")
    return bars


@dataclass(frozen=True)
class SessionOutcome:
    symbol: str
    session: date
    state: str
    reason: str | None
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    flatten_at: datetime | None = None


@dataclass(frozen=True)
class ReplayResult:
    bars_processed: int
    signals: tuple[BreakoutSignal, ...]
    sessions: tuple[SessionOutcome, ...]

    def to_dict(self) -> dict:
        """JSON-safe values preserve decimal prices as strings."""
        def convert(value):
            if isinstance(value, (datetime, date)):
                return value.isoformat()
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [convert(item) for item in value]
            return value
        return convert(asdict(self))


@dataclass
class _Session:
    range_bars: list[Bar] = field(default_factory=list)
    detector: BreakoutDetector | None = None
    skip_reason: str | None = None


def replay(bars: Iterable[Bar]) -> ReplayResult:
    """Consume bars by nondecreasing timestamp, advancing at bar completion.

    Equal timestamps for different symbols are permitted. Duplicate symbol/time
    keys are rejected, including conflicting revisions. No hidden sort is done.
    Exchange-day and early-close validation use the versioned XNYS calendar.
    Ending a dataset while READY means incomplete observation, not a full day
    without a signal. Only sessions with regular-session observations are listed.
    """
    sessions: dict[tuple[str, date], _Session] = {}
    closed_dates: set[tuple[str, date]] = set()
    signals = []
    seen = set()
    previous: datetime | None = None
    count = 0
    for bar in bars:
        stamp = bar.start.astimezone(timezone.utc)
        if previous is not None and stamp < previous:
            raise ValueError("replay input must be chronological")
        key = (bar.symbol, stamp)
        if key in seen:
            raise ValueError("duplicate symbol/timestamp in replay input")
        seen.add(key)
        previous = stamp
        count += 1
        local = bar.start.astimezone(NEW_YORK)
        day = local.date()
        schedule = exchange_session(day)
        if schedule is None:
            closed_dates.add((bar.symbol, day))
            continue
        if not schedule.contains_bar(bar.start):
            continue
        state = sessions.setdefault((bar.symbol, day), _Session())
        observed_at = stamp + timedelta(minutes=1)
        range_end = schedule.range_end
        if local < range_end:
            state.range_bars.append(bar)
        # Freeze at the moment the 09:44 bar completes, or skip when a later
        # observation reveals an incomplete range. Never inspect future bars.
        if state.detector is None and state.skip_reason is None and observed_at >= range_end:
            try:
                opening_range = calculate_opening_range(
                    state.range_bars, bar.symbol, day, observed_at)
            except ValueError as exc:
                state.skip_reason = str(exc)
            else:
                state.detector = BreakoutDetector(opening_range, schedule)
        if state.detector is not None and local >= range_end:
            signal = state.detector.on_bar(bar, observed_at)
            if signal is not None:
                signals.append(signal)
    if not count:
        raise ValueError("replay input contains no bars")
    outcomes = []
    for (symbol, day), state in sorted(sessions.items(), key=lambda x: (x[0][1], x[0][0])):
        if state.detector is not None:
            status = state.detector.state.value
            reason = state.detector.pause_reason
        else:
            status = "skipped"
            reason = state.skip_reason or "dataset ended before range completed"
        schedule = exchange_session(day)
        assert schedule is not None
        outcomes.append(SessionOutcome(symbol, day, status, reason, schedule.opens_at,
                                       schedule.closes_at, schedule.flatten_at))
    outcomes.extend(SessionOutcome(symbol, day, "closed", "not_exchange_session")
                    for symbol, day in closed_dates)
    outcomes.sort(key=lambda row: (row.session, row.symbol))
    return ReplayResult(count, tuple(signals), tuple(outcomes))
