from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier, Event

import pytest
import orb.research_store as module
from orb.dashboard_import import import_dashboard
from orb.presets import PresetStore, REFERENCE
from orb.research_settings import ResearchSettings, CAPS_SCHEMA
from orb.research_store import ResearchStore, ResearchConflict, ResearchIntegrityError
from test_dashboard_import import payload


def seed(store, name='complete_trade'):
    source=payload(name)
    run=import_dashboard(source)
    job=store.import_run(run,source)
    return run,source,job


def preset():
    with PresetStore() as p:
        return p.save('One entry',ResearchSettings(max_session_entries=1,max_concurrent_positions=1,schema_version=CAPS_SCHEMA).to_dict())


def test_source_run_incomplete_and_job_survive_reopen(tmp_path):
    path=tmp_path/'research.sqlite'
    with ResearchStore(path) as store:
        run,source,job=seed(store,'missing_entry')
        assert job['state']=='succeeded'
        assert not run['metrics_eligible']
        assert run['evidence']['summary']['total_return'] is None
    with ResearchStore(path) as store:
        assert store.get_run(run['evaluation_id'])==run
        assert store.source(run['evaluation_id'])==source
        assert store.get_job(job['id'])==job
        assert store.list_runs()==[run]


def test_exact_import_retry_preserves_first_immutable_context(tmp_path):
    with ResearchStore(tmp_path/'research.sqlite') as store:
        run,source,job=seed(store)
        changed=deepcopy(run);changed['import_context']['notice']='different local context'
        assert store.import_run(changed,source)==job
        assert store.get_run(run['evaluation_id'])==run
        assert len(store.list_jobs())==1


def test_frozen_preset_queue_and_parent_remain_immutable_after_edits(tmp_path):
    path=tmp_path/'research.sqlite'
    with ResearchStore(path) as store:
        run,source,_=seed(store)
        with PresetStore(tmp_path/'presets.sqlite') as presets:
            p=presets.save('Study',ResearchSettings(schema_version=CAPS_SCHEMA).to_dict())
            job=store.enqueue_preset(run['evaluation_id'],p)
            presets.save('Changed',ResearchSettings(target_r=Decimal('3'),schema_version=CAPS_SCHEMA).to_dict(),preset_id=p['id'],expected_version=1)
    with ResearchStore(path) as store:
        assert store.get_job(job['id'])['state']=='queued'
        completed=store.execute()
        assert completed['state']=='succeeded'
        applied=store.get_run(completed['result_id'])
        assert applied['applied_settings']['preset']['name']=='Study'
        assert applied['applied_settings']['preset']['version']==1
        assert applied['applied_settings']['values']['entry_policy']['target_r']=='2'
        assert store.source(completed['result_id'])==source
        assert store.get_run(run['evaluation_id'])==run
        assert store.execute() is None
        assert store.execute(job['id'])==completed
        assert store.enqueue_preset(run['evaluation_id'],p)==completed


@pytest.mark.parametrize('scenario',['complete_trade','known_skip','missing_entry'])
def test_queued_completion_does_not_change_metric_eligibility(tmp_path,scenario):
    with ResearchStore(tmp_path/'research.sqlite') as store:
        run,_,_=seed(store,scenario)
        result=store.execute(store.enqueue_preset(run['evaluation_id'],preset())['id'])
        assert result['state']=='succeeded'
        actual=store.get_run(result['result_id'])
        assert actual['metrics_eligible']==run['metrics_eligible']
        assert actual['evidence']['summary']['complete']==run['evidence']['summary']['complete']
        assert not actual['capabilities']['order_submission']


def test_result_and_success_commit_atomically_with_failure_visible(tmp_path):
    with ResearchStore(tmp_path/'research.sqlite') as store:
        run,_,_=seed(store)
        job=store.enqueue_preset(run['evaluation_id'],preset())
        store.db.execute("CREATE TEMP TRIGGER fail_success BEFORE UPDATE ON jobs WHEN NEW.state='succeeded' BEGIN SELECT RAISE(ABORT,'injected secret error'); END")
        result=store.execute(job['id'])
        assert result['state']=='failed' and result['error']=='evaluation_failed'
        assert store.list_runs()==[run]
        assert 'secret' not in json.dumps(store.list_jobs())
        store.db.execute('DROP TRIGGER fail_success')
        retry=store.retry(job['id'])
        assert retry['id']!=job['id'] and retry['request']['retry_of']==job['id']
        assert store.retry(job['id'])==retry
        assert store.execute(retry['id'])['state']=='succeeded'
        assert store.get_job(job['id'])['state']=='failed'


