"""Append-only observations of owned paper orders, not execution-event history.

Cumulative averages can support a flat-attempt gross price-P&L estimate. They
cannot establish individual executions, fees, net P&L or account reconciliation.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json

TERMINAL = {'filled', 'canceled', 'expired', 'rejected', 'replaced'}
STATUSES = TERMINAL | {'new', 'accepted', 'pending_new', 'accepted_for_bidding', 'partially_filled',
                       'pending_cancel', 'pending_replace', 'done_for_day', 'held', 'stopped', 'suspended', 'calculated'}
TABLES = {'paper_observations', 'paper_observation_heads', 'paper_evidence_holds'}
MAX_EVENTS = 10000


class EvidenceError(ValueError):
    """Only fixed reason codes may cross this boundary."""


def canonical(value):
    if value == 0: return '0'
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def numeric(value, nullable=False):
    if value is None and nullable: return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 40:
        raise EvidenceError('invalid_order_number')
    try: number = Decimal(str(value))
    except InvalidOperation: raise EvidenceError('invalid_order_number') from None
    if not number.is_finite() or not 0 <= number <= Decimal('1e12') or abs(number.as_tuple().exponent) > 12:
        raise EvidenceError('invalid_order_number')
    return canonical(number)


def stamp(value):
    if not isinstance(value, str) or len(value) > 64: raise EvidenceError('invalid_observation_time')
    try: parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError: raise EvidenceError('invalid_observation_time') from None
    if parsed.tzinfo is None: raise EvidenceError('invalid_observation_time')
    return parsed.astimezone(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def digest(value):
    return sha256(encoded(value).encode()).hexdigest()


def verified(event):
    try:
        value = json.loads(event['payload'])
        if digest(value) != event['fingerprint']: raise EvidenceError('paper_evidence_integrity_failed')
        return value
    except (ValueError, TypeError, RecursionError):
        raise EvidenceError('paper_evidence_integrity_failed') from None


def notional(item):
    with localcontext() as context:
        context.prec = 80
        return Decimal(item['filled_quantity']) * Decimal(item['average_fill_price'])


def gross_estimate(orders):
    with localcontext() as context:
        context.prec = 80
        return sum((notional(v) for v in orders[1:] if Decimal(v['filled_quantity']) > 0), Decimal(0)) - notional(orders[0])


def install(db):
    """Called only inside the worker's validated v1→v2 migration transaction."""
    db.execute('CREATE TABLE paper_observations (seq INTEGER PRIMARY KEY, day TEXT NOT NULL REFERENCES paper_attempts(day), '
               'observed_at TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE INDEX paper_observations_day ON paper_observations(day, seq)')
    db.execute('CREATE TABLE paper_observation_heads (day TEXT PRIMARY KEY REFERENCES paper_attempts(day), '
               'seq INTEGER NOT NULL REFERENCES paper_observations(seq), last_seen_at TEXT NOT NULL)')
    db.execute('CREATE TABLE paper_evidence_holds (day TEXT PRIMARY KEY REFERENCES paper_attempts(day), '
               'reason TEXT NOT NULL, observed_at TEXT NOT NULL)')
    for operation in ('UPDATE', 'DELETE'):
        db.execute(f"CREATE TRIGGER paper_observations_no_{operation.lower()} BEFORE {operation} ON paper_observations "
                   "BEGIN SELECT RAISE(ABORT, 'paper observations are append-only'); END")


