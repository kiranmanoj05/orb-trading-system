"""Cost sensitivity on owned fill evidence; never broker net P&L or clearance."""
import json
from decimal import Decimal as D, localcontext

from orb.paper_evidence import EvidenceError, canonical, digest
from orb.profile_runs import validated_snapshot


def paper_performance(db, evidence):
    """All-or-unavailable totals; preserve unknown costs and operational holds.

    Use the immutable preset saved with the attempt. Fees are a per-share model;
    extra bps are a stress haircut on reported fills, NOT measured slippage.
    No account FEE record is assigned to a trade without a broker linkage.
    """
    rows = []
    for attempt in evidence['attempts']:
        saved = db.execute('SELECT preset FROM paper_attempts WHERE day=?', (attempt['session'],)).fetchone()
        try:
            preset = validated_snapshot(json.loads(saved['preset']))
        except (ValueError, KeyError, TypeError):
            raise EvidenceError('invalid_saved_paper_preset') from None
        fee = D(preset['settings']['fee_per_share']); bps = D(preset['settings']['slippage_bps'])
        fills = attempt['fill_activities']
        row = dict(session=attempt['session'], state='unavailable', reason='closed_fill_evidence_required',
                   preset_id=preset['id'], preset_version=preset['version'], preset_sha256=digest(preset),
                   order_observation_seq=attempt['observation_seq'],
                   activity_evidence_sha256=digest(fills),
                   gross_price_pnl_usd=None, modelled_fee_usd=None, additional_cost_stress_usd=None,
                   estimated_net_pnl_usd=None, broker_net_pnl_usd=None, broker_trade_fees_usd=None,
                   assumptions=dict(fee_per_share_per_side_usd=canonical(fee), additional_cost_bps=canonical(bps)))
        # Account correction holds invalidate all estimates, not just new entries.
        if evidence['entries_blocked']:
            row['reason'] = 'correction_hold'
        elif (attempt['state'] == 'known_no_entry' and fills['state'] == 'known_no_entry'):
            row.update(state='known_no_entry', reason=None, gross_price_pnl_usd='0',
                       modelled_fee_usd='0', additional_cost_stress_usd='0', estimated_net_pnl_usd='0')
        elif fills['state'] == 'closed_quantities_agree' and not fills['events_truncated']:
            events = fills['events']
            order_times = {o['id']: o['filled_at'] for o in attempt['orders']}
            # A finished scan with quantity agreement supports only a conditional estimate.
            # Require price/notional agreement too; never hide two differing reconstructions.
            with localcontext() as ctx:
                ctx.prec = 80
                price_disagreement = any(
                    sum((D(f['quantity'])*D(f['price']) for f in events if f['order_id'] == o['order_id']), D(0))
                    != D(o['snapshot_quantity'])*D(o['snapshot_average'] or '0')
                    for o in fills['orders'])
            if price_disagreement:
                row['reason'] = 'fill_price_disagreement'
            elif any(
                max((f['transaction_time'] for f in events if f['order_id'] == o['order_id']), default=None)
                != order_times.get(o['order_id'])
                for o in fills['orders'] if D(o['activity_quantity']) > 0
            ):
                row['reason'] = 'fill_timestamp_disagreement'
            elif not events or not any(f['side'] == 'buy' for f in events) or not any(f['side'] == 'sell' for f in events):
                row['reason'] = 'entry_exit_fills_required'
            elif max(f['transaction_time'] for f in events if f['side'] == 'buy') > min(f['transaction_time'] for f in events if f['side'] == 'sell'):
                row['reason'] = 'fill_chronology_disagreement'
            else:
                with localcontext() as ctx:
                    ctx.prec = 80
                    gross = D(fills['gross_price_pnl_usd'])
                    model_fee = sum((D(f['quantity'])*fee for f in events), D(0))
                    stress = sum((D(f['quantity'])*D(f['price'])*bps/10000 for f in events), D(0))
                    row.update(state='conditional_cost_estimate', reason=None, gross_price_pnl_usd=canonical(gross),
                               modelled_fee_usd=canonical(model_fee), additional_cost_stress_usd=canonical(stress),
                               estimated_net_pnl_usd=canonical(gross-model_fee-stress))
        elif fills['events_truncated']:
            row['reason'] = 'displayed_fill_bound_exceeded'
        rows.append(row)
    eligible = bool(rows) and not evidence['attempts_may_be_truncated'] and all(r['estimated_net_pnl_usd'] is not None for r in rows)
    with localcontext() as ctx:
        ctx.prec = 80
        totals = {key: canonical(sum((D(r[key]) for r in rows), D(0))) if eligible else None for key in (
            'gross_price_pnl_usd', 'modelled_fee_usd', 'additional_cost_stress_usd', 'estimated_net_pnl_usd')}
    result = dict(schema_version='orb-paper-performance-v1', attempts=rows, all_saved_attempts_estimated=eligible,
                  attempts_may_be_truncated=evidence['attempts_may_be_truncated'], totals=totals,
                  broker_net_pnl_usd=None, fee_coverage_verified=False, recovery_clearance=False,
                  account_fee_state=evidence['account_fees']['state'],
                  notice='Conditional estimate from saved owned fills and frozen preset costs. Additional bps stress can overlap costs already reflected in fills; it is not measured slippage. Account fees remain unattributed; broker net profit and account return are unavailable. This report cannot start, retry, release risk or clear holds.')
    result['report_id'] = digest(result)
    return result
