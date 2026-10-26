"""Loopback-only historical research API. No broker or order interfaces."""

import argparse
from decimal import InvalidOperation
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from orb.dashboard import build_dashboard_run
from orb.dashboard_fixtures import scenarios
from orb.dashboard_import import MAX_BYTES, import_dashboard
from orb.ingestion import _object, _invalid_constant


def create_app(*, port=8765, examples=False, web_dir=None):
    """Single-process local inspection service; imported runs last this session.

    Host validation prevents browser DNS rebinding. Cross-origin requests and
    non-JSON writes are refused; no CORS or remote filesystem access is exposed.
    Bind only to 127.0.0.1 through the supplied CLI; not a deployment/auth layer.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    runs = {}
    if examples:
        for name, bars in scenarios().items():
            run = build_dashboard_run(bars, data_label='synthetic-dashboard-' + name)
            run['import_context'] = dict(kind='example', synthetic=True, recomputed=True,
                                        original_evaluation_id=None,
                                        notice='Invented acceptance example. Not market performance.')
            runs[run['evaluation_id']] = run
    assets = Path(web_dir).resolve() if web_dir else None
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
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.get('/api/runs')
    async def list_runs():
        return [dict(id=key, label=r['evidence']['manifest']['dataset']['label'],
                     complete=r['metrics_eligible'], synthetic=r['import_context'].get('synthetic'),
                     start=r['applied_settings']['values']['start_session'],
                     end=r['applied_settings']['values']['end_session']) for key, r in runs.items()]

    @app.get('/api/runs/{run_id}')
    async def get_run(run_id: str):
        if run_id not in runs:
            raise HTTPException(404, 'run not found in this server session')
        return runs[run_id]

    @app.post('/api/import', status_code=201)
    async def import_run(request: Request):
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            raise HTTPException(415, 'application/json required')
        if len(runs) >= 12:
            raise HTTPException(409, 'session run limit reached; restart to clear local imports')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise HTTPException(413, 'import exceeds 10 MiB')
        try:
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                                 parse_constant=_invalid_constant)
            run = import_dashboard(payload)
        except (ValueError, InvalidOperation, TypeError, KeyError, OverflowError, RecursionError):
            # Do not reflect user file contents, malformed prices or paths.
            raise HTTPException(422, 'Import rejected. Check source format, report fingerprint/configuration, '
                                'matching candles and the 31-day / 20000-bar limits.') from None
        # There are no awaits between validation and insert; one process owns
        # the session. No durable job or multiworker safety is claimed.
        runs[run['evaluation_id']] = run
        return {'id': run['evaluation_id']}

    @app.get('/')
    async def index():
        if not assets or not (assets / 'index.html').is_file():
            return Response('Build web/ and pass --web-dir web/build. API is available at /api/runs.',
                            media_type='text/plain')
        return FileResponse(assets / 'index.html')

    @app.get('/assets/{name}')
    async def asset(name: str):
        # Explicit asset allowlist, never expose project/state files.
        if not assets or name not in ('app.js', 'app.css', 'THIRD_PARTY_NOTICES.txt') or not (assets / name).is_file():
            raise HTTPException(404, 'asset not found')
        return FileResponse(assets / name)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765, choices=range(1024, 65536), metavar='PORT')
    parser.add_argument('--examples', action='store_true', help='load three explicitly synthetic examples')
    parser.add_argument('--web-dir', type=Path, default=Path('web/build'))
    args = parser.parse_args()
    if not (args.web_dir / 'index.html').is_file():
        parser.error('frontend missing: run npm ci && npm run build in web/ first')
    import uvicorn
    uvicorn.run(create_app(port=args.port, examples=args.examples, web_dir=args.web_dir),
                host='127.0.0.1', port=args.port, workers=1, access_log=False)


if __name__ == '__main__':
    main()
