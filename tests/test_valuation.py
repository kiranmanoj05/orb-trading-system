from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from orb.evaluation import evaluate
from test_evaluation import complete_day, combined
from test_portfolio import path


def sample(result, minute):
    return next(s for s in result['valuation']['samples']
                if s['at'].endswith(f'13:{minute:02}:00+00:00'))


def test_entry_fill_then_completed_close_and_fee_reconciliation():
    r = evaluate(path(close=False), fee_per_share=D('.01'))
    entry, marked = sample(r, 46), sample(r, 47)
    assert entry['positions'][0]['source'] == 'entry_fill'
    assert D(entry['equity']) == D('9999.76')
    assert D(marked['equity']) == D('10004.56')
    assert D(marked['unrealized_net_pnl']) == D('4.56')
    assert D(marked['gross_market_exposure']) == D('2412')
    assert r['portfolio']['account']['carrying_equity'] == '9999.76'
    assert r['valuation']['affects_sizing'] is False


def test_future_close_cannot_revalue_entry_or_earlier_samples():
    bars = path(close=False)
    a = evaluate(bars)
    b = evaluate(bars[:-1] + [replace(bars[-1], close=D('100.9'))])
    assert sample(a, 46) == sample(b, 46)
    assert sample(a, 47)['equity'] != sample(b, 47)['equity']
    assert a['portfolio']['events'][0] == b['portfolio']['events'][0]


def test_missing_mark_is_unknown_no_carry_and_later_mark_does_not_close():
    bars = path(close=False)
    bars.append(replace(bars[-1], start=bars[-1].start+timedelta(minutes=2)))
    r = evaluate(bars)
    assert sample(r, 48)['equity'] is None
    assert sample(r, 48)['positions'][0]['source'] == 'missing'
    assert sample(r, 49)['equity'] is not None
    assert r['portfolio']['trades'][0]['status'] == 'unresolved'
    assert r['portfolio']['account']['realized_pnl'] == '0'
    assert r['summary']['total_return'] is None
    assert r['valuation']['missing_mark_samples'] > 0


def test_two_symbols_require_both_marks():
    bars = combined(path('SPY', close=False), path('QQQ', close=False))
    bars.append(replace(bars[-1], symbol='SPY', start=bars[-1].start+timedelta(minutes=1)))
    r = evaluate(bars)
    assert sample(r, 48)['missing_symbols'] == ['QQQ']
    assert sample(r, 48)['equity'] is None


def test_exit_cash_wins_over_bar_close():
    r = evaluate(path())
    s = sample(r, 47)
    assert s['positions'] == []
    assert D(s['equity']) == D('10062.4')
    assert s['unrealized_net_pnl'] == '0'


@pytest.mark.parametrize('early', [False, True])
def test_complete_flat_day_has_zero_return_and_drawdown(early):
    from datetime import date
    r = evaluate(complete_day(date(2026,11,27) if early else date(2026,10,9)))
    assert r['valuation']['metrics_eligible']
    assert r['summary']['total_return'] == '0'
    assert r['summary']['max_drawdown'] == '0'
    assert len(r['valuation']['samples']) == (211 if early else 391)


def full_trade_day(loss=False):
    bars = complete_day()
    trade = path()
    if loss:
        trade[-1] = replace(trade[-1], low=D('98'))
    replacements = {(b.symbol,b.start):b for b in trade}
    return [replacements.get((b.symbol,b.start),b) for b in bars]


@pytest.mark.parametrize('loss,pnl', [(False,D('62.4')), (True,D('-31.2'))])
def test_complete_scope_metrics_match_hand_calculation(loss,pnl):
    r = evaluate(full_trade_day(loss))
    assert r['valuation']['metrics_eligible']
    assert D(r['summary']['total_return']) == pnl/D('10000')
    assert D(r['summary']['max_drawdown']) == (-pnl/D('10000') if loss else 0)


def test_missing_coverage_suppresses_metrics_even_when_flat():
    bars = full_trade_day()
    r = evaluate(bars[:-1])
    assert r['valuation']['missing_mark_samples'] == 0
    assert not r['valuation']['metrics_eligible']
    assert r['summary']['total_return'] is None


def test_ties_and_timezone_equivalence_remain_reproducible():
    from datetime import timezone
    bars = combined(path('SPY',close=False),path('QQQ',close=False))
    other = sorted((replace(b,start=b.start.astimezone(timezone.utc)) for b in reversed(bars)),key=lambda b:b.start)
    assert evaluate(bars) == evaluate(other)
