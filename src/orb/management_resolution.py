"""Exact terminal evidence for saved command history; no resend/hold clearance."""
from decimal import Decimal
import json

from orb.paper_evidence import TERMINAL, digest, stamp, verified, EvidenceError

MAX_RECORDS = 100
OUTCOMES = {'request_returned','outcome_unknown','not_attempted'}


def assess(db, row, terminal, records):
    """Account for every saved command using later accepted terminal orders.

    A transport timeout remains a timeout. Current terminal identity/quantity
    evidence can support local retirement without proving who canceled/filled
    an order, clearing an account hold or authorising a command retry.
    """
    out=dict(schema_version='orb-management-terminal-assessment-v1',eligible=False,
             authority='local_terminal_history_only',retry_authority=False,correction_clearance=False)
    def refuse(reason):return dict(out,reason=reason)
    if len(records)>MAX_RECORDS:return refuse('management_review_bound')
    orders={o['id']:o for o in terminal['orders']}
    head=db.execute('SELECT * FROM paper_observation_heads WHERE day=?',(row['day'],)).fetchone()
    terminal_at=head['last_seen_at']
    intents={};receipts={};checks=[];refs=[]
    prior=None
    for record in records:
        at=stamp(record['observed_at'])
        if at>terminal_at:return refuse('management_observed_after_terminal')
        if prior is not None and at<prior:return refuse('management_clock_reversed')
        prior=at
        raw=db.execute('SELECT * FROM paper_management_log WHERE seq=?',(record['seq'],)).fetchone()
        verified(raw)
        refs.append(dict(seq=record['seq'],fingerprint=raw['fingerprint'],kind=record['kind'],observed_at=at))
        if record['kind'] in ('cancel_intent','close_intent'):intents[record['seq']]=record
        elif record['kind']=='dispatch':
            if record['intent_seq'] not in intents or record['intent_seq'] in receipts:
                return refuse('management_receipt_identity_mismatch')
            if record.get('outcome') not in ('request_returned','outcome_unknown'):
                return refuse('management_receipt_outcome_unsupported')
            receipts[record['intent_seq']]=record
        else:checks.append(record['seq'])  # A close_check did not claim/send a command.
    closes=[r for r in intents.values() if r['kind']=='close_intent']
    closing=json.loads(row['closing']) if row['closing'] else None
    if len(closes)>1 or bool(closes)!=bool(closing):return refuse('native_closing_intent_missing_or_ambiguous')
    if not records and closing is None:return None  # Preserve old no-management assessment contract.
    comparisons=[];canceled=set();unknown=0
    for seq,intent in intents.items():
        receipt=receipts.get(seq)
        transport=receipt['outcome'] if receipt else 'no_dispatch_receipt'
        if intent['kind']=='cancel_intent':
            targets=intent.get('targets')
            if not isinstance(targets,list) or not targets:return refuse('cancel_targets_unavailable')
            target_ids=[]
            for target in targets:
                order=orders.get(target.get('id'))
                if (order is None or target['id'] in canceled or target.get('role')!=order['role']
                        or order['role']=='closing_exit' or order['status'] not in TERMINAL or order['replaced']):
                    return refuse('cancel_target_terminal_identity_mismatch')
                canceled.add(target['id']);target_ids.append(target['id'])
            outcomes=receipt.get('results') if receipt else None
            if receipt and (not isinstance(outcomes,list) or len(outcomes)!=len(targets)
                    or any(r!={'id':id,'outcome':r.get('outcome')} or r.get('outcome') not in OUTCOMES
                           for r,id in zip(outcomes,target_ids))):
                return refuse('cancel_receipt_target_mismatch')
            unknown+=int(receipt is None or transport=='outcome_unknown'
                         or any(r['outcome']!='request_returned' for r in outcomes or []))
            comparisons.append(dict(intent_seq=seq,kind='cancel_intent',terminal_order_ids=target_ids,
                dispatch_seq=receipt['seq'] if receipt else None,transport_outcome=transport,
                target_outcomes=outcomes,conclusion='targets_terminal_cause_unattributed'))
        else:
            expected=dict(symbol='SPY',side='sell',qty=intent['quantity'],type='market',time_in_force='day',
                          client_order_id=row['client_id']+'-x')
            if closing!=expected or digest(closing)!=intent['closing_sha256']:
                return refuse('closing_request_terms_mismatch')
            selected=[o for o in orders.values() if o['role']=='closing_exit']
            if len(selected)!=1:return refuse('closing_terminal_order_missing_or_ambiguous')
            order=selected[0]
            if (order['quantity']!=intent['quantity'] or Decimal(order['filled_quantity'])>Decimal(intent['quantity'])
                    or order['status'] not in TERMINAL or order['replaced']):
                return refuse('closing_terminal_quantity_mismatch')
            response=receipt.get('response') if receipt else None
            if response is not None and (response.get('replaced') or any(response.get(k)!=order[k] for k in ('id','symbol','side','quantity','role'))):
                return refuse('closing_receipt_identity_mismatch')
            if response is not None and Decimal(response['filled_quantity'])>Decimal(order['filled_quantity']):
                return refuse('closing_receipt_fill_regression')
            unknown+=int(receipt is None or transport=='outcome_unknown' or response is None)
            comparisons.append(dict(intent_seq=seq,kind='close_intent',terminal_order_ids=[order['id']],
                dispatch_seq=receipt['seq'] if receipt else None,transport_outcome=transport,
                response_identity_observed=response is not None,conclusion='owned_closing_order_terminal'))
    return dict(out,eligible=True,reason='saved_commands_accounted_by_terminal_evidence',
                observation_seq=terminal['observation_seq'],terminal_observed_at=terminal_at,
                records=refs,commands=comparisons,passive_check_seqs=checks,
                command_count=len(comparisons),unknown_transport_count=unknown,
                notice='Historical command accounting only. Unknown transport remains unknown; terminal status does not establish cancellation cause, retry permission, fees, net profit or account recovery clearance.')


