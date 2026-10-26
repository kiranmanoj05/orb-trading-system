from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import json
import subprocess
import sys

import pytest

from orb.daily_loss import DailyLossGuard
from orb.evaluation import evaluate, render_report
from orb.session import exchange_session
from test_portfolio import path, run


SESSION = exchange_session(date(2026, 10, 9))
OPEN = SESSION.opens_at


def guard(fraction=D('.02'), reference=D('10000')):
    return DailyLossGuard(SESSION, fraction, reference)


def test_fixed_opening_reference_boundary_and_recovery_latch():
    g = guard()
    assert not g.observe(OPEN, D('11000'))['blocked']  # Not a trailing high-water control.
    assert not g.observe(OPEN+timedelta(minutes=1), D('9800.01'))['blocked']
    breach = g.observe(OPEN+timedelta(minutes=2), D('9800'))
    assert breach['loss_limit'] == breach['loss_from_open'] == 200
    assert breach['reason'] == 'daily_loss_limit'
    recovered = g.observe(OPEN+timedelta(minutes=3), D('10100'))
    assert recovered['blocked'] and recovered['loss_from_open'] == 0
    assert recovered['blocked_at'] == OPEN+timedelta(minutes=2)


def test_unknown_reference_never_rebased_to_later_known_equity():
    g = guard(reference=None)
    r = g.observe(OPEN+timedelta(minutes=1), D('12000'))
    assert r['reason'] == 'daily_loss_baseline_unavailable'
    assert r['reference_equity'] is r['loss_limit'] is r['loss_from_open'] is None
    assert r['blocked_at'] == OPEN


def test_unknown_equity_latches_and_keeps_first_reason():
    g = guard()
    assert g.observe(OPEN, None)['reason'] == 'daily_loss_equity_unavailable'
    assert g.observe(OPEN+timedelta(minutes=1), D('9000'))['reason'] == 'daily_loss_equity_unavailable'
    assert g.observe(OPEN+timedelta(minutes=2), D('10000'))['blocked']


def test_loss_reason_survives_later_unknown_marks_and_negative_equity():
    g = guard()
    assert g.observe(OPEN, D('-100'))['reason'] == 'daily_loss_limit'
    assert g.observe(OPEN+timedelta(minutes=1), None)['reason'] == 'daily_loss_limit'


@pytest.mark.parametrize('fraction', [D('0'), D('-1'), D('1.01'), D('NaN'), 0.02])
def test_invalid_fraction(fraction):
    with pytest.raises(ValueError):
        guard(fraction=fraction)


@pytest.mark.parametrize('reference', [D('0'), D('-1'), D('Infinity'), D('NaN'), 10000])
def test_invalid_reference(reference):
    with pytest.raises(ValueError):
        guard(reference=reference)


@pytest.mark.parametrize('at', [OPEN.replace(tzinfo=None), OPEN-timedelta(seconds=1),
                               SESSION.closes_at+timedelta(seconds=1)])
def test_wrong_session_or_naive_clock_is_rejected_without_mutation(at):
    g = guard()
    before = g.to_dict()
    with pytest.raises(ValueError):
        g.observe(at, D('9000'))
    assert g.to_dict() == before


@pytest.mark.parametrize('equity', [D('NaN'), D('Infinity'), 9800.0])
def test_bad_observation_does_not_mutate_latch(equity):
    g = guard()
    before = g.to_dict()
    with pytest.raises(ValueError):
        g.observe(OPEN, equity)
    assert g.to_dict() == before


def test_clock_order_and_equivalent_timezone():
    g = guard()
    now = OPEN+timedelta(minutes=2)
    g.observe(now, D('10000'))
    before = g.to_dict()
    with pytest.raises(ValueError, match='chronological'):
        g.observe(OPEN, D('9000'))
    assert g.to_dict() == before
    assert not g.observe(now.astimezone(timezone.utc), D('10000'))['blocked']


def test_new_session_requires_new_reference_and_can_reset():
    first = guard()
    first.observe(OPEN, D('9700'))
    monday = exchange_session(date(2026, 10, 12))
    next_guard = DailyLossGuard(monday, D('.02'), D('9700'))
    assert not next_guard.observe(monday.opens_at, D('9700'))['blocked']
    assert first.to_dict()['blocked']


def stop_path():
    bars = path('QQQ')
    bars[-1] = replace(bars[-1], high=D('101'), low=D('98'), close=D('99'))
    return bars


def test_realised_loss_blocks_same_timestamp_candidate_after_exit():
    r = run(stop_path(), path('SPY', delay=1), daily_loss_fraction=D('.001'))
    assert r['trades'][0]['net_pnl'] == '-31.2'
    assert r['trades'][1]['reason'] == 'daily_loss_limit'
    assert r['trades'][1]['net_pnl'] is None
    second = r['events'][2]
    assert second['kind'] == 'rejected'
    assert D(second['admission_snapshot']['equity']) == D('9968.8')
    assert datetime.fromisoformat(second['daily_loss']['blocked_at']) == datetime.fromisoformat(second['at'])


def dipping_path():
    qqq = path('QQQ', close=False, exit_after=3)
    qqq[16] = replace(qqq[16], low=D('99.6'), high=D('100.4'), close=D('99.7'))
    return qqq


