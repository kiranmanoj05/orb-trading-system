from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from orb.breakout import BreakoutDetector, SignalState
from orb.domain import Bar, OpeningRange
from orb.strategy import NEW_YORK

START = datetime(2026, 10, 9, 9, 30, tzinfo=NEW_YORK)
END = START + timedelta(minutes=15)


def detector():
    return BreakoutDetector(OpeningRange("SPY", START, END, D("100"), D("98")))


def bar(start=END, close="100.20"):
    return Bar("SPY", start, D("99"), D("101"), D("98"), D(close), 100)


def test_first_closed_breakout_has_planned_levels_and_stable_identity():
    d = detector()
    signal = d.on_bar(bar(), END + timedelta(minutes=1))
    assert signal is not None
    assert (signal.entry_reference, signal.stop, signal.target) == (D("100.20"), D("99"), D("102.60"))
    assert signal.observed_at == datetime(2026, 10, 9, 13, 46, tzinfo=timezone.utc)
    assert d.state == SignalState.SIGNALLED
    assert detector().on_bar(bar(), END + timedelta(minutes=1)).signal_id == signal.signal_id


@pytest.mark.parametrize("close", ["99.90", "100"])
def test_close_must_be_strictly_above_high_even_when_intrabar_high_crossed(close):
    d = detector()
    assert d.on_bar(bar(close=close), END + timedelta(minutes=1)) is None
    assert d.state == SignalState.READY


def test_breakout_after_one_nonbreaking_bar():
    d = detector()
    assert d.on_bar(bar(close="100"), END + timedelta(minutes=1)) is None
    assert d.on_bar(bar(END + timedelta(minutes=1)), END + timedelta(minutes=2)) is not None


def test_signal_consumes_attempt_and_repeats_do_not_emit():
    d = detector()
    assert d.on_bar(bar(), END + timedelta(minutes=1)) is not None
    assert d.on_bar(bar(), END + timedelta(minutes=1)) is None
    assert d.on_bar(bar(END + timedelta(minutes=1)), END + timedelta(minutes=2)) is None


def test_identical_repeat_before_signal_is_harmless():
    d = detector()
    first = bar(close="100")
    d.on_bar(first, END + timedelta(minutes=1))
    assert d.on_bar(first, END + timedelta(minutes=1, seconds=5)) is None
    assert d.state == SignalState.READY


def test_corrected_consumed_bar_pauses_instead_of_rewriting_decision():
    d = detector()
    d.on_bar(bar(close="100"), END + timedelta(minutes=1))
    assert d.on_bar(bar(), END + timedelta(minutes=1)) is None
    assert d.pause_reason == "conflicting_bar"
    assert d.state == SignalState.PAUSED


def test_unfinished_bar_rejected_without_consuming_attempt():
    d = detector()
    with pytest.raises(ValueError, match="not complete"):
        d.on_bar(bar(), END + timedelta(seconds=59))
    assert d.state == SignalState.READY
    assert d.on_bar(bar(), END + timedelta(minutes=1)) is not None


def test_range_bar_cannot_generate_candidate():
    d = detector()
    assert d.on_bar(bar(END - timedelta(minutes=1)), END) is None
    assert d.state == SignalState.READY


@pytest.mark.parametrize("offset", [1, 2])
def test_missing_first_or_later_eligible_bar_pauses(offset):
    d = detector()
    if offset == 2:
        d.on_bar(bar(close="100"), END + timedelta(minutes=1))
    late = END + timedelta(minutes=offset)
    assert d.on_bar(bar(late), late + timedelta(minutes=1)) is None
    assert d.state == SignalState.PAUSED
    assert d.pause_reason == "missing_or_out_of_order_bar"


def test_older_delivery_pauses():
    d = detector()
    d.on_bar(bar(close="100"), END + timedelta(minutes=1))
    d.on_bar(bar(END + timedelta(minutes=1), "100"), END + timedelta(minutes=2))
    assert d.on_bar(bar(close="100"), END + timedelta(minutes=2)) is None
    assert d.state == SignalState.PAUSED


@pytest.mark.parametrize("seconds,state", [(90, SignalState.SIGNALLED), (91, SignalState.PAUSED)])
def test_freshness_boundary(seconds, state):
    d = detector()
    d.on_bar(bar(), END + timedelta(minutes=1, seconds=seconds))
    assert d.state == state


@pytest.mark.parametrize("delay", [0, 1])
def test_signal_observed_at_or_after_cutoff_expires(delay):
    d = detector()
    for i in range(74):
        b = bar(END + timedelta(minutes=i), "100")
        d.on_bar(b, b.start + timedelta(minutes=1))
    final = bar(datetime(2026, 10, 9, 10, 59, tzinfo=NEW_YORK))
    assert d.on_bar(final, d.cutoff + timedelta(seconds=delay)) is None
    assert d.state == SignalState.EXPIRED


def test_latest_valid_bar_completes_at_1059():
    d = detector()
    for i in range(73):
        b = bar(END + timedelta(minutes=i), "100")
        d.on_bar(b, b.start + timedelta(minutes=1))
    last = bar(datetime(2026, 10, 9, 10, 58, tzinfo=NEW_YORK))
    assert d.on_bar(last, last.start + timedelta(minutes=1)) is not None


@pytest.mark.parametrize("kind", ["symbol", "session", "clock"])
def test_mismatched_input_rejected(kind):
    d = detector()
    b = bar()
    now = END + timedelta(minutes=1)
    if kind == "symbol":
        b = replace(b, symbol="QQQ")
    elif kind == "session":
        b = replace(b, start=b.start + timedelta(days=1))
        now += timedelta(days=1)
    else:
        now = now.replace(tzinfo=None)
    with pytest.raises(ValueError):
        d.on_bar(b, now)


def test_utc_bars_are_equivalent():
    d = detector()
    b = replace(bar(), start=END.astimezone(timezone.utc))
    assert d.on_bar(b, (END + timedelta(minutes=1)).astimezone(timezone.utc)) is not None


@pytest.mark.parametrize("changes", [
    {"high": D("NaN")}, {"low": D("100")},
    {"start": START.replace(tzinfo=None)}, {"end": START},
])
def test_invalid_range_rejected(changes):
    with pytest.raises(ValueError):
        replace(detector().opening_range, **changes)


def test_detector_rejects_nonbaseline_range_window():
    with pytest.raises(ValueError, match="09:30"):
        BreakoutDetector(replace(detector().opening_range, end=END + timedelta(minutes=1)))
