"""Pure opening-range calculation; no network, order or account side effects."""

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from orb.domain import Bar, OpeningRange

NEW_YORK = ZoneInfo("America/New_York")


def calculate_opening_range(
    bars: Iterable[Bar], symbol: str, session: date, available_at: datetime
) -> OpeningRange:
    """Calculate [09:30, 09:45) for an externally validated exchange session.

    available_at is the replay or real clock, preventing premature consumption.
    Holiday and early-close validation belongs to the future session service.
    Input may include other symbols and bars outside the range; they are ignored.
    Missing and duplicate range minutes are errors, never silently repaired.
    """
    if available_at.tzinfo is None or available_at.utcoffset() is None:
        raise ValueError("available_at must be timezone-aware")
    start = datetime.combine(session, time(9, 30), NEW_YORK)
    end = start + timedelta(minutes=15)
    if available_at < end:
        raise ValueError("opening range is not complete yet")
    selected = [b for b in bars if b.symbol == symbol and start <= b.start < end]
    actual = [b.start.astimezone(timezone.utc) for b in selected]
    expected = { (start + timedelta(minutes=i)).astimezone(timezone.utc) for i in range(15) }
    if len(actual) != 15 or set(actual) != expected:
        raise ValueError("opening range requires exactly 15 distinct consecutive minutes")
    high = max(b.high for b in selected)
    low = min(b.low for b in selected)
    if high == low:
        raise ValueError("zero-width opening range is not tradeable")
    return OpeningRange(symbol, start, end, high, low)
