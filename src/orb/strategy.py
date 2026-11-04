"""Pure opening-range calculation; no network, order or account side effects."""

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from orb.domain import Bar, OpeningRange

NEW_YORK = ZoneInfo("America/New_York")


def calculate_opening_range(
    bars: Iterable[Bar], symbol: str, session: date, available_at: datetime, *, range_minutes: int = 15
) -> OpeningRange:
    """Calculate a validated 5/15/30-minute range from 09:30 New York.

    available_at is the replay or real clock, preventing premature consumption.
    Holiday and early-close validation belongs to the exchange session service.
    Input may include other symbols and bars outside the range; they are ignored.
    Missing and duplicate range minutes are errors, never silently repaired.
    """
    if available_at.tzinfo is None or available_at.utcoffset() is None:
        raise ValueError("available_at must be timezone-aware")
    if type(range_minutes) is not int or range_minutes not in (5, 15, 30):
        raise ValueError('supported opening ranges are 5, 15 or 30 minutes')
    start = datetime.combine(session, time(9, 30), NEW_YORK)
    end = start + timedelta(minutes=range_minutes)
    if available_at < end:
        raise ValueError("opening range is not complete yet")
    selected = [b for b in bars if b.symbol == symbol and start <= b.start < end]
    actual = [b.start.astimezone(timezone.utc) for b in selected]
    expected = { (start + timedelta(minutes=i)).astimezone(timezone.utc) for i in range(range_minutes) }
    if len(actual) != range_minutes or set(actual) != expected:
        raise ValueError(f"opening range requires exactly {range_minutes} distinct consecutive minutes")
    high = max(b.high for b in selected)
    low = min(b.low for b in selected)
    if high == low:
        raise ValueError("zero-width opening range is not tradeable")
    return OpeningRange(symbol, start, end, high, low)
