"""Bounded observation/acquisition for the local dashboard; never submits orders."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from orb.acquisition import Credentials, AcquisitionError, FetchLimits, acquire_bundle
from orb.dashboard_import import import_dashboard, MAX_BYTES
from orb.ingestion import _decode
from orb.paper_snapshot import BASE_URL, HTTPResponse, PaperSnapshotError, _http_get
from orb.session import exchange_session


def connection_status():
    configured = all(os.environ.get(n) for n in ('APCA_API_KEY_ID', 'APCA_API_SECRET_KEY'))
    return {'configured': configured, 'connected': False, 'environment': 'paper',
            'execution_enabled': False, 'order_submission': False,
            'message': ('Credentials available to this server. Refresh paper account to verify access.' if configured else
                        'Start this server with APCA_API_KEY_ID and APCA_API_SECRET_KEY in its environment. Keys never go in the browser.')}


def number(value, *, nullable=False):
    if nullable and value is None: return None
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise PaperSnapshotError('invalid paper numeric value')
    try: parsed = Decimal(value)
    except InvalidOperation: raise PaperSnapshotError('invalid paper numeric value') from None
    if not parsed.is_finite() or abs(parsed) > Decimal('1e18'):
        raise PaperSnapshotError('invalid paper numeric value')
    return str(parsed)


def text(value, length=100):
    if not isinstance(value, str) or len(value) > length or any(ord(c) < 32 for c in value):
        raise PaperSnapshotError('invalid paper text value')
    return value


def stamp(value):
    if value is None: return None
    text(value, 64)
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None: raise ValueError()
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError: raise PaperSnapshotError('invalid paper timestamp') from None


def observe_paper(*, credentials=None, transport=None):
    """Account/positions and the latest 500 orders, including cumulative fills.

    An order average is not an individual execution or an attributed ORB trade.
    This observation is not atomic and MUST NOT clear the recovery gate.
    """
    credentials = credentials or Credentials.from_environment()
    transport = transport or _http_get
    headers = {'APCA-API-KEY-ID': credentials.key, 'APCA-API-SECRET-KEY': credentials.secret,
               'Accept': 'application/json', 'User-Agent': 'orb-dashboard-observation/1'}
    started = datetime.now(timezone.utc).isoformat()
    resources = ('/v2/account', '/v2/positions', '/v2/orders?' + urlencode(
        {'status': 'all', 'limit': 500, 'direction': 'desc', 'nested': 'true'}))
    decoded = []
    for path in resources:
        response = transport(BASE_URL + path, headers, 10, 2_000_000)
        if type(response) is not HTTPResponse: raise PaperSnapshotError('invalid paper transport')
        if response.status in (401, 403): raise PaperSnapshotError('Paper authentication denied. Check paper-account keys in the server environment.')
        if response.status != 200: raise PaperSnapshotError(f'Paper HTTP {response.status}; observation stopped.')
        if len(response.body) > 2_000_000: raise PaperSnapshotError('paper response exceeds byte limit')
        if any(v.encode() in response.body for v in (credentials.key, credentials.secret)):
            raise PaperSnapshotError('paper response contains credential material')
        try: decoded.append(_decode(response.body))
        except (ValueError, UnicodeError, RecursionError): raise PaperSnapshotError('invalid paper response') from None
    account, positions, orders = decoded
    try:
        if not isinstance(account, dict) or not isinstance(positions, list) or not isinstance(orders, list) or len(orders) > 500 or len(positions) > 1000:
            raise PaperSnapshotError('invalid paper observation')
        summary = {k: number(account.get(k), nullable=k != 'cash') for k in
                   ('cash', 'equity', 'last_equity', 'buying_power')}
        summary['status'] = text(account['status'], 40)
        clean_positions = [dict(symbol=text(p['symbol'], 24), quantity=number(p['qty']),
                                market_value=number(p.get('market_value'), nullable=True),
                                unrealized_pl=number(p.get('unrealized_pl'), nullable=True)) for p in positions]
        clean_orders = []
        for parent in orders:
            legs = parent.get('legs') or []
            if not isinstance(legs, list) or len(legs) > 2:
                raise PaperSnapshotError('invalid paper bracket legs')
            for o in [parent, *legs]:
                clean_orders.append(dict(id=sha256(text(o['id'], 128).encode()).hexdigest()[:24],
                    symbol=text(o['symbol'], 24), side=text(o['side'], 16), status=text(o['status'], 48),
                    quantity=number(o.get('qty'), nullable=True), filled_quantity=number(o['filled_qty']),
                    average_fill_price=number(o.get('filled_avg_price'), nullable=True),
                    submitted_at=stamp(o.get('submitted_at')), filled_at=stamp(o.get('filled_at')),
                    attribution='unattributed_broker_order'))
        clean_orders = list({o['id']: o for o in clean_orders}.values())
    except (KeyError, TypeError, AttributeError): raise PaperSnapshotError('incomplete paper observation') from None
    return {'mode': 'paper_observation', 'provider': 'alpaca', 'connected': True,
            'observed_from': started, 'observed_at': datetime.now(timezone.utc).isoformat(),
            'account': summary, 'positions': clean_positions, 'orders': clean_orders,
            'order_limit': 500, 'includes_bracket_legs': True, 'history_may_be_truncated': len(orders) == 500,
            'execution_enabled': False, 'orders_submitted': False,
            'notice': 'Latest 500 broker orders; cumulative average fills, not individual executions. Not an atomic recovery snapshot. ORB attribution is unknown.'}


def fetch_run(start_session, end_session, feed, *, credentials=None, transport=None):
    try:
        start, end = date.fromisoformat(start_session), date.fromisoformat(end_session)
        if not 0 <= (end-start).days <= 6: raise ValueError()
        first, last = exchange_session(start), exchange_session(end)
        if first is None or last is None: raise ValueError()
    except (ValueError, TypeError): raise AcquisitionError('Choose exchange sessions across at most seven calendar days.') from None
    with TemporaryDirectory(prefix='orb-dashboard-fetch-') as tmp:
        dest = Path(tmp) / 'capture'
        acquire_bundle(first.opens_at.isoformat(), (last.closes_at-timedelta(minutes=1)).isoformat(), dest,
                       feed=feed, credentials=credentials, transport=transport,
                       limits=FetchLimits(max_pages=10, max_attempts=1, max_total_bytes=8_000_000, timeout_seconds=10))
        payload = {'kind': 'bundle', 'label': f'Alpaca {feed.upper()} · {start} → {end}',
                   'source': (dest/'capture.json').read_text(), 'bars_csv': (dest/'bars.csv').read_text(),
                   'bundle_manifest': (dest/'manifest.json').read_text()}
        return import_dashboard(payload), payload


def local_bundles(root):
    """Only immediate children of a configured trusted data directory; no symlinks."""
    root = Path(root)
    if not root.is_dir() or root.is_symlink(): return []
    found = []
    for directory in sorted(root.iterdir())[:200]:
        if directory.is_symlink() or not directory.is_dir(): continue
        files = [directory/n for n in ('capture.json', 'bars.csv', 'manifest.json')]
        if not all(p.is_file() and not p.is_symlink() for p in files): continue
        label = directory.name[:100]
        if sum(p.stat().st_size for p in files) > MAX_BYTES:
            found.append({'label': label, 'error': 'Bundle exceeds 10 MiB import limit.'}); continue
        try:
            payload = dict(kind='bundle', label=label, source=files[0].read_text(),
                           bars_csv=files[1].read_text(), bundle_manifest=files[2].read_text())
            run = import_dashboard(payload)
            found.append({'label': label, 'run': run, 'payload': payload})
        except (ValueError, OSError, UnicodeError, InvalidOperation, KeyError, TypeError, RecursionError):
            found.append({'label': label, 'error': 'Bundle validation failed; use Import run for details/settings.'})
    return found
