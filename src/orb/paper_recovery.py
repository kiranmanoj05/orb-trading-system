"""Explicit, bounded terminal-correction clearance; never starts/retries orders.

Original holds and receipts remain append-only. New source evidence or another
hold occurrence invalidates the applicable clearance. Account cost attribution
and settlement completeness are deliberately separate from this permission.
"""
from datetime import datetime, timedelta

from orb.paper_evidence import EvidenceError, digest, stamp, verified
from orb.paper_reviews import PaperReviews, ReviewConflict

DAY_TABLES = ('paper_observations', 'paper_fill_activities', 'paper_activity_scans',
              'paper_hold_events', 'paper_admission_log', 'paper_management_log')
MAX_SESSIONS = 20


def day_basis(db, day):
    marks = {}
    for table in DAY_TABLES:
        rows = list(db.execute(f'SELECT * FROM {table} WHERE day=? ORDER BY seq', (day,)))
        for row in rows: verified(row)
        marks[table] = [(r['seq'], r['fingerprint'], r['observed_at']) for r in rows]
    row = db.execute('SELECT * FROM paper_attempts WHERE day=?', (day,)).fetchone()
    hold = db.execute('SELECT * FROM paper_evidence_holds WHERE day=?', (day,)).fetchone()
    head = db.execute('SELECT seq FROM paper_observation_heads WHERE day=?', (day,)).fetchone()
    # An identical observation's last_seen_at may advance without a correction.
    # Its payload/sequence and every new observation/scan/hold remain bound.
    return digest(dict(attempt=dict(row) if row else None, hold=dict(hold) if hold else None,
                       head=head['seq'] if head else None, marks=marks))


def account_basis(db):
    marks = {}
    for table in (*DAY_TABLES, 'paper_fee_log', 'paper_restart_log', 'paper_cash_log', 'paper_review_log'):
        rows = list(db.execute(f'SELECT * FROM {table} ORDER BY seq'))
        for row in rows: verified(row)
        marks[table] = [(r['seq'], r['fingerprint'], r['observed_at']) for r in rows
                        if table != 'paper_review_log' or r['kind'] != 'recovery_case']
    return digest(dict(marks=marks,
        attempts=[dict(r) for r in db.execute('SELECT * FROM paper_attempts ORDER BY day')],
        heads=[tuple(r) for r in db.execute('SELECT * FROM paper_observation_heads ORDER BY day')],
        holds=[dict(r) for r in db.execute('SELECT * FROM paper_evidence_holds ORDER BY day')],
        meta=[tuple(r) for r in db.execute('SELECT * FROM paper_meta ORDER BY key')]))


def clearances(db):
    """Validate historical authority links even when newer evidence supersedes it."""
    values = []
    for row in db.execute("SELECT * FROM paper_review_log WHERE kind='recovery_clearance' ORDER BY seq"):
        value = verified(row)
        case = db.execute("SELECT * FROM paper_review_log WHERE case_id=? AND kind='recovery_case'", (row['case_id'],)).fetchone()
        decision = db.execute("SELECT * FROM paper_review_log WHERE case_id=? AND kind='recovery_decision'", (row['case_id'],)).fetchone()
        if not case or not decision: raise EvidenceError('recovery_link_integrity_failed')
        c, d = verified(case), verified(decision)
        if (case['day'] != row['day'] or decision['day'] != row['day']
                or digest(c) != row['case_id'] or value.get('case_id') != row['case_id']
                or value.get('case_fingerprint') != case['fingerprint']
                or c.get('schema_version') != 'orb-account-recovery-case-v1'
                or value.get('schema_version') != 'orb-terminal-correction-clearance-v1'
                or value.get('authority') != 'reviewed_terminal_holds_only'
                or value.get('restart_required') is not True or value.get('retry_authority') is not False
                or value.get('fee_coverage_verified') is not False or value.get('broker_net_pnl_usd') is not None
                or not c.get('assessment', {}).get('eligible')
                or value.get('sessions') != c['assessment']['sessions']
                or value.get('accepted_at') != row['observed_at']
                or d != dict(case_id=row['case_id'], case_fingerprint=case['fingerprint'],
                             disposition='clear_terminal_corrections', recorded_at=row['observed_at'])
                or decision['observed_at'] != row['observed_at']):
            raise EvidenceError('recovery_link_integrity_failed')
        from orb.paper_retirement import verify_release
        for session in value['sessions']:
            release = db.execute('SELECT * FROM paper_admission_log WHERE seq=?', (session['release_seq'],)).fetchone()
            if not release or release['day'] != session['session']:
                raise EvidenceError('recovery_release_integrity_failed')
            payload = verified(release)
            if payload.get('review_case_id') != session['retirement_case_id']:
                raise EvidenceError('recovery_release_integrity_failed')
            verify_release(db, dict(payload, seq=release['seq'], observed_at=release['observed_at'], session=release['day']))
            accounting = db.execute("SELECT * FROM paper_review_log WHERE case_id=? AND kind='accounting'",
                                    (session['accounting_case_id'],)).fetchone()
            if not accounting or accounting['fingerprint'] != session['accounting_fingerprint']:
                raise EvidenceError('recovery_accounting_integrity_failed')
            verified(accounting)
        values.append(dict(value, seq=row['seq']))
    return values


