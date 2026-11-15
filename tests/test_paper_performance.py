"""Owned-fill cost estimates cannot manufacture broker fees or recovery authority."""
from datetime import timedelta
from decimal import Decimal as D
import json

import pytest

from fastapi.testclient import TestClient

from orb.dashboard_api import create_app
from orb.paper_trading import PaperEngine
from test_paper_activities import setup, save, full, activity
from test_paper_evidence import DAY, seed, capture, snapshot


def test_exact_fill_cost_estimate_uses_frozen_preset_not_account_cash(tmp_path):
    e, j, _ = setup(tmp_path)
    row = e.db.execute('SELECT * FROM paper_attempts').fetchone()
    p = json.loads(row['preset']); p['settings'].update(fee_per_share='0.005', slippage_bps='2')
    from orb.research_settings import ResearchSettings
    p['settings_sha256'] = ResearchSettings.from_dict(p['settings']).fingerprint
    e.db.execute('UPDATE paper_attempts SET preset=?', (json.dumps(p),))
    save(j, full())
    changes = e.db.total_changes
    r = e.performance(); a = r['attempts'][0]
    assert e.db.total_changes == changes
    assert a['state'] == 'conditional_cost_estimate'
    assert a['gross_price_pnl_usd'] == '20'
    assert a['modelled_fee_usd'] == '0.1'
    assert a['additional_cost_stress_usd'] == '0.404'
    assert a['estimated_net_pnl_usd'] == '19.496'
    assert r['totals']['estimated_net_pnl_usd'] == '19.496'
    assert a['broker_net_pnl_usd'] is a['broker_trade_fees_usd'] is None
    assert not r['fee_coverage_verified'] and not r['recovery_clearance']
    assert r == e.performance()
    assert 'orbp-fixture' not in json.dumps(r)
    e.close()


def test_estimate_unavailable_without_fills_and_price_disagreement_is_explicit(tmp_path):
    e, j, _ = setup(tmp_path)
    assert e.performance()['totals']['estimated_net_pnl_usd'] is None
    save(j, [activity(price='99'), full()[1]])
    r = e.performance()
    assert r['attempts'][0]['reason'] == 'fill_price_disagreement'
    assert r['totals']['estimated_net_pnl_usd'] is None
    e.close()


def test_correction_hold_does_not_become_profitable_estimate_on_refresh(tmp_path):
    e, j, _ = setup(tmp_path); save(j, full())
    items = full(); items[0]['price'] = '99'
    save(j, items)
    r = e.performance()
    assert r['attempts'][0]['reason'] == 'correction_hold'
    assert all(v is None for v in r['totals'].values())
    assert e.evidence.holds()
    e.close()


def test_no_entry_zero_requires_order_and_activity_evidence(tmp_path):
    e = PaperEngine(tmp_path/'p.sqlite'); row = seed(e); p = snapshot('canceled')
    for leg in p['legs']: leg['status'] = 'canceled'
    capture(e, row, p)
    assert e.performance()['totals']['estimated_net_pnl_usd'] is None
    from orb.paper_activities import PaperActivities
    save(PaperActivities(e.db), [])
    r = e.performance()
    assert r['attempts'][0]['state'] == 'known_no_entry'
    assert r['totals']['estimated_net_pnl_usd'] == '0'
    e.close()


def test_reopen_preserves_report_without_start_or_network(tmp_path):
    e, j, path = setup(tmp_path); save(j, full()); before = e.performance(); e.close()
    e = PaperEngine(path)
    assert e.performance() == before and not e.enabled and e.thread is None
    e.close()


def test_empty_paper_report_api_is_read_only_and_local(tmp_path):
    app = create_app(paper_db=tmp_path/'p.sqlite')
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        engine = app.state.paper_engine; changes = engine.db.total_changes
        r = client.get('/api/paper/performance')
        assert r.status_code == 200 and r.json()['attempts'] == []
        assert r.json()['totals']['estimated_net_pnl_usd'] is None
        assert engine.db.total_changes == changes and not engine.enabled
        assert client.get('/api/paper/performance', headers={'origin':'https://external.example'}).status_code == 403


def test_invalid_preset_cannot_supply_unverified_costs_and_read_transaction_closes(tmp_path):
    from orb.paper_evidence import EvidenceError
    e, j, _ = setup(tmp_path); save(j, full())
    original = e.db.execute('SELECT preset FROM paper_attempts').fetchone()[0]
    changed = json.loads(original); changed['settings']['fee_per_share'] = '10'
    e.db.execute('UPDATE paper_attempts SET preset=?', (json.dumps(changed),))
    with pytest.raises(EvidenceError, match='invalid_saved_paper_preset'): e.performance()
    assert not e.db.in_transaction
    e.db.execute('UPDATE paper_attempts SET preset=?', (original,))
    assert e.performance()['attempts'][0]['estimated_net_pnl_usd'] is not None
    e.close()


def test_fill_timestamps_must_agree_with_terminal_order_evidence(tmp_path):
    from test_paper_trading import NOW
    e, j, _ = setup(tmp_path)
    items = full()
    # Quantity/notional agreement alone cannot bless different execution times.
    items[0]['transaction_time'] = (NOW-timedelta(seconds=1)).isoformat()
    save(j, items)
    r = e.performance()
    assert r['attempts'][0]['reason'] == 'fill_timestamp_disagreement'
    assert r['totals']['estimated_net_pnl_usd'] is None
    assert not e.db.in_transaction and not e.enabled
    e.close()
