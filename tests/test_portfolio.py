from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
import json
import subprocess
import sys

import pytest

from orb.domain import Bar
from orb.portfolio import replay_portfolio
from orb.risk import RiskLimits
from orb.strategy import NEW_YORK


ALL = RiskLimits(D('1'), D('1'), D('1'), D('1'))


def path(symbol='SPY', day=(2026, 10, 9), delay=0, exit_after=0, close=True):
    start = datetime(*day, 9, 30, tzinfo=NEW_YORK)
    def bar(i, prices):
        return Bar(symbol, start + timedelta(minutes=i), *map(D, prices), 100)
    bars = [bar(i, ('99', '100', '98', '99.5')) for i in range(15)]
    bars += [bar(i, ('99.5', '100', '99.4', '99.5')) for i in range(15, 15+delay)]
    bars.append(bar(15+delay, ('99.5', '101', '99.4', '100.2')))
    bars += [bar(i, ('100.3', '101', '100', '100.5'))
             for i in range(16+delay, 17+delay+exit_after)]
    if close:
        bars[-1] = replace(bars[-1], high=D('104'), close=D('103'))
    return bars


def run(*groups, **kwargs):
    return replay_portfolio(sorted(sum(groups, []), key=lambda b: b.start), **kwargs)


def test_single_trade_matches_hand_calculated_account():
    r = run(path())
    assert r['trades'][0]['quantity'] == 24
    assert D(r['trades'][0]['net_pnl']) == D('62.4')
    assert D(r['account']['cash']) == D('10062.4')
    assert [e['kind'] for e in r['events']] == ['entry', 'exit']


def test_simultaneous_entries_share_cash_and_ignore_input_tie_order():
    a, b = path('SPY'), path('QQQ')
    r = run(a, b, initial_cash=D('1000'), limits=ALL)
    reverse = run(b, a, initial_cash=D('1000'), limits=ALL)
    assert r == reverse
    trades = {t['symbol']: t for t in r['trades']}
    assert trades['QQQ']['quantity'] == 9
    assert trades['SPY']['status'] == 'rejected'
    assert trades['SPY']['entry_price'] is None
    assert D(r['account']['cash']) == D('1023.4')
    assert [e['kind'] for e in r['events']] == ['entry', 'rejected', 'exit']


def test_future_exit_cannot_fund_earlier_entry():
    r = run(path('QQQ', exit_after=3), path('SPY', delay=1),
            initial_cash=D('1000'), limits=ALL)
    assert r['trades'][1]['status'] == 'rejected'
    assert r['events'][1]['account']['realized_pnl'] == '0'


def test_completed_intrabar_exit_can_fund_same_timestamp_entry():
    r = run(path('QQQ'), path('SPY', delay=1), initial_cash=D('1000'), limits=ALL)
    assert [t['quantity'] for t in r['trades']] == [9, 10]
    assert [e['kind'] for e in r['events']] == ['entry', 'exit', 'entry', 'exit']
    assert D(r['account']['cash']) == D('1049.4')


def test_costs_are_applied_once_and_match_ledger():
    bars = path()
    bars[-1] = replace(bars[-1], high=D('101'), low=D('98'), close=D('100'))
    r = run(bars, slippage_bps=D('10'), fee_per_share=D('.01'))
    t = r['trades'][0]
    assert t['quantity'] == 24
    assert D(t['net_pnl']) == D('-36.46320')
    assert D(r['account']['cash']) == D('9963.53680')
    assert D(r['account']['realized_pnl']) == D(t['net_pnl'])


def test_unresolved_positions_hold_cash_and_block_same_symbol_next_day():
    r = run(path(close=False), path(day=(2026, 10, 12)), path('QQQ', day=(2026, 10, 12)),
            initial_cash=D('1000'), limits=ALL)
    assert r['trades'][0]['status'] == 'unresolved'
    assert r['trades'][0]['net_pnl'] is None
    rejected = {t['symbol']: t for t in r['trades'][1:]}
    assert rejected['SPY']['reason'] == 'active_symbol_allocation'
    assert rejected['QQQ']['status'] == 'rejected'
    assert r['account']['allocations'][0]['state'] == 'unresolved'
    assert D(r['account']['gross_exposure']) == D('902.7')


