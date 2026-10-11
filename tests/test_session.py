from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import subprocess
import sys

import pytest

from orb.breakout import BreakoutDetector
from orb.domain import OpeningRange
from orb.replay import replay
from orb.session import exchange_session
from orb.strategy import NEW_YORK
from test_replay import bars


@pytest.mark.parametrize("day", [date(2026, 10, 10), date(2026, 10, 11),
    date(2026, 11, 26), date(2026, 12, 25), date(2026, 7, 3)])
def test_weekends_and_exchange_holidays_are_closed(day):
    assert exchange_session(day) is None


def test_normal_session_and_flatten_plan():
    s = exchange_session(date(2026, 10, 9))
    assert s.opens_at.astimezone(NEW_YORK).hour == 9
    assert s.closes_at.astimezone(NEW_YORK).hour == 16
    assert (s.flatten_at.astimezone(NEW_YORK).hour, s.flatten_at.minute) == (15, 55)


def test_day_after_thanksgiving_closes_at_1300_and_flattens_at_1255():
    s = exchange_session(date(2026, 11, 27))
    assert s.closes_at == datetime.fromisoformat("2026-11-27T18:00:00+00:00")
    assert (s.flatten_at.astimezone(NEW_YORK).hour, s.flatten_at.minute) == (12, 55)


@pytest.mark.parametrize("day,utc_hour", [(date(2026, 3, 6), 14), (date(2026, 3, 9), 13)])
def test_open_tracks_dst_without_london_offset(day, utc_hour):
    assert exchange_session(day).opens_at.hour == utc_hour


def test_half_open_session_only_includes_completed_trading_minutes():
    s = exchange_session(date(2026, 11, 27))
    assert s.contains_bar(s.opens_at)
    assert s.contains_bar(s.closes_at - timedelta(minutes=1))
    assert not s.contains_bar(s.closes_at)
    assert not s.contains_bar(s.opens_at - timedelta(minutes=1))
    with pytest.raises(ValueError, match="timezone"):
        s.contains_bar(s.opens_at.replace(tzinfo=None))


def test_weekday_is_not_enough_but_columbus_day_equities_are_open():
    assert exchange_session(date(2026, 10, 12)) is not None
    assert exchange_session(date(2026, 11, 26)) is None


def test_recorded_unplanned_historical_closure():
    assert exchange_session(date(2012, 10, 29)) is None


@pytest.mark.parametrize("day", [date(2005, 12, 31), date(2031, 1, 1)])
def test_outside_supported_horizon_fails(day):
    with pytest.raises(ValueError, match="calendar date"):
        exchange_session(day)


def test_closed_day_bars_cannot_create_signal():
    result = replay(bars(start=datetime(2026, 11, 26, 9, 30, tzinfo=NEW_YORK)))
    assert not result.signals
    assert result.sessions[0].state == "closed"
    assert result.sessions[0].reason == "not_exchange_session"


def test_early_close_excludes_after_close_data_and_exposes_schedule():
    start = datetime(2026, 11, 27, 9, 30, tzinfo=NEW_YORK)
    data = bars(start=start)[:15]
    data.append(replace(bars(start=start)[-1], start=start.replace(hour=13, minute=0)))
    result = replay(data)
    assert not result.signals
    s = result.sessions[0]
    assert s.state == "ready"
    assert s.closes_at == datetime.fromisoformat("2026-11-27T18:00:00+00:00")
    assert s.flatten_at == datetime.fromisoformat("2026-11-27T17:55:00+00:00")


def test_calendar_detector_rejects_mismatched_range():
    from decimal import Decimal
    s = exchange_session(date(2026, 10, 9))
    wrong = OpeningRange("SPY", s.opens_at + timedelta(days=3),
                         s.range_end + timedelta(days=3), Decimal("100"), Decimal("98"))
    with pytest.raises(ValueError, match="match exchange session"):
        BreakoutDetector(wrong, s)


def test_calendar_cli_reports_early_close():
    run = subprocess.run([sys.executable, "-m", "orb", "session", "2026-11-27"],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["closes_at"] == "2026-11-27T18:00:00+00:00"