def verify(db, case, retirement, release):
    expected=case.get('terminal_retirement',{}).get('management_resolution')
    row=db.execute("SELECT * FROM paper_review_log WHERE case_id=? AND kind='management_resolution'",
                   (release['review_case_id'],)).fetchone()
    if expected is None:
        if row is not None or retirement.get('management_resolution_sha256') is not None:
            raise EvidenceError('management_resolution_integrity_failed')
        return None
    if row is None or row['day']!=release['session']:raise EvidenceError('management_resolution_integrity_failed')
    value=verified(row)
    if (case.get('schema_version')!='orb-paper-review-case-v4' or not expected.get('eligible')
            or value.get('assessment')!=expected or value.get('case_fingerprint')!=release['review_case_fingerprint']
            or value.get('accepted_at')!=retirement['retired_at']
            or retirement.get('management_resolution_sha256')!=row['fingerprint']
            or expected.get('observation_seq')!=release['observation_seq']):
        raise EvidenceError('management_resolution_integrity_failed')
    for ref in expected['records']:
        record=db.execute('SELECT * FROM paper_management_log WHERE seq=?',(ref['seq'],)).fetchone()
        if (record is None or record['day']!=release['session'] or record['fingerprint']!=ref['fingerprint']
                or record['observed_at']!=ref['observed_at']):
            raise EvidenceError('management_resolution_source_changed')
        verified(record)
    return value


def projection(db, day):
    row=db.execute("SELECT * FROM paper_review_log WHERE day=? AND kind='management_resolution' ORDER BY seq DESC LIMIT 1",(day,)).fetchone()
    from orb.paper_admission import PaperAdmission
    releases=[r for r in PaperAdmission(db).records() if r['session']==day and r.get('review_case_id')]
    from orb.paper_retirement import verify_release
    for release in releases:verify_release(db,release)
    if row is None:return None
    value=verified(row)
    release=next((r for r in releases if r['review_case_id']==value['case_id']),None)
    if release is None:raise EvidenceError('management_resolution_integrity_failed')
    a=value['assessment']
    return dict(case_id=value['case_id'],accepted_at=value['accepted_at'],command_count=a['command_count'],
                unknown_transport_count=a['unknown_transport_count'],historical_only=True,
                authority='local_terminal_history_only',retry_authority=False,correction_clearance=False,
                notice='Reviewed terminal history; original transport outcomes remain unchanged. Holds remain. This is not current account reconciliation or permission to resend/resume.')
