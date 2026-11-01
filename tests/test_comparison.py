from copy import deepcopy
from dataclasses import replace
from datetime import date,timedelta
from decimal import Decimal as D
import json
import sqlite3
import subprocess
import sys

import pytest
from orb.comparison import compare,validate_plan,render_comparison,SCHEMA
from orb.dashboard import build_dashboard_run
from orb.dashboard_fixtures import scenarios
from orb.presets import PresetStore,REFERENCE,MWF
from orb.research_settings import ResearchSettings,CAPS_SCHEMA
from orb.research_store import ResearchStore,ResearchConflict,ResearchIntegrityError,APP_ID
from test_dashboard_import import csv_text


def original(missing=False,skip=False,absent=False):
    first=scenarios()['known_skip' if skip else 'complete_trade']
    second=[replace(b,start=b.start+timedelta(days=4)) for b in first] # Tuesday after Friday.
    if missing: second=[b for b in second if not (b.symbol=='SPY' and b.start.minute==46 and b.start.hour==13)]
    monday=[replace(b,start=b.start+timedelta(days=3)) for b in first]
    run=build_dashboard_run(first+monday+([] if absent else second),data_label='synthetic-comparison',
        start_session=date(2026,10,9),end_session=date(2026,10,13))
    run['import_context']={'synthetic':True,'notice':'invented fixtures'}
    return run


def plan(**changes):
    with PresetStore() as store:presets=[store.get(REFERENCE),store.get(MWF)]
    return dict(schema_version=SCHEMA,name='Schedule cost check',split_session='2026-10-13',presets=presets,
                costs=[dict(name='Zero diagnostic',slippage_bps='0',fee_per_share='0'),
                       dict(name='Illustrative stress',slippage_bps='2',fee_per_share='0.005')],**changes)


def test_actual_schedule_cost_and_partition_effects_all_trials_recorded():
    r,runs=compare(original(),plan())
    assert len(r['rows'])==len(runs)==8
    assert r['manifest']['holdout_status'].startswith('retrospective_unsealed')
    rows=r['rows'];lookup={(x['period'],x['preset_id'],x['cost_name']):x for x in rows}
    a=lookup['validation',REFERENCE,'Zero diagnostic'];b=lookup['validation',MWF,'Zero diagnostic']
    assert a['metrics']['closed_trade_count']==1 and b['metrics']['closed_trade_count']==0
    assert b['metrics']['total_return_fraction']=='0' and b['metrics']['expectancy_net_r'] is None
    assert b['metrics']['expectancy_reason']=='no_closed_trades'
    stressed=lookup['validation',REFERENCE,'Illustrative stress']
    assert D(stressed['metrics']['total_return_fraction'])!=D(a['metrics']['total_return_fraction'])
    # Cost changes also move fill-based targets and whole-share sizing. Net return is not forced monotone.
    assert D(stressed['metrics']['fees_paid_usd'])>0
    assert len({run['evaluation_id'] for run in runs})==8
    for row in rows:
        assert row['state']=='complete'
        assert row['metrics']['mean_capital_exposure_fraction'] is not None
    pairs=[p for p in r['paired_sessions'] if p['period']=='validation' and p['cost_name']=='Zero diagnostic'][0]
    assert pairs['eligible_paired_sessions']==1
    assert D(pairs['sessions'][0]['delta_net_pnl_usd'])<0
    assert pairs['uncertainty_interval'] is None
    assert 'Return (fraction)' in render_comparison(r)


def test_r_denominator_risk_utilisation_and_fee_arithmetic_are_explicit():
    r,runs=compare(original(),plan())
    row=next(r for r in r['rows'] if r['period']=='validation' and r['preset_id']==REFERENCE and r['cost_name']=='Illustrative stress')
    run=next(v for v in runs if v['evaluation_id']==row['run_id'])
    a=run['evidence']['portfolio']['account']['allocations'][0]
    t=run['evidence']['portfolio']['trades'][0]
    initial=(D(t['entry_price'])-D(a['stop'])*(1-D('2')/10000)+2*D('0.005'))*a['quantity']
    assert D(a['committed_stop_risk'])==initial
    assert D(row['metrics']['expectancy_net_r'])==D(t['net_pnl'])/initial
    assert D(row['metrics']['fees_paid_usd'])==2*D('0.005')*a['quantity']
    assert D('0')<D(row['metrics']['mean_actual_initial_risk_utilisation_fraction'])<=1


