from copy import deepcopy
import json
import sqlite3
import subprocess
import sys

import pytest
pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.presets import REFERENCE,MWF
from orb.research_store import ResearchStore,ResearchIntegrityError
from test_comparison import original,plan,seed


def client(tmp_path):
    return TestClient(create_app(preset_db=tmp_path/'p.sqlite',research_db=tmp_path/'r.sqlite'),base_url='http://127.0.0.1:8765')


def api_plan():
    p=plan();p.pop('schema_version');p['presets']=[{'id':REFERENCE,'version':1},{'id':MWF,'version':1}];return p


def test_api_queue_report_trials_and_reopen_keep_frozen_protocol(tmp_path):
    with ResearchStore(tmp_path/'r.sqlite') as store:parent=seed(store)
    with client(tmp_path) as c:
        p=api_plan();queued=c.post(f"/api/runs/{parent['evaluation_id']}/queue-comparison",json=p)
        assert queued.status_code==201,queued.text
        job=queued.json();assert job['state']=='queued'
        assert c.post(f"/api/runs/{parent['evaluation_id']}/queue-comparison",json=p).json()==job
        done=c.post('/api/jobs/work-one').json();assert done['state']=='succeeded'
        result=c.get('/api/comparisons/'+done['comparison_id']).json()
        assert len(result['rows'])==8
        assert c.get('/api/comparisons/'+done['comparison_id']+'/report').status_code==200
        for r in result['rows']:
            assert c.get('/api/runs/'+r['run_id']).status_code==200
        before=c.get('/api/runs/'+parent['evaluation_id']).json()
    with client(tmp_path) as c:
        assert c.get('/api/comparisons/'+done['comparison_id']).json()==result
        assert c.get('/api/runs/'+parent['evaluation_id']).json()==before
        assert c.post('/api/jobs/'+job['id']+'/retry').status_code==409


@pytest.mark.parametrize('body',[{},None,{'name':'x','split_session':'2026-10-13','presets':[{'id':REFERENCE,'version':True}],'costs':[]}])
def test_bad_comparison_requests_do_not_queue(tmp_path,body):
    with ResearchStore(tmp_path/'r.sqlite') as store:parent=seed(store)
    with client(tmp_path) as c:
        before=c.get('/api/jobs').json()
        response=(c.post(f"/api/runs/{parent['evaluation_id']}/queue-comparison",content='null',headers={'Content-Type':'application/json'}) if body is None else c.post(f"/api/runs/{parent['evaluation_id']}/queue-comparison",json=body))
        assert response.status_code==422
        assert c.get('/api/jobs').json()==before


def test_comparison_loopback_and_unknown_id_guards(tmp_path):
    with client(tmp_path) as c:
        assert c.get('/api/comparisons',headers={'Origin':'https://evil.example'}).status_code==403
        assert c.post('/api/runs/no/queue-comparison',json=api_plan(),headers={'Origin':'https://evil.example'}).status_code==403
        assert c.get('/api/comparisons/no').status_code==404
        assert c.get('/api/comparisons/no/report').status_code==404


@pytest.mark.parametrize('point',['before_commit','after_commit'])
def test_crash_comparison_has_atomic_result_and_all_trial_runs(tmp_path,point):
    path=tmp_path/'r.sqlite'
    with ResearchStore(path) as store:parent=seed(store);job=store.enqueue_comparison(parent['evaluation_id'],plan())
    code='''
import os,sys
from orb.research_store import ResearchStore
s=ResearchStore(sys.argv[1])
if sys.argv[3]=='before_commit':
 old=s._budget
 def stop():
  old()
  os._exit(23)
 s._budget=stop
s.execute(sys.argv[2])
os._exit(23)
'''
    assert subprocess.run([sys.executable,'-c',code,str(path),job['id'],point]).returncode==23
    with ResearchStore(path) as store:
        if point=='before_commit':
            assert store.list_comparisons()==[] and store.list_runs()==[parent]
            assert store.recover()==1
            assert store.get_job(job['id'])['state']=='interrupted'
        else:
            assert len(store.list_comparisons())==1 and len(store.list_runs())==9
            assert store.get_job(job['id'])['state']=='succeeded'


def test_malformed_v1_schema_is_not_migrated(tmp_path):
    path=tmp_path/'r.sqlite'
    db=sqlite3.connect(path)
    for table in ('sources','runs','jobs'):db.execute(f'CREATE TABLE {table}(wrong TEXT)')
    from orb.research_store import APP_ID
    db.execute(f'PRAGMA application_id={APP_ID}');db.execute('PRAGMA user_version=1');db.commit();db.close()
    with pytest.raises(ResearchIntegrityError):ResearchStore(path)
    db=sqlite3.connect(path)
    assert db.execute('PRAGMA user_version').fetchone()[0]==1
    assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[('sources',),('runs',),('jobs',)]
    db.close()


def test_optional_invented_multisession_source_does_not_replace_baseline_examples(tmp_path):
    app=create_app(examples=True,comparison_example=True,research_db=tmp_path/'r.sqlite',preset_db=tmp_path/'p.sqlite')
    with TestClient(app,base_url='http://127.0.0.1:8765') as c:
        rows=c.get('/api/runs').json();assert len(rows)==4
        demo=next(r for r in rows if 'comparison_three_sessions' in r['label'])
        assert demo['synthetic'] is True
        source=c.get('/api/runs/'+demo['id']).json()
        assert len(source['candles'])==2340
        assert source['evidence']['summary']['complete']
        r=c.post('/api/runs/'+demo['id']+'/queue-comparison',json=api_plan())
        assert r.status_code==201
