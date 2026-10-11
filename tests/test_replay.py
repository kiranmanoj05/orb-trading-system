import csv
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
import subprocess
import sys

import pytest

from orb.domain import Bar
from orb.replay import CSV_FIELDS, load_bars, replay
from orb.strategy import NEW_YORK

START = datetime(2026, 10, 9, 9, 30, tzinfo=NEW_YORK)


def bars(symbol="SPY", start=START):
    rows = [Bar(symbol, start + timedelta(minutes=i), D("99"), D("100"),
                D("98"), D("99.5"), 100) for i in range(15)]
    rows.append(Bar(symbol, start + timedelta(minutes=15), D("99.5"),
                    D("101"), D("99"), D("100.20"), 200))
    return rows


def write_csv(path, rows):
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_FIELDS)
        for b in rows:
            writer.writerow([b.symbol, b.start.isoformat(), b.open, b.high, b.low, b.close, b.volume])


def test_hand_calculated_signal_and_completion_clock():
    result = replay(bars())
    assert result.bars_processed == 16
    assert len(result.signals) == 1
    signal = result.signals[0]
    assert (signal.entry_reference, signal.stop, signal.target) == (D("100.20"), D("99"), D("102.60"))
    assert signal.observed_at == datetime(2026, 10, 9, 13, 46, tzinfo=timezone.utc)
    assert result.sessions[0].state == "signalled"


def test_future_bars_cannot_change_frozen_range_or_create_second_signal():
    data = bars()
    data.append(replace(data[-1], start=data[-1].start + timedelta(minutes=1), high=D("1000")))
    result = replay(data)
    assert len(result.signals) == 1
    assert result.signals[0].stop == D("99")


def test_multiple_symbols_with_same_timestamps_are_independent():
    data = sorted(bars("SPY") + bars("QQQ"), key=lambda b: b.start)
    result = replay(data)
    assert {s.symbol for s in result.signals} == {"SPY", "QQQ"}
    assert len({s.signal_id for s in result.signals}) == 2


def test_session_attempt_resets_on_new_date():
    result = replay(bars() + bars(start=START + timedelta(days=3)))
    assert len(result.signals) == 2
    assert len({s.signal_id for s in result.signals}) == 2


def test_missing_range_minute_skips_session():
    data = bars()
    del data[5]
    result = replay(data)
    assert not result.signals
    assert result.sessions[0].state == "skipped"
    assert "15 distinct" in result.sessions[0].reason


def test_dataset_ending_before_range_completes_is_skipped():
    result = replay(bars()[:10])
    assert result.sessions[0].reason == "dataset ended before range completed"


def test_range_only_is_ready_not_claimed_full_session_without_trade():
    result = replay(bars()[:15])
    assert not result.signals
    assert result.sessions[0].state == "ready"


def test_missing_first_breakout_minute_pauses():
    data = bars()
    data[-1] = replace(data[-1], start=data[-1].start + timedelta(minutes=1))
    result = replay(data)
    assert not result.signals
    assert result.sessions[0].state == "paused"


def test_pre_and_postmarket_do_not_change_range():
    data = bars()
    extra = replace(data[0], high=D("1000"))
    result = replay([replace(extra, start=START - timedelta(minutes=1))] + data +
                    [replace(extra, start=START.replace(hour=16, minute=0))])
    assert result.signals[0].stop == D("99")


@pytest.mark.parametrize("change", ["duplicate", "unsorted", "empty"])
def test_ambiguous_or_empty_input_rejected(change):
    data = bars()
    if change == "duplicate":
        data.insert(1, data[0])
    elif change == "unsorted":
        data[0], data[1] = data[1], data[0]
    else:
        data = []
    with pytest.raises(ValueError):
        replay(data)


def test_csv_roundtrip_and_json_preserve_decimal_strings(tmp_path):
    path = tmp_path / "input.csv"
    write_csv(path, bars())
    assert load_bars(path) == bars()
    payload = json.loads(json.dumps(replay(load_bars(path)).to_dict()))
    assert payload["signals"][0]["entry_reference"] == "100.20"


@pytest.mark.parametrize("text", [
    "wrong,header\n", ",".join(CSV_FIELDS)+"\n",
    ",".join(CSV_FIELDS)+"\nSPY,2026-10-09T09:30:00,99,100,98,99,100\n",
    ",".join(CSV_FIELDS)+"\nSPY,2026-10-09T09:30:00-04:00,99,NaN,98,99,100\n",
    ",".join(CSV_FIELDS)+"\nSPY,2026-10-09T09:30:00-04:00,99,100,98,99,1.5\n",
])
def test_bad_csv_rejected(tmp_path, text):
    path = tmp_path / "bad.csv"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_bars(path)


def test_cli_replay_writes_report_without_orders(tmp_path):
    source = tmp_path / "input.csv"
    output = tmp_path / "results" / "signals.json"
    write_csv(source, bars())
    run = subprocess.run([sys.executable, "-m", "orb", "replay", str(source),
                          "--output", str(output)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(output.read_text())["sessions"][0]["state"] == "signalled"
    assert json.loads(run.stdout)["bars_processed"] == 16


def test_cli_cannot_overwrite_source(tmp_path):
    source = tmp_path / "input.csv"
    write_csv(source, bars())
    original = source.read_text()
    run = subprocess.run([sys.executable, "-m", "orb", "replay", str(source),
                          "--output", str(source)], capture_output=True, text=True)
    assert run.returncode == 2
    assert source.read_text() == original
