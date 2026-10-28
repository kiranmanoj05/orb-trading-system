import json
import pytest
pytest.importorskip('fastapi')
pytest.importorskip('httpx')
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.presets import REFERENCE
from orb.research_settings import ResearchSettings


def client(db):return TestClient(create_app(examples=True,preset_db=db),base_url='http://127.0.0.1:8765')


def test_full_preset_flow_persists_versions_but_never_mutates_old_run(tmp_path):
    db=tmp_path/'p.sqlite'
    with client(db) as c:
        initial=c.get('/api/presets').json();assert initial['durable']
        fixed=initial['presets'][0];assert fixed['readonly']
        duplicate=c.post(f'/api/presets/{REFERENCE}/duplicate',json={'version':1,'name':'Risk study'})
        assert duplicate.status_code==201
        p=duplicate.json();edited={**p['settings'],'fee_per_share':'0.01','risk_fraction':'0.003'}
        saved=c.post('/api/presets',json=dict(id=p['id'],expected_version=1,name='Cost study',settings=edited))
        assert saved.status_code==201
        assert saved.json()['version']==2
        assert c.post('/api/presets',json=dict(id=p['id'],expected_version=1,name='stale',settings=edited)).status_code==409
        source_id=c.get('/api/runs').json()[0]['id']
        old=c.get('/api/runs/'+source_id).json()
        r=c.post(f'/api/runs/{source_id}/apply-preset',json={'preset_id':p['id'],'version':2})
        assert r.status_code==201
        run=c.get('/api/runs/'+r.json()['id']).json()
        assert c.get('/api/runs/'+source_id).json()==old
        assert run['applied_settings']['preset']['version']==2
        assert run['evidence']['manifest']['config']['risk_fraction']=='0.003'
        assert run['evidence']['manifest']['config']['fee_per_share']=='0.01'
        assert c.get(f"/api/presets/{p['id']}/history").json()[0]['settings']==p['settings']
    with client(db) as c:
        data=c.get('/api/presets').json()
        assert len(data['presets'])==3
        assert next(p for p in data['presets'] if not p['readonly'])['version']==2
        assert len(c.get('/api/runs').json())==3  # Preset persistence is not run persistence.


@pytest.mark.parametrize('version',[None,True,0,-1,'1'])
def test_apply_and_duplicate_require_exact_positive_version(tmp_path,version):
    with client(tmp_path/'p.sqlite') as c:
        run=c.get('/api/runs').json()[0]['id']
        assert c.post(f'/api/runs/{run}/apply-preset',json={'preset_id':REFERENCE,'version':version}).status_code==422
        assert c.post(f'/api/presets/{REFERENCE}/duplicate',json={'name':'copy','version':version}).status_code==422


@pytest.mark.parametrize('body',[{'name':'no schema','settings':{}},
    {'name':'bad','settings':{**ResearchSettings().to_dict(),'target_r':'3'}},
    {'name':'bad','settings':ResearchSettings().to_dict(),'id':None,'expected_version':None}])
def test_bad_preset_settings_are_not_silently_dropped(tmp_path,body):
    with client(tmp_path/'p.sqlite') as c:
        assert c.post('/api/presets',json=body).status_code==422
        assert len(c.get('/api/presets').json()['presets'])==2


def test_reference_cannot_be_overwritten_and_mutations_keep_origin_and_body_guards(tmp_path):
    with client(tmp_path/'p.sqlite') as c:
        p=dict(name='overwrite',settings=ResearchSettings().to_dict(),id=REFERENCE,expected_version=1)
        assert c.post('/api/presets',json=p).status_code==409
        assert c.post('/api/presets',json=p,headers={'Origin':'https://evil.example'}).status_code==403
        assert c.post('/api/presets',content='{}').status_code==415
        assert c.post('/api/presets',content='x'*8193,headers={'Content-Type':'application/json'}).status_code==413
        assert c.get('/api/presets/missing/history').status_code==404
        assert c.post('/api/runs/missing/apply-preset',json={'preset_id':REFERENCE,'version':1}).status_code==404


def test_repeat_apply_does_not_rewrite_identical_existing_snapshot(tmp_path):
    with client(tmp_path/'p.sqlite') as c:
        original=c.get('/api/runs').json()[0]['id']
        payload={'preset_id':REFERENCE,'version':1}
        first=c.post(f'/api/runs/{original}/apply-preset',json=payload).json()['id']
        before=c.get('/api/runs/'+first).json()
        retry=c.post(f'/api/runs/{first}/apply-preset',json=payload)
        assert retry.status_code==201
        assert retry.json()['id']==first
        assert c.get('/api/runs/'+first).json()==before
