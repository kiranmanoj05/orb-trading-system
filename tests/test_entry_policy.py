from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import subprocess
import sys

import pytest
from orb.breakout import BreakoutDetector, SignalState
from orb.dashboard_fixtures import scenarios
from orb.dashboard_import import import_dashboard
from orb.entry_policy import EntryPolicy
from orb.evaluation import evaluate, _digest, render_report
from orb.presets import PresetStore, REFERENCE, MWF
from orb.profile_runs import apply_preset
from orb.replay import replay
from orb.research_settings import ResearchSettings, SCHEMA
from orb.session import exchange_session
from orb.simulation import simulate
from orb.strategy import calculate_opening_range
from test_dashboard_import import csv_text


GOLDEN = {'complete_trade':'ef950bb0b2d84d8c40198e28269cdde71e463cbd228bf361513d94582e3c958b',
          'known_skip':'af10210bfa51d451b80a723e919a04e15989170fb94f96d0b2d32514107a77e0',
          'missing_entry':'e8dad703b4a0ba19a0855e40b2248bab78066a7c034b22a17fad4c2940ba97ba'}


def on_date(day, scenario='complete_trade'):
    session=exchange_session(day)
    source=scenarios()[scenario]
    first=source[0].start
    # Keep local exchange-relative minutes across DST/early closes.
    return [replace(b,start=session.opens_at+(b.start-first)) for b in source
            if (b.start-first)<session.closes_at-session.opens_at]


def v2(**changes):
    return {**ResearchSettings(schema_version=SCHEMA).to_dict(),**changes}


@pytest.mark.parametrize('scenario',GOLDEN)
def test_default_and_explicit_reference_match_published_parent_trace(scenario):
    bars=scenarios()[scenario]
    for kwargs in ({},{'entry_policy':EntryPolicy()},{'entry_policy':EntryPolicy(target_r=D('2.00'))}):
        r=evaluate(bars,**kwargs)
        assert _digest({k:r[k] for k in ('summary','portfolio','coverage','valuation')})==GOLDEN[scenario]
        assert r['manifest']['schema_version']=='orb-evaluation-v5'
        assert 'entry_policy' not in r['manifest']['config']


@pytest.mark.parametrize('weekdays',[(),(5,),(6,),(True,),(0,0),(4,0),[0,2,4]])
def test_ambiguous_or_unsupported_days_rejected(weekdays):
    with pytest.raises(ValueError):EntryPolicy(weekdays)


@pytest.mark.parametrize('target',[D('0'),D('.249'),D('10.1'),D('NaN'),D('Infinity'),D('1e-999'),2,True])
def test_invalid_target_rejected(target):
    with pytest.raises(ValueError):EntryPolicy(target_r=target)


@pytest.mark.parametrize('target',['garbage','',2,True])
def test_policy_json_rejects_malformed_target(target):
    with pytest.raises(ValueError):EntryPolicy.from_dict(dict(schema_version='orb-entry-policy-v1',weekdays=[0,2,4],target_r=target))


def test_policy_identity_canonicalises_r_and_changes_with_schedule_or_target():
    assert EntryPolicy(target_r=D('2.00')).strategy_version==BreakoutDetector.VERSION
    assert EntryPolicy((0,2,4),D('3.00')).strategy_version==EntryPolicy((0,2,4),D('3')).strategy_version
    assert EntryPolicy((0,2,4)).strategy_version!=EntryPolicy((1,3)).strategy_version
    with pytest.raises(ValueError):replay(scenarios()['complete_trade'],entry_policy=[])


@pytest.mark.parametrize('day,eligible',[('2026-10-05',True),('2026-10-06',False),('2026-10-07',True),
                                       ('2026-10-08',False),('2026-10-09',True)])
def test_mwf_schedule_is_an_entry_gate_not_a_daily_quota(day,eligible):
    bars=on_date(date.fromisoformat(day))
    r=evaluate(bars,entry_policy=EntryPolicy((0,2,4)))
    assert r['summary']['candidate_count']==(1 if eligible else 0)
    assert r['summary']['complete']
    assert r['manifest']['dataset']['bars']==780
    assert len(r['valuation']['samples'])==391
    if not eligible:
        assert r['summary']['total_return']=='0'
        assert all(o['state']=='inactive' and o['reason']=='weekday_not_eligible' for o in r['portfolio']['replay']['sessions'])
    # No breakout still means no trade on an eligible day.
    no_breakout=[replace(b,open=D('99'),high=D('100'),low=D('98'),close=D('99.5')) for b in bars]
    assert not replay(no_breakout,entry_policy=EntryPolicy((0,2,4))).signals


