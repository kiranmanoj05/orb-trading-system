"""Bounded, retrospective schedule/cost comparisons through the real engine."""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D

from orb.dashboard import build_dashboard_run
from orb.domain import Bar
from orb.evaluation import _digest
from orb.outcomes import outcome_class
from orb.presets import PresetStore
from orb.profile_runs import validated_snapshot
from orb.research_settings import ResearchSettings, canonical
from orb.session import exchange_session
from orb.strategy import NEW_YORK

SCHEMA = 'orb-comparison-plan-v1'
MAX_EVALUATED_BARS = 50_000


def _name(value):
    if not isinstance(value,str) or not 1<=len(value.strip())<=80 or not value.isprintable():
        raise ValueError('comparison labels must be 1–80 printable characters')
    return value.strip()


def validate_plan(value, original):
    if not isinstance(value,dict) or set(value)!={'schema_version','name','split_session','presets','costs'} or value['schema_version']!=SCHEMA:
        raise ValueError('unsupported comparison plan')
    config=original['evidence']['manifest']['config']
    start,end=(date.fromisoformat(config[k]) for k in ('start_session','end_session'))
    if type(value['split_session']) is not str:
        raise ValueError('split session must be an ISO New York session date')
    split=date.fromisoformat(value['split_session'])
    if str(split)!=value['split_session'] or not start<split<=end or exchange_session(split) is None:
        raise ValueError('split must be an actual exchange session strictly after source start and within source bounds')
    days=[start+timedelta(days=i) for i in range((split-start).days)]
    if not any(exchange_session(d) for d in days):
        raise ValueError('development period must contain an exchange session')
    if not isinstance(value['presets'],list) or not 2<=len(value['presets'])<=3:
        raise ValueError('compare two or three frozen schedule presets')
    presets=[validated_snapshot(p) for p in value['presets']]
    if presets[0]!=validated_snapshot(PresetStore.reference()):
        raise ValueError('first comparison preset must be the fixed ORB Reference')
    base=ResearchSettings.from_dict(presets[0]['settings']).current_values()
    schedules=[]
    for p in presets:
        settings=ResearchSettings.from_dict(p['settings']).current_values()
        schedules.append(tuple(settings.pop('weekdays')))
        if settings!={k:v for k,v in base.items() if k!='weekdays'}:
            raise ValueError('schedule-only comparison requires identical risk, target, capital, caps and base costs')
    if len(set(schedules))!=len(schedules):
        raise ValueError('comparison schedules must be distinct')
    if not isinstance(value['costs'],list) or not 1<=len(value['costs'])<=2:
        raise ValueError('provide one or two common cost scenarios')
    costs=[]
    for c in value['costs']:
        if not isinstance(c,dict) or set(c)!={'name','slippage_bps','fee_per_share'}:
            raise ValueError('unsupported cost scenario fields')
        settings=ResearchSettings.from_dict({**base,**{k:c[k] for k in ('slippage_bps','fee_per_share')}})
        costs.append(dict(name=_name(c['name']),slippage_bps=canonical(settings.slippage_bps),fee_per_share=canonical(settings.fee_per_share)))
    if len({c['name'] for c in costs})!=len(costs) or len({(c['slippage_bps'],c['fee_per_share']) for c in costs})!=len(costs):
        raise ValueError('cost scenarios must have distinct names and values')
    if len(original['candles'])*len(presets)*len(costs)>MAX_EVALUATED_BARS:
        raise ValueError('comparison exceeds 50000 evaluated source-bar variants; use a smaller frozen source or family')
    return dict(schema_version=SCHEMA,name=_name(value['name']),split_session=str(split),presets=presets,costs=costs)


def _sessions(start,end):
    return [start+timedelta(days=i) for i in range((end-start).days+1) if exchange_session(start+timedelta(days=i))]


