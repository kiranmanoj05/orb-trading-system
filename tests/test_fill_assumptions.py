"""Hand-calculated execution sensitivity and causal delayed admission."""
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from orb.dashboard_fixtures import scenarios
from orb.evaluation import evaluate, render_report
from orb.fill_assumptions import FillAssumptions
from orb.paper_alignment import study
from orb.portfolio import replay_portfolio
from orb.presets import PresetStore
from orb.simulation import ExecutionConfig, simulate
from test_simulation import scenario, following


@pytest.mark.parametrize('delay', [True, -1, 6, 1.0, '1', None])
def test_invalid_delay_rejected(delay):
    with pytest.raises(ValueError, match='delay'): FillAssumptions(delay)


@pytest.mark.parametrize('spread', [0, 1.0, D('-1'), D('100.01'), D('NaN'), D('Infinity')])
def test_invalid_spread_rejected(spread):
    with pytest.raises(ValueError, match='spread'): FillAssumptions(spread_bps=spread)


def test_contract_is_immutable_and_combined_costs_are_bounded():
    assumptions = FillAssumptions(5, D('100'))
    with pytest.raises(FrozenInstanceError): assumptions.entry_delay_minutes = 2
    with pytest.raises(ValueError, match='combined'):
        ExecutionConfig(slippage_bps=D('9990'), fill_assumptions=assumptions)
    with pytest.raises(ValueError, match='instance'):
        evaluate(scenarios()['complete_trade'], fill_assumptions={})


def test_default_serialization_and_zero_scenario_preserve_reference_paths():
    bars = scenarios()['complete_trade']
    base = simulate(bars); explicit = simulate(bars, ExecutionConfig(fill_assumptions=FillAssumptions()))
    assert 'fill_assumptions' not in base.to_dict()['config']
    assert base.to_dict()['model_version'] == 'minute-long-v1'
    assert explicit.trades == base.trades and explicit.replay_result == base.replay_result
    a = evaluate(bars); b = evaluate(bars, fill_assumptions=FillAssumptions())
    assert a['manifest']['schema_version'] == 'orb-evaluation-v5'
    assert b['manifest']['schema_version'] == 'orb-evaluation-v9'
    assert a['portfolio']['trades'] == b['portfolio']['trades']
    assert a['valuation'] == b['valuation'] and a['coverage'] == b['coverage']
    assert a['evaluation_id'] != b['evaluation_id']
    assert a['manifest']['dataset'] == b['manifest']['dataset']


def test_delayed_open_reprices_fill_target_and_ignores_pre_entry_extrema():
    bars = scenario(prices=('100.3', '110', '98', '100'))  # Would immediately stop.
    bars.append(following(bars, ('101', '102', '100', '101')))
    base = simulate(bars).trades[0]
    delayed = simulate(bars, ExecutionConfig(fill_assumptions=FillAssumptions(1))).trades[0]
    assert base.reason == 'stop' and base.ambiguous_exit
    assert delayed.entry_at == bars[-1].start and delayed.entry_price == D('101')
    assert delayed.stop == D('99') and delayed.target == D('105')
    assert delayed.status == 'unresolved'  # No retrospective stop before entry.
    bars.append(following(bars, ('106', '107', '105', '106')))
    closed = simulate(bars, ExecutionConfig(fill_assumptions=FillAssumptions(1))).trades[0]
    assert closed.entry_at == delayed.entry_at and closed.target == delayed.target
    assert closed.reason == 'target_gap' and closed.exit_price == D('106')
    assert closed.net_pnl == D('5')


def test_spread_is_half_per_market_side_and_round_trip_fees_remain_separate():
    trade = simulate(scenario(prices=('100.30','101','98','100')),
        ExecutionConfig(10, D('1'), D('.01'), FillAssumptions(0,D('4')))).trades[0]
    # 1 bps slippage + 2 bps half-spread = 3 bps per market side.
    assert trade.entry_price == D('100.330090')
    assert trade.exit_price == D('98.9703')
    assert trade.gross_pnl == D('-13.597900') and trade.fees == D('.20')
    assert trade.net_pnl == D('-13.797900')


def test_spread_does_not_change_idealised_limit_touch_price_or_stop_first_policy():
    config = ExecutionConfig(fill_assumptions=FillAssumptions(0,D('4')))
    t = simulate(scenario(prices=('100.3','104','100','103')),config).trades[0]
    assert t.exit_price == t.target and t.reason == 'target'
    t = simulate(scenario(prices=('100.3','104','98','103')),config).trades[0]
    assert t.reason == 'stop' and t.ambiguous_exit


