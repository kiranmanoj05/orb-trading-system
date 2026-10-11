from decimal import Decimal as D
import json
import subprocess
import sys

import pytest

from orb.risk import AccountSnapshot, RiskLimits, size_long


def test_risk_rounds_down_with_exact_budget_boundary():
    result = size_long(AccountSnapshot(D("10000"), D("10000")), D("100"), D("98"),
                       RiskLimits(max_position_fraction=D("1")))
    assert result.quantity == 25
    assert result.planned_risk == result.risk_budget == D("50")
    assert result.reason == "sized:risk_budget"
    assert size_long(AccountSnapshot(D("9999"), D("9999")), D("100"), D("98"),
                     RiskLimits(max_position_fraction=D("1"))).quantity == 24


def test_costs_are_included_in_stop_risk_and_entry_cash():
    result = size_long(AccountSnapshot(D("10000"), D("10000")), D("100"), D("98"),
                       slippage_bps=D("10"), fee_per_share=D("0.01"))
    assert result.risk_per_share == D("2.218")
    assert result.quantity == 22
    assert result.planned_risk == D("48.796")
    assert result.entry_notional == D("2202.2")
    assert result.cash_required == D("2202.42")


@pytest.mark.parametrize("cash,gross,position,quantity,reason", [
    ("10000", "0", "0.1", 10, "position_cap"),
    ("10000", "9900", "1", 1, "gross_exposure_cap"),
    ("199", "0", "1", 1, "cash_cap"),
    ("0", "0", "1", 0, "cash_cap"),
    ("10000", "11000", "1", 0, "gross_exposure_cap"),
])
def test_each_shared_resource_limits_quantity(cash, gross, position, quantity, reason):
    result = size_long(AccountSnapshot(D("10000"), D(cash), D(gross)), D("100"), D("98"),
                       RiskLimits(max_position_fraction=D(position)))
    assert result.quantity == quantity
    assert result.reason == ("below_one_share:" if quantity == 0 else "sized:") + reason


def test_fee_can_make_one_share_unaffordable():
    assert size_long(AccountSnapshot(D("10000"), D("100")), D("100"), D("98"),
                     fee_per_share=D("0.01")).quantity == 0


def test_updated_snapshot_reserves_capacity_for_next_candidate():
    first = size_long(AccountSnapshot(D("10000"), D("10000")), D("100"), D("98"))
    second = size_long(AccountSnapshot(D("10000"), D("10000") - first.cash_required,
                                      D("9900")), D("100"), D("98"))
    assert second.quantity == 1


@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("-1"), 100.0, True])
def test_invalid_snapshot_numbers(value):
    with pytest.raises(ValueError):
        AccountSnapshot(D("10000"), value)


@pytest.mark.parametrize("value", [D("0"), D("-0.1"), D("1.01"), D("NaN"), 0.5])
def test_invalid_limit_fraction(value):
    with pytest.raises(ValueError):
        RiskLimits(risk_fraction=value)


@pytest.mark.parametrize("entry,stop,slip,fee", [
    ("98", "98", "0", "0"), ("97", "98", "0", "0"),
    ("100", "0", "0", "0"), ("100", "98", "10000", "0"),
    ("100", "98", "0", "-1"), ("100", "98", "NaN", "0"),
])
def test_invalid_trade_inputs(entry, stop, slip, fee):
    with pytest.raises(ValueError):
        size_long(AccountSnapshot(D("10000"), D("10000")), D(entry), D(stop),
                  slippage_bps=D(slip), fee_per_share=D(fee))


def test_cli_sizing_and_invalid_input():
    command = [sys.executable, "-m", "orb", "size", "--equity", "10000", "--cash", "10000",
               "--entry", "100.30", "--stop", "99"]
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["decision"]["quantity"] == 24
    failed = subprocess.run(command + ["--risk-fraction", "NaN"], capture_output=True, text=True)
    assert failed.returncode == 2
    assert "Sizing error" in failed.stderr
