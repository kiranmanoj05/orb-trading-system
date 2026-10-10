"""Per-session ORB candidates from completed, consecutive one-minute bars."""

from datetime import datetime, time, timedelta, timezone
from enum import Enum
from hashlib import sha256

from orb.domain import Bar, BreakoutSignal, OpeningRange
from orb.strategy import NEW_YORK


class SignalState(str, Enum):
    READY = "ready"
    SIGNALLED = "signalled"
    EXPIRED = "expired"
    PAUSED = "paused"


class BreakoutDetector:
    """Single-symbol, single-session, in-memory detector; use one event worker.

    The first candidate consumes this detector's daily attempt even if a future
    risk engine rejects it. Persistent reservations and restart recovery are
    intentionally separate future components.
    """

    VERSION = "orb-long-v1"

    def __init__(self, opening_range: OpeningRange) -> None:
        local_start = opening_range.start.astimezone(NEW_YORK)
        local_end = opening_range.end.astimezone(NEW_YORK)
        if (local_start.time() != time(9, 30)
                or local_end.date() != local_start.date()
                or local_end.time() != time(9, 45)):
            raise ValueError("detector requires the 09:30–09:45 New York range")
        self.opening_range = opening_range
        self.cutoff = datetime.combine(local_start.date(), time(11), NEW_YORK)
        self.state = SignalState.READY
        self.pause_reason: str | None = None
        self._last_bar: Bar | None = None

    def _pause(self, reason: str) -> None:
        self.pause_reason = reason
        self.state = SignalState.PAUSED

    def on_bar(self, bar: Bar, observed_at: datetime) -> BreakoutSignal | None:
        """Reject incomplete input; emit at most one candidate before cutoff.

        Identical repeats of the latest consumed bar are harmless. Corrections,
        older deliveries and missing eligible minutes pause a READY detector.
        Staleness tolerance is a provisional operational default of 90 seconds
        after bar completion. Recreate only after an explicit recovery decision.
        """
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if bar.symbol != self.opening_range.symbol:
            raise ValueError("bar symbol does not match detector")
        if bar.start.astimezone(NEW_YORK).date() != self.cutoff.date():
            raise ValueError("bar must belong to the detector session")
        completed_at = bar.start + timedelta(minutes=1)
        if observed_at < completed_at:
            raise ValueError("bar is not complete yet")
        if self.state != SignalState.READY:
            return None
        if self._last_bar is not None and bar.start == self._last_bar.start:
            if bar != self._last_bar:
                self._pause("conflicting_bar")
            return None
        if bar.start < self.opening_range.end:
            return None
        if observed_at >= self.cutoff:
            self.state = SignalState.EXPIRED
            return None
        if observed_at - completed_at > timedelta(seconds=90):
            self._pause("stale_bar")
            return None
        expected = (self.opening_range.end if self._last_bar is None
                    else self._last_bar.start + timedelta(minutes=1))
        if bar.start != expected:
            self._pause("missing_or_out_of_order_bar")
            return None
        self._last_bar = bar
        if bar.close <= self.opening_range.high:
            return None
        stop = self.opening_range.midpoint
        # Stable identity for the single permitted session attempt, not a fill ID.
        identity = f"{self.VERSION}|{bar.symbol}|{self.cutoff.date()}|1"
        signal = BreakoutSignal(
            signal_id=sha256(identity.encode()).hexdigest()[:32],
            strategy_version=self.VERSION,
            symbol=bar.symbol,
            bar_start=bar.start.astimezone(timezone.utc),
            observed_at=observed_at.astimezone(timezone.utc),
            entry_reference=bar.close,
            stop=stop,
            target=bar.close + 2 * (bar.close - stop),
        )
        self.state = SignalState.SIGNALLED
        return signal
