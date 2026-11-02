"""Loopback research API with paper-only execution and historical research."""

import argparse
from contextlib import asynccontextmanager
import sqlite3
from decimal import InvalidOperation
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from orb.dashboard_alpaca import connection_status, observe_paper, fetch_run, local_bundles
from orb.acquisition import AcquisitionError
from orb.paper_snapshot import PaperSnapshotError
from orb.paper_trading import PaperEngine, PaperError
from threading import Lock
from orb.profile_runs import apply_preset

from fastapi.responses import FileResponse, JSONResponse, Response

from orb.dashboard import build_dashboard_run
from orb.dashboard_fixtures import scenarios, comparison_scenario
from orb.dashboard_import import MAX_BYTES, import_dashboard
from orb.ingestion import _object, _invalid_constant
from orb.presets import PresetStore, PresetConflict
from orb.research_settings import settings_contract
from orb.research_store import ResearchStore, ResearchConflict, ResearchIntegrityError


def create_app(*, port=8765, examples=False, web_dir=None, preset_db=None, research_db=None, data_dir=None, comparison_example=False, paper_db=None):
    """Local inspection service with an optional persistent research archive.

    Host validation prevents browser DNS rebinding. Cross-origin requests and
    non-JSON writes are refused; no CORS or remote filesystem access is exposed.
    Bind only to 127.0.0.1 through the supplied CLI; not a deployment/auth layer.
    """
    store = PresetStore(preset_db if preset_db is not None else ':memory:')
    research = ResearchStore(research_db if research_db is not None else ':memory:',
                             max_runs=100 if research_db is not None else 12)
    paper_engine = PaperEngine(paper_db) if paper_db is not None else None
    @asynccontextmanager
    async def lifespan(app):
        try:
            research.recover()
        except ResearchConflict:
            pass  # Another conforming local worker is still active.
        try:
            yield
        finally:
            if paper_engine: await run_in_threadpool(paper_engine.close)
            store.close()
            research.close()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.presets = store
    app.state.research = research

    @app.exception_handler(ResearchIntegrityError)
    async def archive_error(request, exc):
        return JSONResponse({"detail": "Research archive integrity check failed; preserve the database for review."}, status_code=503)
    example_sources = scenarios() if examples else {}
    if comparison_example:
        example_sources["comparison_three_sessions"] = comparison_scenario()
    if example_sources:
        for name, bars in example_sources.items():
            run = build_dashboard_run(bars, data_label='synthetic-dashboard-' + name)
            run['import_context'] = dict(kind='example', synthetic=True, recomputed=True,
                                        original_evaluation_id=None,
                                        notice='Invented acceptance example. Not market performance.')
            source = 'symbol,start,open,high,low,close,volume\n' + ''.join(
                ','.join(str(c[k]) for k in ('symbol','start','open','high','low','close','volume')) + '\n'
                for c in run['candles'])
            research.import_run(run, {'kind':'csv','source':source,'label':run['evidence']['manifest']['dataset']['label']})
    assets = Path(web_dir).resolve() if web_dir else None
    provider_lock = Lock()
    local_imports = []
    def sync_bundles():
        records = []
        for item in local_bundles(data_dir) if data_dir is not None else []:
            if 'error' in item:
                records.append({'label': item['label'], 'error': item['error']}); continue
            try:
                job = research.import_run(item['run'], item['payload'])
                records.append({'label': item['label'], 'id': job['result_id']})
            except ResearchConflict:
                records.append({'label': item['label'], 'error': 'Research archive capacity reached.'})
        local_imports[:] = records
        return records
    sync_bundles()
    host = f'127.0.0.1:{port}'
    origin = f'http://{host}'

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        if request.headers.get('host') != host or request.headers.get('origin', origin) != origin:
            return JSONResponse({'detail': 'local same-origin access required'}, status_code=403)
        response = await call_next(request)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    async def read_json(request, limit=MAX_BYTES):
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            raise HTTPException(415, 'application/json required')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > limit:
                raise HTTPException(413, 'request exceeds endpoint size limit')
        try:
            return json.loads(raw.decode('utf-8'), object_pairs_hook=_object, parse_constant=_invalid_constant)
        except (ValueError, RecursionError):
            raise HTTPException(422, 'invalid JSON request') from None

    def preset_error(exc):
        if isinstance(exc, (PresetConflict, ResearchConflict)):
            raise HTTPException(409, str(exc)) from None
        if isinstance(exc, KeyError):
            raise HTTPException(404, 'preset version not found') from None
        raise HTTPException(422, 'invalid preset request or unsupported settings') from None

    @app.get('/api/paper/status')
    async def paper_status():
        if not paper_engine: return {'available': False, 'enabled': False, 'message': 'Paper execution storage not configured.'}
        return {'available': True, **await run_in_threadpool(paper_engine.status)}

    @app.post('/api/paper/start')
    async def start_paper(request: Request):
        if not paper_engine: raise HTTPException(409, 'Paper storage is not configured.')
        p = await read_json(request, 4096)
        try:
            if not isinstance(p, dict) or set(p) != {'preset_id', 'version', 'feed'} or type(p['version']) is not int:
                raise ValueError('exact saved preset required')
            record = store.get(p['preset_id'], p['version'])
            return await run_in_threadpool(paper_engine.start, record, p['feed'])
        except (ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, (PaperError, AcquisitionError)): raise HTTPException(409, str(exc)) from None
            raise HTTPException(422, 'Invalid paper preset request.') from None

    @app.post('/api/paper/stop')
    async def stop_paper(request: Request):
        await empty_command(request)
        if not paper_engine: raise HTTPException(409, 'Paper storage is not configured.')
        return await run_in_threadpool(paper_engine.stop)

    @app.get('/api/alpaca/status')
    async def alpaca_status():
        return connection_status()

    @app.get('/api/local-bundles')
    async def list_local_bundles(): return {'imports': local_imports}

    @app.post('/api/local-bundles/refresh')
    async def refresh_local_bundles(request: Request):
        await empty_command(request)
        return {'imports': await run_in_threadpool(sync_bundles)}

    @app.post('/api/alpaca/observe')
    async def paper_observation(request: Request):
        await empty_command(request)
        if not provider_lock.acquire(blocking=False): raise HTTPException(409, 'An Alpaca request is already running.')
        try: return await run_in_threadpool(observe_paper)
        except (AcquisitionError, PaperSnapshotError) as exc: raise HTTPException(502, str(exc)) from None
        finally: provider_lock.release()

    @app.post('/api/alpaca/fetch', status_code=201)
    async def fetch_alpaca(request: Request):
        p = await read_json(request, 4096)
        if not isinstance(p, dict) or set(p) != {'start_session', 'end_session', 'feed'}:
            raise HTTPException(422, 'Provide start_session, end_session and feed (sip/iex).')
        if not provider_lock.acquire(blocking=False): raise HTTPException(409, 'An Alpaca request is already running.')
        try:
            run, payload = await run_in_threadpool(fetch_run, p['start_session'], p['end_session'], p['feed'])
            job = research.import_run(run, payload)
            return {'id': job['result_id']}
        except AcquisitionError as exc: raise HTTPException(502, str(exc)) from None
        except ResearchConflict as exc: raise HTTPException(409, str(exc)) from None
        except (ValueError, TypeError, KeyError): raise HTTPException(422, 'Acquired data could not be evaluated within import bounds.') from None
        finally: provider_lock.release()

    @app.get('/api/presets')
    async def list_presets():
        return dict(contract=settings_contract(), presets=store.list(), durable=preset_db is not None)

    @app.get('/api/presets/{preset_id}/history')
    async def history(preset_id: str):
        try:
            return store.history(preset_id)
        except (KeyError, ValueError) as exc:
            preset_error(exc)

    @app.post('/api/presets', status_code=201)
    async def save_preset(request: Request):
        payload = await read_json(request, 8192)
        try:
            if not isinstance(payload, dict) or set(payload) not in (
                    {'name', 'settings'}, {'name', 'settings', 'id', 'expected_version'}):
                raise ValueError('unexpected preset fields')
            if 'id' in payload and (not isinstance(payload['id'], str) or
                    type(payload['expected_version']) is not int or payload['expected_version'] < 1):
                raise ValueError('update requires an exact saved version')
            return store.save(payload['name'], payload['settings'],
                preset_id=payload.get('id'), expected_version=payload.get('expected_version'))
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            preset_error(exc)

    @app.post('/api/presets/{preset_id}/duplicate', status_code=201)
    async def duplicate(preset_id: str, request: Request):
        p = await read_json(request, 8192)
        try:
            if not isinstance(p, dict) or set(p) != {'version', 'name'}:
                raise ValueError('unexpected duplicate fields')
            return store.duplicate(preset_id, p['version'], p['name'])
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            preset_error(exc)

    async def queue_version(run_id, request):
        p = await read_json(request, 8192)
        try:
            research.get_run(run_id)
        except KeyError:
            raise HTTPException(404, 'source run not found') from None
        try:
            if not isinstance(p, dict) or set(p) != {'preset_id', 'version'}:
                raise ValueError('exact saved version required')
            if type(p['version']) is not int or p['version'] < 1:
                raise ValueError('exact saved version required')
            record = store.get(p['preset_id'], p['version'])
            return research.enqueue_preset(run_id, record)
        except ResearchIntegrityError:
            raise
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            preset_error(exc)

    @app.post('/api/runs/{run_id}/queue-preset', status_code=201)
    async def queue_run(run_id: str, request: Request):
        return await queue_version(run_id, request)

    @app.post('/api/runs/{run_id}/apply-preset', status_code=201)
    async def rerun(run_id: str, request: Request):
        job = await queue_version(run_id, request)
        try:
            result = research.execute(job['id'])
        except ResearchConflict as exc:
            raise HTTPException(409, str(exc)) from None
        if result['state'] != 'succeeded':
            raise HTTPException(409, 'Research job did not complete. Inspect Research jobs; no run was published.')
        return {'id':result['result_id']}

    @app.post('/api/runs/{run_id}/queue-comparison', status_code=201)
    async def queue_comparison(run_id: str, request: Request):
        payload=await read_json(request,32768)
        try:
            if not isinstance(payload,dict) or set(payload)!={'name','split_session','presets','costs'}:
                raise ValueError('unsupported comparison request')
            if not isinstance(payload['presets'],list) or not 2<=len(payload['presets'])<=3:
                raise ValueError('two or three exact preset versions required')
            snapshots=[]
            for p in payload['presets']:
                if not isinstance(p,dict) or set(p)!={'id','version'} or type(p['version']) is not int:
                    raise ValueError('exact saved preset version required')
                snapshots.append(store.get(p['id'],p['version']))
            from orb.comparison import SCHEMA
            return research.enqueue_comparison(run_id,{'schema_version':SCHEMA,**payload,'presets':snapshots})
        except ResearchIntegrityError: raise
        except (ValueError,KeyError,TypeError,sqlite3.Error) as exc: preset_error(exc)

    @app.get('/api/comparisons')
    async def comparison_list():
        return [dict(id=c['comparison_id'],name=c['manifest']['plan']['name'],
                     parent_evaluation_id=c['manifest']['parent_evaluation_id']) for c in research.list_comparisons()]

    @app.get('/api/comparisons/{identity}')
    async def comparison_get(identity: str):
        try: return research.get_comparison(identity)
        except KeyError: raise HTTPException(404,'comparison not found') from None

    @app.get('/api/comparisons/{identity}/report')
    async def comparison_report(identity: str):
        from orb.comparison import render_comparison
        try: return Response(render_comparison(research.get_comparison(identity)),media_type='text/markdown')
        except KeyError: raise HTTPException(404,'comparison not found') from None

    @app.get('/api/storage')
    async def storage():
        return {'durable':research.durable,'max_runs':research.max_runs,
                'execution':'explicit local work-one; no background scheduler',
                'order_submission':False}

    async def empty_command(request):
        async for chunk in request.stream():
            if chunk:
                raise HTTPException(422, 'job command does not accept a request body')

    @app.get('/api/jobs')
    async def jobs(): return research.list_jobs()

    @app.post('/api/jobs/work-one')
    async def work_one(request: Request):
        await empty_command(request)
        try: return research.execute()
        except ResearchConflict as exc: raise HTTPException(409,str(exc)) from None

    @app.post('/api/jobs/recover')
    async def recover_jobs(request: Request):
        await empty_command(request)
        try: return {'interrupted':research.recover()}
        except ResearchConflict as exc: raise HTTPException(409,str(exc)) from None

    @app.post('/api/jobs/{job_id}/retry', status_code=201)
    async def retry_job(job_id: str, request: Request):
        await empty_command(request)
        try: return research.retry(job_id)
        except KeyError: raise HTTPException(404,'job not found') from None
        except ResearchConflict as exc: raise HTTPException(409,str(exc)) from None

    @app.get('/api/runs')
    async def list_runs():
        return [dict(id=r['evaluation_id'], label=r['evidence']['manifest']['dataset']['label'],
                     complete=r['metrics_eligible'], synthetic=r['import_context'].get('synthetic'),
                     preset=r['applied_settings']['preset'],
                     trial=(dict(name=t['preset']['name'],version=t['preset']['version'],period=t['period'],cost=t['cost_scenario']['name'])
                            if (t:=r['applied_settings'].get('comparison_trial')) else None),
                     start=r['applied_settings']['values']['start_session'],
                     end=r['applied_settings']['values']['end_session']) for r in research.list_runs()]

    @app.get('/api/runs/{run_id}')
    async def get_run(run_id: str):
        try: return research.get_run(run_id)
        except KeyError: raise HTTPException(404,'research run not found') from None

    @app.get('/api/runs/{run_id}/source')
    async def get_source(run_id: str):
        try: return research.source(run_id)
        except KeyError: raise HTTPException(404,'research source not found') from None

    @app.post('/api/import', status_code=201)
    async def import_run(request: Request):
        payload = await read_json(request)
        try:
            run = import_dashboard(payload)
        except (ValueError, InvalidOperation, TypeError, KeyError, OverflowError, RecursionError):
            raise HTTPException(422, 'Import rejected. Check source format, report fingerprint/configuration, '
                                'matching candles and the 31-day / 20000-bar limits.') from None
        try:
            job = research.import_run(run, payload)
        except ResearchConflict as exc:
            raise HTTPException(409,str(exc)) from None
        return {'id':job['result_id']}

    @app.get('/')
    async def index():
        if not assets or not (assets / 'index.html').is_file():
            return Response('Build web/ and pass --web-dir web/build. API is available at /api/runs.',
                            media_type='text/plain')
        return FileResponse(assets / 'index.html')

    @app.get('/assets/{name}')
    async def asset(name: str):
        # Explicit asset allowlist, never expose project/state files.
        if not assets or name not in ('app.js', 'app.css', 'THIRD_PARTY_NOTICES.txt', 'droplet.mp4', 'droplet-poster.jpg', 'wallpaper-default.mp4', 'wallpaper-poster.png', 'nocturne.svg') or not (assets / name).is_file():
            raise HTTPException(404, 'asset not found')
        return FileResponse(assets / name)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765, choices=range(1024, 65536), metavar='PORT')
    parser.add_argument('--paper-db', type=Path, default=Path('results/dashboard-paper.sqlite'))
    parser.add_argument('--examples', action='store_true', help='load three explicitly synthetic examples')
    parser.add_argument('--web-dir', type=Path, default=Path('web/build'))
    parser.add_argument('--preset-db', type=Path, default=Path('results/dashboard-presets.sqlite'))
    parser.add_argument('--comparison-example', action='store_true', help='load an explicitly invented three-session comparison source')
    parser.add_argument('--research-db', type=Path, default=Path('results/dashboard-research.sqlite'))
    parser.add_argument('--data-dir', type=Path, default=Path('data'), help='discover complete bundles in immediate child folders')
    args = parser.parse_args()
    if not (args.web_dir / 'index.html').is_file():
        parser.error('frontend missing: run npm ci && npm run build in web/ first')
    import uvicorn
    uvicorn.run(create_app(port=args.port, examples=args.examples, web_dir=args.web_dir, preset_db=args.preset_db, research_db=args.research_db, data_dir=args.data_dir, comparison_example=args.comparison_example, paper_db=args.paper_db),
                host='127.0.0.1', port=args.port, workers=1, access_log=False)


if __name__ == '__main__':
    main()