def test_schedule_uses_ny_session_date_not_utc_date_and_holidays_do_not_roll():
    bars=on_date(date(2026,11,2))
    r=replay(bars,entry_policy=EntryPolicy((0,)))
    assert r.signals[0].observed_at.isoformat()=='2026-11-02T14:46:00+00:00'
    # An observation after midnight UTC is still Friday NY (outside regular hours).
    extended=bars+[replace(bars[-1],start=bars[-1].start+timedelta(hours=4))]
    assert len(replay(extended,entry_policy=EntryPolicy((0,))).signals)==1
    holiday=[replace(b,start=b.start+timedelta(days=77)) for b in scenarios()['complete_trade']] # Christmas Friday
    assert all(o.state=='closed' for o in replay(holiday,entry_policy=EntryPolicy((0,2,4))).sessions)
    tue=on_date(date(2026,10,6));wed=on_date(date(2026,10,7))
    assert not replay(tue+wed,entry_policy=EntryPolicy((1,))).sessions[-1].state=='signalled'


def test_standalone_detector_validates_completed_bars_and_honours_schedule():
    day=date(2026,10,6);bars=on_date(day);session=exchange_session(day)
    opening=calculate_opening_range(bars,'SPY',day,session.range_end)
    detector=BreakoutDetector(opening,session,entry_policy=EntryPolicy((0,2,4)))
    assert detector.state==SignalState.INACTIVE
    breakout=next(b for b in bars if b.symbol=='SPY' and b.start==session.range_end)
    with pytest.raises(ValueError,match='complete'):detector.on_bar(breakout,breakout.start)
    assert detector.on_bar(breakout,breakout.start+timedelta(minutes=1)) is None


def test_target_r_reaches_signal_fill_exit_and_report_without_future_funding():
    bars=scenarios()['complete_trade']
    two=evaluate(bars)
    three=evaluate(bars,entry_policy=EntryPolicy(target_r=D('3')))
    signal=three['portfolio']['replay']['signals'][0];trade=three['portfolio']['trades'][0]
    assert signal['target']=='103.8'  # 100.2 + 3*(100.2-99)
    assert D(trade['target'])==D('104.2') # actual 100.3 fill, not signal close
    assert trade['reason']=='stop_gap' and D(trade['net_pnl'])==D('-31.2')
    assert two['portfolio']['trades'][0]['reason']=='target'
    assert signal['signal_id']!=two['portfolio']['replay']['signals'][0]['signal_id']
    assert three['manifest']['schema_version']=='orb-evaluation-v6'
    assert '3R' in render_report(three)
    assert 'New York eligible weekdays: Mon, Tue, Wed, Thu, Fri' in render_report(three)
    # Changing later prices cannot alter earlier signals or admission state.
    changed=list(bars)
    for i,b in enumerate(changed):
        if b.start>bars[0].start+timedelta(minutes=17):changed[i]=replace(b,open=D('200'),high=D('201'),low=D('199'),close=D('200'))
    cutoff=bars[0].start+timedelta(minutes=17)
    assert tuple(s for s in replay(changed,entry_policy=EntryPolicy(target_r=D('3'))).signals if s.observed_at<=cutoff)==replay(bars,entry_policy=EntryPolicy(target_r=D('3'))).signals
    changed_result=evaluate(changed,entry_policy=EntryPolicy(target_r=D('3')))
    early=lambda r:[e for e in r['portfolio']['events'] if e['at']<=cutoff.isoformat()]
    assert early(changed_result)==early(three)


def test_ambiguous_exit_and_missing_entry_remain_conservative_for_variants():
    bars=scenarios()['complete_trade'];start=bars[0].start
    bars=[replace(b,low=D('98'),high=D('105')) if b.symbol=='SPY' and b.start==start+timedelta(minutes=16) else b for b in bars]
    result=simulate(bars,entry_policy=EntryPolicy(target_r=D('3')))
    assert result.trades[0].reason=='stop' and result.trades[0].ambiguous_exit
    missing=evaluate(scenarios()['missing_entry'],entry_policy=EntryPolicy(target_r=D('3')))
    assert missing['summary']['total_return'] is None
    inactive=evaluate(on_date(date(2026,10,6),'missing_entry'),entry_policy=EntryPolicy((0,2,4)))
    assert inactive['summary']['outcomes_complete']
    assert not inactive['summary']['coverage_complete'] and inactive['summary']['total_return'] is None