def test_missing_validation_does_not_rewrite_development_and_cannot_hide_exposure():
    complete,_=compare(original(),plan());incomplete,runs=compare(original(True),plan())
    dev=lambda r:[x['metrics'] for x in r['rows'] if x['period']=='development']
    assert dev(complete)==dev(incomplete)
    for x in incomplete['rows']:
        if x['period']=='validation':
            assert x['state']=='incomplete'
            assert x['metrics']['total_return_fraction'] is None
            assert x['metrics']['expectancy_net_r'] is None
            assert x['metrics']['mean_capital_exposure_fraction'] is None
    assert all(p['eligible_paired_sessions']==0 for p in incomplete['paired_sessions'] if p['period']=='validation')


def test_wholly_absent_requested_period_and_known_no_fill_remain_distinct():
    run=original(absent=True)
    # The parent evidence declares both dates, so absent validation is still requested.
    r,runs=compare(run,plan())
    assert len(runs)==4
    assert all(x['state']=='missing_period_data' and x['metrics'] is None for x in r['rows'] if x['period']=='validation')
    known,_=compare(original(skip=True),plan())
    assert all(x['state']=='complete' for x in known['rows'])
    assert all(x['metrics']['total_return_fraction']=='0' for x in known['rows'])


@pytest.mark.parametrize('split',['2026-10-09','2026-10-14','2026-10-10','not-a-date',None])
def test_invalid_split_never_rolls_holiday_or_repairs_date(split):
    p=plan();p['split_session']=split
    with pytest.raises((ValueError,TypeError)):validate_plan(p,original())


@pytest.mark.parametrize('key,value',[('target_r','3'),('risk_fraction','0.003'),('fee_per_share','0.01'),('max_session_entries','1')])
def test_non_schedule_differences_rejected_not_silently_ignored(key,value):
    p=plan()
    with PresetStore() as store:
        settings=ResearchSettings(weekdays=(0,2,4),schema_version=CAPS_SCHEMA).to_dict();settings[key]=value
        p['presets'][1]=store.save('different',settings)
    with pytest.raises(ValueError,match='identical'):validate_plan(p,original())


def test_unknown_fields_costs_and_duplicate_schedule_are_rejected():
    for mutate in (lambda p:p.update(extra=True),lambda p:p['costs'][0].update(slippage_bps=True),
                   lambda p:p['costs'].append(deepcopy(p['costs'][0])),
                   lambda p:p['presets'].__setitem__(1,p['presets'][0])):
        p=plan();mutate(p)
        with pytest.raises(ValueError):validate_plan(p,original())


def seed(store):
    run=original()
    source={'kind':'csv','source':csv_text([replace(b,start=b.start+timedelta(days=i)) for i in (0,3,4) for b in scenarios()['complete_trade']]),'label':'synthetic-comparison'}
    store.import_run(run,source)
    return run


def test_durable_comparison_atomic_runs_report_and_reopen(tmp_path):
    path=tmp_path/'research.sqlite'
    with ResearchStore(path) as store:
        parent=seed(store);job=store.enqueue_comparison(parent['evaluation_id'],plan())
        done=store.execute(job['id'])
        assert done['state']=='succeeded' and done['result_id'] is None
        result=store.get_comparison(done['comparison_id'])
        assert len(store.list_runs())==9
        assert store.enqueue_comparison(parent['evaluation_id'],plan())==done
        before=store.get_run(parent['evaluation_id'])
    with ResearchStore(path) as store:
        assert store.get_comparison(done['comparison_id'])==result
        assert store.get_run(parent['evaluation_id'])==before
        assert store.execute(job['id'])==done
        assert store.db.execute('PRAGMA user_version').fetchone()[0]==2
    out=tmp_path/'comparison.md'
    c=subprocess.run([sys.executable,'-m','orb.research_store','--db',str(path),'export-comparison','--id',done['comparison_id'],'--output',str(out)],check=True,capture_output=True)
    assert out.read_text()==render_comparison(result)


