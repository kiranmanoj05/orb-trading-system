from copy import deepcopy
from dataclasses import replace
from datetime import timezone
from decimal import Decimal as D
import json
from pathlib import Path
import subprocess
import sys

import pytest

from orb.dashboard import build_dashboard_run
from orb.dashboard_fixtures import scenarios
from orb.evaluation import _digest


@pytest.mark.parametrize('name,eligible,kind', [
    ('complete_trade', True, 'closed'), ('known_skip', True, 'known_no_entry'),
    ('missing_entry', False, 'uncertain_entry'),
])
def test_synthetic_acceptance_runs_expose_evidence_without_execution(name, eligible, kind):
    run = build_dashboard_run(scenarios()[name], data_label='synthetic')
    assert run['metrics_eligible'] is eligible
    assert run['decisions'][0]['outcome_class'] == kind
    assert run['mode'] == 'historical_simulation'
    assert not any(run['capabilities'].values())
    assert run['evidence']['manifest']['dataset']['provenance_verified'] is False
    assert run['evaluation_id'] == run['evidence']['evaluation_id']
    assert len(run['opening_ranges']) == 2
    assert len(run['candles']) == (779 if name == 'missing_entry' else 780)
    assert run['candles'][0]['start'] == '2026-10-09T13:30:00+00:00'
    assert run['candles'][0]['available_at'] == '2026-10-09T13:31:00+00:00'
    assert run['opening_ranges'][0]['available_at'] == '2026-10-09T13:45:00+00:00'
    assert json.loads(json.dumps(run)) == run


def test_canonical_chart_and_config_identity_are_reproducible():
    bars = scenarios()['complete_trade']
    other = sorted((replace(b, start=b.start.astimezone(timezone.utc), open=b.open * D('1.00'))
                    for b in reversed(bars)), key=lambda b: b.start)
    assert build_dashboard_run(bars) == build_dashboard_run(other)
    a = build_dashboard_run(bars)
    b = build_dashboard_run(bars, fee_per_share=D('.01'))
    assert a['applied_settings']['sha256'] != b['applied_settings']['sha256']
    assert b['applied_settings']['sha256'] == _digest(b['applied_settings']['values'])
    assert b['applied_settings']['values']['fee_per_share'] == '0.01'
    assert b['applied_settings']['preset'] is None  # No saved preset implementation claimed.


def test_chart_never_repairs_missing_range_or_mixes_signal_target_with_fill():
    bars = scenarios()['complete_trade']
    run = build_dashboard_run(bars)
    decision = run['decisions'][0]
    assert D(decision['signal']['target']) != D(decision['trade']['target'])
    assert all(e['signal_id'] == decision['signal_id'] for e in decision['events'])
    missing = build_dashboard_run(bars[1:])
    assert [r['symbol'] for r in missing['opening_ranges']] == ['QQQ']
    assert not missing['health']['coverage_complete']
    # Editing a view cannot silently mutate the retained research evidence.
    original = deepcopy(run['evidence'])
    decision['trade']['reason'] = 'changed display'
    run['applied_settings']['values']['risk_fraction'] = '1'
    assert run['evidence'] == original


def test_invalid_source_order_rejected_instead_of_repaired_for_chart():
    with pytest.raises(ValueError, match='chronological'):
        build_dashboard_run(list(reversed(scenarios()['complete_trade'])))


def test_fixture_cli_is_repeatable_and_will_not_replace_existing_directory(tmp_path):
    def run(out):
        return subprocess.run([sys.executable, '-m', 'orb.dashboard_fixtures', '--out-dir', str(out)],
                              capture_output=True, text=True)
    first, second = tmp_path/'one', tmp_path/'two'
    assert run(first).returncode == run(second).returncode == 0
    assert sorted(p.name for p in first.iterdir()) == sorted(p.name for p in second.iterdir())
    assert len(list(first.iterdir())) == 6
    for p in first.iterdir():
        assert p.read_bytes() == (second/p.name).read_bytes()
    before = (first/'known_skip.json').read_bytes()
    assert run(first).returncode != 0
    assert (first/'known_skip.json').read_bytes() == before
