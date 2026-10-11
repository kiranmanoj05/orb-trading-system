from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from orb.domain import Bar
from orb.simulation import ExecutionConfig, simulate
from orb.strategy import NEW_YORK


def scenario(day=(2026, 10, 9), minute=46, prices=("100.30", "101", "100", "100.5")):
    start = datetime(*day, 9, 30, tzinfo=NEW_YORK)
    bars = [Bar("SPY", start + timedelta(minutes=i), D("99"), D("100"), D("98"), D("99.5"), 100)
            for i in range(15)]
    bars.append(Bar("SPY", start + timedelta(minutes=15), D("99.5"), D("100.5"), D("99.4"), D("100.20"), 200))
    bars.append(Bar("SPY", start.replace(minute=minute), *map(D, prices), 200))
    return bars


def following(bars, prices, gap=1):
    return Bar("SPY", bars[-1].start + timedelta(minutes=gap), *map(D, prices), 100)


def test_actual_entry_reprices_target_and_end_is_unresolved():
    trade = simulate(scenario()).trades[0]
    assert trade.entry_price == D("100.30")
    assert trade.target == D("102.90")
    assert trade.entry_at.minute == 46
    assert trade.status == "unresolved"
    assert trade.net_pnl is None


@pytest.mark.parametrize("prices,reason,exit_price,ambiguous", [
    (("100.30", "103", "100", "102"), "target", "102.90", False),
    (("100.30", "101", "98", "100"), "stop", "99", False),
    (("100.30", "103", "98", "100"), "stop", "99", True),
])
def test_entry_bar_exits_and_ambiguity(prices, reason, exit_price, ambiguous):
    trade = simulate(scenario(prices=prices)).trades[0]
    assert (trade.reason, trade.exit_price, trade.ambiguous_exit) == (reason, D(exit_price), ambiguous)
    assert trade.exit_at.minute == 47  # Intrabar event only observed at completion.


@pytest.mark.parametrize("prices,reason,price", [
    (("98", "100", "97", "99"), "stop_gap", "98"),
    (("104", "105", "98", "100"), "target_gap", "104"),
])
def test_open_gaps_take_precedence_over_intrabar_prices(prices, reason, price):
    bars = scenario()
    bars.append(following(bars, prices))
    trade = simulate(bars).trades[0]
    assert (trade.reason, trade.exit_price) == (reason, D(price))
    assert trade.exit_at.minute == 47


def test_costs_and_quantity_are_hand_calculated():
    trade = simulate(scenario(prices=("100.30", "101", "98", "100")),
                     ExecutionConfig(10, D("10"), D("0.01"))).trades[0]
    assert trade.entry_price == D("100.40030")
    assert trade.exit_price == D("98.901")
    assert trade.gross_pnl == D("-14.99300")
    assert trade.fees == D("0.20")
    assert trade.net_pnl == D("-15.19300")


def test_limit_target_has_no_adverse_slippage():
    trade = simulate(scenario(prices=("100.30", "104", "100", "103")),
                     ExecutionConfig(slippage_bps=D("10"))).trades[0]
    assert trade.exit_price == trade.target == D("103.20090")


@pytest.mark.parametrize("opening", ["99", "98"])
def test_entry_at_or_below_stop_is_unfilled(opening):
    trade = simulate(scenario(prices=(opening, "101", "98", "100"))).trades[0]
    assert trade.status == "unfilled"
    assert trade.reason == "entry_at_or_below_stop"


def test_missing_entry_and_no_future_bar_are_distinct():
    assert simulate(scenario(minute=47)).trades[0].reason == "missing_entry_bar"
    assert simulate(scenario()[:-1]).trades[0].reason == "dataset_ended_before_entry"


def test_position_gap_is_unresolved_even_if_later_target_touched():
    bars = scenario()
    bars.append(following(bars, ("104", "105", "103", "104"), gap=2))
    trade = simulate(bars).trades[0]
    assert trade.reason == "missing_position_bar"
    assert trade.net_pnl is None


