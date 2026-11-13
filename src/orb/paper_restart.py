"""Native startup inventory checkpoint, not settled-cash or correction clearance."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
from hashlib import sha256

from orb.paper_evidence import EvidenceError, PaperEvidence, TERMINAL, bundle, digest, encoded, numeric, stamp, verified
from orb.paper_admission import PaperAdmission
from orb.paper_management import PaperManagement, inventory, READ_FAILURES
from orb.paper_fees import PaperFees
from orb.session import exchange_session
from orb.strategy import NEW_YORK

TABLES={'paper_restart_log'}
MAX_EVENTS=2000
ALLOW={'flat_inventory_agrees','owned_inventory_agrees'}


def install(db):
    db.execute('CREATE TABLE paper_restart_log (seq INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)')
    for op in ('UPDATE','DELETE'):
        db.execute(f'CREATE TRIGGER paper_restart_log_no_{op.lower()} BEFORE {op} ON paper_restart_log '
                   "BEGIN SELECT RAISE(ABORT, 'paper restart checkpoints are append-only'); END")


def capacity_hold(db):
    values=dict(db.execute("SELECT key,value FROM paper_meta WHERE key IN ('restart-hold','restart-hold-at')"))
    if not values:return None
    if set(values)!={'restart-hold','restart-hold-at'} or values['restart-hold']!='restart_capacity_reached':
        raise EvidenceError('restart_hold_integrity_failed')
    return dict(day='account_restart_evidence',reason=values['restart-hold'],observed_at=stamp(values['restart-hold-at']))


def basis(db):
    PaperAdmission(db).report();PaperManagement(db).report()
    marks={}
    for table in ('paper_observations','paper_fill_activities','paper_activity_scans','paper_hold_events','paper_review_log','paper_fee_log','paper_admission_log','paper_management_log'):
        rows=list(db.execute(f'SELECT * FROM {table} ORDER BY seq'))
        for r in rows:verified(r)
        marks[table]=[(r['seq'],r['fingerprint']) for r in rows]
    return digest(dict(attempts=[dict(r) for r in db.execute('SELECT * FROM paper_attempts ORDER BY day')],
        heads=[tuple(r) for r in db.execute('SELECT * FROM paper_observation_heads ORDER BY day')],marks=marks,
        meta=[tuple(r) for r in db.execute('SELECT * FROM paper_meta ORDER BY key')],holds=PaperEvidence(db).holds()))


def classify(row,owned,stable):
    if row is None:return 'flat_inventory_agrees' if not stable['holdings'] and not stable['orders'] else 'unattributed_inventory'
    orders=owned['orders']
    if not orders:return 'entry_lookup_unknown'
    if owned['closing_expected'] and not any(o['role']=='closing_exit' for o in orders):return 'closing_lookup_unknown'
    entry=orders[0];filled=Decimal(entry['filled_quantity'])
    if (any(o['replaced'] for o in orders) or entry['quantity'] is None
            or Decimal(entry['quantity'])!=Decimal(numeric(json.loads(row['request'])['qty']))
            or (entry['status']=='filled' and entry['quantity']!=entry['filled_quantity'])
            or (not owned['legs_complete'] and (filled>0 or entry['status'] not in TERMINAL))):return 'owned_evidence_incomplete'
    residual=filled-sum((Decimal(o['filled_quantity']) for o in orders[1:]),Decimal(0))
    if residual<0 or Decimal(stable['holdings'].get('SPY','0'))!=residual:return 'position_disagreement'
    known={o['id']:o for o in orders};listed={o['id']:o for o in stable['orders']}
    for item in stable['orders']:
        if item['symbol']!='SPY':continue
        own=known.get(item['id'])
        if own is None:return 'unattributed_spy_order'
        if (own['status'] in TERMINAL or item['side']!=own['side'] or item['filled_quantity']!=own['filled_quantity']
                or (own['quantity'] is not None and item['quantity']!=own['quantity'])):return 'open_order_disagreement'
    # Inactive nested exits need not appear in a flat open-order listing. Entry
    # and explicit closing orders must be represented when nonterminal.
    for own in orders:
        if own['role'] in ('entry','closing_exit') and own['status'] not in TERMINAL and own['id'] not in listed:
            return 'working_order_not_listed'
    return 'owned_inventory_agrees'


def collect(client,rows,expected_scope,now,elapsed):
    passes=[];captures=[];scope=expected_scope;state='snapshot_read_failed'
    if len(rows)>1:return dict(state='multiple_pending_attempts',passes=[],scope=scope),captures
    if rows and scope is None:return dict(state='missing_saved_scope',passes=[],scope=None),captures
    row=rows[0] if rows else None
    def call(method,*args):
        if elapsed()>22:raise EvidenceError('restart_snapshot_timeout')
        return method(*args)
    try:
        for _ in range(2):
            clock=call(client.request,'GET','/v2/clock');account=call(client.request,'GET','/v2/account')
            if (not isinstance(account,dict) or not isinstance(account.get('id'),str)
                    or not 1<=len(account['id'])<=256):raise EvidenceError('close_account_scope_mismatch')
            observed_scope=sha256(account['id'].encode()).hexdigest()
            if scope is not None and observed_scope!=scope:raise EvidenceError('close_account_scope_mismatch')
            positions=call(client.request,'GET','/v2/positions')
            open_orders=call(client.request,'GET','/v2/orders?status=open&limit=500&nested=false')
            raw_parent=raw_close=None
            if row:
                raw_parent=call(client.find,row['client_id'])
                if raw_parent is not None:
                    if not isinstance(raw_parent,dict) or not isinstance(raw_parent.get('id'),str):raise EvidenceError('restart_owned_evidence_invalid')
                    raw_parent=call(client.order,raw_parent['id'])
                if row['closing']:raw_close=call(client.find,json.loads(row['closing'])['client_order_id'])
            observed=now+timedelta(seconds=elapsed());at=stamp(observed.isoformat())
            if not isinstance(clock,dict) or type(clock.get('is_open')) is not bool:raise EvidenceError('close_clock_unavailable')
            when=datetime.fromisoformat(stamp(clock.get('timestamp')))
            if abs(observed-when)>timedelta(seconds=30):raise EvidenceError('close_clock_unavailable')
            session=exchange_session(observed.astimezone(NEW_YORK).date())
            if clock['is_open'] and (not session or not session.opens_at<=observed<session.closes_at):raise EvidenceError('close_clock_unavailable')
            snap=inventory(account,positions,open_orders,observed_scope,None)
            scope=observed_scope
            owned=None
            if row:
                captures.append((row,deepcopy(raw_parent),deepcopy(raw_close),at))
                try:owned=bundle(row,raw_parent,raw_close,at)
                except (EvidenceError,ValueError,KeyError,TypeError):raise EvidenceError('restart_owned_evidence_invalid') from None
            status=classify(row,owned,snap['stable'])
            passes.append(dict(snap,owned=owned,state=status,observed_at=at,broker_clock=stamp(clock['timestamp']),market_open=clock['is_open']))
        first,last=passes
        if (first['stable']!=last['stable'] or first['owned']!=last['owned'] or first['market_open']!=last['market_open']):
            state='inventory_changed'
        else:state=last['state']
    except EvidenceError as exc:
        code=str(exc);state=code if code in READ_FAILURES|{'restart_owned_evidence_invalid','restart_snapshot_timeout'} else 'snapshot_read_failed'
    except Exception:state='snapshot_read_failed'
    return dict(state=state,passes=passes,scope=scope),captures


class PaperRestart:
    def __init__(self,db):self.db=db

    def commitment(self):
        rows=list(self.db.execute('SELECT * FROM paper_restart_log ORDER BY seq'))
        for r in rows:verified(r)
        return [(r['seq'],r['fingerprint']) for r in rows]

    def save(self,expected,snapshot,captures,started_at,finished_at,epoch,reason):
        started_at,finished_at=stamp(started_at),stamp(finished_at)
        if finished_at<started_at:raise EvidenceError('restart_clock_reversed')
        if reason not in ('startup','manual_read'):raise EvidenceError('invalid_restart_reason')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if basis(self.db)!=expected:raise EvidenceError('restart_basis_changed')
            old=self.db.execute('SELECT * FROM paper_restart_log ORDER BY seq DESC LIMIT 1').fetchone()
            if old and verified(old)['started_at']>started_at:raise EvidenceError('stale_restart_check')
            if self.db.execute('SELECT COUNT(*) FROM paper_restart_log').fetchone()[0]>=MAX_EVENTS:
                self.db.execute("INSERT OR IGNORE INTO paper_meta VALUES ('restart-hold','restart_capacity_reached')")
                self.db.execute("INSERT OR IGNORE INTO paper_meta VALUES ('restart-hold-at',?)",(finished_at,))
                self.db.execute('COMMIT');return dict(can_continue=False,state='restart_capacity_reached',seq=None)
            scope=snapshot['scope']
            if scope is not None:
                saved=self.db.execute("SELECT value FROM paper_meta WHERE key='account'").fetchone()
                if saved is not None and saved[0]!=scope:raise EvidenceError('restart_account_scope_changed')
                self.db.execute("INSERT OR IGNORE INTO paper_meta VALUES ('account',?)",(scope,))
            state=snapshot['state']
            for row,parent,closing,at in captures:
                try:PaperEvidence(self.db).capture(row,parent,closing,at,transaction=False)
                except EvidenceError:state='restart_owned_evidence_invalid'
            holds=PaperEvidence(self.db).holds()
            if holds:state='correction_hold'
            if datetime.fromisoformat(finished_at)-datetime.fromisoformat(started_at)>timedelta(seconds=30):state='checkpoint_expired'
            value=dict(schema_version='orb-native-restart-check-v1',epoch=epoch,reason=reason,started_at=started_at,
                state=state,can_continue=state in ALLOW,basis_before=expected,basis_after=basis(self.db),snapshot=snapshot,
                cash_reconciled=False,correction_clearance=False,retry_authority=False)
            seq=self.db.execute('INSERT INTO paper_restart_log(observed_at,fingerprint,payload) VALUES (?,?,?)',
                (finished_at,digest(value),encoded(value))).lastrowid
            self.db.execute('COMMIT');return dict(can_continue=value['can_continue'],state=state,seq=seq)
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise

    def report(self,epoch=None,required=True,blocked=False,applied_seq=None):
        self.commitment()
        row=self.db.execute('SELECT * FROM paper_restart_log ORDER BY seq DESC LIMIT 1').fetchone()
        value=dict(verified(row),seq=row['seq'],observed_at=row['observed_at']) if row else None
        matches=value['basis_after']==basis(self.db) if value else False
        return dict(schema_version='orb-native-restart-report-v1',latest=value,worker_check_required=required,
            explicit_restart_required=blocked,worker_checkpoint_seq=applied_seq,latest_matches_worker_epoch=bool(value and value['epoch']==epoch),
            basis_matches_current=matches,hold=capacity_hold(self.db),
            notice='Startup quantity/identity checkpoint, not settled cash, fee reconciliation or correction clearance. Saved results are not reusable permission after reopening. Missing lookups remain unknown; no command replay or automatic start.')

    def export(self,after=0,through=None):
        if type(after)is not int or after<0:raise EvidenceError('invalid_evidence_cursor')
        if through is None:through=self.db.execute('SELECT COALESCE(MAX(seq),0) FROM paper_restart_log').fetchone()[0]
        if type(through)is not int or through<0:raise EvidenceError('invalid_evidence_cursor')
        rows=list(self.db.execute('SELECT * FROM paper_restart_log WHERE seq>? AND seq<=? ORDER BY seq LIMIT 101',(after,through)))
        events=[dict(verified(r),seq=r['seq'],observed_at=r['observed_at']) for r in rows[:100]]
        return dict(schema_version='orb-native-restart-export-v1',events=events,through=through,
                    next_after=events[-1]['seq'] if events else after,has_more=len(rows)>100)
