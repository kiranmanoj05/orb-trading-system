"""Explicit SPY study scope, frozen paper rules and complete/unknown outcomes."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from orb.dashboard import build_dashboard_run
from orb.dashboard_api import create_app
from orb.dashboard_fixtures import scenarios
from orb.evaluation import evaluate
from orb.paper_alignment import from_run, study, render_study
from orb.paper_trading import PaperError
from orb.presets import PresetStore
from orb.research_settings import ResearchSettings
from test_evaluation import complete_day


def original(name='complete_trade'):
    run = build_dashboard_run(scenarios()[name], data_label='synthetic-alignment')
    run['import_context'] = {'synthetic': True}
    return run


def paper(): return PresetStore.paper('paper-balanced')


def test_baseline_is_unchanged_and_spy_scope_retains_full_source_identity():
    bars = scenarios()['complete_trade']
    baseline = evaluate(bars); spy = evaluate(bars, symbols=('SPY',))
    assert baseline['manifest']['schema_version'] == 'orb-evaluation-v5'
    assert baseline['manifest']['config']['symbols'] == ['SPY','QQQ']
    assert spy['manifest']['schema_version'] == 'orb-evaluation-v8'
    assert spy['manifest']['config']['symbols'] == ['SPY']
    assert spy['manifest']['dataset'] == baseline['manifest']['dataset']
    assert {c['symbol'] for c in spy['coverage']} == {'SPY'}
    assert spy['summary']['complete']


def test_both_cost_cells_recompute_exact_paper_settings_without_mutating_inputs():
    source = original(); snapshot = paper(); before = deepcopy((source,snapshot))
    result = from_run(source,snapshot)
    assert (source,snapshot) == before
    assert result == from_run(source,snapshot)
    assert result['parent_evaluation_id'] == source['evaluation_id']
    assert result['source_dataset']['sha256'] == source['evidence']['manifest']['dataset']['sha256']
    assert result['preset']['settings'] == snapshot['settings']
    assert result['evaluated_symbols'] == ['SPY'] and result['excluded_source_symbols'] == ['QQQ']
    assert result['all_trials_eligible'] and len(result['trials']) == 2
    for row in result['trials']:
        config = row['report']['config']
        assert config['symbols'] == ['SPY']
        assert config['initial_cash'] == '100000' and config['risk_fraction'] == '0.005'
        assert config['entry_caps']['max_session_entries'] == 1
        assert len(row['trades']) == 1 and row['trades'][0]['symbol'] == 'SPY'
        assert row['report']['synthetic'] and not row['report']['profitability_established']
        assert row['report']['applied_settings']['preset']['settings_sha256'] == snapshot['settings_sha256']
    a,b = result['trials']
    assert b['report']['engine_metrics']['fees_paid_usd'] != a['report']['engine_metrics']['fees_paid_usd']
    assert D(result['stress_delta_net_pnl_usd']) == D(b['report']['trade_statistics']['net_pnl_usd'])-D(a['report']['trade_statistics']['net_pnl_usd'])
    assert not result['broker_execution_verified'] and not result['recovery_clearance']
    assert 'Historical simulation only' in render_study(result)


def test_excluded_qqq_candidate_cannot_consume_spy_risk_or_session_capacity():
    spy = [b for b in scenarios()['complete_trade'] if b.symbol == 'SPY']
    both = sorted(spy+[replace(b,symbol='QQQ') for b in spy], key=lambda b:(b.start,b.symbol))
    full = study(both,paper()); solo = study(spy,paper())
    for a,b in zip(full['trials'],solo['trials']):
        assert a['trades'] == b['trades']
        assert a['report']['trade_statistics'] == b['report']['trade_statistics']
        assert a['report']['config']['symbols'] == ['SPY']
    assert full['source_dataset']['sha256'] != solo['source_dataset']['sha256']


def test_missing_qqq_remains_in_baseline_but_is_not_required_by_declared_spy_scope():
    bars = [b for b in scenarios()['complete_trade'] if b.symbol == 'SPY']
    assert not evaluate(bars)['summary']['complete']
    result = study(bars,paper())
    assert result['all_trials_eligible']
    assert result['excluded_source_symbols'] == []


def test_missing_spy_minutes_withhold_aggregates_even_when_qqq_is_complete():
    result = from_run(original('missing_entry'),paper())
    assert not result['all_trials_eligible'] and result['stress_delta_net_pnl_usd'] is None
    assert all(r['state']=='incomplete' and r['report']['trade_statistics']['net_pnl_usd'] is None for r in result['trials'])
    assert any(c['state'] != 'complete' for c in result['trials'][0]['coverage'])


def test_known_no_entry_does_not_become_missing_evidence_or_a_closed_win():
    result = from_run(original('known_skip'),paper())
    assert result['all_trials_eligible']
    for row in result['trials']:
        assert row['report']['trade_statistics']['closed_trade_count'] == 0
        assert row['report']['trade_statistics']['net_pnl_usd'] == '0'
        assert row['report']['trade_statistics']['win_fraction'] is None


@pytest.mark.parametrize('symbols',[('QQQ',),('SPY','SPY'),['SPY'],(),('MES',),True])
def test_unsupported_scopes_are_not_silently_repaired(symbols):
    with pytest.raises(ValueError,match='scope'): evaluate(scenarios()['complete_trade'],symbols=symbols)


def test_excluded_source_duplicates_and_wrong_chronology_are_still_rejected():
    bars = scenarios()['complete_trade']
    duplicated = sorted(bars+[next(b for b in bars if b.symbol=='QQQ')],key=lambda b:b.start)
    with pytest.raises(ValueError,match='unique source'): evaluate(duplicated,symbols=('SPY',))
    with pytest.raises(ValueError,match='chronological'): study(list(reversed(bars)),paper())


@pytest.mark.parametrize('day,minutes',[(date(2026,11,27),210),(date(2026,11,2),390)])
def test_scope_obeys_actual_early_close_and_new_york_session(day,minutes):
    result = study(complete_day(day),paper())
    coverage = result['trials'][0]['coverage']
    assert coverage[0]['expected_minutes'] == minutes and coverage[0]['session'] == str(day)
    assert len(coverage) == 1 and result['all_trials_eligible']


def test_explicit_requested_missing_session_is_not_dropped():
    result = study(scenarios()['complete_trade'],paper(),start_session=date(2026,10,9),end_session=date(2026,10,12))
    assert not result['all_trials_eligible']
    assert any(c['session']=='2026-10-12' and c['state']=='missing' for c in result['trials'][0]['coverage'])


def test_invalid_or_unsupported_preset_never_creates_paper_execution():
    p = paper(); p['settings']['risk_fraction'] = '0.02'
    with pytest.raises(ValueError,match='fingerprint'): study(scenarios()['complete_trade'],p)
    p['settings_sha256'] = ResearchSettings.from_dict(p['settings']).fingerprint
    with pytest.raises(PaperError,match='Paper v1'): study(scenarios()['complete_trade'],p)
    with pytest.raises(PaperError): study(scenarios()['complete_trade'],PresetStore.reference())


def test_extreme_cost_override_is_retained_as_rejected_not_clipped():
    p=paper();p['settings']['fee_per_share']='1000'
    p['settings_sha256']=ResearchSettings.from_dict(p['settings']).fingerprint
    r=study(scenarios()['complete_trade'],p)
    assert r['trials'][1]['state']=='unsupported_cost_settings'
    assert r['trials'][1]['report'] is None and r['stress_delta_net_pnl_usd'] is None
    assert not r['all_trials_eligible']


def test_source_binding_and_absent_spy_do_not_manufacture_results():
    source=original();source['evidence']['manifest']['dataset']['sha256']='wrong'
    with pytest.raises(ValueError,match='identity'): from_run(source,paper())
    with pytest.raises(ValueError,match='SPY source'): study([b for b in scenarios()['complete_trade'] if b.symbol=='QQQ'],paper())


def test_api_freezes_version_preserves_source_and_never_starts_worker(tmp_path):
    app=create_app(examples=True,paper_db=tmp_path/'p.sqlite')
    with TestClient(app,base_url='http://127.0.0.1:8765') as c:
        identity=c.get('/api/runs').json()[0]['id'];source=c.get('/api/runs/'+identity).json()
        p=paper();post=lambda payload:c.post('/api/runs/'+identity+'/paper-alignment',json=payload)
        changes=app.state.paper_engine.db.total_changes
        response=post({'preset_id':p['id'],'version':1})
        assert response.status_code==200 and response.json()['preset']['settings_sha256']==p['settings_sha256']
        assert c.get('/api/runs/'+identity).json()==source
        assert not app.state.paper_engine.enabled and app.state.paper_engine.thread is None
        assert app.state.paper_engine.db.total_changes==changes
        for payload in [{'preset_id':p['id'],'version':True},{'preset_id':p['id'],'version':1,'start':True}]:
            assert post(payload).status_code==422
        assert post({'preset_id':p['id'],'version':2}).status_code==404
        assert post({'preset_id':'orb-reference','version':1}).status_code==422
        assert c.post('/api/runs/missing/paper-alignment',json={'preset_id':p['id'],'version':1}).status_code==404
        assert c.post('/api/runs/'+identity+'/paper-alignment',json={'preset_id':p['id'],'version':1},headers={'origin':'https://external.example'}).status_code==403


def test_cli_recomputes_and_protects_source_and_preset_paths(tmp_path):
    path=tmp_path/'bars.csv';preset=tmp_path/'preset.json';report=tmp_path/'report.md'
    bars=scenarios()['complete_trade']
    path.write_text('symbol,start,open,high,low,close,volume\n'+''.join(f'{b.symbol},{b.start.isoformat()},{b.open},{b.high},{b.low},{b.close},{b.volume}\n' for b in bars))
    preset.write_text(json.dumps(paper()))
    args=[sys.executable,'-m','orb.paper_alignment',str(path),'--preset',str(preset),'--synthetic','--report',str(report)]
    result=subprocess.run(args,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['synthetic']
    assert 'SPY paper-preset historical study' in report.read_text()
    before=(path.read_bytes(),preset.read_bytes())
    for dest in [path,preset]:
        failed=subprocess.run(args+['--output',str(dest)],capture_output=True,text=True)
        assert failed.returncode==2
    assert before==(path.read_bytes(),preset.read_bytes())
