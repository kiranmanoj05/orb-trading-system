"""Completed-minute valuation evidence; does not change execution or sizing."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from orb.session import exchange_session
from orb.strategy import NEW_YORK
from orb.outcomes import outcomes_complete


def market_valuation(bars, portfolio, coverage):
    """Value post-event holdings only with prices available at that instant.

    Each regular-session minute is sampled, including missing minutes. A held
    lot needs that minute's completed close; at its entry instant its actual
    fill is used instead. No last-price carry, interpolation or exit inference.
    Overnight-held lots are unknown until a new completed close is available.
    This series is a reporting overlay, not a sizing snapshot.
    """
    minute = timedelta(minutes=1)
    closes = {}
    for bar in bars:
        session = exchange_session(bar.start.astimezone(NEW_YORK).date())
        if session and session.opens_at <= bar.start < session.closes_at:
            closes[(bar.symbol, bar.start.astimezone(timezone.utc) + minute)] = bar.close
    events = {}
    for event in portfolio['events']:
        events.setdefault(datetime.fromisoformat(event['at']), []).append(event)
    trades = {t['signal_id']: t for t in portfolio['trades']}
    initial = Decimal(portfolio['config']['initial_cash'])
    clock = set(events)
    for day in sorted({c['session'] for c in coverage}):
        session = exchange_session(datetime.fromisoformat(day).date())
        stamp = session.opens_at
        # Opening sample prevents an overnight price from masquerading as fresh.
        while stamp <= session.closes_at:
            clock.add(stamp)
            stamp += minute
    state = {'cash': str(initial), 'allocations': []}
    samples = []
    for stamp in sorted(clock):
        if stamp in events:
            state = events[stamp][-1]['account']
        cash = Decimal(state['cash'])
        value = unrealized = Decimal('0')
        positions = []
        missing = []
        for lot in state['allocations']:
            if lot['state'] not in ('open', 'unresolved'):
                continue
            entry = Decimal(lot['entry_price'])
            entry_at = datetime.fromisoformat(trades[lot['order_id']]['entry_at'])
            price = entry if stamp == entry_at else closes.get((lot['symbol'], stamp))
            if price is None:
                missing.append(lot['symbol'])
            else:
                value += price * lot['quantity']
                unrealized += (price - entry) * lot['quantity'] - Decimal(lot['entry_fee'])
            positions.append({'signal_id': lot['order_id'], 'symbol': lot['symbol'],
                              'quantity': lot['quantity'],
                              'price': str(price) if price is not None else None,
                              'source': 'entry_fill' if stamp == entry_at else
                                        'completed_close' if price is not None else 'missing'})
        samples.append({'at': stamp.astimezone(timezone.utc).isoformat(),
                        'cash': str(cash), 'positions': positions,
                        'missing_symbols': sorted(missing),
                        'equity': str(cash + value) if not missing else None,
                        'gross_market_exposure': str(value) if not missing else None,
                        'unrealized_net_pnl': str(unrealized) if not missing else None})
    valid = bool(samples) and all(s['equity'] is not None for s in samples)
    scope_complete = bool(coverage) and all(c['state'] == 'complete' for c in coverage)
    known_outcomes = outcomes_complete(portfolio['trades'])
    eligible = valid and scope_complete and known_outcomes
    peak = initial
    drawdown = Decimal('0')
    if eligible:
        for sample in samples:
            equity = Decimal(sample['equity'])
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak)
    return {'model_version': 'completed-minute-valuation-v2',
            'affects_sizing': False, 'samples': samples,
            'missing_mark_samples': sum(s['equity'] is None for s in samples),
            'metrics_eligible': eligible,
            'coverage_complete': scope_complete,
            'outcomes_complete': known_outcomes,
            'marks_complete': valid,
            'total_return': str((Decimal(samples[-1]['equity']) - initial) / initial) if eligible else None,
            'max_drawdown': str(drawdown) if eligible else None,
            'limitations': ['Minute samples cannot reveal intraminute drawdown.',
                            'Marks are observed closes, not liquidation quotes; exit fees are not estimated.',
                            'Admission uses separate fresh marked snapshots; this series is post-event reporting.',
                            'Incomplete coverage or outcomes suppress aggregate return and drawdown.']}
