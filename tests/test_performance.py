"""Performance arithmetic, inference bounds and source integrity; invented bars only."""
from copy import deepcopy
from decimal import Decimal as D
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from orb.dashboard import build_dashboard_run
from orb.dashboard_api import create_app
from orb.dashboard_fixtures import scenarios
from orb.performance import block_interval, historical_performance, render_performance


def run(name='complete_trade', **kwargs):
    result = build_dashboard_run(scenarios()[name], data_label='invented-performance-test', **kwargs)
    result['import_context'] = {'synthetic': True}
    return result


def test_net_metrics_benchmarks_no_dividend_claim_and_reproducible_identity():
    source = run(); before = deepcopy(source)
    r = historical_performance(source)
    assert source == before
    assert r == historical_performance(source)
    assert r['synthetic'] and r['profitability_established'] is False
    assert r['session_count'] == 1
    assert D(r['trade_statistics']['net_pnl_usd']) == D(source['evidence']['summary']['closed_subset']['net_pnl'])
    b = r['benchmarks']['passive'][0]
    assert b['quantity'] == 101 and b['net_price_pnl_usd'] == '50.5'
    assert b['return_fraction'] == '0.00505'
    assert r['session_mean_interval']['lower'] is None
    assert 'insufficient_sessions' in r['session_mean_interval']['reason']
    text = render_performance(r)
    assert 'Synthetic: True' in text and 'profitability established: False' in text
    assert 'dividends' in text and 'SPY only' in text


def test_benchmark_costs_and_whole_share_cash_constraint_are_applied_per_side():
    r = historical_performance(run(slippage_bps=D('2'), fee_per_share=D('.005')))
    b = r['benchmarks']['passive'][0]
    entry = D('99')*D('1.0002'); exit = D('99.5')*D('.9998')
    quantity = b['quantity']
    assert quantity*(entry+D('.005')) <= 10000 < (quantity+1)*(entry+D('.005'))
    assert D(b['net_price_pnl_usd']) == quantity*(exit-entry-D('.01'))
    assert D(b['modelled_fees_usd']) == quantity*D('.01')


def test_missing_data_withholds_all_aggregate_and_benchmark_performance():
    r = historical_performance(run('missing_entry'))
    assert not r['metrics_eligible']
    assert r['trade_statistics']['net_pnl_usd'] is None
    assert r['trade_statistics']['win_fraction'] is None
    assert r['trade_statistics']['profit_factor'] is None
    assert r['session_mean_interval']['reason'] == 'incomplete_evidence'
    assert all(v['return_fraction'] is None for v in r['benchmarks']['passive'])
    assert all(v['net_pnl_usd'] is None for v in r['daily'].values())


def test_known_no_trade_session_is_zero_not_omitted_or_an_invented_win():
    r = historical_performance(run('known_skip'))
    assert r['session_count'] == 1
    assert r['trade_statistics']['net_pnl_usd'] == '0'
    assert r['trade_statistics']['expectancy_usd'] is None
    assert r['trade_statistics']['win_fraction'] is None
    assert r['daily']['2026-10-09']['net_pnl_usd'] == '0'


def test_block_resampling_preserves_session_dependence_and_scaling():
    values = ['-3']*10+['1']*10+['4']*10
    r = block_interval(values)
    assert r == block_interval(values)
    assert D(r['lower']) < 0 < D(r['upper'])
    assert r['unit'] == 'NY_session_net_pnl_usd'
    scaled = block_interval([str(2*D(v)) for v in values])
    assert D(scaled['lower']).quantize(D('.000000000001')) == (2*D(r['lower'])).quantize(D('.000000000001'))
    assert D(scaled['upper']).quantize(D('.000000000001')) == (2*D(r['upper'])).quantize(D('.000000000001'))
    assert block_interval(['5']*20)['lower'] == '5'
    assert block_interval(['5']*20)['upper'] == '5'
    assert block_interval(['0']*19)['lower'] is None
    assert block_interval(values, block_length=10)['reason'].startswith('insufficient')


@pytest.mark.parametrize('kwargs', [{'block_length': 0}, {'block_length': True}, {'resamples': 199},
                                  {'resamples': 5001}, {'seed': -1}, {'seed': True}])
def test_bounded_resampling(kwargs):
    with pytest.raises(ValueError): block_interval(['1']*20, **kwargs)


def test_report_api_local_only_missing_and_format_validation():
    app = create_app(examples=True)
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        identity = client.get('/api/runs').json()[0]['id']
        response = client.get(f'/api/runs/{identity}/performance')
        assert response.status_code == 200
        assert response.json()['synthetic']
        md = client.get(f'/api/runs/{identity}/performance?format=markdown')
        assert md.status_code == 200 and 'attachment' in md.headers['content-disposition']
        assert 'ORB performance report' in md.text
        assert client.get(f'/api/runs/{identity}/performance?format=html').status_code == 422
        assert client.get('/api/runs/missing/performance').status_code == 404
        assert client.get(f'/api/runs/{identity}/performance', headers={'origin':'https://external.example'}).status_code == 403
        assert client.get('/api/paper/performance').status_code == 409


def test_cli_recomputes_from_csv_and_protects_source(tmp_path):
    bars = scenarios()['complete_trade']; path = tmp_path/'invented.csv'
    path.write_text('symbol,start,open,high,low,close,volume\n'+''.join(
        f'{b.symbol},{b.start.isoformat()},{b.open},{b.high},{b.low},{b.close},{b.volume}\n' for b in bars))
    report = tmp_path/'report.md'
    command = [sys.executable, '-m', 'orb', 'performance', str(path), '--synthetic', '--data-label', 'synthetic-invented', '--report', str(report)]
    r = subprocess.run(command, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout)
    assert result['synthetic']
    assert result['profitability_established'] is False
    assert 'ORB performance report' in report.read_text()
    original = path.read_bytes()
    failed = subprocess.run(command+['--output', str(path)], capture_output=True, text=True)
    assert failed.returncode == 2 and path.read_bytes() == original
