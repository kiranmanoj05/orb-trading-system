"""Evidence-bound correction review records. No authority to clear holds/orders."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re

from orb.paper_evidence import EvidenceError, digest, encoded, numeric, stamp, verified

TABLES = {'paper_hold_events', 'paper_review_log'}
MAX_HOLDS = 10000
MAX_REVIEWS = 2000
DISPOSITIONS = {'keep_blocked', 'request_provider_review', 'accept_terminal_accounting'}


class ReviewConflict(ValueError):
    pass


def install(db):
    db.execute('CREATE TABLE paper_hold_events (seq INTEGER PRIMARY KEY, day TEXT NOT NULL REFERENCES paper_attempts(day), '
               'observed_at TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(day,fingerprint))')
    db.execute('CREATE TABLE paper_review_log (seq INTEGER PRIMARY KEY, day TEXT NOT NULL REFERENCES paper_attempts(day), '
               'kind TEXT NOT NULL, case_id TEXT NOT NULL, observed_at TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL, '
               'UNIQUE(case_id,kind))')
    for table in TABLES:
        db.execute(f'CREATE INDEX {table}_day ON {table}(day,seq)')
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} '
                       "BEGIN SELECT RAISE(ABORT, 'paper review evidence is append-only'); END")
    for r in db.execute('SELECT * FROM paper_evidence_holds ORDER BY day').fetchall():
        record_hold(db,r['day'],r['reason'],r['observed_at'],'legacy_first_reason_only')


def record_hold(db, day, reason, at, source):
    """Called inside the originating evidence transaction; never clear a hold."""
    db.execute('INSERT OR IGNORE INTO paper_evidence_holds VALUES (?,?,?)',(day,reason,at))
    marks = {}
    for table in ('paper_observations','paper_fill_activities','paper_activity_scans'):
        marks[table] = db.execute(f'SELECT COALESCE(MAX(seq),0) FROM {table} WHERE day=?',(day,)).fetchone()[0]
    safe_reason = reason if isinstance(reason,str) and re.fullmatch('[a-z_]{1,80}',reason) else 'legacy_unknown_hold'
    value = dict(reason=safe_reason,source=source,evidence_marks=marks)
    fingerprint = digest(value)
    if db.execute('SELECT 1 FROM paper_hold_events WHERE day=? AND fingerprint=?',(day,fingerprint)).fetchone(): return
    if db.execute('SELECT COUNT(*) FROM paper_hold_events').fetchone()[0] >= MAX_HOLDS:
        db.execute("INSERT OR REPLACE INTO paper_meta VALUES ('hold-history-incomplete','yes')")
        return
    db.execute('INSERT INTO paper_hold_events(day,observed_at,fingerprint,payload) VALUES(?,?,?,?)',
               (day,stamp(at),fingerprint,encoded(value)))


def basis(db, day):
    attempt = db.execute('SELECT * FROM paper_attempts WHERE day=?',(day,)).fetchone()
    if not attempt: raise KeyError('No saved ORB attempt for this session.')
    # Verify all retained evidence, not just a mutable latest-row projection.
    marks = {}
    for table in ('paper_observations','paper_fill_activities','paper_activity_scans','paper_hold_events'):
        rows = list(db.execute(f'SELECT * FROM {table} WHERE day=? ORDER BY seq',(day,)))
        for r in rows: verified(r)
        marks[table] = [(r['seq'],r['fingerprint']) for r in rows]
    holds = [dict(r) for r in db.execute('SELECT * FROM paper_evidence_holds ORDER BY day')]
    meta = [tuple(r) for r in db.execute('SELECT * FROM paper_meta ORDER BY key')]
    from orb.paper_fees import PaperFees
    fees = PaperFees(db).commitment()
    from orb.paper_admission import PaperAdmission
    admission = PaperAdmission(db).commitment()
    return digest(dict(attempt=dict(attempt),marks=marks,holds=holds,meta=meta,fees=fees,admission=admission))


def capture_preflight(client, expected_scope):
    """Exactly two bounded account/position/open-order passes, GET only.

    Empty matching inventory is a local review aid, never reconciliation clearance.
    Private response fields and raw provider identifiers are not persisted.
    """
    passes = []
    try:
        for _ in range(2):
            a = client.request('GET','/v2/account')
            if not isinstance(a,dict) or not isinstance(a.get('id'),str): raise ValueError()
            if sha256(a['id'].encode()).hexdigest() != expected_scope:
                return dict(state='account_scope_mismatch',passes=passes)
            flags = [a.get('status')=='ACTIVE',a.get('trading_blocked') is False,a.get('account_blocked') is False]
            if a.get('currency') != 'USD': return dict(state='account_currency_unverified',passes=passes)
            cash,equity = numeric(a.get('cash')),numeric(a.get('equity'))
            positions = client.request('GET','/v2/positions')
            orders = client.request('GET','/v2/orders?status=open&limit=500&nested=false')
            if not isinstance(positions,list) or not isinstance(orders,list) or len(positions)>500 or len(orders)>500:
                raise ValueError()
            # Retain counts only. Nonempty inventories are never certified or
            # summarized into a lossy apparently-flat account.
            view = dict(cash_usd=cash,equity_usd=equity,active_unblocked=all(flags),
                        position_count=len(positions),open_order_count=len(orders))
            passes.append(view)
            if len(orders)==500: return dict(state='inventory_truncated',passes=passes)
            if positions or orders: return dict(state='inventory_present',passes=passes)
            if not all(flags): return dict(state='account_blocked',passes=passes)
        return dict(state='flat_two_passes' if passes[0]==passes[1] else 'account_changed',passes=passes)
    except Exception:
        return dict(state='preflight_read_failed',passes=passes)


class PaperReviews:
    def __init__(self,db): self.db=db

    def hold_history(self,day):
        rows=list(self.db.execute('SELECT * FROM paper_hold_events WHERE day=? ORDER BY seq',(day,)))
        values=[dict(verified(r),seq=r['seq'],first_observed_at=r['observed_at']) for r in rows]
        incomplete = self.db.execute("SELECT 1 FROM paper_meta WHERE key='hold-history-incomplete'").fetchone() is not None
        return dict(events=values[-100:],event_count=len(values),truncated=len(values)>100,
                    legacy_history=any(v['source']=='legacy_first_reason_only' for v in values),
                    incomplete=incomplete or not values)

    def prepare(self,day,expected_basis,preflight,started,at):
        at,started=stamp(at),stamp(started)
        if datetime.fromisoformat(at)<datetime.fromisoformat(started):
            preflight=dict(preflight,state='preflight_clock_reversed')
        elif datetime.fromisoformat(at)-datetime.fromisoformat(started)>timedelta(seconds=30):
            preflight=dict(preflight,state='preflight_too_slow')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if not self.db.execute('SELECT 1 FROM paper_evidence_holds WHERE day=?',(day,)).fetchone():
                raise ReviewConflict('No correction hold for this session.')
            current=basis(self.db,day)
            from orb.terminal_accounting import assess
            assessment = assess(self.db,day,preflight,started,at) if current==expected_basis else dict(eligible=False,reason='basis_changed_during_preflight')
            value=dict(schema_version='orb-paper-review-case-v2',session=day,basis=expected_basis,
                       stale_on_arrival=current!=expected_basis,preflight=preflight,
                       observed_from=started,observed_to=at,hold_history=self.hold_history(day),
                       authority='review_only_holds_remain',fees_usd=None,net_pnl_usd=None,terminal_accounting=assessment)
            case_id=digest(value)
            self._append(day,'case',case_id,value,at)
            self.db.execute('COMMIT')
            return self.case(case_id,at)
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise

    def _append(self,day,kind,case_id,value,at):
        existing=self.db.execute('SELECT * FROM paper_review_log WHERE case_id=? AND kind=?',(case_id,kind)).fetchone()
        if existing:
            verified(existing)
            if existing['fingerprint']!=digest(value): raise ReviewConflict('Conflicting review record.')
            return
        if self.db.execute('SELECT COUNT(*) FROM paper_review_log').fetchone()[0]>=MAX_REVIEWS:
            raise ReviewConflict('Review history capacity reached; preserve the database for archival.')
        self.db.execute('INSERT INTO paper_review_log(day,kind,case_id,observed_at,fingerprint,payload) VALUES(?,?,?,?,?,?)',
                        (day,kind,case_id,at,digest(value),encoded(value)))

    def case(self,case_id,now):
        row=self.db.execute("SELECT * FROM paper_review_log WHERE case_id=? AND kind='case'",(case_id,)).fetchone()
        if not row:raise KeyError('Review case not found.')
        value=verified(row)
        age=datetime.fromisoformat(stamp(now))-datetime.fromisoformat(row['observed_at'])
        stale=value['stale_on_arrival'] or value['basis']!=basis(self.db,row['day']) or not timedelta(0)<=age<=timedelta(minutes=2)
        decision=self.db.execute("SELECT * FROM paper_review_log WHERE case_id=? AND kind='decision'",(case_id,)).fetchone()
        return dict(value,case_id=case_id,fingerprint=row['fingerprint'],stale=stale,
                    decision=verified(decision) if decision else None)

    def record(self,case_id,expected_fingerprint,disposition,now):
        if not isinstance(disposition,str) or disposition not in DISPOSITIONS:raise ReviewConflict('Unsupported review disposition; holds cannot be cleared.')
        now=stamp(now)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            case=self.case(case_id,now)
            if case['fingerprint']!=expected_fingerprint:raise ReviewConflict('Review fingerprint changed; prepare a new case.')
            if case['decision']:
                if case['decision']['disposition']!=disposition:raise ReviewConflict('This case already has a different recorded decision.')
                self.db.execute('COMMIT');return case
            if case['stale']:raise ReviewConflict('Review evidence changed or expired; prepare a new case.')
            if disposition=='accept_terminal_accounting':
                if not case.get('terminal_accounting',{}).get('eligible'):
                    raise ReviewConflict('This case does not support terminal accounting; refresh evidence and prepare a new case.')
                accounting=dict(schema_version='orb-reviewed-terminal-accounting-v1',case_id=case_id,case_fingerprint=expected_fingerprint,
                    basis=case['basis'],accepted_at=now,assessment=case['terminal_accounting'],authority='accounting_only_holds_remain')
                self._append(case['session'],'accounting',case_id,accounting,now)
            value=dict(schema_version='orb-paper-review-decision-v2',case_id=case_id,case_fingerprint=expected_fingerprint,
                       disposition=disposition,recorded_at=now,authority='review_only_holds_remain')
            self._append(case['session'],'decision',case_id,value,now)
            self.db.execute('COMMIT');return self.case(case_id,now)
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise

    def report(self,day,now):
        rows=list(self.db.execute("SELECT case_id FROM paper_review_log WHERE day=? AND kind='case' ORDER BY seq DESC LIMIT 6",(day,)))
        accounting_row=self.db.execute("SELECT * FROM paper_review_log WHERE day=? AND kind='accounting' ORDER BY seq DESC LIMIT 1",(day,)).fetchone()
        accounting=None
        if accounting_row:
            value=verified(accounting_row)
            current=value['basis']==basis(self.db,day)
            accounting=dict(state='accepted_current_basis' if current else 'superseded_by_new_evidence',case_id=value['case_id'],
                accepted_at=value['accepted_at'],gross_price_pnl_usd=value['assessment']['gross_price_pnl_usd'] if current else None,
                fees_usd=None,net_pnl_usd=None,authority='accounting_only_holds_remain')
        return dict(reviewed_accounting=accounting,hold_history=self.hold_history(day),cases=[self.case(r['case_id'],now) for r in rows[:5]],
                    cases_truncated=len(rows)>5,authority='review_only_holds_remain')

    def export(self,after=0,through=None,*,holds=False):
        table = 'paper_hold_events' if holds else 'paper_review_log'
        if type(after)is not int or after<0:raise EvidenceError('invalid_evidence_cursor')
        if through is None:through=self.db.execute(f'SELECT COALESCE(MAX(seq),0) FROM {table}').fetchone()[0]
        if type(through)is not int or through<0:raise EvidenceError('invalid_evidence_cursor')
        rows=list(self.db.execute(f'SELECT * FROM {table} WHERE seq>? AND seq<=? ORDER BY seq LIMIT 101',(after,through)))
        events=[dict(seq=r['seq'],session=r['day'],kind='hold' if holds else r['kind'],case_id=None if holds else r['case_id'],fingerprint=r['fingerprint'],
                     observed_at=r['observed_at'],record=verified(r)) for r in rows[:100]]
        return dict(schema_version='orb-paper-holds-export-v1' if holds else 'orb-paper-reviews-export-v1',events=events,through=through,
                    next_after=events[-1]['seq'] if events else after,has_more=len(rows)>100)
