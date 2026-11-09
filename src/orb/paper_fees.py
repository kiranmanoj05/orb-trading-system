"""Account FEE observations, never per-trade attribution or a net-profit ledger."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re
from urllib.parse import urlencode

from orb.paper_activities import identity
from orb.paper_evidence import EvidenceError, canonical, digest, encoded, stamp, verified
from orb.strategy import NEW_YORK

TABLES = {'paper_fee_log'}
PAGE_SIZE, MAX_PAGES, MAX_EVENTS = 100, 10, 12000


def install(db):
    db.execute('CREATE TABLE paper_fee_log (seq INTEGER PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL, '
               'activity_id TEXT, observed_at TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE INDEX paper_fee_log_identity ON paper_fee_log(activity_id,seq)')
    db.execute('CREATE INDEX paper_fee_log_day ON paper_fee_log(day,kind,seq)')
    for op in ('UPDATE', 'DELETE'):
        db.execute(f'CREATE TRIGGER paper_fee_log_no_{op.lower()} BEFORE {op} ON paper_fee_log '
                   "BEGIN SELECT RAISE(ABORT, 'paper fees are append-only'); END")


def normalize(row, until):
    if not isinstance(row, dict) or row.get('activity_type') != 'FEE':
        raise EvidenceError('invalid_fee_record')
    key = identity(row.get('id'))
    amount = row.get('net_amount')
    if amount is not None:
        if not isinstance(amount, str) or not 1 <= len(amount) <= 40:
            raise EvidenceError('invalid_fee_amount')
        try:
            number = Decimal(amount)
            if not number.is_finite() or number.copy_abs() > Decimal('1e12') or abs(number.as_tuple().exponent) > 12:
                raise ValueError()
        except (InvalidOperation, ValueError): raise EvidenceError('invalid_fee_amount') from None
        amount = canonical(number)
    currency = row.get('currency')
    if currency is not None and (not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency)):
        raise EvidenceError('invalid_fee_currency')
    day = row.get('date')
    if day is not None:
        try:
            parsed = date.fromisoformat(day)
            if str(parsed) != day: raise ValueError()
        except (ValueError, TypeError): raise EvidenceError('invalid_fee_date') from None
    created = stamp(row['created_at']) if row.get('created_at') is not None else None
    if created is not None and created > until: raise EvidenceError('future_fee_timestamp')
    status = row.get('status') if row.get('status') in ('executed', 'pending', 'canceled') else 'unknown'
    return dict(activity_id=key, activity_type='FEE', signed_cash_amount=amount, currency=currency,
                activity_date=day, created_at=created, status=status, attribution='account_only_unattributed')


def window(day, now):
    """Posting-day window, including a one-day lead; not a session attribution."""
    parsed = date.fromisoformat(day)
    today = now.astimezone(NEW_YORK).date()
    if str(parsed) != day or not 0 <= (today - parsed).days <= 90:
        raise ValueError('Choose a posting date within the past 90 days.')
    after = datetime.combine(parsed, time(), NEW_YORK).astimezone(timezone.utc) - timedelta(days=1)
    return after.isoformat(), stamp(now.isoformat())


def fetch(client, after, until):
    params = dict(after=after, until=until, direction='asc', page_size=PAGE_SIZE)
    events, seen, pages, received, finished, failure = [], {}, 0, 0, False, None
    for _ in range(MAX_PAGES):
        try:
            page = client.request('GET', '/v2/account/activities/FEE?' + urlencode(params))
            pages += 1
            if not isinstance(page, list) or len(page) > PAGE_SIZE: raise EvidenceError('invalid_fee_page')
            for row in page:
                event = normalize(row, until)
                key = event['activity_id']
                if key in seen:
                    if digest(event) != digest(seen[key]):
                        events.append(event)
                        raise EvidenceError('fee_revision_in_scan')
                    raise EvidenceError('fee_pagination_repeated')
                seen[key] = event
                events.append(event)
                received += 1
            if len(page) < PAGE_SIZE:
                finished = True
                break
            params['page_token'] = page[-1]['id']
        except EvidenceError as exc:
            failure = str(exc)
            break
        except Exception:
            failure = 'fee_read_failed'
            break
    if not finished and failure is None: failure = 'fee_page_limit'
    return events, dict(after=after, until=until, scan_finished=finished, pages=pages,
                        received=received, failure=failure)


class PaperFees:
    def __init__(self, db): self.db = db

    def hold(self):
        rows = dict(self.db.execute("SELECT key,value FROM paper_meta WHERE key IN ('fee-hold-reason','fee-hold-at')"))
        if not rows: return None
        if set(rows) != {'fee-hold-reason', 'fee-hold-at'}: raise EvidenceError('fee_hold_integrity_failed')
        reason = rows['fee-hold-reason']
        if reason not in ('fee_record_changed','fee_record_missing','fee_capacity_reached','fee_clock_reversed','invalid_fee_evidence'):
            raise EvidenceError('fee_hold_integrity_failed')
        return dict(day='account_fee_evidence', reason=reason, observed_at=stamp(rows['fee-hold-at']))

    def commitment(self):
        rows = list(self.db.execute('SELECT * FROM paper_fee_log ORDER BY seq'))
        for row in rows: verified(row)
        return [(r['seq'], r['fingerprint']) for r in rows]

    def save(self, day, events, scan, at, scope):
        """Scope binding, versions, scan membership and sticky hold commit together."""
        at = stamp(at)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            bound = self.db.execute("SELECT value FROM paper_meta WHERE key='account'").fetchone()
            if (bound and bound[0] != scope) or (not bound and self.db.execute('SELECT 1 FROM paper_attempts').fetchone()):
                raise EvidenceError('fee_account_scope_changed')
            if not bound: self.db.execute("INSERT INTO paper_meta VALUES ('account',?)", (scope,))
            self.commitment()
            old = self.db.execute("SELECT * FROM paper_fee_log WHERE kind='scan' AND day=? ORDER BY seq DESC LIMIT 1", (day,)).fetchone()
            if old and verified(old)['until'] > scan['until']: raise EvidenceError('stale_fee_scan')
            def hold(reason):
                if self.hold() is None:
                    self.db.executemany('INSERT INTO paper_meta VALUES (?,?)', [('fee-hold-reason', reason), ('fee-hold-at', at)])
            if old and at < old['observed_at']: hold('fee_clock_reversed')
            if scan['failure'] not in (None,'fee_read_failed','fee_page_limit'):
                hold('invalid_fee_evidence')
            # The last finished scan survives an intervening failed read.
            if scan['scan_finished']:
                for r in self.db.execute("SELECT * FROM paper_fee_log WHERE kind='scan' AND day=? ORDER BY seq DESC", (day,)):
                    prior = verified(r)
                    if prior['scan_finished'] and prior['after'] == scan['after'] and prior['until'] <= scan['until']:
                        ids = {e['activity_id'] for e in events}
                        if any(m['activity_id'] not in ids for m in prior['members']): hold('fee_record_missing')
                        break
            count = self.db.execute('SELECT COUNT(*) FROM paper_fee_log').fetchone()[0]
            members = []
            for event in events:
                fp = digest(event)
                previous = list(self.db.execute("SELECT * FROM paper_fee_log WHERE kind='fee' AND activity_id=? ORDER BY seq", (event['activity_id'],)))
                for r in previous:
                    prior = verified(r)
                    for field in ('signed_cash_amount', 'currency', 'activity_date', 'created_at'):
                        if prior[field] is not None and event[field] != prior[field]: hold('fee_record_changed')
                    if prior['status'] in ('executed', 'canceled') and event['status'] != prior['status']: hold('fee_record_changed')
                if not any(r['fingerprint'] == fp for r in previous):
                    # Reserve a row for a receipt, even when the event limit is hit.
                    if count >= MAX_EVENTS - 1:
                        hold('fee_capacity_reached')
                        scan = dict(scan, scan_finished=False, failure='fee_capacity_reached')
                        break
                    self.db.execute("INSERT INTO paper_fee_log(day,kind,activity_id,observed_at,fingerprint,payload) VALUES (?,'fee',?,?,?,?)",
                                    (day, event['activity_id'], at, fp, encoded(event)))
                    count += 1
                members.append(dict(activity_id=event['activity_id'], fingerprint=fp))
            receipt = dict(scan, members=members)
            fp = digest(receipt)
            if not old or old['fingerprint'] != fp:
                if count >= MAX_EVENTS: hold('fee_capacity_reached')
                else:
                    self.db.execute("INSERT INTO paper_fee_log(day,kind,observed_at,fingerprint,payload) VALUES (?,'scan',?,?,?)",
                                    (day, at, fp, encoded(receipt)))
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise

    def report(self):
        self.commitment()
        row = self.db.execute("SELECT * FROM paper_fee_log WHERE kind='scan' ORDER BY seq DESC LIMIT 1").fetchone()
        scan = dict(verified(row), observed_at=row['observed_at'], anchor_date=row['day']) if row else None
        events = []
        if scan:
            for member in scan['members']:
                r = self.db.execute("SELECT * FROM paper_fee_log WHERE kind='fee' AND activity_id=? AND fingerprint=? ORDER BY seq LIMIT 1",
                                    (member['activity_id'], member['fingerprint'])).fetchone()
                if not r: raise EvidenceError('fee_membership_integrity_failed')
                events.append(dict(verified(r), seq=r['seq'], first_observed_at=r['observed_at']))
        state, total = 'not_observed', None
        held = self.hold()
        if held: state = 'correction_hold'
        elif scan:
            if not scan['scan_finished']: state = 'scan_incomplete'
            elif not events: state = 'no_fee_records_observed'
            elif any(e['signed_cash_amount'] is None or e['currency'] != 'USD' or e['status'] != 'executed' for e in events):
                state = 'amount_currency_or_status_unavailable'
            else:
                state = 'observed_usd_cash_only'
                with localcontext() as context:
                    context.prec = 80
                    total = canonical(sum((Decimal(e['signed_cash_amount']) for e in events), Decimal(0)))
        return dict(state=state, scan=scan, hold=held, observed_signed_usd_cash=total, event_count=len(events),
                    events=events[:100], events_truncated=len(events)>100, fee_coverage_verified=False,
                    trade_fees_usd=None, trade_net_pnl_usd=None,
                    notice='Bounded account FEE records only, unattributed to trades. Negative cash is a debit; positive cash is a credit. A finished scan is not fee completeness. No per-trade fees, net profit or reconciliation is established.')

    def export(self, after=0, through=None):
        if type(after) is not int or after < 0: raise EvidenceError('invalid_evidence_cursor')
        if through is None: through = self.db.execute('SELECT COALESCE(MAX(seq),0) FROM paper_fee_log').fetchone()[0]
        if type(through) is not int or through < 0: raise EvidenceError('invalid_evidence_cursor')
        rows = list(self.db.execute('SELECT * FROM paper_fee_log WHERE seq>? AND seq<=? ORDER BY seq LIMIT 101', (after, through)))
        events = [dict(verified(r), seq=r['seq'], kind=r['kind'], anchor_date=r['day'], first_observed_at=r['observed_at']) for r in rows[:100]]
        return dict(schema_version='orb-paper-fees-export-v1', events=events, through=through,
                    next_after=events[-1]['seq'] if events else after, has_more=len(rows)>100)
