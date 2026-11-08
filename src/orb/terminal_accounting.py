"""Narrow reviewed terminal gross accounting, with no execution authority."""
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
import json

from orb.paper_evidence import TERMINAL, canonical, correction, digest, verified

ALLOWED_HOLDS = {'average_changed_without_fill', 'fill_timestamp_changed_without_fill', 'fill_activity_changed'}
MAX_SELECTED = 200


def frozen_context(db, day):
    """Preserve the reviewed settings/risk context without raw client identifiers."""
    row = db.execute('SELECT * FROM paper_attempts WHERE day=?', (day,)).fetchone()
    meta, commitments = {}, []
    for r in db.execute('SELECT * FROM paper_meta ORDER BY key'):
        commitments.append(dict(key_sha256=digest(r['key']),value_sha256=digest(r['value'])))
        if r['key'].startswith(('equity:', 'loss-budget:', 'loss-blocked:')) or r['key']=='hold-history-incomplete':
            meta[r['key']] = r['value']
    return dict(schema_version='orb-terminal-context-v1',preset=json.loads(row['preset']),
                attempt_fingerprint=digest(dict(row)),risk_metadata=meta,metadata_commitments=commitments,
                attempt_state=row['state'])


def assess(db, day, preflight, started, now):
    out = dict(schema_version='orb-terminal-accounting-assessment-v1',eligible=False,reason=None,
               gross_price_pnl_usd=None,fees_usd=None,net_pnl_usd=None,
               authority='accounting_only_holds_remain')
    def refuse(reason): return dict(out,reason=reason)
    if preflight['state']!='flat_two_passes': return refuse('flat_preflight_required')
    history=list(db.execute('SELECT * FROM paper_hold_events WHERE day=? ORDER BY seq',(day,)))
    if not history or db.execute("SELECT 1 FROM paper_meta WHERE key='hold-history-incomplete'").fetchone():
        return refuse('hold_history_incomplete')
    for r in history:
        notice=verified(r)
        if notice['source']=='legacy_first_reason_only': return refuse('legacy_hold_history')
        if notice['reason'] not in ALLOWED_HOLDS: return refuse('unsupported_hold_reason')
    observations=list(db.execute('SELECT * FROM paper_observations WHERE day=? ORDER BY seq',(day,)))
    head=db.execute('SELECT * FROM paper_observation_heads WHERE day=?',(day,)).fetchone()
    if not observations or not head or observations[-1]['seq']!=head['seq']: return refuse('order_head_unavailable')
    prior={}; complete=False
    for r in observations:
        p=verified(r)
        if prior:
            reason=correction(dict(orders=list(prior.values()),legs_complete=complete),p)
            if reason and reason not in ALLOWED_HOLDS: return refuse('unsupported_order_revision')
        complete |= p['legs_complete']
        for order in p['orders']: prior[order['id']]=order
    latest=verified(observations[-1]);orders=latest['orders']
    if latest['missing_entry'] or not latest['legs_complete'] or not orders: return refuse('terminal_order_evidence_incomplete')
    if latest['closing_expected'] and not any(o['role']=='closing_exit' for o in orders): return refuse('closing_order_missing')
    if any(o['status'] not in TERMINAL or o['replaced'] for o in orders): return refuse('orders_not_terminal')
    if any(o['status']=='filled' and o['quantity']!=o['filled_quantity'] for o in orders): return refuse('terminal_quantity_mismatch')
    filled=Decimal(orders[0]['filled_quantity'])
    exited=sum((Decimal(o['filled_quantity']) for o in orders[1:]),Decimal(0))
    if filled<=0 or filled!=exited: return refuse('terminal_position_not_flat')
    if any(not o['average_fill_price'] or not o['filled_at'] for o in orders if Decimal(o['filled_quantity'])>0):
        return refuse('fill_price_or_time_missing')
    scan_row=db.execute('SELECT * FROM paper_activity_scans WHERE day=? ORDER BY seq DESC LIMIT 1',(day,)).fetchone()
    if not scan_row: return refuse('activity_scan_missing')
    scan=verified(scan_row)
    if not scan['scan_finished'] or scan['failure']: return refuse('activity_scan_incomplete')
    members=scan.get('matched_versions')
    if not isinstance(members,list): return refuse('activity_membership_unavailable')
    if not 1<=len(members)<=MAX_SELECTED: return refuse('activity_review_bound')
    selected={v['activity_id']:v['fingerprint'] for v in members}
    if len(selected)!=len(members): return refuse('activity_membership_ambiguous')
    events={}; known_versions={}
    for r in db.execute('SELECT * FROM paper_fill_activities WHERE day=? ORDER BY seq',(day,)):
        event=verified(r);key=event['activity_id']
        if key in events:
            if any(event[k]!=events[key][k] for k in event.keys()-{'price','transaction_time'}):
                return refuse('unsupported_activity_revision')
        events[key]=event;known_versions[key]=r['fingerprint']
    if selected!=known_versions: return refuse('activity_membership_changed')
    events=list(events.values());order_map={o['id']:o for o in orders}
    if any(e['order_id'] not in order_map or e['side']!=order_map[e['order_id']]['side'] for e in events):
        return refuse('activity_order_identity_mismatch')
    # Freshness is a review condition, not proof of a complete provider history.
    begin,end=datetime.fromisoformat(started),datetime.fromisoformat(now)
    for at in (head['last_seen_at'],scan_row['observed_at']):
        age=begin-datetime.fromisoformat(at)
        if not timedelta(0)<=age<=timedelta(minutes=2): return refuse('source_observation_stale')
    if not head['last_seen_at']<=scan['until']<=scan_row['observed_at']<=started<=now:
        return refuse('source_chronology_unverified')
    if any(e['transaction_time']>scan['until'] for e in events): return refuse('future_activity_evidence')
    buys=[e['transaction_time'] for e in events if e['side']=='buy']
    sells=[e['transaction_time'] for e in events if e['side']=='sell']
    if not buys or not sells or max(buys)>min(sells): return refuse('entry_exit_chronology_unverified')
    with localcontext() as ctx:
        ctx.prec=80
        comparisons=[];gross=Decimal(0)
        for o in orders:
            own=[e for e in events if e['order_id']==o['id']]
            qty=sum((Decimal(e['quantity']) for e in own),Decimal(0))
            value=sum((Decimal(e['quantity'])*Decimal(e['price']) for e in own),Decimal(0))
            if qty!=Decimal(o['filled_quantity']): return refuse('activity_quantity_mismatch')
            if own and max(e['transaction_time'] for e in own)!=o['filled_at']:
                return refuse('activity_order_time_mismatch')
            expected=Decimal(o['filled_quantity'])*Decimal(o['average_fill_price'] or '0')
            if value!=expected: return refuse('activity_order_notional_mismatch')
            comparisons.append(dict(order_id=o['id'],quantity=canonical(qty),notional_usd=canonical(value)))
            gross+=value*(-1 if o['side']=='buy' else 1)
    return dict(out,eligible=True,reason='terminal_price_time_evidence_agrees',gross_price_pnl_usd=canonical(gross),
                observation_seq=head['seq'],scan_seq=scan_row['seq'],scan_fingerprint=scan_row['fingerprint'],
                selected_versions=members,orders=orders,activities=events,comparisons=comparisons,
                context=frozen_context(db,day),
                notice='Manual acceptance of this exact terminal gross reconstruction. No fee/net/account-return claim, provider certification or permission to resume trading.')