def test_failed_comparison_cannot_publish_partial_trials_or_summary(tmp_path):
    with ResearchStore(tmp_path/'research.sqlite') as store:
        parent=seed(store);job=store.enqueue_comparison(parent['evaluation_id'],plan())
        store.db.execute("CREATE TEMP TRIGGER fail_summary BEFORE INSERT ON comparisons BEGIN SELECT RAISE(ABORT,'injected'); END")
        assert store.execute(job['id'])['state']=='failed'
        assert store.list_runs()==[parent] and store.list_comparisons()==[]
        store.db.execute('DROP TRIGGER fail_summary')
        retry=store.retry(job['id']);assert store.execute(retry['id'])['comparison_id']


def test_capacity_failure_rolls_back_entire_comparison(tmp_path):
    with ResearchStore(tmp_path/'research.sqlite',max_runs=3) as store:
        parent=seed(store);job=store.enqueue_comparison(parent['evaluation_id'],plan())
        assert store.execute(job['id'])['state']=='failed'
        assert store.list_runs()==[parent] and store.list_comparisons()==[]


def test_v1_additive_research_upgrade_preserves_original_snapshots(tmp_path):
    path=tmp_path/'old.sqlite'
    with ResearchStore(path) as store:parent=seed(store);jobs=store.list_jobs()
    db=sqlite3.connect(path);db.execute('DROP TABLE comparisons');db.execute('PRAGMA user_version=1');db.commit();db.close()
    with ResearchStore(path) as store:
        assert store.get_run(parent['evaluation_id'])==parent
        assert store.list_jobs()==jobs
        assert store.list_comparisons()==[]


def test_corrupt_comparison_fails_closed(tmp_path):
    with ResearchStore(tmp_path/'research.sqlite') as store:
        parent=seed(store);done=store.execute(store.enqueue_comparison(parent['evaluation_id'],plan())['id'])
        store.db.execute("UPDATE comparisons SET snapshot='{}'")
        with pytest.raises(ResearchIntegrityError):store.get_comparison(done['comparison_id'])


def test_future_validation_prices_cannot_change_development_metrics():
    first=original();changed=deepcopy(first)
    for b in changed['candles']:
        if b['start'][:10]=='2026-10-13':b.update(open='80',high='90',low='70',close='85')
    a,_=compare(first,plan());b,_=compare(changed,plan())
    assert [r['metrics'] for r in a['rows'] if r['period']=='development']==[r['metrics'] for r in b['rows'] if r['period']=='development']


def test_custom_tuethu_uses_ny_weekdays_without_changing_other_controls():
    p=plan()
    with PresetStore() as store:
        p['presets'].append(store.save('ORB TueThu study',ResearchSettings(weekdays=(1,3),schema_version=CAPS_SCHEMA).to_dict()))
    result,runs=compare(original(),p)
    assert len(result['rows'])==len(runs)==12
    t=[r for r in result['rows'] if r['preset_name']=='ORB TueThu study' and r['cost_name']=='Zero diagnostic']
    assert t[0]['metrics']['closed_trade_count']==0
    assert t[1]['metrics']['closed_trade_count']==1


def test_zero_time_exposure_and_missing_uncertainty_are_not_invented():
    result,_=compare(original(skip=True),plan())
    assert all(r['metrics']['sampled_exposure_minutes']==0 for r in result['rows'])
    assert all(r['metrics']['mean_capital_exposure_fraction']=='0' for r in result['rows'])
    assert all(p['uncertainty_interval'] is None for p in result['paired_sessions'])


def test_comparison_work_budget_is_enforced_before_execution():
    run=original();run['candles']*=6
    with pytest.raises(ValueError,match='50000 evaluated'):validate_plan(plan(),run)


def test_one_cost_scenario_is_supported_without_invented_sensitivity_delta():
    p=plan();p['costs']=p['costs'][:1]
    result,runs=compare(original(),p)
    assert len(result['rows'])==len(runs)==4 and result['cost_sensitivity']==[]
