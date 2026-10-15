from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
import subprocess
import sys

import pytest

from orb.account import AccountLedger
from orb.evaluation import evaluate
from orb.risk import AccountSnapshot, MarketMark, RiskLimits, size_long
from orb.strategy import NEW_YORK
from test_portfolio import path, run


NOW = datetime(2026, 10, 9, 9, 47, tzinfo=NEW_YORK)


def held_account():
    account = AccountLedger(D('10000'))
    account.reserve('a', 'QQQ', D('100'), D('98'))
    account.fill('a', D('100'))
    return account


def test_aggregate_budget_is_distinct_from_trade_risk_budget():
    result = size_long(AccountSnapshot(D('10000'), D('10000'), D('0'), D('85')),
                       D('100'), D('98'), RiskLimits(max_position_fraction=D('1')))
    assert result.quantity == 7
    assert result.reason == 'sized:aggregate_risk_cap'
    assert result.risk_budget == 50
    assert result.planned_risk == 14


@pytest.mark.parametrize('committed', ['100', '101'])
def test_exhausted_or_breached_aggregate_budget_rejects(committed):
    result = size_long(AccountSnapshot(D('10000'), D('10000'), D('0'), D(committed)),
                       D('100'), D('98'))
    assert result.quantity == 0
    assert result.reason == 'below_one_share:aggregate_risk_cap'


@pytest.mark.parametrize('value', [D('0'), D('-1'), D('1.01'), D('NaN'), 0.01])
def test_invalid_aggregate_limit(value):
    with pytest.raises(ValueError):
        RiskLimits(max_total_risk_fraction=value)


def test_negative_committed_risk_is_invalid():
    with pytest.raises(ValueError):
        AccountSnapshot(D('10000'), D('10000'), committed_stop_risk=D('-1'))


def test_marked_snapshot_uses_current_value_but_retains_initial_risk():
    account = held_account()
    snapshot = account.marked_snapshot(NOW, {'QQQ': MarketMark(D('80'), NOW)})
    assert snapshot.equity == 9500
    assert snapshot.gross_exposure == 2000
    assert snapshot.available_cash == 7500
    assert snapshot.committed_stop_risk == 50
    assert account.snapshot().equity == 10000  # Explicit carrying snapshot.
    snapshot = account.marked_snapshot(NOW, {'QQQ': MarketMark(D('120'), NOW)})
    assert snapshot.equity == 10500
    assert snapshot.committed_stop_risk == 50  # No risk release from a mark.


@pytest.mark.parametrize('offset', [-60, 60])
def test_stale_and_future_marks_cannot_admit_orders(offset):
    account = held_account()
    before = account.to_dict()
    with pytest.raises(ValueError, match='missing_or_stale'):
        account.reserve('b', 'SPY', D('100'), D('98'), at=NOW,
                        marks={'QQQ': MarketMark(D('100'), NOW+timedelta(seconds=offset))})
    assert account.to_dict() == before


def test_missing_mark_preserves_resources_and_timezone_equivalent_mark_is_fresh():
    account = held_account()
    before = account.to_dict()
    with pytest.raises(ValueError, match='missing_or_stale'):
        account.marked_snapshot(NOW, {})
    assert account.to_dict() == before
    assert account.marked_snapshot(NOW, {'QQQ': MarketMark(D('100'), NOW.astimezone(timezone.utc))}).equity == 10000


@pytest.mark.parametrize('kwargs', [{'at': NOW}, {'marks': {}}])
def test_marked_reservation_requires_time_and_marks_together(kwargs):
    with pytest.raises(ValueError, match='both'):
        AccountLedger(D('10000')).reserve('a', 'SPY', D('100'), D('98'), **kwargs)


@pytest.mark.parametrize('price,stamp,source', [
    (D('0'), NOW, 'completed_close'),
    (D('NaN'), NOW, 'completed_close'),
    (D('100'), NOW.replace(tzinfo=None), 'completed_close'),
    (D('100'), NOW, 'future_close'),
])
def test_invalid_marks(price, stamp, source):
    with pytest.raises(ValueError):
        MarketMark(price, stamp, source)


def test_pending_open_and_unresolved_risk_is_reserved_and_released():
    account = AccountLedger(D('10000'), RiskLimits(D('.01'), D('1'), D('1'), D('.015')))
    assert account.reserve('a', 'SPY', D('100'), D('98')).quantity == 50
    second = account.reserve('b', 'QQQ', D('100'), D('98'))
    assert second.quantity == 25 and second.reason == 'sized:aggregate_risk_cap'
    assert account.committed_stop_risk == 150
    account.cancel('b')
    account.fill('a', D('99'))
    assert account.committed_stop_risk == 50  # Better fill releases actual risk.
    account.mark_unresolved('a')
    assert account.committed_stop_risk == 50
    account.close('a', D('90'))  # Gap loss can exceed the planned stop risk.
    assert account.committed_stop_risk == 0
    assert account.realized_pnl == -450