def order(value, role, observed_at):
    if not isinstance(value, dict): raise EvidenceError('invalid_order_shape')
    identity, status = value.get('id'), value.get('status')
    if not isinstance(identity, str) or not 1 <= len(identity) <= 128 or not identity.isprintable():
        raise EvidenceError('invalid_order_identity')
    if not isinstance(status, str) or not 1 <= len(status) <= 48 or not status.isprintable():
        raise EvidenceError('invalid_order_status')
    if status not in STATUSES: raise EvidenceError('unknown_order_status')
    kind = value.get('type')
    if kind is not None and kind not in ('market', 'limit', 'stop', 'stop_limit', 'trailing_stop'):
        raise EvidenceError('invalid_order_type')
    if value.get('symbol') != 'SPY' or value.get('side') != ('buy' if role == 'entry' else 'sell'):
        raise EvidenceError('order_identity_changed')
    qty, filled = numeric(value.get('qty'), nullable=True), numeric(value.get('filled_qty'))
    price = numeric(value.get('filled_avg_price'), nullable=True)
    # A zero broker placeholder is unavailable, never a zero-cost fill.
    if price is not None and Decimal(price) == 0: price = None
    if qty is not None and Decimal(filled) > Decimal(qty): raise EvidenceError('overfilled_order')
    at = stamp(value['filled_at']) if value.get('filled_at') else None
    if at and at > observed_at: raise EvidenceError('future_fill_timestamp')
    return dict(id=sha256(identity.encode()).hexdigest()[:24], role=role, symbol='SPY', side=value['side'],
                status=status, quantity=qty, filled_quantity=filled, average_fill_price=price,
                order_type=kind, limit_price=numeric(value.get('limit_price'), nullable=True),
                stop_price=numeric(value.get('stop_price'), nullable=True),
                filled_at=at, replaced=bool(value.get('replaced_by')) or status in ('replaced', 'pending_replace'),
                attribution='matched_durable_intent')


def bundle(row, parent, closing, at):
    if parent is None:
        return dict(schema_version='orb-paper-observation-v1', orders=[], legs_complete=False,
                    closing_expected=bool(row['closing']), missing_entry=True)
    if not isinstance(parent, dict): raise EvidenceError('invalid_order_shape')
    expected = json.loads(row['request'])
    if parent.get('client_order_id') != row['client_id'] or numeric(parent.get('qty')) != numeric(expected['qty']):
        raise EvidenceError('order_identity_changed')
    legs = parent.get('legs') if parent.get('legs') is not None else []
    if not isinstance(legs, list) or len(legs) > 2: raise EvidenceError('invalid_bracket_legs')
    exits = sorted((order(v, 'bracket_exit', at) for v in legs), key=lambda v: v['id'])
    orders = [order(parent, 'entry', at), *exits]
    if closing is not None:
        if not isinstance(closing, dict): raise EvidenceError('invalid_order_shape')
        request = json.loads(row['closing']) if row['closing'] else None
        if (not request or closing.get('client_order_id') != request['client_order_id']
                or numeric(closing.get('qty')) != numeric(request['qty'])):
            raise EvidenceError('closing_identity_changed')
        orders.append(order(closing, 'closing_exit', at))
    ids = [v['id'] for v in orders]
    if len(set(ids)) != len(ids): raise EvidenceError('duplicate_order_identity')
    if sum((Decimal(v['filled_quantity']) for v in orders[1:]), Decimal(0)) > Decimal(orders[0]['filled_quantity']):
        raise EvidenceError('exit_quantity_exceeds_entry')
    return dict(schema_version='orb-paper-observation-v1', orders=orders, legs_complete=len(legs) == 2,
                closing_expected=bool(row['closing']), missing_entry=False)