@pytest.mark.parametrize("day,hour,minute", [((2026, 10, 9), 15, 55), ((2026, 11, 27), 12, 55)])
def test_normal_and_early_session_flatten_at_open(day, hour, minute):
    bars = scenario(day=day)
    deadline = bars[-1].start.replace(hour=hour, minute=minute)
    while bars[-1].start < deadline:
        bars.append(following(bars, ("100", "101", "99.5", "100")))
    bars[-1] = replace(bars[-1], high=D("104"), low=D("98"))
    trade = simulate(bars).trades[0]
    assert trade.reason == "session_flatten"
    assert trade.exit_price == D("100")
    assert trade.exit_at == deadline


def test_missing_flatten_bar_does_not_invent_close():
    bars = scenario()
    bars.append(replace(bars[-1], start=bars[-1].start.replace(hour=15, minute=55)))
    assert simulate(bars).trades[0].status == "unresolved"


def test_multiple_symbols_and_dates_do_not_share_position_state():
    first = scenario(prices=("100.3", "104", "100", "103"))
    second = [replace(b, symbol="QQQ") for b in scenario()]
    third = scenario(day=(2026, 10, 12), prices=("100.3", "101", "98", "100"))
    result = simulate(sorted(first + second + third, key=lambda b: b.start))
    assert [(t.symbol, t.status) for t in result.trades] == [
        ("SPY", "closed"), ("QQQ", "unresolved"), ("SPY", "closed")]


@pytest.mark.parametrize("config", [
    {"quantity": 0}, {"quantity": True}, {"quantity": 1.5},
    {"slippage_bps": D("NaN")}, {"slippage_bps": D("10000")},
    {"slippage_bps": D("-1")}, {"fee_per_share": D("Infinity")},
    {"fee_per_share": D("-1")}, {"fee_per_share": 0.1},
])
def test_invalid_config_rejected(config):
    with pytest.raises(ValueError):
        ExecutionConfig(**config)


def test_input_contract_and_signal_prefix_are_preserved():
    bars = scenario()
    with pytest.raises(ValueError, match="chronological"):
        simulate(reversed(bars))
    with pytest.raises(ValueError, match="duplicate"):
        simulate(bars + [bars[-1]])
    before = simulate(bars).replay_result.signals
    bars.append(following(bars, ("104", "105", "103", "104")))
    assert simulate(bars).replay_result.signals == before
    payload = simulate(bars).to_dict()
    assert payload["trades"][0]["entry_price"] == "100.30"
    assert payload["config"]["slippage_bps"] == "0"


def test_no_candidate_means_no_attempt():
    assert simulate(scenario()[:15]).trades == ()


def test_last_candidate_can_fill_at_1059_but_not_at_1100():
    bars = scenario()[:15]
    while bars[-1].start.minute != 57 or bars[-1].start.hour != 10:
        bars.append(following(bars, ("99.5", "100", "99.4", "99.5")))
    bars.append(following(bars, ("99.5", "101", "99.4", "100.20")))
    bars.append(following(bars, ("100.3", "103", "100", "102.9")))
    trade = simulate(bars).trades[0]
    assert trade.entry_at.hour == 10 and trade.entry_at.minute == 59
    assert trade.status == "closed"
    del bars[-1]
    bars.append(following(bars, ("100.3", "103", "100", "102.9"), gap=2))
    assert simulate(bars).trades[0].status == "unfilled"


def test_cli_simulation_roundtrip_costs_and_input_protection(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    fixture = Path("fixtures/orb_execution_sample.csv")
    output = tmp_path / "nested" / "simulation.json"
    run = subprocess.run([sys.executable, "-m", "orb", "simulate", str(fixture),
                          "--quantity", "10", "--fee-per-share", "0.01",
                          "--output", str(output)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    payload = json.loads(output.read_text())
    assert payload == json.loads(run.stdout)
    assert D(payload["trades"][0]["net_pnl"]) == D("25.80")
    source = tmp_path / "source.csv"
    source.write_text(fixture.read_text())
    original = source.read_bytes()
    failed = subprocess.run([sys.executable, "-m", "orb", "simulate", str(source),
                             "--output", str(source)], capture_output=True, text=True)
    assert failed.returncode == 2
    assert source.read_bytes() == original
    failed = subprocess.run([sys.executable, "-m", "orb", "simulate", str(source),
                             "--slippage-bps", "NaN"], capture_output=True, text=True)
    assert failed.returncode == 2
