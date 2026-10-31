from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest
pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.presets import REFERENCE
from orb.research_settings import ResearchSettings, CAPS_SCHEMA
from test_dashboard_import import payload


def client(tmp_path,examples=False):
    return TestClient(create_app(examples=examples,preset_db=tmp_path/'p.sqlite',research_db=tmp_path/'r.sqlite'),base_url='http://127.0.0.1:8765')


def test_real_server_reopen_keeps_source_applied_version_and_queued_jobs(tmp_path):
    with client(tmp_path) as c:
        imported=c.post('/api/import',json=payload('missing_entry'))
        assert imported.status_code==201
        source=imported.json()['id'];original=c.get('/api/runs/'+source).json()
        duplicated=c.post(f'/api/presets/{REFERENCE}/duplicate',json={'version':1,'name':'Frozen draft'}).json()
        job=c.post(f'/api/runs/{source}/queue-preset',json={'preset_id':duplicated['id'],'version':1}).json()
        assert job['state']=='queued'
        c.post('/api/presets',json={'id':duplicated['id'],'expected_version':1,'name':'Later changed',
            'settings':ResearchSettings(max_session_entries=1,schema_version=CAPS_SCHEMA).to_dict()})
    with client(tmp_path) as c:
        assert c.get('/api/storage').json()['durable']
        assert c.get('/api/runs/'+source).json()==original
        assert c.get('/api/runs/'+source+'/source').json()==payload('missing_entry')
        assert next(j for j in c.get('/api/jobs').json() if j['id']==job['id'])['state']=='queued'
        done=c.post('/api/jobs/work-one').json()
        assert done['state']=='succeeded'
        applied=c.get('/api/runs/'+done['result_id']).json()
        assert applied['applied_settings']['preset']['version']==1
        assert applied['applied_settings']['preset']['name']=='Frozen draft'
        assert not applied['metrics_eligible']
        assert applied['evidence']['summary']['total_return'] is None
    with client(tmp_path) as c:
        assert c.get('/api/runs/'+done['result_id']).json()==applied
        assert c.post('/api/jobs/work-one').json() is None
        assert c.post('/api/jobs/'+job['id']+'/retry').status_code==409


def test_startup_marks_interrupted_without_retry_and_has_working_retry_endpoint(tmp_path):
    with client(tmp_path,True) as c:
        source=c.get('/api/runs').json()[0]['id']
        job=c.post(f'/api/runs/{source}/queue-preset',json={'preset_id':REFERENCE,'version':1}).json()
        c.app.state.research.db.execute("UPDATE jobs SET state='running',claim='dead' WHERE id=?",(job['id'],))
    with client(tmp_path) as c:
        saved=next(j for j in c.get('/api/jobs').json() if j['id']==job['id'])
        assert saved['state']=='interrupted' and saved['result_id'] is None
        assert c.post('/api/jobs/work-one').json() is None
        retry=c.post(f"/api/jobs/{job['id']}/retry")
        assert retry.status_code==201
        assert retry.json()['id']!=job['id']
        assert c.post('/api/jobs/work-one').json()['state']=='succeeded'
        assert len(c.get('/api/runs').json())==4


@pytest.mark.parametrize('endpoint',['/api/jobs','/api/storage','/api/runs/no/source'])
def test_archive_reads_keep_loopback_guard(tmp_path,endpoint):
    with client(tmp_path) as c:
        assert c.get(endpoint,headers={'Origin':'https://evil.example'}).status_code==403


@pytest.mark.parametrize('endpoint',['/api/jobs/work-one','/api/jobs/recover','/api/jobs/no/retry','/api/runs/no/queue-preset'])
def test_job_mutations_keep_loopback_guard(tmp_path,endpoint):
    with client(tmp_path) as c:
        assert c.post(endpoint,headers={'Origin':'https://evil.example'},json={}).status_code==403


def test_invalid_import_cannot_persist_raw_invalid_content(tmp_path):
    with client(tmp_path) as c:
        assert c.post('/api/import',json={'kind':'csv','source':'do-not-retain-this-invalid-source'}).status_code==422
        assert c.get('/api/runs').json()==[] and c.get('/api/jobs').json()==[]
        assert c.app.state.research.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]==0


def test_bad_preset_queue_does_not_create_job_or_run(tmp_path):
    with client(tmp_path,True) as c:
        source=c.get('/api/runs').json()[0]['id'];before=c.get('/api/jobs').json()
        for body in ({'preset_id':REFERENCE,'version':True},{'preset_id':REFERENCE,'version':999},{}):
            assert c.post(f'/api/runs/{source}/queue-preset',json=body).status_code in (404,422)
        assert c.get('/api/jobs').json()==before
        assert c.post('/api/runs/missing/queue-preset',json={'preset_id':REFERENCE,'version':1}).status_code==404


def test_corruption_is_visible_sanitised_503_not_recomputed(tmp_path):
    with client(tmp_path,True) as c:
        source=c.get('/api/runs').json()[0]['id']
        c.app.state.research.db.execute("UPDATE runs SET snapshot='private corrupt content' WHERE id=?",(source,))
        r=c.get('/api/runs/'+source)
        assert r.status_code==503
        assert 'private' not in r.text
        assert c.get('/api/runs').status_code==503
        assert c.get('/api/runs/missing').status_code==404


@pytest.mark.parametrize('endpoint',['/api/jobs/work-one','/api/jobs/recover','/api/jobs/no/retry'])
def test_job_commands_reject_ignored_payload_fields(tmp_path,endpoint):
    with client(tmp_path) as c:
        assert c.post(endpoint,json={'unsupported_setting':'never ignore'}).status_code==422
        assert c.get('/api/jobs').json()==[]