def _metrics(run):
    e=run['evidence'];trades=e['portfolio']['trades'];complete=run['metrics_eligible']
    closed=[t for t in trades if t['status']=='closed']
    allocations={a['order_id']:a for a in e['portfolio']['account']['allocations']}
    rs=[];utilisation=[]
    budgets={v['signal_id']:D(v['sizing']['risk_budget']) for v in e['portfolio']['events'] if v.get('sizing') and v['sizing']['quantity']}
    for t in trades:
        a=allocations.get(t['signal_id'])
        if a and t['signal_id'] in budgets:
            risk=D(a['committed_stop_risk'])
            utilisation.append(risk/budgets[t['signal_id']])
            if t['status']=='closed' and risk>0:rs.append(D(t['net_pnl'])/risk)
    samples=e['valuation']['samples'];exposed=D('0');weighted=D('0');minutes=D('0')
    for left,right in zip(samples,samples[1:]):
        span=(datetime.fromisoformat(right['at'])-datetime.fromisoformat(left['at'])).total_seconds()
        if span!=60:continue  # No overnight interpolation or assumed overnight value.
        minutes+=1
        if left['positions']:exposed+=1
        if left['equity'] is not None and D(left['equity'])>0 and left['gross_market_exposure'] is not None:
            weighted+=D(left['gross_market_exposure'])/D(left['equity'])
    mean=lambda values:canonical(sum(values,D('0'))/len(values)) if values else None
    return {'metrics_eligible':complete,'total_return_fraction':e['summary']['total_return'],
            'sampled_max_drawdown_fraction':e['summary']['max_drawdown'],
            'candidate_count':len(trades),'closed_trade_count':len(closed),
            'outcome_class_counts':e['summary']['outcome_class_counts'],
            'closed_subset_net_pnl_usd':e['summary']['closed_subset']['net_pnl'],
            'closed_subset_mean_net_r':mean(rs),
            'expectancy_net_r':mean(rs) if complete else None,
            'expectancy_reason':None if complete and rs else 'no_closed_trades' if complete else 'incomplete_evidence',
            'mean_actual_initial_risk_utilisation_fraction':mean(utilisation),
            'sampled_exposure_minutes':int(exposed) if complete else None,
            'mean_capital_exposure_fraction':canonical(weighted/minutes) if complete and minutes else None,
            'fees_paid_usd':canonical(sum((D(a['entry_fee'])+D(a['exit_fee']) for a in allocations.values()),D('0'))),
            'admission_reasons':dict(sorted(Counter(v['reason'] for v in e['portfolio']['events'] if v['kind'] in ('entry','rejected')).items())),
            'statistic_scope':'R uses closed net P&L / actual initial cost-aware stop risk; closed-subset fields are not full-period estimates'}


def _daily(run):
    e=run['evidence'];values={}
    for c in e['coverage']:
        day=c['session'];r=values.setdefault(day,{'eligible':True,'net_pnl_usd':D('0')})
        r['eligible'] &= c['state']=='complete'
    for t in e['portfolio']['trades']:
        # Candidate session comes from the signal, including known no-fill outcomes.
        signal=next(s for s in e['portfolio']['replay']['signals'] if s['signal_id']==t['signal_id'])
        day=str(datetime.fromisoformat(signal['bar_start']).astimezone(NEW_YORK).date())
        if outcome_class(t) in ('uncertain_entry','uncertain_position'):
            values[day]['eligible']=False
        if t['status']=='closed':values[day]['net_pnl_usd']+=D(t['net_pnl'])
    # Scope-wide unknown outcomes/marks may carry across days. Do not salvage a subset silently.
    if not run['metrics_eligible']:
        for row in values.values():row['eligible']=False
    return {d:{'eligible':r['eligible'],'net_pnl_usd':canonical(r['net_pnl_usd']) if r['eligible'] else None} for d,r in values.items()}


