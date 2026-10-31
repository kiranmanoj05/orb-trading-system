"""Local immutable research archive and explicitly driven queue; never orders."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from importlib.metadata import version
import platform
from pathlib import Path
import sqlite3
from threading import Lock, RLock
from uuid import uuid4

from orb.evaluation import _digest, engine_fingerprint
from orb.ingestion import _object, _invalid_constant
from orb.profile_runs import apply_preset, validated_snapshot

APP_ID = 0x4F524252
MAX_RUNS = 100
MAX_JOBS = 1000
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


class ResearchConflict(ValueError):
    pass


class ResearchIntegrityError(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def engine_context():
    return {'source_sha256':engine_fingerprint(), 'python':platform.python_version(),
            'dependencies':{name:version(name) for name in
                ('exchange-calendars','numpy','pandas','tzdata','pyluach',
                 'korean-lunar-calendar','toolz','python-dateutil')}}


def engine_identity():
    return _digest(engine_context())


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read(text, fingerprint):
    if sha256(text.encode()).hexdigest() != fingerprint:
        raise ResearchIntegrityError('research archive integrity mismatch; preserve database for review')
    try:
        return json.loads(text, object_pairs_hook=_object, parse_constant=_invalid_constant)
    except (ValueError, RecursionError):
        raise ResearchIntegrityError('invalid research archive content') from None


class ResearchStore:
    """One DB, immutable snapshots; queued jobs pin source/settings/engine.

    SQLite transactions make result + success atomic. A local OS file lock
    serialises evaluation and recovery across processes. No daemon, remote
    provider acquisition, cross-host lock or broker execution is implemented.
    """
    def __init__(self, path=':memory:', *, max_runs=MAX_RUNS):
        self.path = str(path)
        self.durable = self.path != ':memory:'
        if self.durable:
            p = Path(self.path)
            if p.is_symlink():
                raise ResearchIntegrityError('research database symlinks are unsupported')
            p.parent.mkdir(parents=True, exist_ok=True)
        self.lock, self.worker_lock = RLock(), Lock()
        self.max_runs = max_runs
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA synchronous=FULL')
            with self.transaction():
                identity = self.db.execute('PRAGMA application_id').fetchone()[0]
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if identity == version == 0 and not tables:
                    self.db.execute('CREATE TABLE sources (id TEXT PRIMARY KEY, payload TEXT NOT NULL, fingerprint TEXT NOT NULL)')
                    self.db.execute('CREATE TABLE runs (id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), '
                        'snapshot TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL)')
                    self.db.execute('CREATE TABLE jobs (sequence INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, '
                        'request_key TEXT NOT NULL UNIQUE, input TEXT NOT NULL, fingerprint TEXT NOT NULL, '
                        "state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','interrupted')), "
                        'created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, result_id TEXT REFERENCES runs(id), '
                        'error TEXT, claim TEXT)')
                    self.db.execute(f'PRAGMA application_id={APP_ID}')
                    self.db.execute('PRAGMA user_version=1')
                    identity, version, tables = APP_ID, 1, {'sources','runs','jobs'}
                if identity==APP_ID and version in (1,2):
                    expected={'sources':{'id','payload','fingerprint'},
                        'runs':{'id','source_id','snapshot','fingerprint','created_at'},
                        'jobs':{'sequence','id','request_key','input','fingerprint','state','created_at','started_at','finished_at','result_id','error','claim'}}
                    if version==2: expected['comparisons']={'id','job_id','snapshot','fingerprint','created_at'}
                    for table,columns in expected.items():
                        if {r[1] for r in self.db.execute(f'PRAGMA table_info({table})')}!=columns:
                            raise ResearchIntegrityError('unrecognised research schema; no migration attempted')
                if identity == APP_ID and version == 1 and tables == {'sources','runs','jobs'}:
                    self.db.execute('CREATE TABLE comparisons (id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id), snapshot TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL)')
                    self.db.execute('PRAGMA user_version=2')
                elif identity != APP_ID or version != 2 or tables != {'sources','runs','jobs','comparisons'}:
                    raise ResearchIntegrityError('unrecognised research database; no migration attempted')
            self.db.execute('PRAGMA journal_mode=DELETE')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    @contextmanager
    def worker(self):
        if not self.worker_lock.acquire(blocking=False):
            raise ResearchConflict('research worker is busy')
        handle = None
        try:
            if self.durable:
                import fcntl  # POSIX local service: Linux/macOS, not Windows.
                import os
                path = self.path + '.worker.lock'
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                handle = os.fdopen(fd, 'a')
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ResearchConflict('research worker is busy') from None
            yield
        finally:
            if handle is not None:
                handle.close()
            self.worker_lock.release()

    def _budget(self):
        total = self.db.execute('SELECT COALESCE(SUM(length(CAST(snapshot AS BLOB))),0) FROM runs').fetchone()[0]
        total += self.db.execute('SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM sources').fetchone()[0]
        total += self.db.execute('SELECT COALESCE(SUM(length(CAST(input AS BLOB))),0) FROM jobs').fetchone()[0]
        total += self.db.execute('SELECT COALESCE(SUM(length(CAST(snapshot AS BLOB))),0) FROM comparisons').fetchone()[0]
        if total > MAX_ARCHIVE_BYTES:
            raise ResearchConflict('research archive byte limit reached; preserve/export evidence before maintenance')

    def _put_run(self, run, source_id):
        identity = run['evaluation_id']
        if (identity != run['evidence']['evaluation_id'] or _digest(run['evidence']['manifest']) != identity
                or run['mode'] != 'historical_simulation' or run['capabilities']['order_submission']):
            raise ResearchIntegrityError('invalid computed research snapshot')
        old = self.db.execute('SELECT * FROM runs WHERE id=?', (identity,)).fetchone()
        if old:
            _read(old['snapshot'], old['fingerprint'])
            return  # Preserve first immutable snapshot/context, as in the prior API.
        if self.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] >= self.max_runs:
            raise ResearchConflict('research run capacity reached; preserve/export evidence before maintenance')
        text = _json(run)
        self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?)',
                        (identity, source_id, text, sha256(text.encode()).hexdigest(), _now()))

    def import_run(self, run, payload):
        """Call only after import_dashboard has validated/recomputed the source."""
        text = _json(payload)
        source_id = sha256(text.encode()).hexdigest()
        request = {'kind': 'import', 'source_id': source_id, 'result_id': run['evaluation_id'],
                   'engine': engine_identity(), 'engine_context':engine_context()}
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO sources VALUES(?,?,?)', (source_id, text, source_id))
            existing = self.db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
            _read(existing['payload'], existing['fingerprint'])
            self._put_run(run, source_id)
            job = self._enqueue(request, state='succeeded', result_id=run['evaluation_id'])
            self._budget()
            return job

    def get_run(self, identity):
        with self.lock:
            row = self.db.execute('SELECT * FROM runs WHERE id=?', (identity,)).fetchone()
            if row is None: raise KeyError('research run not found')
            run = _read(row['snapshot'], row['fingerprint'])
            if run.get('evaluation_id') != identity or _digest(run['evidence']['manifest']) != identity:
                raise ResearchIntegrityError('research run identity mismatch')
            return run

    def source(self, identity):
        with self.lock:
            row = self.db.execute('SELECT s.* FROM sources s JOIN runs r ON r.source_id=s.id WHERE r.id=?', (identity,)).fetchone()
            if row is None: raise KeyError('research source not found')
            return _read(row['payload'], row['fingerprint'])

    def list_runs(self):
        with self.lock:
            ids = [r[0] for r in self.db.execute('SELECT id FROM runs ORDER BY rowid')]
            return [self.get_run(identity) for identity in ids]

    def _enqueue(self, request, *, state='queued', result_id=None, key=None):
        text = _json(request)
        fingerprint = sha256(text.encode()).hexdigest()
        key = key or fingerprint
        old = self.db.execute('SELECT id FROM jobs WHERE request_key=?', (key,)).fetchone()
        if old: return self.get_job(old['id'])
        if self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] >= MAX_JOBS:
            raise ResearchConflict('research job history capacity reached')
        identity, at = uuid4().hex, _now()
        self.db.execute('INSERT INTO jobs(id,request_key,input,fingerprint,state,created_at,finished_at,result_id) '
            'VALUES(?,?,?,?,?,?,?,?)', (identity,key,text,fingerprint,state,at,at if state=='succeeded' else None,result_id))
        return self.get_job(identity)

    def enqueue_preset(self, source_run_id, preset):
        snapshot = validated_snapshot(preset)
        with self.transaction():
            self.get_run(source_run_id)
            job = self._enqueue({'kind': 'preset', 'source_run_id': source_run_id,
                                 'preset': snapshot, 'engine': engine_identity(), 'engine_context':engine_context()})
            self._budget()
            return job

    def enqueue_comparison(self, source_run_id, plan):
        from orb.comparison import validate_plan
        with self.transaction():
            original=self.get_run(source_run_id)
            frozen=validate_plan(plan, original)
            context=engine_context()
            job=self._enqueue({'kind':'comparison','source_run_id':source_run_id,'plan':frozen,
                               'engine':_digest(context),'engine_context':context})
            self._budget()
            return job

    def get_comparison(self, identity):
        with self.lock:
            row=self.db.execute('SELECT * FROM comparisons WHERE id=?',(identity,)).fetchone()
            if row is None: raise KeyError('comparison not found')
            result=_read(row['snapshot'],row['fingerprint'])
            if result['comparison_id']!=identity or _digest(result['manifest'])!=identity:
                raise ResearchIntegrityError('comparison identity mismatch')
            return result

    def list_comparisons(self):
        with self.lock:
            return [self.get_comparison(r[0]) for r in self.db.execute('SELECT id FROM comparisons ORDER BY rowid DESC')]

    def get_job(self, identity):
        with self.lock:
            r = self.db.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone()
            if r is None: raise KeyError('research job not found')
            request = _read(r['input'], r['fingerprint'])
            comparison=self.db.execute('SELECT id FROM comparisons WHERE job_id=?',(identity,)).fetchone()
            return {k:r[k] for k in ('id','state','created_at','started_at','finished_at','result_id','error')} | {
                'request':request, 'engine_current':request['engine']==engine_identity(),
                **({'comparison_id':comparison['id']} if comparison else {})}

    def list_jobs(self):
        with self.lock:
            return [self.get_job(r[0]) for r in self.db.execute('SELECT id FROM jobs ORDER BY sequence DESC')]

    def _recover(self):
        # Caller holds the OS worker lock: no conforming worker can still commit.
        with self.transaction():
            for row in self.db.execute("SELECT input,fingerprint FROM jobs WHERE state='running'"):
                _read(row["input"], row["fingerprint"])
            count = self.db.execute("UPDATE jobs SET state='interrupted',error='worker_interrupted',finished_at=?,claim=NULL "
                "WHERE state='running'", (_now(),)).rowcount
            return count

    def recover(self):
        with self.worker(): return self._recover()

    def retry(self, identity):
        with self.transaction():
            old = self.get_job(identity)
            if old['state'] not in ('failed','interrupted'):
                raise ResearchConflict('only failed or interrupted research jobs can be retried')
            if not old['engine_current']:
                raise ResearchConflict('engine changed; queue a new explicit saved-version experiment')
            request = {**old['request'], 'retry_of': identity}
            result = self._enqueue(request, key='retry:'+identity)
            self._budget()
            return result

    def execute(self, identity=None):
        """Execute at most one explicitly requested job; no automatic retry."""
        with self.worker():
            self._recover()
            with self.transaction():
                if identity is None:
                    row = self.db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY sequence LIMIT 1").fetchone()
                    if row is None: return None
                    identity = row['id']
                job = self.get_job(identity)
                if job['state']=='succeeded': return job
                if job['state']!='queued': raise ResearchConflict('job is not queued; use explicit retry')
                claim = uuid4().hex
                self.db.execute("UPDATE jobs SET state='running',started_at=?,claim=? WHERE id=?", (_now(),claim,identity))
            try:
                request = job['request']
                if not job['engine_current']:
                    raise ResearchConflict('engine_changed')
                self.source(request['source_run_id'])  # Validate retained original evidence too.
                original = self.get_run(request['source_run_id'])
                comparison=None
                if request['kind']=='comparison':
                    from orb.comparison import compare
                    comparison,runs=compare(original,request['plan'])
                else:
                    runs=[apply_preset(original, request['preset'])]
                with self.transaction():
                    if engine_identity()!=request['engine']:
                        raise ResearchConflict('engine_changed')
                    parent = self.db.execute('SELECT source_id FROM runs WHERE id=?', (request['source_run_id'],)).fetchone()
                    for run in runs:
                        self._put_run(run, parent['source_id'])
                    if comparison is not None:
                        text=_json(comparison)
                        self.db.execute('INSERT INTO comparisons VALUES(?,?,?,?,?)',
                            (comparison['comparison_id'],identity,text,sha256(text.encode()).hexdigest(),_now()))
                    self.db.execute("UPDATE jobs SET state='succeeded',result_id=?,finished_at=?,claim=NULL WHERE id=? AND claim=?",
                        (runs[0]['evaluation_id'] if comparison is None else None,_now(),identity,claim))
                    self._budget()
            except (ValueError, KeyError, TypeError, sqlite3.Error):
                # No raw source/prices, paths or exception strings in job errors.
                with self.transaction():
                    self.db.execute("UPDATE jobs SET state='failed',error=?,finished_at=?,claim=NULL WHERE id=? AND claim=?",
                        ('engine_changed' if not self.get_job(identity)['engine_current'] else 'evaluation_failed',_now(),identity,claim))
            return self.get_job(identity)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('command',choices=('list','jobs','work-one','recover','retry','export','comparisons','queue-comparison','export-comparison'))
    parser.add_argument('--id')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--plan',type=Path)
    args=parser.parse_args()
    try:
        if not args.db.is_file(): raise ValueError('existing research database required')
        with ResearchStore(args.db) as store:
            if args.command=='comparisons': result=store.list_comparisons()
            elif args.command=='queue-comparison':
                if args.plan is None or args.plan.stat().st_size>32768: raise ValueError('bounded --plan JSON required')
                result=store.enqueue_comparison(args.id,json.loads(args.plan.read_text(),object_pairs_hook=_object,parse_constant=_invalid_constant))
            elif args.command=='export-comparison':
                from orb.comparison import render_comparison
                if args.output is None: raise ValueError('export-comparison requires --output and --id')
                result=store.get_comparison(args.id)
                with args.output.open('x',encoding='utf-8') as output:
                    output.write(render_comparison(result) if args.output.suffix=='.md' else json.dumps(result,indent=2)+'\n')
                result={'exported':args.id}
            elif args.command=='list': result=[r['evaluation_id'] for r in store.list_runs()]
            elif args.command=='jobs': result=store.list_jobs()
            elif args.command=='work-one': result=store.execute(args.id)
            elif args.command=='recover': result={'interrupted':store.recover()}
            elif args.command=='retry': result=store.retry(args.id)
            else:
                if args.output is None: raise ValueError('export requires --id and --output')
                result=store.get_run(args.id)
                with args.output.open('x',encoding='utf-8') as output:
                    output.write(json.dumps(result,indent=2)+'\n')
                result={'exported':args.id}
        print(json.dumps(result,indent=2))
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        parser.exit(2,f'Research archive error: {exc}\n')


if __name__=='__main__': main()
