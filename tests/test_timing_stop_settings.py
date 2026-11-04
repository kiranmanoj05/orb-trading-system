"""Causal timing/stop variants; no provider credentials or performance inference."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from orb.breakout import BreakoutDetector, SignalState
from orb.dashboard import build_dashboard_run
from orb.dashboard_api import create_app
from orb.dashboard_import import import_dashboard
from orb.domain import Bar
from orb.entry_policy import EntryPolicy, POLICY_V2
from orb.evaluation import evaluate, render_report
from orb.paper_trading import PaperEngine, PaperError
from orb.presets import PresetStore
from orb.profile_runs import apply_preset
from orb.replay import replay
from orb.research_settings import ResearchSettings, STRATEGY_SCHEMA, TIMING_FIELDS
from orb.session import exchange_session
from orb.strategy import calculate_opening_range
from test_dashboard_import import csv_text


def policy(**changes):
    return EntryPolicy(schema_version=POLICY_V2, **changes)


def settings(**changes):
    return {**ResearchSettings(schema_version=STRATEGY_SCHEMA).to_dict(), **changes}


def full_day(day=date(2026, 11, 4), breakout_minute=5):
    session = exchange_session(day)
    result = []
    for i in range(int((session.closes_at - session.opens_at).total_seconds() // 60)):
        for symbol in ('QQQ', 'SPY'):
            prices = ('99', '100', '98', '99')
            if symbol == 'SPY' and i == breakout_minute:
                prices = ('100', '101.2', '99.8', '101.02')
            if symbol == 'SPY' and i > breakout_minute:
                prices = ('101.04', '106', '100.8', '101.04')
            if symbol == 'SPY' and i == breakout_minute + 5:
                prices = ('101.04', '108', '100.8', '101.04')
            result.append(Bar(symbol, session.opens_at + timedelta(minutes=i),
                              *(D(v) for v in prices), 100))
    return result


@pytest.mark.parametrize('day', [date(2026, 3, 6), date(2026, 3, 9), date(2026, 11, 4), date(2026, 11, 27)])
def test_new_york_windows_respect_dst_and_early_close_without_changing_reference(day):
    bars = full_day(day)
    assert not replay(bars).signals
    selected = policy(range_minutes=5)
    result = evaluate(bars, entry_policy=selected)
    signal = result['portfolio']['replay']['signals'][0]
    session = exchange_session(day)
    assert signal['observed_at'] == (session.opens_at + timedelta(minutes=6)).isoformat()
    assert result['summary']['complete']
    assert result['manifest']['config']['entry_policy'] == selected.to_dict()
    assert result['portfolio']['trades'][0]['entry_at'] == signal['observed_at']
    assert result['portfolio']['trades'][0]['stop'] == '99'


def test_range_low_changes_initial_risk_target_and_exit_not_the_candidate_clock():
    bars = full_day()
    mid = evaluate(bars, entry_policy=policy(range_minutes=5))
    low = evaluate(bars, entry_policy=policy(range_minutes=5, stop_policy='range_low'))
    a, b = (v['portfolio']['trades'][0] for v in (mid, low))
    assert a['entry_at'] == b['entry_at'] and a['stop'] == '99' and b['stop'] == '98'
    assert D(a['target']) == D(a['entry_price']) + 2 * (D(a['entry_price']) - D(a['stop']))
    assert D(b['target']) == D(b['entry_price']) + 2 * (D(b['entry_price']) - D(b['stop']))
    assert a['exit_at'] < b['exit_at']
    assert mid['manifest']['engine']['strategy_version'] != low['manifest']['engine']['strategy_version']


def test_delay_gates_candle_starts_and_missing_intervening_minutes_still_pause():
    bars = full_day()
    selected = policy(range_minutes=5, entry_delay_minutes=15)
    signal = replay(bars, entry_policy=selected).signals[0]
    session = exchange_session(date(2026, 11, 4))
    assert signal.bar_start == session.opens_at + timedelta(minutes=20)
    assert signal.observed_at == session.opens_at + timedelta(minutes=21)
    broken = [b for b in bars if not (b.symbol == 'SPY' and b.start == session.opens_at + timedelta(minutes=10))]
    result = evaluate(broken, entry_policy=selected)
    assert not result['portfolio']['replay']['signals']
    assert not result['summary']['complete'] and result['summary']['total_return'] is None
    assert any(s['reason'] == 'missing_or_out_of_order_bar' for s in result['portfolio']['replay']['sessions'])


@pytest.mark.parametrize('breakout_minute,expected', [(28, 1), (29, 0), (30, 0)])
def test_completed_close_must_be_strictly_before_custom_cutoff(breakout_minute, expected):
    result = replay(full_day(breakout_minute=breakout_minute),
                    entry_policy=policy(range_minutes=5, entry_cutoff_local='10:00'))
    assert len(result.signals) == expected


@pytest.mark.parametrize('minutes', [5, 15, 30])
def test_range_requires_exact_distinct_completed_minutes_and_freezes(minutes):
    bars = full_day(breakout_minute=minutes)
    session = exchange_session(date(2026, 11, 4))
    p = policy(range_minutes=minutes)
    ready = p.range_end(session)
    with pytest.raises(ValueError, match='not complete'):
        calculate_opening_range(bars, 'SPY', session.day, ready - timedelta(seconds=1), range_minutes=minutes)
    opening = calculate_opening_range(bars, 'SPY', session.day, ready, range_minutes=minutes)
    assert opening.high == 100 and opening.low == 98
    broken = [b for b in bars if not (b.symbol == 'SPY' and b.start == session.opens_at + timedelta(minutes=2))]
    assert not replay(broken, entry_policy=p).signals
    original = replay(bars, entry_policy=p).signals
    future = [replace(b, high=D('800')) if b.start > original[0].observed_at else b for b in bars]
    assert replay(future, entry_policy=p).signals == original


@pytest.mark.parametrize('changes', [dict(range_minutes='10'), dict(range_minutes=5),
    dict(entry_delay_minutes='1'), dict(entry_delay_minutes=False), dict(entry_cutoff_local='9:45'),
    dict(entry_cutoff_local='10:01'), dict(stop_policy='trailing'), dict(stop_policy=None),
    dict(range_minutes='30', entry_delay_minutes='30', entry_cutoff_local='10:00')])
def test_invalid_or_empty_windows_fail_before_any_evaluation(changes):
    with pytest.raises(ValueError):
        ResearchSettings.from_dict(settings(**changes))


def test_policy_and_settings_versions_are_explicit_and_legacy_bytes_stay_legacy():
    for s in (ResearchSettings(), ResearchSettings(schema_version='orb-research-settings-v2'),
              ResearchSettings(schema_version='orb-research-settings-v3')):
        old = s.to_dict()
        assert not set(TIMING_FIELDS) & set(old)
        assert ResearchSettings.from_dict(old).to_dict() == old
        assert ResearchSettings.from_dict(s.current_values()).policy.is_reference
    selected = ResearchSettings.from_dict(settings(range_minutes='5', stop_policy='range_low'))
    assert ResearchSettings.from_config({'entry_caps': selected.engine_kwargs()['entry_caps'].to_dict(),
        'entry_policy': selected.policy.to_dict(), **{k: v for k, v in selected.to_dict().items()
            if k in ('initial_cash', 'risk_fraction', 'max_position_fraction', 'max_gross_fraction',
                     'max_total_risk_fraction', 'daily_loss_fraction', 'slippage_bps', 'fee_per_share')}}) == selected
    assert EntryPolicy.from_dict(selected.policy.to_dict()) == selected.policy
    with pytest.raises(ValueError): EntryPolicy(range_minutes=5)
    malformed = selected.policy.to_dict(); malformed.pop('stop_policy')
    with pytest.raises(ValueError): EntryPolicy.from_dict(malformed)


def test_paper_rejects_new_variants_explicitly_but_accepts_reference_window_v4():
    with PresetStore() as store:
        source = store.paper('paper-balanced')
        valid = ResearchSettings.from_dict(source['settings']).current_values()
        saved = store.save('V4 equivalent paper experiment', valid)
        assert PaperEngine.validate(saved, 'iex').max_session_entries == 1
        for key, value in [('range_minutes', '5'), ('entry_delay_minutes', '5'),
                           ('entry_cutoff_local', '11:30'), ('stop_policy', 'range_low')]:
            selected = store.save('Research-only variant', {**valid, key: value})
            with pytest.raises(PaperError, match='research-only'):
                PaperEngine.validate(selected, 'iex')


def test_frozen_profile_import_chart_and_report_all_agree(tmp_path):
    bars = full_day()
    original = build_dashboard_run(bars)
    original['import_context'] = dict(synthetic=True, kind='test')
    before = deepcopy(original)
    with PresetStore(tmp_path / 'p.sqlite') as store:
        p = store.save('5m low experiment', settings(range_minutes='5', stop_policy='range_low'))
        run = apply_preset(original, p)
        store.save('30m later draft', settings(range_minutes='30'), preset_id=p['id'], expected_version=1)
    assert original == before
    assert run['opening_ranges'][0]['available_at'] == (bars[0].start + timedelta(minutes=5)).isoformat()
    assert run['applied_settings']['values']['effective_rules']['stop_policy'] == 'range_low'
    assert run['applied_settings']['preset']['version'] == 1
    imported = import_dashboard(dict(kind='csv', source=csv_text(bars), report=run['evidence']))
    assert imported['evidence']['manifest']['profile'] == run['evidence']['manifest']['profile']
    assert imported['decisions'] == run['decisions']
    text = render_report(run['evidence'])
    assert 'Opening range: 5 minutes' in text and 'range_low' in text
    assert 'strictly before 11:00' in text


def test_cli_and_api_use_the_same_v4_contract_and_immutable_result(tmp_path):
    values = settings(range_minutes='5', stop_policy='range_low')
    source = tmp_path / 'source.csv'; source.write_text(csv_text(full_day()))
    file = tmp_path / 'settings.json'; file.write_text(json.dumps(values))
    db = tmp_path / 'p.sqlite'; out = tmp_path / 'run.json'
    def cli(*args):
        return subprocess.run([sys.executable, '-m', 'orb.presets', '--db', str(db), *args],
                              text=True, capture_output=True)
    created = cli('create', '--name', '5m CLI', '--settings', str(file))
    assert created.returncode == 0, created.stderr
    preset = json.loads(created.stdout)
    launched = cli('run', '--id', preset['id'], '--version', '1', '--csv', str(source), '--output', str(out))
    assert launched.returncode == 0, launched.stderr
    run = json.loads(out.read_text())
    assert run['decisions'][0]['trade']['stop'] == '98'
    with TestClient(create_app(preset_db=db), base_url='http://127.0.0.1:8765') as c:
        imported = c.post('/api/import', json=dict(kind='csv', source=source.read_text(), report=run['evidence']))
        assert imported.status_code == 201, imported.text
        assert c.get('/api/presets').json()['contract']['schema_version'] == STRATEGY_SCHEMA
        bad = c.post('/api/presets', json=dict(name='Bad', settings={**values, 'stop_policy':'trailing'}))
        assert bad.status_code == 422
        snap = c.get('/api/runs/' + imported.json()['id']).json()
        assert snap['applied_settings']['preset']['settings'] == values


def test_custom_stop_keeps_conservative_ambiguous_exit_and_missing_position_gates():
    bars = full_day()
    session = exchange_session(date(2026, 11, 4))
    at = session.opens_at + timedelta(minutes=6)
    changed = [replace(b, high=D('108'), low=D('97')) if b.symbol == 'SPY' and b.start == at else b for b in bars]
    p = policy(range_minutes=5, stop_policy='range_low')
    trade = evaluate(changed, entry_policy=p)['portfolio']['trades'][0]
    assert trade['reason'] == 'stop' and trade['ambiguous_exit'] and trade['exit_price'] == '98'
    missing = [b for b in bars if not (b.symbol == 'SPY' and b.start == at)]
    result = evaluate(missing, entry_policy=p)
    assert not result['summary']['complete'] and result['summary']['total_return'] is None
    assert result['portfolio']['trades'][0]['reason'] == 'missing_entry_bar'


def test_custom_cutoff_only_gates_entries_and_does_not_stop_position_management():
    bars = full_day(breakout_minute=28)
    session = exchange_session(date(2026, 11, 4))
    # Keep the position below its target until after the 10:00 entry cutoff.
    changed = []
    for b in bars:
        minute = int((b.start - session.opens_at).total_seconds() // 60)
        if b.symbol == 'SPY' and 29 <= minute < 35:
            b = replace(b, high=D('101.2'))
        changed.append(b)
    p = policy(range_minutes=5, entry_cutoff_local='10:00')
    trade = evaluate(changed, entry_policy=p)['portfolio']['trades'][0]
    assert trade['status'] == 'closed' and trade['reason'] == 'target'
    assert trade['exit_at'] > p.entry_cutoff(session).isoformat()


def test_v4_queued_job_retains_exact_window_across_preset_edit_and_server_restart(tmp_path):
    def client():
        return TestClient(create_app(preset_db=tmp_path/'p.sqlite', research_db=tmp_path/'r.sqlite'),
                          base_url='http://127.0.0.1:8765')
    with client() as c:
        source = c.post('/api/import', json=dict(kind='csv', source=csv_text(full_day()))).json()['id']
        saved = c.post('/api/presets', json=dict(name='Frozen 5m', settings=settings(range_minutes='5'))).json()
        job = c.post(f'/api/runs/{source}/queue-preset', json=dict(preset_id=saved['id'], version=1)).json()
        assert job['state'] == 'queued'
        edited = c.post('/api/presets', json=dict(id=saved['id'], expected_version=1, name='Later 30m',
                                                settings=settings(range_minutes='30')))
        assert edited.status_code == 201
    with client() as c:
        done = c.post('/api/jobs/work-one').json()
        assert done['state'] == 'succeeded'
        result = c.get('/api/runs/' + done['result_id']).json()
        assert result['applied_settings']['preset']['version'] == 1
        assert result['applied_settings']['values']['effective_rules']['range_minutes'] == 5
        assert result['decisions'][0]['trade']['entry_at']
    with client() as c:
        assert c.get('/api/runs/' + done['result_id']).json() == result


@pytest.mark.parametrize('key,value', [('range_minutes','5'), ('entry_delay_minutes','5'),
                                      ('entry_cutoff_local','11:30'), ('stop_policy','range_low')])
def test_weekday_only_comparisons_reject_timing_or_stop_confounders(key, value):
    from orb.comparison import validate_plan
    from test_comparison import plan, original
    p = plan()
    with PresetStore() as store:
        p['presets'][1] = store.save('Not schedule-only', settings(weekdays=[0,2,4], **{key:value}))
    with pytest.raises(ValueError, match='identical'):
        validate_plan(p, original())


def test_alternate_window_signal_is_not_accepted_by_baseline_only_durable_journal():
    from orb.journal import EntryIntent, JournalError
    signal = replay(full_day(), entry_policy=policy(range_minutes=5)).signals[0]
    with pytest.raises(JournalError, match='baseline'):
        EntryIntent.from_signal(signal, 1, 'a'*64)
