"""Bounded, read-only FILL activity evidence; no fee or reconciliation authority."""
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
from urllib.parse import urlencode

from orb.paper_evidence import EvidenceError, canonical, digest, encoded, numeric, stamp, verified
from orb.strategy import NEW_YORK

TABLES = {'paper_fill_activities', 'paper_activity_scans'}
MAX_PAGES = 10
PAGE_SIZE = 100
MAX_EVENTS = 10000


def install(db):
    db.execute('CREATE TABLE paper_fill_activities (seq INTEGER PRIMARY KEY, day TEXT NOT NULL REFERENCES paper_attempts(day), '
               'activity_id TEXT NOT NULL, observed_at TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL, '
               'UNIQUE(activity_id, fingerprint))')
    db.execute('CREATE TABLE paper_activity_scans (seq INTEGER PRIMARY KEY, day TEXT NOT NULL REFERENCES paper_attempts(day), '
               'observed_at TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)')
    for table in TABLES:
        db.execute(f'CREATE INDEX {table}_day ON {table}(day,seq)')
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} '
                       "BEGIN SELECT RAISE(ABORT, 'paper activities are append-only'); END")


def identity(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or not value.isprintable():
        raise EvidenceError('invalid_activity_identity')
    return sha256(value.encode()).hexdigest()[:24]


def normalize(value, known, until):
    """Validate routing IDs for every row; retain only known durable order fills."""
    if not isinstance(value, dict): raise EvidenceError('invalid_activity_shape')
    activity_id, order_id = identity(value.get('id')), identity(value.get('order_id'))
    if order_id not in known: return None
    order = known[order_id]
    if (value.get('activity_type') != 'FILL' or value.get('type') not in ('fill', 'partial_fill')
            or value.get('symbol') != 'SPY' or value.get('side') != order['side']):
        raise EvidenceError('invalid_owned_fill_activity')
    qty, price = numeric(value.get('qty')), numeric(value.get('price'))
    if Decimal(qty) <= 0 or Decimal(price) <= 0: raise EvidenceError('invalid_owned_fill_activity')
    at = stamp(value.get('transaction_time'))
    if at > until: raise EvidenceError('future_activity_timestamp')
    return dict(activity_id=activity_id, order_id=order_id, symbol='SPY', side=order['side'],
                role=order['role'], quantity=qty, price=price, transaction_time=at,
                attribution='matched_durable_order', fees_usd=None)


def fetch(client, day, known, until):
    """Freeze creation-time query bounds; short final page means scan finished only.

    Alpaca filters activities by creation time, not necessarily execution time.
    This is never a completeness certificate for executions or later corrections.
    """
    start = datetime.combine(day, time(), NEW_YORK).astimezone(timezone.utc) - timedelta(days=1)
    params = dict(after=start.isoformat(), until=until, direction='asc', page_size=PAGE_SIZE)
    rows, seen, token, count, pages = [], {}, None, 0, 0
    finished, failure = False, None
    for _ in range(MAX_PAGES):
        try:
            if token: params['page_token'] = token
            page = client.request('GET', '/v2/account/activities/FILL?' + urlencode(params))
            pages += 1
            if not isinstance(page, list) or len(page) > PAGE_SIZE: raise EvidenceError('invalid_activity_page')
            for item in page:
                key = identity(item.get('id')) if isinstance(item, dict) else identity(None)
                event = normalize(item, known, until)
                if key in seen:
                    if event is not None and seen[key] is not None and digest(event) != digest(seen[key]):
                        rows.append(event)
                        raise EvidenceError('activity_revision_in_scan')
                    raise EvidenceError('activity_pagination_repeated')
                seen[key] = event
                count += 1
                if event: rows.append(event)
            if len(page) < PAGE_SIZE:
                finished = True
                break
            token = page[-1]['id']
        except EvidenceError as exc:
            failure = str(exc)
            break
        except Exception:
            failure = 'activity_read_failed'
            break
    if not finished and failure is None: failure = 'activity_page_limit'
    return rows, dict(after=params['after'], until=until, scan_finished=finished, pages=pages,
                     received=count, matched=len(rows), failure=failure)


class PaperActivities:
    def __init__(self, db): self.db = db

    def known_orders(self, day):
        known = {}
        for r in self.db.execute('SELECT payload,fingerprint FROM paper_observations WHERE day=? ORDER BY seq', (day,)):
            for item in verified(r)['orders']: known[item['id']] = item
        return known

    def save(self, day, events, scan, at):
        at = stamp(at)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            def hold(reason):
                from orb.paper_reviews import record_hold
                record_hold(self.db,day,reason,at,'fill_activity')
            old_scan = self.db.execute('SELECT * FROM paper_activity_scans WHERE day=? ORDER BY seq DESC LIMIT 1', (day,)).fetchone()
            if old_scan:
                prior_scan = verified(old_scan)
                if prior_scan['until'] > scan['until']:
                    raise EvidenceError('stale_activity_scan')
                if at < old_scan['observed_at']: hold('activity_clock_reversed')
            current_ids = {v['activity_id'] for v in events}
            previous = list(self.db.execute('SELECT * FROM paper_fill_activities WHERE day=?', (day,)))
            if scan['scan_finished'] and old_scan:
                previous_scan = verified(old_scan)
                if previous_scan['after'] == scan['after'] and previous_scan['until'] <= scan['until']:
                    if any(r['activity_id'] not in current_ids for r in previous): hold('previous_activity_missing')
            added = 0
            for event in events:
                fingerprint = digest(event)
                prior = list(self.db.execute('SELECT * FROM paper_fill_activities WHERE activity_id=?', (event['activity_id'],)))
                for r in prior:
                    verified(r)
                    if r['day'] != day or r['fingerprint'] != fingerprint: hold('fill_activity_changed')
                if any(r['fingerprint'] == fingerprint for r in prior): continue
                if self.db.execute('SELECT COUNT(*) FROM paper_fill_activities').fetchone()[0] >= MAX_EVENTS:
                    hold('fill_activity_capacity_reached')
                    scan = dict(scan, scan_finished=False, failure='fill_activity_capacity_reached')
                    break
                self.db.execute('INSERT INTO paper_fill_activities(day,activity_id,observed_at,fingerprint,payload) VALUES (?,?,?,?,?)',
                                (day, event['activity_id'], at, fingerprint, encoded(event)))
                added += 1
            if scan['failure'] in ('invalid_owned_fill_activity', 'future_activity_timestamp', 'invalid_order_number', 'invalid_observation_time', 'activity_revision_in_scan'):
                hold('invalid_owned_fill_activity')
            scan = dict(scan, added=added)
            # Identical receipts do not consume storage. Distinct receipts are bounded.
            fingerprint = digest(scan)
            if not old_scan or old_scan['fingerprint'] != fingerprint:
                if self.db.execute('SELECT COUNT(*) FROM paper_activity_scans').fetchone()[0] >= MAX_EVENTS:
                    hold('activity_scan_capacity_reached')
                else:
                    self.db.execute('INSERT INTO paper_activity_scans(day,observed_at,fingerprint,payload) VALUES (?,?,?,?)',
                                    (day, at, fingerprint, encoded(scan)))
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise

    def report(self, day, orders, outcome, held):
        records = list(self.db.execute('SELECT * FROM paper_fill_activities WHERE day=? ORDER BY seq', (day,)))
        events = [dict(verified(r), first_observed_at=r['observed_at'], seq=r['seq']) for r in records]
        row = self.db.execute('SELECT * FROM paper_activity_scans WHERE day=? ORDER BY seq DESC LIMIT 1', (day,)).fetchone()
        scan = dict(verified(row), observed_at=row['observed_at']) if row else None
        state, gross = 'not_observed', None
        compared = []
        with localcontext() as context:
            context.prec = 80
            by_order = {}
            for event in events:
                qty, value = by_order.get(event['order_id'], (Decimal(0), Decimal(0)))
                by_order[event['order_id']] = qty + Decimal(event['quantity']), value + Decimal(event['quantity']) * Decimal(event['price'])
            for order in orders:
                qty, value = by_order.get(order['id'], (Decimal(0), Decimal(0)))
                snapshot_qty = Decimal(order['filled_quantity'])
                compared.append(dict(order_id=order['id'], role=order['role'], activity_quantity=canonical(qty),
                                     snapshot_quantity=canonical(snapshot_qty), quantities_agree=qty == snapshot_qty,
                                     activity_average=canonical(value / qty) if qty else None,
                                     snapshot_average=order['average_fill_price']))
            if held: state = 'correction_hold'
            elif scan:
                if not scan['scan_finished']: state = 'scan_incomplete'
                elif not orders: state = 'order_evidence_unavailable'
                elif any(k not in {o['id'] for o in orders} for k in by_order): state = 'order_evidence_unavailable'
                elif not all(v['quantities_agree'] for v in compared): state = 'quantity_mismatch'
                elif outcome == 'known_no_entry' and not events: state, gross = 'known_no_entry', '0'
                elif outcome == 'closed_price_evidence':
                    state = 'closed_quantities_agree'
                    gross = canonical(sum(((-1 if e['side'] == 'buy' else 1) * Decimal(e['quantity']) * Decimal(e['price']) for e in events), Decimal(0)))
                else: state = 'open_or_incomplete_orders'
        return dict(state=state, scan=scan, event_count=len(events), events=events[:100], events_truncated=len(events) > 100,
                    orders=compared, gross_price_pnl_usd=gross, fees_usd=None, net_pnl_usd=None,
                    notice='Reported FILL activities matched to durable orders. Quantity agreement is not execution completeness, correction resolution or account reconciliation. Creation-time scan; fees/net unavailable.')

    def export(self, after=0, through=None):
        if type(after) is not int or after < 0: raise EvidenceError('invalid_evidence_cursor')
        if through is None: through = self.db.execute('SELECT COALESCE(MAX(seq),0) FROM paper_fill_activities').fetchone()[0]
        if type(through) is not int or through < 0: raise EvidenceError('invalid_evidence_cursor')
        rows = list(self.db.execute('SELECT * FROM paper_fill_activities WHERE seq>? AND seq<=? ORDER BY seq LIMIT 101', (after, through)))
        events = [dict(verified(r), seq=r['seq'], session=r['day'], first_observed_at=r['observed_at']) for r in rows[:100]]
        return dict(schema_version='orb-paper-activities-export-v1', events=events, through=through,
                    next_after=events[-1]['seq'] if events else after, has_more=len(rows) > 100)