def compare(original, value):
    """Reevaluate all trials; never subtract estimated costs from saved profits."""
    plan=validate_plan(value,original)
    config=original['evidence']['manifest']['config'];start=date.fromisoformat(config['start_session']);end=date.fromisoformat(config['end_session'])
    split=date.fromisoformat(plan['split_session'])
    periods=[('development',start,split-timedelta(days=1)),('validation',split,end)]
    source=[Bar(c['symbol'],datetime.fromisoformat(c['start']),*(D(c[k]) for k in ('open','high','low','close')),c['volume']) for c in original['candles']]
    rows=[];runs=[]
    for period,lo,hi in periods:
        bars=[b for b in source if lo<=b.start.astimezone(NEW_YORK).date()<=hi]
        for cost in plan['costs']:
            for profile in plan['presets']:
                row={'period':period,'start_session':str(lo),'end_session':str(hi),
                     'preset_id':profile['id'],'preset_version':profile['version'],'preset_name':profile['name'],
                     'cost_name':cost['name'],'cost_assumptions':deepcopy(cost),'run_id':None,'metrics':None,
                     'daily':{str(d):{'eligible':False,'net_pnl_usd':None} for d in _sessions(lo,hi)}}
                if not bars:
                    row['state']='missing_period_data';rows.append(row);continue
                settings=ResearchSettings.from_dict(profile['settings']).current_values()
                settings.update({k:cost[k] for k in ('slippage_bps','fee_per_share')})
                supported=ResearchSettings.from_dict(settings)
                run=build_dashboard_run(bars,data_label=original['evidence']['manifest']['dataset']['label'],
                    start_session=lo,end_session=hi,**supported.engine_kwargs())
                # A distinct trial identity freezes base profile AND effective cost override.
                m=run['evidence']['manifest'];m['base_schema_version']=m['schema_version'];m['schema_version']='orb-evaluation-comparison-trial-v1'
                m['comparison_trial']={'parent_evaluation_id':original['evaluation_id'],'period':period,
                    'preset':deepcopy(profile),'cost_scenario':deepcopy(cost),
                    'effective_settings':supported.to_dict(),'period_start_policy':'independent_flat_account_reset'}
                run['evaluation_id']=run['evidence']['evaluation_id']=_digest(m)
                run['import_context']=deepcopy(original['import_context'])
                run['import_context'].update(parent_evaluation_id=original['evaluation_id'],notice='Retrospective independent period/cost trial; source archive belongs to the full parent dataset.')
                run['applied_settings']['comparison_trial']=deepcopy(m['comparison_trial'])
                run['applied_settings']['sha256']=_digest({'values':run['applied_settings']['values'],'comparison_trial':m['comparison_trial']})
                row.update(state='complete' if run['metrics_eligible'] else 'incomplete',run_id=run['evaluation_id'],metrics=_metrics(run),daily=_daily(run))
                rows.append(row);runs.append(run)
    paired=[]
    for period,lo,hi in periods:
        for cost in plan['costs']:
            group=[r for r in rows if r['period']==period and r['cost_name']==cost['name']]
            reference=group[0]
            for variant in group[1:]:
                sessions=[]
                for day in reference['daily']:
                    a,b=reference['daily'][day],variant['daily'][day]
                    eligible=a['eligible'] and b['eligible']
                    sessions.append({'session':day,'eligible':eligible,'delta_net_pnl_usd':canonical(D(b['net_pnl_usd'])-D(a['net_pnl_usd'])) if eligible else None})
                paired.append({'period':period,'cost_name':cost['name'],'preset_id':variant['preset_id'],
                    'eligible_paired_sessions':sum(r['eligible'] for r in sessions),'sessions':sessions,
                    'uncertainty_interval':None,'uncertainty_reason':'session/block uncertainty estimation remains required; ETF trades are not independent observations'})
    sensitivity=[]
    if len(plan['costs'])==2:
        for period,lo,hi in periods:
            for profile in plan['presets']:
                cells=[r for r in rows if r['period']==period and r['preset_id']==profile['id']]
                eligible=all(r['metrics'] and r['metrics']['metrics_eligible'] for r in cells)
                sensitivity.append({'period':period,'preset_id':profile['id'],'eligible':bool(eligible),
                    'from_cost':plan['costs'][0]['name'],'to_cost':plan['costs'][1]['name'],
                    'delta_return_fraction':canonical(D(cells[1]['metrics']['total_return_fraction'])-D(cells[0]['metrics']['total_return_fraction'])) if eligible else None,
                    'interpretation':'difference of fully rerun outcomes, not a fixed-quantity fee subtraction or guaranteed monotone drag'})
    manifest={'schema_version':'orb-comparison-v1','parent_evaluation_id':original['evaluation_id'],
              'parent_dataset':deepcopy(original['evidence']['manifest']['dataset']), 'plan':plan,
              'holdout_status':'retrospective_unsealed; imported source was already accessible',
              'period_accounting':'independent flat-capital restarts; do not concatenate period equity or risk state',
              'trial_ids':[r['run_id'] for r in rows],'cash_benchmark_return_fraction':'0',
              'cash_benchmark_assumptions':'zero interest/costs, only comparable when strategy evidence is eligible'}
    result={'comparison_id':_digest(manifest),'manifest':manifest,'rows':rows,'paired_sessions':paired,'cost_sensitivity':sensitivity,
            'limitations':['Engineering comparison, not a trading-edge claim or an untouched final holdout.',
                'Cost scenarios are assumptions, not measured spread/latency or provider costs; fills are still idealised.',
                'Costs are fully rerun: fill-based targets, sizing and exit paths can change; net outcomes need not be monotone in cost settings.',
                'Initial cost-aware stop risk is not a guarantee of realised loss; fees are per side and whole-share sizing is rerun.',
                'Missing periods/outcomes remain explicit; closed-subset statistics are labelled and aggregate metrics are withheld.',
                'No ranking, winner selection, annualisation or automatic parameter changes.',
                'Passive/exposure-matched benchmarks, confidence intervals and permitted multi-period data remain required.']}
    return result,runs


def render_comparison(result):
    lines=['# ORB retrospective schedule/cost comparison','',f"Comparison: {result['comparison_id']}",
           result['manifest']['holdout_status'],result['manifest']['period_accounting'],'',
           '| Period | Preset | Costs | State | Closed | Return (fraction) | Expectancy (net R) |',
           '| --- | --- | --- | --- | ---: | ---: | ---: |']
    for r in result['rows']:
        m=r['metrics'] or {};show=lambda k:'unavailable' if m.get(k) is None else str(m[k])
        escape=lambda s:s.replace('|','\\|')
        lines.append(f"| {r['period']} | {escape(r['preset_name'])} v{r['preset_version']} | {escape(r['cost_name'])} ({r['cost_assumptions']['slippage_bps']} bps; {r['cost_assumptions']['fee_per_share']} USD/share/side) | {r['state']} | {show('closed_trade_count')} | {show('total_return_fraction')} | {show('expectancy_net_r')} |")
    lines+=['','All attempted cells are retained. Cash benchmark: 0% at zero interest/costs.','']+['- '+s for s in result['limitations']]
    return '\n'.join(lines)+'\n'