def test_costs_count_once_in_reserved_and_filled_stop_risk():
    account = AccountLedger(D('10000'))
    decision = account.reserve('a', 'SPY', D('100'), D('98'),
                               slippage_bps=D('10'), fee_per_share=D('.01'))
    assert account.committed_stop_risk == decision.planned_risk == D('48.796')
    account.fill('a', D('100.10'))
    assert account.committed_stop_risk == D('48.796')
    account.close('a', D('97.902'))
    assert account.realized_pnl == D('-48.796')
    assert account.committed_stop_risk == 0


def test_simultaneous_entry_uses_prior_fill_not_previous_candle_close():
    result = run(path('QQQ', close=False), path('SPY', close=False), fee_per_share=D('.01'))
    second = result['events'][1]
    assert second['marks']['QQQ']['source'] == 'entry_fill'
    assert D(second['marks']['QQQ']['price']) == D('100.3')
    assert D(second['admission_snapshot']['equity']) == D('9999.76')
    assert D(second['admission_snapshot']['committed_stop_risk']) == D('31.68')


def rising_qqq():
    bars = path('QQQ', close=False)
    bars[-1] = replace(bars[-1], high=D('102'), close=D('102'))
    return bars


def test_fresh_held_close_changes_later_equity_and_sizing():
    result = run(rising_qqq(), path('SPY', delay=1))
    second = result['events'][1]
    assert second['marks']['QQQ']['source'] == 'completed_close'
    assert D(second['admission_snapshot']['equity']) == D('10040.8')
    assert D(second['admission_snapshot']['gross_exposure']) == D('2448')
    assert result['trades'][1]['quantity'] == 25  # Carrying equity would allow 24.


def test_missing_held_mark_rejects_without_inventing_exit_or_flat_pnl():
    result = run(path('QQQ', close=False), path('SPY', delay=2))
    trade = result['trades'][1]
    assert trade['status'] == 'rejected'
    assert trade['reason'] == 'missing_or_stale_position_mark'
    assert trade['net_pnl'] is None
    event = result['events'][1]
    assert event['admission_snapshot'] is None
    assert event['missing_mark_symbols'] == ['QQQ']
    assert result['account']['allocations'][0]['state'] == 'unresolved'
    assert D(result['account']['committed_stop_risk']) > 0


def test_future_held_candle_cannot_repair_missing_current_mark():
    qqq = path('QQQ', close=False)
    late = replace(qqq[-1], start=qqq[-1].start+timedelta(minutes=2), high=D('104'), close=D('103'))
    prefix = run(qqq, path('SPY', delay=2))
    full = run(qqq+[late], path('SPY', delay=2))
    assert prefix['events'][1] == full['events'][1]


def test_marked_gross_cap_accounts_for_held_market_value():
    result = run(rising_qqq(), path('SPY', delay=1),
                 limits=RiskLimits(D('1'), D('.25'), D('.3'), D('1')))
    second = result['events'][1]
    assert second['sizing']['reason'] == 'sized:gross_exposure_cap'
    assert second['sizing']['quantity'] == 5
    assert D(second['admission_snapshot']['gross_exposure']) == D('2448')


def test_aggregate_cap_sizes_second_symbol_and_release_follows_exit_time():
    limits = RiskLimits(D('.005'), D('1'), D('1'), D('.006'))
    result = run(path('QQQ', close=False), path('SPY', close=False), limits=limits)
    assert [t['quantity'] for t in result['trades']] == [38, 8]
    assert result['events'][1]['sizing']['reason'] == 'sized:aggregate_risk_cap'
    assert D(result['account']['committed_stop_risk']) == D('59.8')
    released = run(path('QQQ'), path('SPY', delay=1), limits=limits)
    assert [t['quantity'] for t in released['trades']] == [38, 38]


def test_evaluation_identity_includes_aggregate_limit():
    a = evaluate(path(), limits=RiskLimits(max_total_risk_fraction=D('.01')))
    b = evaluate(path(), limits=RiskLimits(max_total_risk_fraction=D('.02')))
    assert a['evaluation_id'] != b['evaluation_id']
    assert b['manifest']['config']['max_total_risk_fraction'] == '0.02'
    assert b['manifest']['schema_version'] == 'orb-evaluation-v3'


def test_cli_reports_aggregate_binding_and_validates_limit():
    command = [sys.executable, '-m', 'orb', 'size', '--equity', '10000', '--cash', '10000',
               '--entry', '100', '--stop', '98', '--committed-stop-risk', '85']
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['decision']['quantity'] == 7
    invalid = subprocess.run(command+['--max-total-risk-fraction', 'NaN'], capture_output=True, text=True)
    assert invalid.returncode == 2
    assert 'Sizing error' in invalid.stderr
