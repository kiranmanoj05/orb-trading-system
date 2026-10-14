from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import subprocess
import sys

import pytest

from orb.domain import Bar
from orb.evaluation import evaluate, render_report
from orb.replay import load_bars
from orb.session import exchange_session
from orb.strategy import NEW_YORK
from test_portfolio import path, ALL


def combined(*groups):
    return sorted(sum(groups, []), key=lambda b: b.start)


def complete_day(day=date(2026, 10, 9)):
    session = exchange_session(day)
    minutes = int((session.closes_at - session.opens_at).total_seconds() // 60)
    return [Bar(symbol, session.opens_at + timedelta(minutes=i),
                D('99'), D('100'), D('98'), D('99.5'), 10)
            for i in range(minutes) for symbol in ('SPY', 'QQQ')]


def test_closed_subset_does_not_hide_unknown_position():
    r = evaluate(combined(path('SPY'), path('QQQ', close=False)))
    assert r['summary']['trade_status_counts'] == {
        'closed': 1, 'unresolved': 1, 'unfilled': 0, 'rejected': 0}
    assert D(r['summary']['closed_subset']['net_pnl']) == D('62.4')
    assert len(r['summary']['unresolved_signal_ids']) == 1
    assert not r['summary']['complete']
    assert r['summary']['total_return'] is None
    assert r['summary']['max_drawdown'] is None
    assert r['portfolio']['trades'][0]['net_pnl'] is None  # QQQ sorts first


def test_absent_symbol_is_visible_without_a_candidate():
    r = evaluate(path())
    c = {x['symbol']: x for x in r['coverage']}
    assert c['QQQ']['state'] == 'missing'
    assert c['QQQ']['missing_minutes'] == 390
    assert c['SPY']['observed_minutes'] == 17
    assert not r['summary']['complete']


@pytest.mark.parametrize('day,minutes', [(date(2026, 10, 9), 390), (date(2026, 11, 27), 210)])
def test_complete_no_signal_session_and_early_close(day, minutes):
    r = evaluate(complete_day(day))
    assert r['summary']['complete']
    assert all(c['expected_minutes'] == minutes and not c['missing_minutes'] for c in r['coverage'])
    assert r['summary']['candidate_count'] == 0
    assert r['summary']['closed_subset']['win_fraction'] is None
    assert r['summary']['session_state_counts'] == {'expired': 2}


def test_explicit_bounds_detect_entirely_missing_sessions_skip_weekend():
    r = evaluate(complete_day(), start_session=date(2026, 10, 9), end_session=date(2026, 10, 12))
    assert len(r['coverage']) == 4
    assert r['summary']['incomplete_symbol_sessions'] == 2
    assert {x['session'] for x in r['coverage']} == {'2026-10-09', '2026-10-12'}
    assert r['manifest']['config']['bounds_source'] == 'explicit'


def test_missing_minute_is_counted_even_after_signal_or_exit():
    bars = complete_day()
    del bars[-1]
    r = evaluate(bars)
    assert r['summary']['incomplete_symbol_sessions'] == 1
    assert next(c for c in r['coverage'] if c['symbol'] == 'QQQ')['missing_minutes'] == 1


def test_no_entry_is_unfilled_not_a_flat_closed_trade():
    r = evaluate(path()[:-1])
    assert r['summary']['trade_status_counts']['unfilled'] == 1
    assert r['summary']['closed_subset']['count'] == 0
    assert r['summary']['trade_reasons'] == {'dataset_ended_before_entry': 1}


def test_cash_rejection_remains_in_denominator():
    r = evaluate(combined(path('SPY'), path('QQQ')), initial_cash=D('1000'), limits=ALL)
    assert r['summary']['candidate_count'] == 2
    assert r['summary']['trade_status_counts']['rejected'] == 1
    assert r['summary']['closed_subset']['count'] == 1
    assert r['portfolio']['trades'][1]['net_pnl'] is None


def test_negative_pnl_costs_and_ambiguity_match_account():
    bars = path()
    bars[-1] = replace(bars[-1], low=D('98'), high=D('104'))
    r = evaluate(bars, slippage_bps=D('10'), fee_per_share=D('.01'))
    assert r['summary']['ambiguous_closed_exits'] == 1
    assert r['summary']['closed_subset']['losses'] == 1
    assert D(r['summary']['closed_subset']['net_pnl']) == D('-36.46320')
    assert r['summary']['closed_subset']['win_fraction'] == '0'


def test_identity_is_stable_across_ties_timezone_and_decimal_spelling():
    bars = combined(path('SPY'), path('QQQ'))
    reversed_ties = sorted(reversed(bars), key=lambda b: b.start)
    converted = [replace(b, start=b.start.astimezone(timezone.utc),
                         open=D(str(b.open) + '0') if '.' in str(b.open) else b.open)
                 for b in reversed_ties]
    a = evaluate(bars, initial_cash=D('10000.00'))
    b = evaluate(converted, initial_cash=D('1E4'))
    assert a['evaluation_id'] == b['evaluation_id']
    assert a['manifest'] == b['manifest']
    assert a['portfolio'] == b['portfolio']


def test_price_volume_config_label_and_bounds_change_identity():
    bars = path()
    base = evaluate(bars)['evaluation_id']
    variants = [evaluate([replace(bars[0], volume=11)] + bars[1:]),
                evaluate([replace(bars[0], close=D('99.6'))] + bars[1:]),
                evaluate(bars, fee_per_share=D('.01')), evaluate(bars, data_label='synthetic'),
                evaluate(bars, start_session=date(2026,10,9), end_session=date(2026,10,12))]
    assert all(r['evaluation_id'] != base for r in variants)


@pytest.mark.parametrize('kwargs', [
    {'start_session': date(2026,10,9)},
    {'start_session': date(2026,10,12), 'end_session': date(2026,10,9)},
    {'start_session': date(2026,10,12), 'end_session': date(2026,10,12)},
    {'data_label': ' '},
])
def test_invalid_scope(kwargs):
    with pytest.raises(ValueError):
        evaluate(path(), **kwargs)


def test_invalid_order_duplicate_and_universe_are_not_repaired():
    bars = path()
    for bad in (list(reversed(bars)), [bars[0]] + bars,
                [replace(b, symbol='ABC') for b in bars], []):
        with pytest.raises(ValueError):
            evaluate(bad)


def test_report_discloses_incomplete_outcomes_and_fingerprints():
    r = evaluate(path(close=False), data_label='synthetic')
    report = render_report(r)
    assert 'Complete within declared coverage: False' in report
    assert '| unresolved | 1 |' in report and '| QQQ | missing |' in report
    assert r['evaluation_id'] in report
    assert 'unknown exits are not zero' in report
    assert 'not calculated' in report


def test_cli_json_markdown_and_repeatability(tmp_path):
    source = Path('fixtures/orb_execution_sample.csv')
    out, report = tmp_path/'evaluation.json', tmp_path/'evaluation.md'
    args = [sys.executable, '-m', 'orb', 'evaluate', str(source),
            '--output', str(out), '--report', str(report), '--data-label', 'synthetic']
    first = subprocess.run(args, capture_output=True, text=True, check=True)
    second = subprocess.run(args, capture_output=True, text=True, check=True)
    assert first.stdout == second.stdout == out.read_text()
    result = json.loads(first.stdout)
    assert result == evaluate(load_bars(source), data_label='synthetic')
    assert report.read_text() == render_report(result)


@pytest.mark.parametrize('collision', ['input_report', 'input_json', 'same_outputs'])
def test_cli_rejects_output_collision_before_writing(tmp_path, collision):
    source = tmp_path/'input.csv'
    source.write_bytes(Path('fixtures/orb_execution_sample.csv').read_bytes())
    original = source.read_bytes()
    out = tmp_path/'out.json'
    args = [sys.executable, '-m', 'orb', 'evaluate', str(source)]
    if collision == 'input_report':
        args += ['--output', str(out), '--report', str(source)]
    elif collision == 'input_json':
        args += ['--output', str(source)]
    else:
        args += ['--output', str(out), '--report', str(out)]
    r = subprocess.run(args, capture_output=True, text=True)
    assert r.returncode == 2
    assert source.read_bytes() == original
    assert not out.exists()