@pytest.mark.parametrize('missing', [0,1,2])
def test_missing_wait_or_entry_minute_never_jumps_to_a_later_fill(missing):
    bars = scenario()
    bars.append(following(bars, ('101','102','100','101')))
    bars.append(following(bars, ('101','102','100','101')))
    del bars[16+missing]
    t = simulate(bars,ExecutionConfig(fill_assumptions=FillAssumptions(2))).trades[0]
    assert t.status == 'unfilled' and t.reason == 'missing_entry_bar'
    assert t.entry_at is None and t.net_pnl is None


def test_delayed_entry_cutoff_is_a_known_skip_not_missing_data():
    bars = scenario()
    # Move the breakout to 10:58: observation 10:59, delayed boundary 11:00.
    stamp = bars[15].start.replace(hour=10,minute=58)
    bars = bars[:15]+[replace(bars[14],start=bars[15].start+timedelta(minutes=i))
                      for i in range(73)]+[replace(bars[15],start=stamp)]
    t = simulate(bars,ExecutionConfig(fill_assumptions=FillAssumptions(1))).trades[0]
    assert t.status == 'unfilled' and t.reason == 'entry_cutoff' and t.entry_at is None


def test_delayed_below_stop_stays_known_no_entry():
    bars = scenario()
    bars.append(following(bars,('98','100','97','99')))
    t = simulate(bars,ExecutionConfig(fill_assumptions=FillAssumptions(1))).trades[0]
    assert t.reason == 'entry_at_or_below_stop' and t.entry_price is None


def test_account_sizes_with_same_spread_as_fills_and_reconciles_net_pnl():
    bars = scenario(prices=('100.3','101','98','100'))
    a = replay_portfolio(bars, slippage_bps=D('3'), fee_per_share=D('.01'))
    b = replay_portfolio(bars, slippage_bps=D('1'), fee_per_share=D('.01'),
                         fill_assumptions=FillAssumptions(0,D('4')))
    assert a['trades'] == b['trades'] and a['account'] == b['account']
    assert D(b['account']['realized_pnl']) == D(b['trades'][0]['net_pnl'])
    assert b['events'][0]['at'] == a['events'][0]['at']


def test_wait_does_not_reserve_cash_and_future_prices_do_not_change_admission():
    bars = scenario()
    bars.append(following(bars,('101','102','100','101')))
    delayed = replay_portfolio(bars,fill_assumptions=FillAssumptions(1))
    assert len(delayed['events']) == 1 and delayed['events'][0]['at'] == bars[-1].start.isoformat()
    before = delayed['events'][0]
    bars.append(following(bars,('110','111','109','110')))
    after = replay_portfolio(bars,fill_assumptions=FillAssumptions(1))['events'][0]
    assert before == after
    # Changing entry candle future extrema/close cannot influence admission.
    bars[-2] = replace(bars[-2], high=D('120'), low=D('98'),close=D('99'))
    changed = replay_portfolio(bars,fill_assumptions=FillAssumptions(1))['events'][0]
    assert before == changed


def test_all_trials_are_frozen_and_passive_benchmark_uses_effective_spread():
    r = study(scenarios()['complete_trade'],PresetStore.paper('paper-balanced'))
    assert r['schema_version'] == 'orb-paper-alignment-v2'
    assert len(r['trials']) == 4 and len({t['report']['evaluation_id'] for t in r['trials']}) == 4
    base, cost, delay, combined = r['trials']
    assert delay['fill_assumptions']['entry_delay_minutes'] == 1
    assert combined['fill_assumptions']['spread_bps'] == '4'
    assert combined['report']['config']['effective_market_side_bps'] == '2'
    # Same fee assumptions; passive account receives the same spread stress.
    assert combined['report']['benchmarks']['passive'][0]['return_fraction'] != base['report']['benchmarks']['passive'][0]['return_fraction']
    assert combined['report']['applied_settings']['paper_alignment']['fill_assumptions'] == combined['fill_assumptions']
    assert delay['trades'][0]['entry_at'] != base['trades'][0]['entry_at']
    assert r['preset']['settings'] == PresetStore.paper('paper-balanced')['settings']


def test_wait_gap_keeps_delayed_outcome_uncertain_and_withholds_comparison():
    bars = scenarios()['complete_trade']
    boundary = next(t for t in simulate(bars).trades if t.symbol=='SPY').entry_at
    # Immediate entry can close in its first candle; delayed entry cannot bridge
    # the next missing minute. Coverage independently fails for every cell.
    bars = [b for b in bars if not (b.symbol=='SPY' and b.start==boundary+timedelta(minutes=1))]
    r=study(bars,PresetStore.paper('paper-balanced'))
    assert not r['all_trials_eligible']
    for row in r['trials'][2:]:
        assert row['trades'][0]['reason'] == 'missing_entry_bar'
        assert row['report']['trade_statistics']['net_pnl_usd'] is None
        assert row['delta_vs_saved_net_pnl_usd'] is None
