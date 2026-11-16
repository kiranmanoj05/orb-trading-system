"""SPY-only candle research using an exact paper-compatible saved preset.

This is not a broker emulator, paper-account result or an execution permission.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import date
from datetime import datetime
from decimal import Decimal as D, localcontext

from orb.dashboard import build_dashboard_run
from orb.domain import Bar
from orb.evaluation import _digest
from orb.paper_trading import PaperEngine
from orb.performance import historical_performance
from orb.profile_runs import validated_snapshot
from orb.strategy import NEW_YORK

MAX_BARS, MAX_DAYS = 20_000, 31


def study(bars, preset, *, data_label='unspecified', start_session=None,
          end_session=None, synthetic=False, parent_evaluation_id=None, expected_dataset_sha=None):
    snapshot = validated_snapshot(preset)
    # Reuse the real worker's pure validator. No client, engine or DB is created.
    settings = PaperEngine.validate(snapshot, 'iex')
    data = list(bars)
    if not data or len(data) > MAX_BARS: raise ValueError('study requires 1–20000 source bars')
    if any(not D('1e-9') <= p <= D('1e9') or abs(p.as_tuple().exponent) > 12 or len(p.as_tuple().digits) > 32
           for b in data for p in (b.open,b.high,b.low,b.close)):
        raise ValueError('study source prices exceed supported numeric bounds')
    if not any(b.symbol == 'SPY' for b in data): raise ValueError('study requires SPY source bars')
    if (start_session is None) != (end_session is None): raise ValueError('provide both study session bounds')
    dates = [b.start.astimezone(NEW_YORK).date() for b in data]
    start, end = start_session or min(dates), end_session or max(dates)
    if not 0 <= (end-start).days < MAX_DAYS: raise ValueError('study supports at most 31 calendar dates')
    scenarios = [('Saved costs', D(0), D(0)), ('Additional stress', D('2'), D('.005'))]
    rows = []
    dataset = None
    for name, extra_bps, extra_fee in scenarios:
        costs = dict(slippage_bps=str(settings.slippage_bps+extra_bps),
                     fee_per_share=str(settings.fee_per_share+extra_fee))
        row = dict(name=name, cost_assumptions=costs, state='unsupported_cost_settings',
                   report=None, trades=None, coverage=None)
        try:
            effective = replace(settings, slippage_bps=D(costs['slippage_bps']), fee_per_share=D(costs['fee_per_share']))
        except ValueError:
            rows.append(row)
            continue  # Keep a rejected stress cell; never clip or drop it.
        run = build_dashboard_run(data, data_label=data_label, start_session=start, end_session=end,
                                  symbols=('SPY',), **effective.engine_kwargs())
        dataset = run['evidence']['manifest']['dataset']
        if expected_dataset_sha is not None and dataset['sha256'] != expected_dataset_sha:
            raise ValueError('study source identity changed')
        trial = dict(schema_version='orb-paper-alignment-trial-v1', preset=snapshot,
                     costs=costs, parent_evaluation_id=parent_evaluation_id,
                     scope=['SPY'], execution_model='idealised_next_minute_ohlcv_not_broker_limits')
        manifest = run['evidence']['manifest']
        manifest['base_schema_version'] = manifest['schema_version']
        manifest['schema_version'] = 'orb-evaluation-paper-alignment-v1'
        manifest['paper_alignment'] = deepcopy(trial)
        run['evaluation_id'] = run['evidence']['evaluation_id'] = _digest(manifest)
        run['applied_settings']['preset'] = deepcopy(snapshot)
        run['applied_settings']['paper_alignment'] = deepcopy(trial)
        run['applied_settings']['sha256'] = _digest(run['applied_settings']['values'] | {'paper_alignment':trial})
        run['import_context'] = dict(synthetic=synthetic)
        row.update(state='complete' if run['metrics_eligible'] else 'incomplete',
                   report=historical_performance(run), trades=run['evidence']['portfolio']['trades'],
                   coverage=run['evidence']['coverage'])
        rows.append(row)
    eligible = all(r['state'] == 'complete' for r in rows)
    with localcontext() as ctx:
        ctx.prec = 80
        delta = (str(D(rows[1]['report']['trade_statistics']['net_pnl_usd'])-
                     D(rows[0]['report']['trade_statistics']['net_pnl_usd'])) if eligible else None)
    result = dict(schema_version='orb-paper-alignment-v1', source_dataset=dataset,
        parent_evaluation_id=parent_evaluation_id, preset=snapshot, evaluated_symbols=['SPY'],
        excluded_source_symbols=sorted({b.symbol for b in data}-{'SPY'}),
        start_session=str(start), end_session=str(end), synthetic=synthetic,
        parameter_compatibility='worker_pure_validation_passed; feed_entitlement_not_checked',
        trials=rows, all_trials_eligible=eligible,
        stress_delta_net_pnl_usd=delta,
        broker_execution_verified=False, profitability_established=False, recovery_clearance=False,
        limitations=[
            'SPY scope and saved strategy/risk/cost settings are aligned; quote freshness, spreads, marketable limits, bracket activation, liquidity and cancel timing are not simulated.',
            'Both cost cells rerun sizing, fills, targets and risk limits; the stress delta is not a fixed post-hoc fee subtraction and need not be monotone.',
            'Additional stress is an assumed 2 bps per market side plus 0.005 USD per share per side, not measured broker fees or an optimised preset.',
            'All source bars remain in the dataset identity; only SPY drives decisions, account valuation and completeness in this explicitly scoped study.',
            'Retrospective/unsealed research. Missing SPY minutes and unresolved outcomes withhold metrics; no signal is forced and no final holdout is created.',
            'Results are historical simulations, not Alpaca fills, account profit or permission to start/recover trading. Existing presets, runs and paper state are unchanged.',
        ])
    result['study_id'] = _digest(result)
    return result


def from_run(original, preset):
    config = original['evidence']['manifest']['config']
    data = [Bar(c['symbol'], datetime.fromisoformat(c['start']),
                *(D(c[k]) for k in ('open','high','low','close')), c['volume']) for c in original['candles']]
    return study(data, preset, data_label=original['evidence']['manifest']['dataset']['label'],
        start_session=date.fromisoformat(config['start_session']), end_session=date.fromisoformat(config['end_session']),
        synthetic=bool(original.get('import_context',{}).get('synthetic')),
        parent_evaluation_id=original['evaluation_id'], expected_dataset_sha=original['evidence']['manifest']['dataset']['sha256'])


def render_study(result):
    show = lambda v:'Unavailable' if v is None else str(v)
    preset = result['preset']
    lines = ['# SPY paper-preset historical study', '', f"Study: {result['study_id']}",
        f"Preset: {preset['name']} v{preset['version']} ({preset['settings_sha256']})",
        f"Dates: {result['start_session']} to {result['end_session']}; synthetic: {result['synthetic']}",
        'Historical simulation only. Broker execution/profitability/recovery are not verified.', '',
        '| Trial | State | Closed trades | Modelled net USD | Return fraction |', '| --- | --- | ---: | ---: | ---: |']
    for row in result['trials']:
        report=row['report']; s=report['trade_statistics'] if report else {}; m=report['engine_metrics'] if report else {}
        lines.append(f"| {row['name']} | {row['state']} | {show(s.get('closed_trade_count'))} | {show(s.get('net_pnl_usd'))} | {show(m.get('total_return_fraction'))} |")
    lines += ['', f"Stress minus saved-cost net P&L (USD): {show(result['stress_delta_net_pnl_usd'])}", '',
              '## Limits', '']+['- '+v for v in result['limitations']]
    return '\n'.join(lines)+'\n'


def main():
    import argparse
    import json
    from pathlib import Path
    from orb.ingestion import _object, _invalid_constant
    from orb.presets import PresetStore
    from orb.replay import load_bars
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', type=Path)
    parser.add_argument('--preset', type=Path, help='exact saved preset JSON; default is Paper Balanced v1')
    parser.add_argument('--start-session', type=date.fromisoformat)
    parser.add_argument('--end-session', type=date.fromisoformat)
    parser.add_argument('--data-label', default='unspecified')
    parser.add_argument('--synthetic', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        if args.csv.stat().st_size > 10*1024*1024: raise ValueError('source exceeds 10 MiB')
        if args.preset and args.preset.stat().st_size > 8192: raise ValueError('preset exceeds 8 KiB')
        snapshot = (json.loads(args.preset.read_text(), object_pairs_hook=_object, parse_constant=_invalid_constant)
                    if args.preset else PresetStore.paper('paper-balanced'))
        outputs = [p.resolve() for p in (args.output, args.report) if p]
        inputs = [p.resolve() for p in (args.csv, args.preset) if p]
        if len(set(outputs)) != len(outputs) or any(p in inputs for p in outputs):
            raise ValueError('outputs must differ from each other and source/preset inputs')
        result = study(load_bars(args.csv), snapshot, data_label=args.data_label,
            start_session=args.start_session, end_session=args.end_session, synthetic=args.synthetic)
        payload = json.dumps(result, indent=2)+'\n'
        for path, content in ((args.output, payload), (args.report, render_study(result))):
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        print(payload, end='')
    except (ValueError, OSError, TypeError, KeyError) as exc:
        parser.exit(2, f'Study error: {exc}\n')


if __name__ == '__main__':
    main()