def test_import_transaction_failure_cannot_leave_source_or_run(tmp_path):
    with ResearchStore(tmp_path/'research.sqlite') as store:
        source=payload();run=import_dashboard(source)
        store.db.execute("CREATE TEMP TRIGGER fail_job BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(sqlite3.IntegrityError):store.import_run(run,source)
        assert store.list_runs()==[] and store.list_jobs()==[]
        assert store.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]==0


def test_engine_change_fails_queued_job_without_reinterpreting_history(tmp_path,monkeypatch):
    with ResearchStore(tmp_path/'research.sqlite') as store:
        run,_,oldjob=seed(store)
        queued=store.enqueue_preset(run['evaluation_id'],preset())
        monkeypatch.setattr(module,'engine_fingerprint',lambda:'new-engine')
        result=store.execute(queued['id'])
        assert result['state']=='failed' and result['error']=='engine_changed'
        with pytest.raises(ResearchConflict,match='engine changed'):store.retry(queued['id'])
        assert store.get_run(run['evaluation_id'])==run
        assert not store.get_job(oldjob['id'])['engine_current']


def test_same_job_concurrent_workers_and_recovery_cannot_steal_active_claim(tmp_path,monkeypatch):
    path=tmp_path/'research.sqlite'
    with ResearchStore(path) as store:
        run,_,_=seed(store);job=store.enqueue_preset(run['evaluation_id'],preset())
    started,finish=Event(),Event()
    original=module.apply_preset
    def held(*args):
        started.set();assert finish.wait(10)
        return original(*args)
    monkeypatch.setattr(module,'apply_preset',held)
    def work():
        with ResearchStore(path) as store:return store.execute(job['id'])
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(work);assert started.wait(10)
        with ResearchStore(path) as other:
            assert other.get_job(job['id'])['state']=='running'
            with pytest.raises(ResearchConflict,match='busy'):other.execute(job['id'])
            with pytest.raises(ResearchConflict,match='busy'):other.recover()
        finish.set();assert future.result()['state']=='succeeded'
    with ResearchStore(path) as store:assert len(store.list_runs())==2


@pytest.mark.parametrize('point',['claimed','before_commit','after_commit'])
def test_abrupt_exit_retains_interruption_or_atomic_success(tmp_path,point):
    path=tmp_path/'research.sqlite'
    with ResearchStore(path) as store:
        run,_,_=seed(store);job=store.enqueue_preset(run['evaluation_id'],preset())
    code='''
import os,sys
from pathlib import Path
import orb.research_store as m
s=m.ResearchStore(Path(sys.argv[1]))
if sys.argv[3]=='claimed':
 def stop(*args):os._exit(23)
 m.apply_preset=stop
elif sys.argv[3]=='before_commit':
 old=s._put_run
 def stop(*args):
  old(*args)
  os._exit(23)
 s._put_run=stop
s.execute(sys.argv[2])
os._exit(23)
'''
    assert subprocess.run([sys.executable,'-c',code,str(path),job['id'],point]).returncode==23
    with ResearchStore(path) as store:
        state=store.get_job(job['id'])['state']
        if point=='after_commit':
            assert state=='succeeded' and store.recover()==0
            assert len(store.list_runs())==2
        else:
            assert state=='running' and store.recover()==1
            assert store.get_job(job['id'])['state']=='interrupted'
            assert store.list_runs()==[run]
            assert store.execute() is None  # No silent retry.
            retry=store.retry(job['id'])
            assert store.execute(retry['id'])['state']=='succeeded'


@pytest.mark.parametrize('table,column',[('runs','snapshot'),('sources','payload'),('jobs','input')])
def test_corrupt_archive_bytes_fail_closed(tmp_path,table,column):
    with ResearchStore(tmp_path/'research.sqlite') as store:
        run,_,job=seed(store)
        store.db.execute(f"UPDATE {table} SET {column}='{{}}'")
        action={'runs':lambda:store.get_run(run['evaluation_id']),
                'sources':lambda:store.source(run['evaluation_id']),
                'jobs':lambda:store.get_job(job['id'])}[table]
        with pytest.raises(ResearchIntegrityError):action()


