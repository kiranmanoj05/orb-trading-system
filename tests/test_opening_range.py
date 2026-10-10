from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from orb.domain import Bar
from orb.strategy import calculate_opening_range

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 9)
START = datetime(2026, 10, 9, 9, 30, tzinfo=NY)
READY = START + timedelta(minutes=15)


def fixture_bars(start=START):
    return [Bar("SPY", start + timedelta(minutes=i), D("99"), D("100"),
                D("98"), D("99.5"), 100) for i in range(15)]


def test_known_range_and_midpoint():
    result = calculate_opening_range(fixture_bars(), "SPY", DAY, READY)
    assert (result.high, result.low, result.midpoint) == (D("100"), D("98"), D("99"))


def test_0945_and_other_symbol_do_not_change_range():
    bars = fixture_bars()
    bars += [replace(bars[0], start=READY, high=D("200"))]
    bars += [replace(bars[0], symbol="QQQ", high=D("300"))]
    assert calculate_opening_range(bars, "SPY", DAY, READY).high == D("100")


@pytest.mark.parametrize("change", ["missing", "duplicate", "replacement_duplicate"])
def test_incomplete_or_duplicate_range_rejected(change):
    bars = fixture_bars()
    if change == "missing":
        bars.pop()
    elif change == "duplicate":
        bars.append(bars[0])
    else:
        bars[-1] = bars[0]
    with pytest.raises(ValueError, match="15 distinct"):
        calculate_opening_range(bars, "SPY", DAY, READY)


def test_range_cannot_be_used_before_last_bar_completes():
    with pytest.raises(ValueError, match="not complete"):
        calculate_opening_range(fixture_bars(), "SPY", DAY, READY - timedelta(seconds=1))


def test_utc_input_and_out_of_order_bars_produce_same_range():
    bars = [replace(b, start=b.start.astimezone(timezone.utc)) for b in fixture_bars()]
    assert calculate_opening_range(reversed(bars), "SPY", DAY, READY).midpoint == D("99")


@pytest.mark.parametrize("day,utc_hour", [(date(2026, 3, 6), 14), (date(2026, 3, 9), 13)])
def test_new_york_daylight_saving_changes_utc_boundary(day, utc_hour):
    start = datetime.combine(day, START.time(), NY)
    result = calculate_opening_range(fixture_bars(start), "SPY", day, start + timedelta(minutes=15))
    assert result.start.astimezone(timezone.utc).hour == utc_hour


@pytest.mark.parametrize("changes", [
    {"start": START.replace(tzinfo=None)}, {"start": START.replace(second=1)},
    {"high": D("98")}, {"low": D("100")}, {"close": D("NaN")},
    {"open": 99.0}, {"volume": -1}, {"volume": True}, {"symbol": "spy"},
])
def test_invalid_bar_rejected(changes):
    with pytest.raises(ValueError):
        replace(fixture_bars()[0], **changes)


def test_zero_width_range_rejected():
    bars = [replace(b, open=D("99"), high=D("99"), low=D("99"), close=D("99"))
            for b in fixture_bars()]
    with pytest.raises(ValueError, match="zero-width"):
        calculate_opening_range(bars, "SPY", DAY, READY)
