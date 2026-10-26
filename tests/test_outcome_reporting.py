"""Research eligibility is separate from a trade being filled."""

from dataclasses import replace
from decimal import Decimal as D

import pytest

from orb.evaluation import evaluate, render_report
from orb.outcomes import outcome_class, outcomes_complete
from test_evaluation import complete_day
from test_valuation import full_trade_day
from test_portfolio import path


def skipped_day():
    bars = complete_day()
    for i, bar in enumerate(bars):
        if bar.symbol == 'SPY' and bar.start.hour == 13 and bar.start.minute == 45:
            bars[i] = replace(bar, open=D('99.5'), high=D('100.5'),
                              low=D('99.4'), close=D('100.2'))
        if bar.symbol == 'SPY' and bar.start.hour == 13 and bar.start.minute == 46:
            bars[i] = replace(bar, open=D('98'), high=D('100'), low=D('97'), close=D('99.5'))
    return bars


def test_known_skip_is_complete_flat_account_but_not_a_closed_trade():
    result = evaluate(skipped_day(), fee_per_share=D('.01'))
    summary, valuation = result['summary'], result['valuation']
    assert result['portfolio']['trades'][0]['reason'] == 'entry_at_or_below_stop'
    assert summary['trade_status_counts'] == dict(closed=0, unresolved=0, unfilled=1, rejected=0)
    assert summary['outcome_class_counts'] == {'known_no_entry': 1}
    assert all(summary[k] for k in ('complete', 'coverage_complete', 'outcomes_complete', 'marks_complete'))
    assert valuation['metrics_eligible']
    assert summary['total_return'] == summary['max_drawdown'] == '0'
    assert summary['closed_subset']['win_fraction'] is None
    assert summary['closed_subset']['count'] == 0
    assert D(result['portfolio']['account']['cash']) == D('10000')
    assert all(D(s['equity']) == D('10000') for s in valuation['samples'])


def test_skip_does_not_relax_unrelated_coverage_gate():
    result = evaluate(skipped_day()[:-1])
    assert result['summary']['outcomes_complete']
    assert result['summary']['marks_complete']
    assert not result['summary']['coverage_complete']
    assert not result['summary']['complete']
    assert result['summary']['total_return'] is None


def test_missing_entry_bar_is_unknown_even_with_known_cash():
    bars = skipped_day()
    bars = [b for b in bars if not (b.symbol == 'SPY' and b.start.hour == 13 and b.start.minute == 46)]
    result = evaluate(bars)
    assert result['summary']['trade_reasons'] == {'missing_entry_bar': 1}
    assert result['summary']['outcome_class_counts'] == {'uncertain_entry': 1}
    assert not result['summary']['outcomes_complete']
    assert result['summary']['total_return'] is None


def test_known_skip_cannot_mask_a_second_symbols_unknown_position():
    trade_bars = path('QQQ', close=False)
    bars = skipped_day()
    replacements = {b.start: b for b in trade_bars}
    bars = [replacements.get(b.start, b) if b.symbol == 'QQQ' else b for b in bars]
    bars = [b for b in bars if not (b.symbol == 'QQQ' and b.start.hour == 13 and b.start.minute == 47)]
    result = evaluate(bars)
    assert result['summary']['outcome_class_counts'] == {'known_no_entry': 1, 'uncertain_position': 1}
    assert not result['summary']['complete']
    assert result['summary']['total_return'] is None


@pytest.mark.parametrize('status,reason,quantity,entry_at,expected', [
    ('unfilled', 'entry_cutoff', 0, None, 'known_no_entry'),
    ('unfilled', 'entry_at_or_below_stop', 0, None, 'known_no_entry'),
    ('rejected', 'missing_or_stale_position_mark', 0, None, 'known_no_entry'),
    ('unfilled', 'missing_entry_bar', 0, None, 'uncertain_entry'),
    ('unfilled', 'dataset_ended_before_entry', 0, None, 'uncertain_entry'),
    ('unfilled', 'future_reason', 0, None, 'uncertain_entry'),
    ('unfilled', 'entry_cutoff', 1, None, 'uncertain_entry'),
    ('unfilled', 'entry_cutoff', 0, '2026-10-09T13:46:00Z', 'uncertain_entry'),
    ('unresolved', 'dataset_ended_with_position', 1, None, 'uncertain_position'),
    ('future_status', 'future_reason', 0, None, 'unknown_outcome'),
])
def test_taxonomy_fails_closed_on_uncertain_or_unrecognised_evidence(status, reason, quantity, entry_at, expected):
    trade = dict(status=status, reason=reason, quantity=quantity, entry_at=entry_at)
    assert outcome_class(trade) == expected
    assert outcomes_complete([trade]) == (expected == 'known_no_entry')


def test_report_percentages_cost_units_and_undefined_rate():
    report = render_report(evaluate(skipped_day()))
    assert 'Total return: 0%' in report
    assert 'win rate: unavailable (undefined: no closed trades)' in report
    assert 'Fee assumption: 0 USD per share per side' in report
    assert 'Market-side slippage assumption: 0 bps' in report
    assert 'None' not in report
    winning = render_report(evaluate(full_trade_day()))
    assert 'Total return: 0.624%' in winning
    assert 'Closed-trade win rate: 100%' in winning
    assert 'Aggregate planned-risk limit: 1%' in winning
