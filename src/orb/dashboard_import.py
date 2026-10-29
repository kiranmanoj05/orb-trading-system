"""Bounded local research imports. Uploaded performance is never authoritative."""

from datetime import date
from decimal import Decimal, InvalidOperation
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from orb.dashboard import build_dashboard_run
from orb.evaluation import _digest
from orb.ingestion import _decode, normalise_capture
from orb.replay import load_bars
from orb.research_settings import ResearchSettings
from orb.strategy import NEW_YORK

MAX_BYTES = 10 * 1024 * 1024
MAX_BARS = 20_000
MAX_DAYS = 31
CONFIG_FIELDS = {'initial_cash', 'risk_fraction', 'max_position_fraction',
                 'max_gross_fraction', 'max_total_risk_fraction', 'daily_loss_fraction',
                 'slippage_bps', 'fee_per_share', 'start_session', 'end_session',
                 'bounds_source', 'symbols'}


def _settings(report):
    if not isinstance(report, dict) or not isinstance(report.get('manifest'), dict):
        raise ValueError('saved evaluation must contain a manifest')
    manifest = report['manifest']
    schema = manifest.get('schema_version')
    base = manifest.get('base_schema_version') if schema in ('orb-evaluation-bundle-v1', 'orb-evaluation-profile-v1') else schema
    if base not in ('orb-evaluation-v4', 'orb-evaluation-v5', 'orb-evaluation-v6', 'orb-evaluation-v7'):
        raise ValueError('supported saved evaluations are v4–v7 and their wrappers')
    if report.get('evaluation_id') != _digest(manifest):
        raise ValueError('saved evaluation manifest fingerprint mismatch')
    config = manifest.get('config')
    expected_fields = CONFIG_FIELDS | ({'entry_policy'} if base in ('orb-evaluation-v6', 'orb-evaluation-v7') else set()) | ({'entry_caps'} if base == 'orb-evaluation-v7' else set())
    if not isinstance(config, dict) or set(config) != expected_fields:
        raise ValueError('unsupported saved evaluation configuration fields')
    if config['symbols'] != ['SPY', 'QQQ'] or config['bounds_source'] not in ('explicit', 'observed_dates'):
        raise ValueError('unsupported saved evaluation scope')
    supported = ResearchSettings.from_config(config)
    if schema == 'orb-evaluation-profile-v1':
        from orb.profile_runs import validated_snapshot
        profile = validated_snapshot(manifest.get('profile'))
        if set(manifest['profile']) != set(profile):
            raise ValueError('unsupported saved profile metadata')
        saved_settings = ResearchSettings.from_dict(profile.get('settings'))
        if saved_settings.current_values() != supported.current_values() or profile.get('settings_sha256') != saved_settings.fingerprint:
            raise ValueError('profile snapshot disagrees with applied settings')
    start, end = (date.fromisoformat(config[k]) for k in ('start_session', 'end_session'))
    if not 0 <= (end - start).days < MAX_DAYS:
        raise ValueError('local viewer supports at most 31 calendar days per import')
    return {**supported.engine_kwargs(),
            **(dict(start_session=start, end_session=end) if config['bounds_source'] == 'explicit' else {})}