def active_holds(db, raw):
    accepted = {}
    for value in clearances(db):
        for session in value['sessions']: accepted[session['session']] = session
    if db.execute("SELECT 1 FROM paper_meta WHERE key='hold-history-incomplete'").fetchone():
        return raw
    return [r for r in raw if r['day'] not in accepted
            or accepted[r['day']]['source_sha256'] != day_basis(db, r['day'])]


def selected_activities(db, day):
    """Only an active, explicit clearance can select corrected source versions."""
    history = clearances(db)
    if db.execute("SELECT 1 FROM paper_meta WHERE key='hold-history-incomplete'").fetchone(): return None
    for value in reversed(history):
        session = next((s for s in value['sessions'] if s['session']==day), None)
        if session is None: continue
        if session['source_sha256'] != day_basis(db, day): return None
        row = db.execute("SELECT * FROM paper_review_log WHERE kind='accounting' AND case_id=?",
                         (session['accounting_case_id'],)).fetchone()
        return dict(members=verified(row)['assessment']['selected_versions'], case_id=value['case_id'])
    return None


def assess(db, preflight, started, at):
    from orb.paper_evidence import PaperEvidence
    from orb.paper_admission import PaperAdmission
    from orb.terminal_accounting import assess as terminal_assess
    out = dict(eligible=False, reason=None, sessions=[], restart_required=True,
               broker_net_pnl_usd=None, fee_coverage_verified=False)
    def refuse(reason): return dict(out, reason=reason)
    if preflight['state'] != 'flat_two_passes': return refuse('flat_account_preflight_required')
    holds = PaperEvidence(db).holds()
    days = sorted({r['day'] for r in holds})
    if not days: return refuse('no_active_corrections')
    if len(days) > MAX_SESSIONS: return refuse('recovery_session_bound')
    if any(not db.execute('SELECT 1 FROM paper_attempts WHERE day=?', (d,)).fetchone() for d in days):
        return refuse('account_fee_cash_or_restart_hold')
    if db.execute("SELECT 1 FROM paper_attempts WHERE state!='done'").fetchone():
        return refuse('all_intents_must_be_terminal')
    admission = PaperAdmission(db)
    admission_view = admission.report()
    if admission_view['active_count']: return refuse('active_reservation')
    if admission_view['legacy_completed_count']: return refuse('legacy_untracked_account_history')
    from decimal import Decimal
    from orb.paper_evidence import TERMINAL
    attempts = list(db.execute('SELECT * FROM paper_attempts ORDER BY day LIMIT 101'))
    if len(attempts) > 100: return refuse('account_attempt_bound')
    for attempt in attempts:
        head = db.execute('SELECT o.* FROM paper_observations o JOIN paper_observation_heads h ON h.seq=o.seq WHERE h.day=?', (attempt['day'],)).fetchone()
        current = verified(head) if head else None
        orders = current['orders'] if current else []
        if (not current or current['missing_entry'] or not current['legs_complete'] or not orders
                or (current['closing_expected'] and not any(o['role']=='closing_exit' for o in orders))
                or any(o['status'] not in TERMINAL or o['replaced'] for o in orders)
                or any(o['status']=='filled' and o['quantity']!=o['filled_quantity'] for o in orders)
                or Decimal(orders[0]['filled_quantity']) != sum((Decimal(o['filled_quantity']) for o in orders[1:]), Decimal(0))):
            return refuse('all_saved_terminal_orders_required')
    records = admission.records(); sessions = []
    for day in days:
        release = next((r for r in records if r['session'] == day and r['kind'] == 'terminal_release'), None)
        if not release or not release.get('review_case_id'): return refuse('reviewed_retirement_required')
        terminal = terminal_assess(db, day, preflight, started, at)
        if not terminal['eligible']: return refuse(terminal['reason'])
        row = db.execute("SELECT * FROM paper_review_log WHERE day=? AND kind='accounting' ORDER BY seq DESC LIMIT 1", (day,)).fetchone()
        accepted = verified(row) if row else None
        if not accepted or any(accepted['assessment'][key] != terminal[key] for key in ('orders', 'activities')):
            return refuse('current_terminal_accounting_required')
        sessions.append(dict(session=day, source_sha256=day_basis(db, day), release_seq=release['seq'],
            retirement_case_id=release['review_case_id'], accounting_case_id=row['case_id'],
            accounting_fingerprint=row['fingerprint']))
    return dict(out, eligible=True, reason='reviewed_flat_terminal_account', sessions=sessions)


