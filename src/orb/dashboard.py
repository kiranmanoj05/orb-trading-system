"""Read-only historical dashboard contract backed by the evaluator.

No web server, credential access, order methods or independent strategy rules.
All numeric money/price/fraction values stay decimal strings for the client.
"""

from copy import deepcopy
from datetime import date, timedelta, timezone

from orb.evaluation import evaluate, _decimal, _digest
from orb.outcomes import outcome_class
from orb.strategy import NEW_YORK, calculate_opening_range
from orb.session import exchange_session


def build_dashboard_run(bars, *args, **kwargs) -> dict:
    """Evaluate once and attach matching chart/evidence data, never a live view.

    This contract accepts canonical bars, not arbitrary imported JSON.
    Validated source/report imports live in dashboard_import.
    The full historical candle series is for retrospective inspection only.
    available_at is explicit so a later replay can hide unavailable candles.
    """
    data = list(bars)
    result = evaluate(data, *args, **kwargs)
    canonical = sorted(data, key=lambda b: (b.start, b.symbol))
    candles = [dict(symbol=b.symbol, start=b.start.astimezone(timezone.utc).isoformat(),
                    available_at=(b.start + timedelta(minutes=1)).astimezone(timezone.utc).isoformat(),
                    **{n: _decimal(getattr(b, n)) for n in ('open', 'high', 'low', 'close')},
                    volume=b.volume) for b in canonical]
    ranges = []
    for coverage in result['coverage']:
        day = date.fromisoformat(coverage['session'])
        session = exchange_session(day)
        window = [b for b in canonical if b.symbol == coverage['symbol']
                  and session.opens_at <= b.start < session.range_end]
        if len(window) != 15:
            continue  # Never display a fabricated range over missing minutes.
        opening = calculate_opening_range(window, coverage['symbol'], day, session.range_end)
        ranges.append(dict(symbol=opening.symbol, session=str(day),
                           start=opening.start.astimezone(timezone.utc).isoformat(),
                           available_at=opening.end.astimezone(timezone.utc).isoformat(),
                           high=_decimal(opening.high), low=_decimal(opening.low),
                           midpoint=_decimal(opening.midpoint)))
    applied = {**result['manifest']['config'],
               'strategy_version': result['manifest']['engine']['strategy_version'],
               'session_timezone': str(NEW_YORK),
               'reference_rules': {'range_minutes': 15, 'entry_cutoff_local': '11:00',
                                   'stop_policy': 'range_midpoint', 'target_r': '2',
                                   'candidate_cap_per_symbol_session': 1}}
    signals = {s['signal_id']: s for s in result['portfolio']['replay']['signals']}
    decisions = [dict(signal_id=t['signal_id'], symbol=t['symbol'],
                      outcome_class=outcome_class(t), status=t['status'], reason=t['reason'],
                      signal=deepcopy(signals[t['signal_id']]), trade=deepcopy(t),
                      events=[deepcopy(e) for e in result['portfolio']['events']
                              if e['signal_id'] == t['signal_id']])
                 for t in result['portfolio']['trades']]
    return {
        'schema_version': 'orb-dashboard-run-v1', 'mode': 'historical_simulation',
        'evaluation_id': result['evaluation_id'],
        'capabilities': {'order_submission': False, 'live_stream': False,
                         'preset_editing': False},
        'applied_settings': {'sha256': _digest(applied), 'values': applied,
                             'preset': None, 'strategy_reference': 'ORB Reference', 'mutable': False},
        'health': {key: result['summary'][key] for key in
                   ('coverage_complete', 'outcomes_complete', 'marks_complete')},
        'metrics_eligible': result['valuation']['metrics_eligible'],
        'candles': candles, 'opening_ranges': ranges, 'decisions': decisions,
        'evidence': result,
        'limitations': ['Retrospective data snapshot; not a live account or causal news explanation.',
                        'Applied settings are frozen; saved preset changes require a separate evaluation.',
                        'Historical candles after a decision are review evidence, not signal inputs.'],
    }