def import_dashboard(payload: dict) -> dict:
    """Accept CSV or original capture/bundle bytes; recompute with this engine.

    No archive extraction, server paths, remote URLs or credentials are accepted.
    A matching manifest proves internal identity, not provenance/authenticity.
    v4 metrics are explicitly regenerated, never silently re-labelled as v5.
    """
    if not isinstance(payload, dict) or set(payload) - {'kind', 'source', 'report', 'label', 'bars_csv', 'bundle_manifest'}:
        raise ValueError('unexpected import fields')
    if len(json.dumps(payload).encode()) > MAX_BYTES:
        raise ValueError('import exceeds 10 MiB')
    source, kind = payload.get('source'), payload.get('kind')
    label = payload.get('label', 'local-import-unverified')
    if not isinstance(source, str) or not isinstance(label, str) or not 1 <= len(label.strip()) <= 120:
        raise ValueError('provide source text and a label of 1–120 characters')
    # Reject pathological Decimal expansion before capture canonicalisation.
    if any(len(m[1].lstrip('+-')) > 2 or abs(int(m[1])) > 12
           for m in re.finditer(r'[eE]([+-]?\d+)', source)):
        raise ValueError('source numeric exponent exceeds local viewer limits')
    bundle = None
    if kind == 'csv':
        if 'bars_csv' in payload or 'bundle_manifest' in payload:
            raise ValueError('CSV import cannot include bundle metadata')
        if len(source.splitlines()) > MAX_BARS + 1:
            raise ValueError('local viewer supports at most 20000 bars')
        with TemporaryDirectory(prefix='orb-import-') as directory:
            path = Path(directory) / 'bars.csv'
            path.write_text(source, encoding='utf-8')
            bars = load_bars(path)
    elif kind in ('capture', 'bundle'):
        bars, canonical, bundle = normalise_capture(source.encode('utf-8'))
        if kind == 'bundle':
            if not isinstance(payload.get('bars_csv'), str) or not isinstance(payload.get('bundle_manifest'), str):
                raise ValueError('bundle requires capture.json, bars.csv and manifest.json')
            if payload['bars_csv'].encode() != canonical or _decode(payload['bundle_manifest'].encode()) != bundle:
                raise ValueError('bundle integrity mismatch')
        elif 'bars_csv' in payload or 'bundle_manifest' in payload:
            raise ValueError('capture import cannot include partial bundle files')
    else:
        raise ValueError('supported import kinds: csv, capture, bundle')
    days = [b.start.astimezone(NEW_YORK).date() for b in bars]
    if not bars or len(bars) > MAX_BARS or (max(days) - min(days)).days >= MAX_DAYS:
        raise ValueError('local viewer supports 20000 bars across at most 31 calendar days')
    if any(not Decimal('1e-9') <= p <= Decimal('1e9') or len(p.as_tuple().digits) > 32
           for b in bars for p in (b.open, b.high, b.low, b.close)):
        raise ValueError('source prices exceed local viewer numeric bounds')
    saved = payload.get('report')
    settings = _settings(saved) if saved is not None else {}
    run = build_dashboard_run(bars, data_label=label.strip(), **settings)
    evidence = run['evidence']
    if saved is not None:
        dataset = saved['manifest'].get('dataset', {})
        if dataset.get('sha256') != evidence['manifest']['dataset']['sha256']:
            raise ValueError('saved evaluation does not match the source candles')
        original_bundle = dataset.get('source_bundle')
        if original_bundle and (not bundle or original_bundle.get('bundle_id') != bundle['bundle_id']):
            raise ValueError('saved bundle evaluation requires the matching original capture or bundle')
        if saved['manifest']['config']['start_session'] != evidence['manifest']['config']['start_session'] or \
                saved['manifest']['config']['end_session'] != evidence['manifest']['config']['end_session']:
            raise ValueError('saved evaluation date scope does not match source')
    if bundle:
        manifest = evidence['manifest']
        manifest['base_schema_version'] = manifest['schema_version']
        manifest['schema_version'] = 'orb-evaluation-bundle-v1'
        manifest['dataset']['source_bundle'] = {**bundle, 'integrity_verified': True}
        run['evaluation_id'] = evidence['evaluation_id'] = _digest(manifest)
    if saved is not None and saved['manifest']['schema_version'] == 'orb-evaluation-profile-v1':
        from copy import deepcopy
        snapshot = deepcopy(saved['manifest']['profile'])
        manifest = evidence['manifest']
        manifest['base_schema_version'] = ('orb-evaluation-v7' if 'entry_caps' in manifest['config'] else
                                           'orb-evaluation-v6' if 'entry_policy' in manifest['config'] else 'orb-evaluation-v5')
        manifest['schema_version'] = 'orb-evaluation-profile-v1'
        manifest['profile'] = snapshot
        run['applied_settings']['preset'] = snapshot
        run['applied_settings']['sha256'] = _digest({'values': run['applied_settings']['values'], 'preset': snapshot})
        run['evaluation_id'] = evidence['evaluation_id'] = _digest(manifest)
    run['import_context'] = {
        'kind': kind, 'synthetic': bundle['data_kind'] == 'synthetic' if bundle else None,
        'original_evaluation_id': saved['evaluation_id'] if saved else None,
        'original_schema': saved['manifest']['schema_version'] if saved else None,
        'recomputed': True, 'provider_authenticity_verified': False,
        'notice': 'Recomputed from source with the current engine. Original report metrics are not used.'}
    return run
