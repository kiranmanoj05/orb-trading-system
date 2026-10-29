from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import pytest
pytest.importorskip('fastapi')
pytest.importorskip('httpx')
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.presets import PresetStore,MWF
from orb.research_settings import ResearchSettings,SCHEMA,CAPS_SCHEMA


def settings(**changes):return {**ResearchSettings(schema_version=SCHEMA).to_dict(),**changes}


def test_api_contract_and_builtins_keep_reference_and_mwf_distinct(tmp_path):
    with TestClient(create_app(examples=True,preset_db=tmp_path/'p.sqlite'),base_url='http://127.0.0.1:8765') as c:
        data=c.get('/api/presets').json();reference,mwf=data['presets']
        assert reference['settings']['schema_version']=='orb-research-settings-v1'
        assert mwf['settings']['weekdays']==[0,2,4]
        assert mwf['settings']['target_r']=='2'
        assert data['contract']['schema_version']==CAPS_SCHEMA
        assert data['contract']['defaults']['weekdays']==[0,1,2,3,4]
        assert c.post('/api/presets',json=dict(id=MWF,expected_version=1,name='overwrite',settings=mwf['settings'])).status_code==409
        source=c.get('/api/runs').json()[0]['id']
        before=c.get('/api/runs/'+source).json()
        variant=c.post('/api/presets',json=dict(name='TueThu 3R',settings=settings(weekdays=[1,3],target_r='3'))).json()
        applied=c.post(f'/api/runs/{source}/apply-preset',json=dict(preset_id=variant['id'],version=1))
        assert applied.status_code==201
        result=c.get('/api/runs/'+applied.json()['id']).json()
        assert result['evidence']['summary']['candidate_count']==0 # Friday is ineligible, not rescheduled.
        assert result['evidence']['summary']['total_return']=='0'
        assert all(d['reason']=='weekday_not_eligible' for d in result['evidence']['portfolio']['replay']['sessions'])
        assert result['applied_settings']['values']['entry_policy']['target_r']=='3'
        assert result['applied_settings']['preset']['settings']['weekdays']==[1,3]
        assert c.get('/api/runs/'+source).json()==before
        imported=c.post('/api/import',json=dict(kind='csv',source=Path('fixtures/orb_execution_sample.csv').read_text(),report=result['evidence']))
        assert imported.status_code==422 # Different source never borrows valid policy evidence.


@pytest.mark.parametrize('changes',[{'weekdays':[]},{'weekdays':[5]},{'weekdays':[False]},
    {'weekdays':[0,0]},{'weekdays':[4,0]},{'weekdays':'MWF'},{'target_r':'bad'},
    {'target_r':'NaN'},{'target_r':3},{'target_r':'0'},{'target_r':'11'},
    {'schema_version':'orb-research-settings-v99'}])
def test_invalid_calendar_and_target_inputs_never_become_defaults_or_500(tmp_path,changes):
    with TestClient(create_app(preset_db=tmp_path/'p.sqlite'),base_url='http://127.0.0.1:8765') as c:
        response=c.post('/api/presets',json=dict(name='Invalid',settings=settings(**changes)))
        assert response.status_code==422,response.text
        assert len(c.get('/api/presets').json()['presets'])==2


def test_cli_v2_target_and_weekday_settings_drive_actual_result(tmp_path):
    db=tmp_path/'p.sqlite';file=tmp_path/'settings.json';file.write_text(json.dumps(settings(target_r='3')))
    cli=lambda *args:subprocess.run([sys.executable,'-m','orb.presets','--db',str(db),*args],capture_output=True,text=True)
    created=cli('create','--name','CLI 3R','--settings',str(file));assert created.returncode==0,created.stderr
    p=json.loads(created.stdout);out=tmp_path/'run.json'
    result=cli('run','--id',p['id'],'--version','1','--csv','fixtures/orb_execution_sample.csv','--output',str(out))
    assert result.returncode==0,result.stderr
    run=json.loads(out.read_text())
    assert run['evidence']['manifest']['base_schema_version']=='orb-evaluation-v6'
    assert Decimal(run['evidence']['portfolio']['trades'][0]['target'])==Decimal('104.2')
    assert run['applied_settings']['preset']['settings']['target_r']=='3'
