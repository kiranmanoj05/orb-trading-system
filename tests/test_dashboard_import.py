from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
from pathlib import Path

import pytest

from orb.dashboard_fixtures import scenarios
from orb.dashboard_import import import_dashboard, MAX_BYTES, MAX_BARS
from orb.evaluation import _digest, evaluate
from orb.ingestion import evaluate_bundle, write_bundle


def csv_text(bars):
    return 'symbol,start,open,high,low,close,volume\n' + ''.join(
        ','.join([b.symbol, b.start.isoformat(), *(str(getattr(b, k)) for k in ('open','high','low','close')),str(b.volume)])+'\n'
        for b in bars)


def payload(name='complete_trade'):
    return dict(kind='csv', source=csv_text(scenarios()[name]), label='test local source')


@pytest.mark.parametrize('name,eligible,kind', [('complete_trade',True,'closed'),
    ('known_skip',True,'known_no_entry'), ('missing_entry',False,'uncertain_entry')])
def test_source_import_preserves_outcomes_and_null_metrics(name,eligible,kind):
    run=import_dashboard(payload(name))
    assert run['metrics_eligible'] is eligible
    assert run['decisions'][0]['outcome_class']==kind
    assert (run['evidence']['summary']['total_return'] is not None) is eligible
    assert run['import_context']['synthetic'] is None  # CSV labels are not proof.
    assert not run['import_context']['provider_authenticity_verified']
    assert not any(run['capabilities'].values())


def test_saved_report_settings_and_original_identity_are_retained_not_metrics():
    bars=scenarios()['complete_trade']
    saved=evaluate(bars, initial_cash=D('12000'), fee_per_share=D('.001'), slippage_bps=D('2'))
    original=saved['evaluation_id']
    saved['summary']['total_return']='999999'  # Uploaded economic claims are ignored.
    run=import_dashboard({**payload(), 'report':saved})
    assert run['import_context']['original_evaluation_id']==original
    assert run['applied_settings']['values']['initial_cash']=='12000'
    assert run['applied_settings']['values']['fee_per_share']=='0.001'
    assert run['evidence']['summary']['total_return']!='999999'
    assert run['evaluation_id']==_digest(run['evidence']['manifest'])
    assert run['applied_settings']['sha256']==_digest(run['applied_settings']['values'])


def test_old_v4_report_is_explicitly_regenerated_without_rewriting_original():
    saved=evaluate(scenarios()['known_skip'])
    saved['manifest']['schema_version']='orb-evaluation-v4'
    saved['evaluation_id']=_digest(saved['manifest'])
    saved['summary']['complete']=False
    saved['summary']['total_return']=None
    before=deepcopy(saved)
    run=import_dashboard({**payload('known_skip'),'report':saved})
    assert run['import_context']['original_schema']=='orb-evaluation-v4'
    assert run['evidence']['manifest']['schema_version']=='orb-evaluation-v5'
    assert run['evidence']['summary']['total_return']=='0'
    assert saved==before


@pytest.mark.parametrize('case',['hash','source','unknown_setting','schema','scope','bounds','nonfinite','huge'])
def test_incompatible_saved_report_fails_instead_of_defaulting_settings(case):
    saved=evaluate(scenarios()['complete_trade'])
    p=payload()
    if case=='hash': saved['evaluation_id']='0'*64
    if case=='source': p=payload('known_skip')
    if case=='unknown_setting': saved['manifest']['config']['target_r']='3'
    if case=='schema': saved['manifest']['schema_version']='orb-evaluation-v99'
    if case=='scope': saved['manifest']['config']['symbols']=['SPY']
    if case=='bounds': saved['manifest']['config']['start_session']='2025-01-01'
    if case=='nonfinite': saved['manifest']['config']['initial_cash']='NaN'
    if case=='huge': saved['manifest']['config']['initial_cash']='1e999999999'
    if case!='hash': saved['evaluation_id']=_digest(saved['manifest'])
    with pytest.raises(ValueError): import_dashboard({**p,'report':saved})


def test_explicit_absent_session_scope_survives_import():
    bars=scenarios()['complete_trade']
    saved=evaluate(bars,start_session=__import__('datetime').date(2026,10,8),end_session=__import__('datetime').date(2026,10,9))
    run=import_dashboard({**payload(),'report':saved})
    assert not run['metrics_eligible']
    assert len(run['evidence']['coverage'])==4
    assert run['applied_settings']['values']['bounds_source']=='explicit'


def test_original_bundle_three_files_and_report_validate_together(tmp_path):
    raw=Path('fixtures/alpaca_capture_synthetic.json').read_bytes()
    bundle=tmp_path/'bundle'
    write_bundle(raw,bundle)
    saved=evaluate_bundle(bundle,fee_per_share=D('.01'))
    p=dict(kind='bundle',source=raw.decode(),bars_csv=(bundle/'bars.csv').read_text(),
           bundle_manifest=(bundle/'manifest.json').read_text(),report=saved)
    run=import_dashboard(p)
    assert run['import_context']['synthetic'] is True
    assert run['import_context']['original_evaluation_id']==saved['evaluation_id']
    assert run['evidence']['manifest']['base_schema_version']=='orb-evaluation-v5'
    assert run['evidence']['manifest']['dataset']['source_bundle']['integrity_verified']
    assert not run['metrics_eligible']
    assert run['evaluation_id']==_digest(run['evidence']['manifest'])
    for key in ('bars_csv','bundle_manifest'):
        changed={**p,key:p[key]+'X'}
        with pytest.raises(ValueError): import_dashboard(changed)
    # A raw CSV cannot replace bundle provenance even with the same candles.
    with pytest.raises(ValueError): import_dashboard(dict(kind='csv',source=p['bars_csv'],report=saved))


def test_capture_without_report_still_preserves_unverified_source_identity():
    run=import_dashboard(dict(kind='capture',source=Path('fixtures/alpaca_capture_synthetic.json').read_text()))
    assert run['import_context']['synthetic']
    assert run['evidence']['manifest']['dataset']['source_bundle']['request']['feed']=='iex'


@pytest.mark.parametrize('case',['kind','extra','blank_label','reverse','duplicates','oversized','too_many','long_span','exponent','extreme_price'])
def test_malformed_or_unbounded_source_does_not_reach_evaluation(case):
    p=payload()
    if case=='kind': p['kind']='url'
    if case=='extra': p['path']='/etc/passwd'
    if case=='blank_label':p['label']=' '
    if case=='reverse':p['source']=csv_text(list(reversed(scenarios()['complete_trade'])))
    if case=='duplicates':p['source']=csv_text(scenarios()['complete_trade'][:1]*2)
    if case=='oversized':p['source']='x'*(MAX_BYTES+1)
    if case=='too_many':p['source']='x\n'*(MAX_BARS+2)
    if case=='long_span':
        bars=scenarios()['complete_trade'];p['source']=csv_text([bars[0],replace(bars[-1],start=bars[-1].start+timedelta(days=31))])
    if case=='exponent':p['source']=p['source'].replace('100.3','1e999999999')
    if case=='extreme_price':p['source']=p['source'].replace('100.3','10000000000')
    with pytest.raises(ValueError): import_dashboard(p)
