"""Apply an exact saved research version to server-owned historical source."""

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal

from orb.dashboard import build_dashboard_run
from orb.domain import Bar
from orb.evaluation import _digest
from orb.research_settings import ResearchSettings


def validated_snapshot(preset):
    fields = ('id', 'version', 'name', 'settings', 'settings_sha256')
    required = set(fields)
    if not isinstance(preset, dict) or not required.issubset(preset):
        raise ValueError('invalid saved preset snapshot')
    if (not isinstance(preset['id'], str) or not 1 <= len(preset['id']) <= 80
            or type(preset['version']) is not int or preset['version'] < 1
            or not isinstance(preset['name'], str) or not 1 <= len(preset['name']) <= 80
            or not preset['name'].isprintable()):
        raise ValueError('invalid saved preset identity')
    settings = ResearchSettings.from_dict(preset['settings'])
    if preset['settings_sha256'] != settings.fingerprint:
        raise ValueError('preset fingerprint mismatch')
    return {k: deepcopy(preset[k]) for k in fields}


def apply_preset(original, preset):
    snapshot = validated_snapshot(preset)
    settings = ResearchSettings.from_dict(snapshot['settings'])
    bars = [Bar(c['symbol'], datetime.fromisoformat(c['start']),
                *(Decimal(c[k]) for k in ('open', 'high', 'low', 'close')), c['volume'])
            for c in original['candles']]
    source = original['evidence']['manifest']
    config = source['config']
    scope = (dict(start_session=date.fromisoformat(config['start_session']),
                  end_session=date.fromisoformat(config['end_session']))
             if config['bounds_source'] == 'explicit' else {})
    run = build_dashboard_run(bars, data_label=source['dataset']['label'], **settings.engine_kwargs(), **scope)
    manifest = run['evidence']['manifest']
    if manifest['dataset']['sha256'] != source['dataset']['sha256']:
        raise ValueError('source candle identity changed')
    if 'source_bundle' in source['dataset']:
        manifest['dataset']['source_bundle'] = deepcopy(source['dataset']['source_bundle'])
    manifest['base_schema_version'] = manifest['schema_version']
    manifest['schema_version'] = 'orb-evaluation-profile-v1'
    manifest['profile'] = snapshot
    run['evaluation_id'] = run['evidence']['evaluation_id'] = _digest(manifest)
    run['applied_settings']['preset'] = deepcopy(snapshot)
    run['applied_settings']['sha256'] = _digest({'values': run['applied_settings']['values'], 'preset': snapshot})
    run['import_context'] = deepcopy(original['import_context'])
    run['import_context'].update(parent_evaluation_id=original['evaluation_id'],
        notice='New historical evaluation using an exact saved research preset. '
               'Source integrity metadata is retained; no provider authenticity is established.')
    run['limitations'] = [v for v in run['limitations'] if 'No preset editor yet' not in v]
    run['capabilities']['preset_editing'] = True  # Editing creates versions, never mutates this run.
    return run