def test_no_entry_bar_never_spends_account_capacity():
    r = run(path()[:-1])
    assert r['trades'][0]['status'] == 'unfilled'
    assert r['trades'][0]['quantity'] == 0
    assert r['events'] == []
    assert r['account']['cash'] == '10000'


def test_position_gap_never_releases_capacity():
    bars = path(close=False)
    bars.append(replace(bars[-1], start=bars[-1].start+timedelta(minutes=2), high=D('104')))
    r = run(bars)
    assert r['trades'][0]['reason'] == 'missing_position_bar'
    assert r['account']['allocations'][0]['state'] == 'unresolved'
    assert r['account']['realized_pnl'] == '0'


@pytest.mark.parametrize('day,hour', [((2026, 10, 9), 15), ((2026, 11, 27), 12)])
def test_calendar_flatten_reconciles_account(day, hour):
    bars = path(day=day, close=False)
    deadline = bars[-1].start.replace(hour=hour, minute=55)
    while bars[-1].start < deadline:
        bars.append(replace(bars[-1], start=bars[-1].start+timedelta(minutes=1)))
    r = run(bars)
    assert r['trades'][0]['reason'] == 'session_flatten'
    assert D(r['account']['gross_exposure']) == 0
    assert D(r['account']['cash']) == D('10000')


def test_empty_replay_and_invalid_inputs():
    with pytest.raises(ValueError, match='no bars'):
        run([])
    with pytest.raises(ValueError):
        run(path(), initial_cash=D('0'))
    with pytest.raises(ValueError):
        run(path(), fee_per_share=D('NaN'))
    with pytest.raises(ValueError, match='chronological'):
        replay_portfolio(reversed(path()))


def test_future_prices_do_not_change_admission_or_entry_snapshot():
    bars = path(close=False)
    prefix = run(bars)
    bars.append(replace(bars[-1], start=bars[-1].start+timedelta(minutes=1), high=D('110')))
    full = run(bars)
    assert prefix['events'][0] == full['events'][0]


def test_cli_roundtrip_validation_and_input_protection(tmp_path):
    output = tmp_path/'out.json'
    command = [sys.executable, '-m', 'orb', 'portfolio', 'fixtures/orb_execution_sample.csv']
    done = subprocess.run(command+['--output', str(output)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == json.loads(output.read_text())
    assert D(json.loads(done.stdout)['account']['cash']) == D('10062.4')
    source = tmp_path/'input.csv'
    from pathlib import Path
    source.write_bytes(Path(command[-1]).read_bytes())
    original = source.read_bytes()
    failed = subprocess.run(command[:-1]+[str(source), '--output', str(source)], capture_output=True)
    assert failed.returncode == 2
    assert source.read_bytes() == original
    failed = subprocess.run(command+['--risk-fraction', '2'], capture_output=True)
    assert failed.returncode == 2


def test_gross_cap_binds_second_symbol_without_double_allocation():
    r = run(path('QQQ', close=False), path('SPY', close=False),
            limits=RiskLimits(D('1'), D('.25'), D('.3')))
    assert [t['quantity'] for t in r['trades']] == [24, 5]
    assert r['events'][1]['sizing']['reason'] == 'sized:gross_exposure_cap'
    assert D(r['account']['gross_exposure']) == D('2908.7')


def test_ambiguous_exit_and_gap_loss_reconcile_with_account():
    bars = path()
    bars[-1] = replace(bars[-1], low=D('98'))
    r = run(bars)
    assert r['trades'][0]['ambiguous_exit'] is True
    assert D(r['account']['realized_pnl']) == D('-31.2')
    bars = path(close=False)
    bars.append(replace(bars[-1], start=bars[-1].start+timedelta(minutes=1),
                        open=D('90'), high=D('100'), low=D('89'), close=D('99')))
    r = run(bars)
    assert r['trades'][0]['reason'] == 'stop_gap'
    assert D(r['account']['realized_pnl']) == D('-247.2')


def test_raw_entry_below_stop_is_rejected_even_if_slippage_lifts_fill():
    bars = path()
    bars[-1] = replace(bars[-1], open=D('98.99'), low=D('98.9'))
    r = run(bars, slippage_bps=D('10'))
    assert r['trades'][0]['reason'] == 'pre_slippage_entry_at_or_below_stop'
    assert r['account']['cash'] == '10000'
