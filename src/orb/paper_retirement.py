"""Reviewed terminal intent retirement; never correction/restart clearance."""
from orb.paper_evidence import EvidenceError, verified


def assess(db, day, terminal):
    out = dict(schema_version='orb-terminal-retirement-assessment-v1',eligible=False,
               authority='local_terminal_retirement_holds_remain')
    def refuse(reason): return dict(out,reason=reason)
    if not terminal.get('eligible'): return refuse('terminal_accounting_required')
    from orb.paper_admission import PaperAdmission
    admission = PaperAdmission(db)
    admission.report()  # Validate existing releases and frozen intent bindings.
    row = db.execute('SELECT * FROM paper_attempts WHERE day=?',(day,)).fetchone()
    if row['state']=='done': return refuse('intent_already_terminal')
    from orb.paper_management import PaperManagement
    management = PaperManagement(db)
    management.report()
    from orb.management_resolution import assess as assess_management
    resolution=assess_management(db,row,terminal,[r for r in management.records() if r['session']==day])
    if resolution is not None and not resolution['eligible']:
        return dict(refuse(resolution['reason']),management_resolution=resolution)
    reservation = next((r for r in admission.records() if r['session']==day and r['kind']=='reserved'),None)
    if reservation is None: return refuse('legacy_untracked_reservation')
    return dict(out,eligible=True,reason='reviewed_terminal_intent_can_retire',reservation_seq=reservation['seq'],
                **(dict(schema_version='orb-terminal-retirement-assessment-v2',management_resolution=resolution) if resolution is not None else {}))


def verify_release(db, release):
    """Detect missing/cross-linked retirement records on every admission report.

    Old release records remain unchanged. Owner rewriting all data/hashes is
    outside this local integrity boundary; this is not reviewer authentication.
    """
    records = {}
    for kind in ('case','accounting','retirement','decision'):
        row=db.execute('SELECT * FROM paper_review_log WHERE case_id=? AND kind=?',
                       (release['review_case_id'],kind)).fetchone()
        if row is None or row['day']!=release['session']: raise EvidenceError('retirement_review_integrity_failed')
        records[kind]=verified(row)
        if kind=='case' and row['fingerprint']!=release['review_case_fingerprint']:
            raise EvidenceError('retirement_review_integrity_failed')
    case,accounting,retirement,decision=(records[k] for k in ('case','accounting','retirement','decision'))
    if (case.get('schema_version') not in ('orb-paper-review-case-v3','orb-paper-review-case-v4')
            or not case.get('terminal_retirement',{}).get('eligible')
            or case['terminal_retirement'].get('reservation_seq')!=release['reservation_seq']
            or accounting.get('case_fingerprint')!=release['review_case_fingerprint']
            or not accounting.get('assessment',{}).get('eligible')
            or accounting['assessment']!=case.get('terminal_accounting')
            or accounting['assessment'].get('observation_seq')!=release['observation_seq']
            or retirement.get('case_fingerprint')!=release['review_case_fingerprint']
            or retirement.get('basis_before')!=case.get('basis')
            or retirement.get('release_seq')!=release['seq']
            or retirement.get('reservation_seq')!=release['reservation_seq']
            or retirement.get('observation_seq')!=release['observation_seq']
            or decision.get('disposition')!='retire_terminal_intent'
            or retirement.get('retired_at')!=decision.get('recorded_at')
            or retirement.get('retired_at')!=release['observed_at']
            or decision.get('case_fingerprint')!=release['review_case_fingerprint']):
        raise EvidenceError('retirement_review_integrity_failed')
    from orb.management_resolution import verify as verify_management
    verify_management(db,case,retirement,release)