def test_unrelated_database_and_symlinks_rejected_without_migration(tmp_path):
    path=tmp_path/'other.sqlite'
    db=sqlite3.connect(path);db.execute('CREATE TABLE other(x)');db.commit();db.close()
    with pytest.raises(ResearchIntegrityError):ResearchStore(path)
    link=tmp_path/'link.sqlite';link.symlink_to(path)
    with pytest.raises(ResearchIntegrityError):ResearchStore(link)
    db=sqlite3.connect(path);assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[('other',)];db.close()


def test_archive_limits_are_atomic_and_idempotent(tmp_path,monkeypatch):
    with ResearchStore(tmp_path/'research.sqlite',max_runs=1) as store:
        run,source,job=seed(store)
        assert store.import_run(run,source)==job
        other=payload('missing_entry')
        with pytest.raises(ResearchConflict,match='capacity'):store.import_run(import_dashboard(other),other)
        assert store.list_runs()==[run] and len(store.list_jobs())==1
        monkeypatch.setattr(module,'MAX_ARCHIVE_BYTES',1)
        with pytest.raises(ResearchConflict,match='byte limit'):store.enqueue_preset(run['evaluation_id'],preset())
        assert len(store.list_jobs())==1


def test_cli_work_list_export_and_exclusive_write(tmp_path):
    path=tmp_path/'research.sqlite'
    with ResearchStore(path) as store:
        run,_,_=seed(store);job=store.enqueue_preset(run['evaluation_id'],preset())
    cmd=[sys.executable,'-m','orb.research_store','--db',str(path)]
    result=subprocess.run(cmd+['work-one'],check=True,capture_output=True,text=True)
    completed=json.loads(result.stdout);assert completed['id']==job['id'] and completed['state']=='succeeded'
    out=tmp_path/'run.json'
    subprocess.run(cmd+['export','--id',completed['result_id'],'--output',str(out)],check=True,capture_output=True)
    exported=json.loads(out.read_text());assert exported['applied_settings']['preset']['version']==1
    failed=subprocess.run(cmd+['export','--id',completed['result_id'],'--output',str(out)],capture_output=True)
    assert failed.returncode==2 and json.loads(out.read_text())==exported
    missing=tmp_path/'missing.sqlite'
    assert subprocess.run([sys.executable,'-m','orb.research_store','--db',str(missing),'list'],capture_output=True).returncode==2
    assert not missing.exists()


def test_cross_process_worker_lock_and_recovery_exclusion(tmp_path):
    path=tmp_path/'research.sqlite'
    with ResearchStore(path) as store:
        run,_,_=seed(store);job=store.enqueue_preset(run['evaluation_id'],preset())
        code='''
import sys
from orb.research_store import ResearchStore,ResearchConflict
with ResearchStore(sys.argv[1]) as s:
 for action in (lambda:s.execute(sys.argv[2]),s.recover):
  try:action()
  except ResearchConflict:print('busy')
'''
        with store.worker():
            r=subprocess.run([sys.executable,'-c',code,str(path),job['id']],check=True,capture_output=True,text=True)
            assert r.stdout.splitlines()==['busy','busy']
            assert store.get_job(job['id'])['state']=='queued'
        assert store.execute()['state']=='succeeded'


def test_job_capacity_does_not_leave_partial_queue_row(tmp_path,monkeypatch):
    with ResearchStore(tmp_path/'research.sqlite') as store:
        run,_,_=seed(store)
        monkeypatch.setattr(module,'MAX_JOBS',1)
        with pytest.raises(ResearchConflict,match='job history capacity'):store.enqueue_preset(run['evaluation_id'],preset())
        assert len(store.list_jobs())==1 and store.list_runs()==[run]


def test_dependency_change_requires_new_experiment_but_archived_runs_remain_readable(tmp_path,monkeypatch):
    with ResearchStore(tmp_path/'research.sqlite') as store:
        run,_,_=seed(store);job=store.enqueue_preset(run['evaluation_id'],preset())
        old_version=module.version
        monkeypatch.setattr(module,'version',lambda name:'changed-calendar' if name=='exchange-calendars' else old_version(name))
        assert not store.get_job(job['id'])['engine_current']
        assert store.execute()['error']=='engine_changed'
        assert store.get_run(run['evaluation_id'])==run