def test_unrealised_dip_without_candidate_latches_after_recovery():
    r = run(dipping_path(), path('SPY', delay=3), daily_loss_fraction=D('.001'))
    assert r['trades'][0]['status'] == 'unresolved'
    assert r['trades'][1]['reason'] == 'daily_loss_limit'
    reject = r['events'][1]
    assert D(reject['admission_snapshot']['equity']) > 10000  # Recovered by admission.
    assert reject['daily_loss']['blocked_at'].endswith('13:47:00+00:00')
    assert any(o['reason'] == 'daily_loss_limit' and D(o['equity']) == D('9985.6')
               for o in r['daily_loss_observations'])
    assert D(r['account']['committed_stop_risk']) > 0  # No forced sale.


def test_entry_fee_can_block_next_simultaneous_entry():
    r = run(path('QQQ'), path('SPY'), fee_per_share=D('.01'), daily_loss_fraction=D('.00002'))
    assert r['trades'][0]['quantity'] == 24
    assert r['events'][0]['daily_loss']['reason'] == 'daily_loss_limit'
    assert r['trades'][1]['reason'] == 'daily_loss_limit'
    assert r['events'][1]['marks']['QQQ']['source'] == 'entry_fill'


def test_known_cash_next_session_resets_after_weekend():
    r = run(stop_path(), path('SPY', delay=1), path('SPY', day=(2026,10,12)),
            daily_loss_fraction=D('.001'))
    assert [t['status'] for t in r['trades']] == ['closed', 'rejected', 'closed']
    sessions = r['daily_loss_sessions']
    assert [s['session'] for s in sessions] == ['2026-10-09', '2026-10-12']
    assert sessions[0]['reason'] == 'daily_loss_limit'
    assert D(sessions[1]['reference_equity']) == D('9968.8')
    assert not sessions[1]['blocked']


def test_overnight_unknown_opening_equity_blocks_even_with_fresh_later_marks():
    r = run(path('QQQ', close=False), path('QQQ', day=(2026,10,12), close=False),
            path('SPY', day=(2026,10,12)))
    assert r['trades'][-1]['reason'] == 'daily_loss_baseline_unavailable'
    reject = next(e for e in r['events'] if e['symbol'] == 'SPY')
    assert not reject['missing_mark_symbols']
    assert reject['admission_snapshot'] is not None
    assert r['daily_loss_sessions'][1]['reference_equity'] is None


def test_missing_mark_latches_data_gap_even_after_marks_resume():
    qqq = path('QQQ', close=False)
    qqq.append(replace(qqq[-1], start=qqq[-1].start+timedelta(minutes=2)))
    r = run(qqq, path('SPY', delay=3))
    assert r['trades'][0]['reason'] == 'missing_position_bar'
    assert r['trades'][1]['reason'] == 'daily_loss_equity_unavailable'
    assert r['events'][1]['missing_mark_symbols'] == []
    assert r['events'][1]['daily_loss']['blocked_at'].endswith('13:48:00+00:00')


@pytest.mark.parametrize('day,minutes', [((2026,10,9),391), ((2026,11,27),211)])
def test_risk_clock_includes_gaps_and_early_close(day, minutes):
    r = run(path(day=day))
    assert len([o for o in r['daily_loss_observations'] if o['phase'] == 'before_entries']) == minutes
    assert not r['daily_loss_sessions'][0]['blocked']  # Missing bars while flat don't hide inventory loss.


def test_appending_future_prices_does_not_rewrite_earlier_risk_or_admission():
    qqq = dipping_path()
    before = run(qqq, path('SPY', delay=3), daily_loss_fraction=D('.001'))
    future = replace(qqq[-1], start=qqq[-1].start+timedelta(minutes=1), high=D('110'), close=D('109'))
    after = run(qqq+[future], path('SPY', delay=3), daily_loss_fraction=D('.001'))
    assert before['events'][:2] == after['events'][:2]
    cutoff = before['events'][1]['at']
    assert [o for o in before['daily_loss_observations'] if o['at'] <= cutoff] == [
        o for o in after['daily_loss_observations'] if o['at'] <= cutoff]


def test_daily_fraction_is_fingerprinted_and_reported():
    a = evaluate(path(), daily_loss_fraction=D('.02'))
    b = evaluate(path(), daily_loss_fraction=D('.01'))
    assert a['evaluation_id'] != b['evaluation_id']
    assert b['manifest']['config']['daily_loss_fraction'] == '0.01'
    assert b['manifest']['schema_version'] == 'orb-evaluation-v5'
    assert 'Daily-loss entry-block threshold: 1%' in render_report(b)


def test_cli_daily_loss_fraction_and_invalid_value():
    command = [sys.executable, '-m', 'orb', 'evaluate', 'fixtures/orb_daily_loss_sample.csv',
               '--daily-loss-fraction', '.001']
    done = subprocess.run(command, capture_output=True, text=True, check=True)
    result = json.loads(done.stdout)
    assert result['manifest']['config']['daily_loss_fraction'] == '0.001'
    assert result['portfolio']['trades'][1]['reason'] == 'daily_loss_limit'
    assert D(result['portfolio']['account']['cash']) == D('9968.8')
    assert result['summary']['daily_loss_block_reasons'] == {'daily_loss_limit': 1}
    bad = subprocess.run(command[:-1]+['NaN'], capture_output=True, text=True)
    assert bad.returncode == 2 and 'Replay error' in bad.stderr