def correction(previous, current):
    old = {v['id']: v for v in previous['orders']}
    new = {v['id']: v for v in current['orders']}
    if previous['orders'] and current['orders'] and previous['orders'][0]['id'] != current['orders'][0]['id']:
        return 'order_identity_changed'
    if previous['legs_complete'] and current['legs_complete']:
        if {v['id'] for v in previous['orders'] if v['role'] == 'bracket_exit'} != {v['id'] for v in current['orders'] if v['role'] == 'bracket_exit'}:
            return 'bracket_identity_changed'
    old_close = {v['id'] for v in previous['orders'] if v['role'] == 'closing_exit'}
    new_close = {v['id'] for v in current['orders'] if v['role'] == 'closing_exit'}
    if old_close and new_close and old_close != new_close: return 'closing_identity_changed'
    for key in old.keys() & new.keys():
        a, b = old[key], new[key]
        x, y = Decimal(a['filled_quantity']), Decimal(b['filled_quantity'])
        if y < x: return 'cumulative_quantity_decreased'
        if a['status'] in TERMINAL and b['status'] != a['status']: return 'terminal_order_changed'
        for field in ('order_type', 'limit_price', 'stop_price'):
            if a[field] is not None and b[field] is not None and a[field] != b[field]: return 'order_terms_changed'
        if x == y and x > 0 and a['average_fill_price'] and b['average_fill_price'] and a['average_fill_price'] != b['average_fill_price']:
            return 'average_changed_without_fill'
        if x == y and x > 0 and a['filled_at'] and b['filled_at'] and a['filled_at'] != b['filled_at']:
            return 'fill_timestamp_changed_without_fill'
        if y > x and a['average_fill_price'] and b['average_fill_price']:
            if notional(b) <= notional(a):
                return 'cumulative_notional_regressed'
    if any(v['replaced'] for v in current['orders']): return 'replaced_order'
    return None


