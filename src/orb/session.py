"""Regular US equity sessions from a versioned local exchange calendar."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache

import exchange_calendars as xcals

from orb.strategy import NEW_YORK

CALENDAR_START = date(2006, 1, 1)
CALENDAR_END = date(2030, 12, 31)


@dataclass(frozen=True)
class ExchangeSession:
    day: date
    opens_at: datetime
    closes_at: datetime

    @property
    def range_end(self) -> datetime:
        return self.opens_at + timedelta(minutes=15)

    @property
    def flatten_at(self) -> datetime:
        """Planned flatten deadline; no position closing is implemented yet."""
        return self.closes_at - timedelta(minutes=5)

    @property
    def entry_cutoff(self) -> datetime:
        return min(datetime.combine(self.day, time(11), NEW_YORK), self.flatten_at).astimezone(timezone.utc)

    def contains_bar(self, start: datetime) -> bool:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("bar start must be timezone-aware")
        return self.opens_at <= start and start + timedelta(minutes=1) <= self.closes_at


@lru_cache(maxsize=1)
def _calendar():
    return xcals.get_calendar("XNYS", start=str(CALENDAR_START), end=str(CALENDAR_END))


@lru_cache(maxsize=1024)
def exchange_session(day: date) -> ExchangeSession | None:
    """Use XNYS as the baseline US equity regular-session schedule.

    Closed dates return None. Out-of-scope dates fail explicitly rather than
    guessing weekday hours. This is a local schedule, not a live halt feed.
    """
    if not CALENDAR_START <= day <= CALENDAR_END:
        raise ValueError(f"calendar date must be within {CALENDAR_START}–{CALENDAR_END}")
    calendar = _calendar()
    label = day.isoformat()
    if not calendar.is_session(label):
        return None
    result = ExchangeSession(day, calendar.session_open(label).to_pydatetime(),
                             calendar.session_close(label).to_pydatetime())
    if result.opens_at.astimezone(NEW_YORK).time() != time(9, 30):
        raise ValueError("baseline ORB requires a 09:30 New York session open")
    if result.range_end >= result.entry_cutoff:
        raise ValueError("session is too short for baseline ORB")
    return result