def test_existing_exposure_and_inactive_day_mark_gaps_are_not_skipped():
    tue=on_date(date(2026,10,6));first=tue[0].start
    tue=[b for b in tue if b.start<first+timedelta(minutes=18)]
    tue=[replace(b,open=D('100.3'),high=D('100.6'),low=D('100.1'),close=D('100.4'))
         if b.start>=first+timedelta(minutes=16) else b for b in tue]
    wed=on_date(date(2026,10,7),'known_skip')
    wed=[replace(b,open=D('100'),high=D('101'),low=D('100'),close=D('100.5')) for b in wed]
    r=evaluate(tue+wed,entry_policy=EntryPolicy((1,)))
    assert r['portfolio']['account']['committed_stop_risk']!='0'
    assert r['portfolio']['account']['allocations'][0]['state']=='unresolved'
    assert len([s for s in r['valuation']['samples'] if s['at'].startswith('2026-10-07')])==391
    gap=wed[40].start
    broken=[b for b in wed if not (b.symbol=='SPY' and b.start==gap)]
    missing=evaluate(tue+broken,entry_policy=EntryPolicy((1,)))
    assert not missing['summary']['marks_complete']
    assert missing['valuation']['missing_mark_samples']>0
    assert missing['portfolio']['daily_loss_sessions'][-1]['blocked']


def test_early_close_and_mwf_do_not_change_flatten_deadline():
    day=date(2026,11,27);bars=on_date(day);start=bars[0].start
    bars=[replace(b,open=D('100.3'),high=D('100.6'),low=D('100.1'),close=D('100.4'))
          if b.start>=start+timedelta(minutes=16) else b for b in bars]
    r=evaluate(bars,entry_policy=EntryPolicy((0,2,4),D('3')))
    assert r['summary']['complete']
    t=r['portfolio']['trades'][0]
    assert t['reason']=='session_flatten' and t['exit_at']=='2026-11-27T17:55:00+00:00'


def test_v1_rows_keep_exact_values_and_hash_when_v2_versions_are_added(tmp_path):
    with PresetStore(tmp_path/'p.sqlite') as s:
        p=s.duplicate(REFERENCE,1,'Legacy')
        before=deepcopy(p)
        assert p['settings']['schema_version']=='orb-research-settings-v1'
        updated=s.save('Variant',v2(weekdays=[1,3],target_r='3'),preset_id=p['id'],expected_version=1)
        assert s.get(p['id'],1)==before
        assert updated['settings']['weekdays']==[1,3]
        assert s.get(MWF)['settings']['target_r']=='2'
        with pytest.raises(ValueError):s.save('overwrite',v2(),preset_id=MWF,expected_version=1)
    with PresetStore(tmp_path/'p.sqlite') as s:assert s.get(p['id'],1)==before


def test_variant_round_trip_preserves_applied_policy_and_original_run():
    source=import_dashboard(dict(kind='csv',source=csv_text(scenarios()['complete_trade'])))
    old=deepcopy(source)
    with PresetStore() as s:p=s.save('3R MWF',v2(weekdays=[0,2,4],target_r='3'))
    run=apply_preset(source,p)
    assert source==old
    assert run['evidence']['manifest']['base_schema_version']=='orb-evaluation-v6'
    imported=import_dashboard(dict(kind='csv',source=csv_text(scenarios()['complete_trade']),report=run['evidence']))
    assert imported['applied_settings']['preset']==run['applied_settings']['preset']
    assert imported['evidence']['manifest']['config']['entry_policy']['target_r']=='3'
    tampered=deepcopy(run['evidence']);tampered['manifest']['config']['entry_policy']['weekdays']=[1,3]
    tampered['evaluation_id']=_digest(tampered['manifest'])
    with pytest.raises(ValueError):import_dashboard(dict(kind='csv',source=csv_text(scenarios()['complete_trade']),report=tampered))


def test_variant_simulation_serialisation_keeps_policy_and_reference_shape():
    bars=scenarios()['complete_trade']
    baseline=simulate(bars).to_dict()
    assert baseline['model_version']=='minute-long-v1' and 'entry_policy' not in baseline
    variant=simulate(bars,entry_policy=EntryPolicy((0,2,4),D('3'))).to_dict()
    assert variant['model_version']=='minute-long-policy-v1'
    assert variant['entry_policy']['target_r']=='3'
    assert variant['entry_policy']['weekdays']==[0,2,4]