class PaperEvidence:
    def __init__(self, db): self.db = db

    def holds(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM paper_evidence_holds ORDER BY day')]

    def _hold(self, day, reason, at):
        self.db.execute('INSERT OR IGNORE INTO paper_evidence_holds VALUES (?,?,?)', (day, reason, at))

    def capture(self, row, parent, closing, observed_at):
        at = stamp(observed_at)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            try: payload = bundle(row, parent, closing, at)
            except (EvidenceError, TypeError, KeyError) as exc:
                reason = str(exc) if isinstance(exc, EvidenceError) else 'malformed_owned_order_evidence'
                self._hold(row['day'], reason, at)
                self.db.execute('COMMIT')
                raise EvidenceError(reason) from None
            old = self.db.execute('SELECT o.*, h.last_seen_at FROM paper_observations o '
                'JOIN paper_observation_heads h ON h.seq=o.seq WHERE h.day=?', (row['day'],)).fetchone()
            if old and old['day'] != row['day']: raise EvidenceError('paper_evidence_integrity_failed')
            if old and at < old['last_seen_at']:
                self._hold(row['day'], 'observation_clock_reversed', at)
            if any(v['replaced'] for v in payload['orders']): self._hold(row['day'], 'replaced_order', at)
            if old:
                verified(old)
                # Compare all previously seen order IDs, even after a transient
                # missing response, rather than forgetting old cumulative fills.
            historical = {}
            complete_legs_seen = False
            for event in self.db.execute('SELECT payload, fingerprint FROM paper_observations WHERE day=? ORDER BY seq', (row['day'],)):
                data = verified(event)
                complete_legs_seen |= data['legs_complete']
                for item in data['orders']: historical[item['id']] = item
            if old:
                latest = verified(old)
                prior = dict(latest, orders=list(historical.values()), legs_complete=complete_legs_seen)
                reason = correction(prior, payload)
                if reason: self._hold(row['day'], reason, at)
            fingerprint = digest(payload)
            if old and old['fingerprint'] == fingerprint:
                self.db.execute('UPDATE paper_observation_heads SET last_seen_at=MAX(last_seen_at,?) WHERE day=?', (at, row['day']))
            else:
                if self.db.execute('SELECT COUNT(*) FROM paper_observations').fetchone()[0] >= MAX_EVENTS:
                    self._hold(row['day'], 'paper_evidence_capacity_reached', at)
                else:
                    seq = self.db.execute('INSERT INTO paper_observations(day,observed_at,fingerprint,payload) VALUES (?,?,?,?)',
                        (row['day'], at, fingerprint, encoded(payload))).lastrowid
                    self.db.execute('INSERT OR REPLACE INTO paper_observation_heads VALUES (?,?,?)', (row['day'], seq, at))
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise

    def events(self, after=0, limit=100, through=None):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise EvidenceError('invalid_evidence_cursor')
        if through is None: through = self.db.execute('SELECT COALESCE(MAX(seq),0) FROM paper_observations').fetchone()[0]
        if type(through) is not int or through < 0: raise EvidenceError('invalid_evidence_cursor')
        rows = list(self.db.execute('SELECT * FROM paper_observations WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?', (after, through, limit + 1)))
        output = []
        for r in rows[:limit]:
            payload = verified(r)
            output.append(dict(seq=r['seq'], session=r['day'], observed_at=r['observed_at'],
                               fingerprint=r['fingerprint'], observation=payload))
        return dict(schema_version='orb-paper-evidence-export-v1', observations=output,
                    next_after=output[-1]['seq'] if output else after, through=through, has_more=len(rows) > limit,
                    notice='Locally observed cumulative order snapshots, not individual execution events or broker audit records.')

    def report(self):
        from orb.paper_activities import PaperActivities
        activities = PaperActivities(self.db)
        holds = {v['day']: v for v in self.holds()}
        rows = list(self.db.execute('SELECT * FROM paper_attempts ORDER BY day DESC LIMIT 101'))
        attempts, orders = [], []
        for row in rows[:100]:
            h = self.db.execute('SELECT o.*,h.last_seen_at FROM paper_observations o '
                'JOIN paper_observation_heads h ON h.seq=o.seq WHERE h.day=?', (row['day'],)).fetchone()
            payload = verified(h) if h else None
            if h and h['day'] != row['day']: raise EvidenceError('paper_evidence_integrity_failed')
            outcome, gross = 'not_observed', None
            filled = exited = None
            if payload:
                owned = payload['orders']; orders.extend(owned)
                entry = owned[0] if owned else None
                if entry:
                    filled = Decimal(entry['filled_quantity'])
                    exited = sum((Decimal(v['filled_quantity']) for v in owned[1:]), Decimal(0))
                    terminal = all(v['status'] in TERMINAL and not v['replaced'] for v in owned)
                    if row['day'] in holds: outcome = 'correction_hold'
                    elif entry['status'] == 'filled' and entry['quantity'] != entry['filled_quantity']:
                        outcome = 'terminal_quantity_mismatch'
                    elif not payload['legs_complete'] and (filled > 0 or not terminal): outcome = 'incomplete_bracket'
                    elif payload['closing_expected'] and not any(v['role'] == 'closing_exit' for v in owned): outcome = 'missing_closing_order'
                    elif terminal and filled == 0 and exited == 0: outcome, gross = 'known_no_entry', Decimal(0)
                    elif terminal and filled == exited:
                        if all(v['average_fill_price'] for v in owned if Decimal(v['filled_quantity']) > 0):
                            gross = gross_estimate(owned)
                            outcome = 'closed_price_evidence'
                        else: outcome = 'missing_fill_price'
                    else: outcome = 'open_or_pending'
                else: outcome = 'order_not_found'
            if row['day'] in holds: outcome, gross = 'correction_hold', None
            preset = json.loads(row['preset'])
            attempts.append(dict(session=row['day'], state=outcome, preset_name=preset['name'], preset_version=preset['version'],
                observed_at=h['last_seen_at'] if h else None, entry_filled=canonical(filled) if filled is not None else None,
                exit_filled=canonical(exited) if exited is not None else None,
                gross_price_pnl_usd=canonical(gross) if gross is not None else None, net_pnl_usd=None,
                hold_reason=holds.get(row['day'], {}).get('reason'), observation_seq=h['seq'] if h else None,
                orders=payload['orders'] if payload else [],
                fill_activities=activities.report(row['day'], payload['orders'] if payload else [], outcome, row['day'] in holds)))
        return dict(schema_version='orb-paper-evidence-report-v1', attempts=attempts, orders=orders,
                    holds=list(holds.values()), entries_blocked=bool(holds), attempts_may_be_truncated=len(rows) > 100,
                    notice='Saved ORB-owned cumulative snapshots. Closed gross price P&L is an estimate from broker averages; fees/net P&L unavailable. No account-wide reconciliation or execution-event completeness is implied.')