class PaperRecovery:
    def __init__(self, db): self.db = db

    def prepare(self, expected, preflight, started, at):
        started, at = stamp(started), stamp(at)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            current = account_basis(self.db)
            age = datetime.fromisoformat(at) - datetime.fromisoformat(started)
            if not timedelta(0) <= age <= timedelta(seconds=30):
                preflight = dict(preflight, state='preflight_expired')
            assessment = (assess(self.db, preflight, started, at) if current == expected
                          else dict(eligible=False, reason='basis_changed_during_preflight', sessions=[]))
            raw = self.db.execute('SELECT day FROM paper_evidence_holds ORDER BY day LIMIT 1').fetchone()
            if raw is None: raise ReviewConflict('No saved terminal correction to review.')
            case = dict(schema_version='orb-account-recovery-case-v1', source_basis=expected,
                        preflight=preflight, observed_from=started, observed_to=at, assessment=assessment)
            PaperReviews(self.db)._append(raw['day'], 'recovery_case', digest(case), case, at)
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise

    def accept(self, case_id, fingerprint, at):
        at = stamp(at)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute("SELECT * FROM paper_review_log WHERE case_id=? AND kind='recovery_case'", (case_id,)).fetchone()
            if row is None: raise KeyError('Recovery case not found.')
            case = verified(row)
            if row['fingerprint'] != fingerprint: raise ReviewConflict('Recovery fingerprint changed.')
            existing = self.db.execute("SELECT 1 FROM paper_review_log WHERE case_id=? AND kind='recovery_clearance'", (case_id,)).fetchone()
            if existing:
                clearances(self.db)
                self.db.execute('COMMIT'); return
            age = datetime.fromisoformat(at) - datetime.fromisoformat(case['observed_to'])
            if (not timedelta(0) <= age <= timedelta(minutes=2) or account_basis(self.db) != case['source_basis']):
                raise ReviewConflict('Recovery evidence changed or expired; prepare again.')
            if not case['assessment']['eligible'] or assess(self.db, case['preflight'], case['observed_from'], case['observed_to']) != case['assessment']:
                raise ReviewConflict('This account does not support terminal correction clearance.')
            value = dict(schema_version='orb-terminal-correction-clearance-v1', case_id=case_id,
                         case_fingerprint=fingerprint, accepted_at=at, sessions=case['assessment']['sessions'],
                         authority='reviewed_terminal_holds_only', restart_required=True,
                         retry_authority=False, fee_coverage_verified=False, broker_net_pnl_usd=None)
            journal = PaperReviews(self.db)
            journal._append(row['day'], 'recovery_clearance', case_id, value, at)
            journal._append(row['day'], 'recovery_decision', case_id, dict(case_id=case_id,
                case_fingerprint=fingerprint, disposition='clear_terminal_corrections', recorded_at=at), at)
            clearances(self.db)
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise

    def report(self, now):
        history = clearances(self.db)
        row = self.db.execute("SELECT * FROM paper_review_log WHERE kind='recovery_case' ORDER BY seq DESC LIMIT 1").fetchone()
        case = None
        if row:
            value = verified(row)
            age = datetime.fromisoformat(stamp(now)) - datetime.fromisoformat(value['observed_to'])
            case = dict(value, case_id=row['case_id'], fingerprint=row['fingerprint'],
                        stale=value['source_basis'] != account_basis(self.db) or not timedelta(0) <= age <= timedelta(minutes=2),
                        accepted=any(v['case_id'] == row['case_id'] for v in history))
        return dict(case=case, clearances=history[-5:], history_truncated=len(history)>5,
                    notice='Only supported retired terminal corrections. Original holds/commands remain saved. Explicit Start and fresh restart checks required; fees/net profit remain unverified.')
