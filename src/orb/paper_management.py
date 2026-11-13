"""Durable cancel/close commands; unknown dispatch is never a retry authority."""
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import re

from orb.paper_admission import PaperAdmission
from orb.paper_evidence import EvidenceError, PaperEvidence, TERMINAL, STATUSES, canonical, digest, encoded, numeric, stamp, verified, order
from orb.paper_activities import identity
from orb.session import exchange_session
from orb.strategy import NEW_YORK

TABLES = {'paper_management_log'}
MAX_EVENTS = 12000
REASONS = {'flatten_deadline','partial_entry','stale_entry','protection_ended'}
READ_FAILURES = {'close_account_scope_mismatch','close_account_unavailable','close_inventory_truncated_or_invalid',
                 'invalid_close_inventory','duplicate_close_position','close_position_order_mismatch',
                 'close_clock_unavailable','close_inventory_changed','invalid_order_number',
                 'invalid_activity_identity','invalid_observation_time'}


def install(db):
    db.execute('CREATE TABLE paper_management_log (seq INTEGER PRIMARY KEY, day TEXT NOT NULL REFERENCES paper_attempts(day), '
               'observed_at TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE INDEX paper_management_log_day ON paper_management_log(day,seq)')
    for op in ('UPDATE','DELETE'):
        db.execute(f'CREATE TRIGGER paper_management_log_no_{op.lower()} BEFORE {op} ON paper_management_log '
                   "BEGIN SELECT RAISE(ABORT, 'paper management is append-only'); END")


def symbol(value):
    if not isinstance(value,str) or not re.fullmatch('[A-Z0-9./-]{1,16}',value): raise EvidenceError('invalid_close_inventory')
    return value


def inventory(account, positions, orders, scope, residual):
    if (not isinstance(account,dict) or not isinstance(account.get('id'),str)
            or sha256(account['id'].encode()).hexdigest()!=scope): raise EvidenceError('close_account_scope_mismatch')
    if (account.get('currency')!='USD' or account.get('status')!='ACTIVE'
            or account.get('trading_blocked') is not False or account.get('account_blocked') is not False):
        raise EvidenceError('close_account_unavailable')
    equity,cash=numeric(account.get('equity')),numeric(account.get('cash'))
    if not isinstance(positions,list) or len(positions)>=500 or not isinstance(orders,list) or len(orders)>=500:
        raise EvidenceError('close_inventory_truncated_or_invalid')
    holdings={}
    for p in positions:
        if not isinstance(p,dict): raise EvidenceError('invalid_close_inventory')
        s=symbol(p.get('symbol'))
        # Signed quantities on unrelated positions are retained, not liquidated.
        raw=p.get('qty')
        if not isinstance(raw,str): raise EvidenceError('invalid_close_inventory')
        negative=raw.startswith('-');qty=numeric(raw[1:] if negative else raw)
        if s in holdings: raise EvidenceError('duplicate_close_position')
        holdings[s]=canonical((-1 if negative else 1)*Decimal(qty))
    open_orders=[];seen=set()
    for o in orders:
        if not isinstance(o,dict): raise EvidenceError('invalid_close_inventory')
        key=identity(o.get('id'));s=symbol(o.get('symbol'))
        qty,filled=numeric(o.get('qty')),numeric(o.get('filled_qty'))
        if (key in seen or o.get('side') not in ('buy','sell') or o.get('status') not in STATUSES-TERMINAL
                or Decimal(qty)<=0 or Decimal(filled)>Decimal(qty)):
            raise EvidenceError('invalid_close_inventory')
        seen.add(key);open_orders.append(dict(id=key,symbol=s,side=o['side'],status=o['status'],quantity=qty,filled_quantity=filled))
    if residual is not None and (Decimal(holdings.get('SPY','0'))!=Decimal(residual) or any(o['symbol']=='SPY' for o in open_orders)):
        raise EvidenceError('close_position_order_mismatch')
    stable=dict(scope=scope,currency='USD',holdings=dict(sorted(holdings.items())),orders=sorted(open_orders,key=lambda o:o['id']))
    return dict(stable=stable,observed_equity_usd=equity,observed_cash_usd=cash)


def capture_close_inventory(client, scope, residual, now, elapsed):
    """Two bounded regular-session passes, eight GETs maximum, no writes.

    Quantity/identity inventory must agree. Marked equity/cash are context only:
    a moving market must not be mistaken for a position-quantity correction.
    """
    passes=[];state='inventory_read_failed'
    try:
        for _ in range(2):
            clock=client.request('GET','/v2/clock')
            account=client.request('GET','/v2/account')
            if (not isinstance(account,dict) or not isinstance(account.get('id'),str)
                    or sha256(account['id'].encode()).hexdigest()!=scope):
                raise EvidenceError('close_account_scope_mismatch')
            positions=client.request('GET','/v2/positions')
            orders=client.request('GET','/v2/orders?status=open&limit=500&nested=false')
            observed=now+timedelta(seconds=elapsed())
            at=stamp(observed.isoformat());session=exchange_session(observed.astimezone(NEW_YORK).date())
            if (not isinstance(clock,dict) or clock.get('is_open') is not True or not session
                    or not session.opens_at<=observed<session.closes_at
                    or abs(observed-datetime.fromisoformat(stamp(clock.get('timestamp'))))>timedelta(seconds=30)):
                raise EvidenceError('close_clock_unavailable')
            snapshot=inventory(account,positions,orders,scope,residual)
            passes.append(dict(snapshot,observed_at=at,broker_clock=stamp(clock['timestamp']),market_open=True))
        if passes[0]['stable']!=passes[1]['stable']: raise EvidenceError('close_inventory_changed')
        state='two_agreeing_inventory_passes'
    except EvidenceError as exc: state=str(exc) if str(exc) in READ_FAILURES else 'inventory_read_failed'
    except Exception: state='inventory_read_failed'
    return dict(schema_version='orb-close-inventory-v1',state=state,passes=passes,
                fee_or_cash_reconciliation=False,recovery_clearance=False)


class PaperManagement:
    def __init__(self,db): self.db=db

    def records(self):
        output=[]
        for r in self.db.execute('SELECT * FROM paper_management_log ORDER BY seq'):
            value=verified(r)
            if value.get('kind') not in ('cancel_intent','close_intent','dispatch','close_check') or value.get('session')!=r['day']:
                raise EvidenceError('management_integrity_failed')
            output.append(dict(value,seq=r['seq'],observed_at=r['observed_at']))
        return output

    def commitment(self):
        self.records()
        return [(r['seq'],r['fingerprint']) for r in self.db.execute('SELECT seq,fingerprint FROM paper_management_log ORDER BY seq')]

    def _append(self,day,at,value,slots=1):
        if self.db.execute('SELECT COUNT(*) FROM paper_management_log').fetchone()[0]+slots>MAX_EVENTS:
            raise EvidenceError('management_capacity_reached')
        cursor=self.db.execute('INSERT INTO paper_management_log(day,observed_at,fingerprint,payload) VALUES (?,?,?,?)',
                               (day,stamp(at),digest(value),encoded(value)))
        return cursor.lastrowid

    def _proof(self,day,at=None,terminal=False):
        row=self.db.execute('SELECT * FROM paper_attempts WHERE day=?',(day,)).fetchone()
        if not row or row['state']=='done': raise EvidenceError('management_attempt_unavailable')
        h=self.db.execute('SELECT o.*,h.last_seen_at FROM paper_observations o JOIN paper_observation_heads h ON h.seq=o.seq WHERE h.day=?',(day,)).fetchone()
        proof=verified(h) if h else None
        orders=proof['orders'] if proof else []
        if not orders or h['day']!=day: raise EvidenceError('management_order_proof_unavailable')
        if at is not None:
            age=datetime.fromisoformat(stamp(at))-datetime.fromisoformat(stamp(h['last_seen_at']))
            if not timedelta(0)<=age<=timedelta(seconds=30): raise EvidenceError('management_order_proof_expired')
        if any(o['replaced'] for o in orders): raise EvidenceError('management_order_proof_unavailable')
        entry=orders[0];qty=Decimal(entry['filled_quantity'])
        expected=Decimal(numeric(json.loads(row['request']).get('qty')))
        if (entry['role']!='entry' or entry['quantity'] is None or Decimal(entry['quantity'])!=expected or qty>expected
                or (entry['status']=='filled' and qty!=expected)
                or (not proof['legs_complete'] and (qty>0 or entry['status'] not in TERMINAL))):
            raise EvidenceError('management_order_proof_unavailable')
        residual=qty-sum((Decimal(o['filled_quantity']) for o in orders[1:]),Decimal(0))
        if residual<0: raise EvidenceError('management_order_proof_unavailable')
        if terminal and (row['closing'] or residual<=0 or not all(o['status'] in TERMINAL for o in orders)):
            raise EvidenceError('management_terminal_proof_unavailable')
        return row,h,proof,canonical(residual)

    def basis(self,day):
        row,h,_,_=self._proof(day)
        PaperAdmission(self.db).report()
        return digest(dict(attempt=dict(row),observation=(h['seq'],h['fingerprint'],h['last_seen_at']),
            meta=[tuple(r) for r in self.db.execute('SELECT * FROM paper_meta ORDER BY key')],
            holds=PaperEvidence(self.db).holds(),admission=PaperAdmission(self.db).commitment()))

    def _guard(self,day):
        self.report();PaperAdmission(self.db).report()
        if PaperEvidence(self.db).holds(): raise EvidenceError('management_correction_hold')

    def claim_cancel(self,day,targets,reason,at,expected_scope):
        if reason not in REASONS: raise EvidenceError('invalid_cancel_reason')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._guard(day)
            row,h,proof,_=self._proof(day,at)
            scope=self.db.execute("SELECT value FROM paper_meta WHERE key='account'").fetchone()
            if scope is None or scope[0]!=expected_scope:raise EvidenceError('management_account_scope_changed')
            if row['closing']: raise EvidenceError('management_closing_already_claimed')
            known={o['id']:o for o in proof['orders'] if o['status'] not in TERMINAL}
            selected=[];raw=[]
            for target in targets:
                key=identity(target)
                if key not in known or any(o['id']==key for o in selected): raise EvidenceError('cancel_target_not_owned')
                selected.append(dict(id=key,role=known[key]['role']));raw.append(target)
            prior={o['id'] for r in self.records() if r['session']==day and r['kind']=='cancel_intent' for o in r['targets']}
            new=[(s,r) for s,r in zip(selected,raw) if s['id'] not in prior]
            if not new: self.db.execute('COMMIT');return None
            value=dict(kind='cancel_intent',session=day,scope=expected_scope,reason=reason,targets=[s for s,_ in new],
                request_sha256=digest(json.loads(row['request'])),preset_sha256=digest(json.loads(row['preset'])),
                observation_seq=h['seq'],observation_sha256=h['fingerprint'])
            seq=self._append(day,at,value,slots=2)
            self.db.execute('COMMIT')
            return dict(seq=seq,targets=[r for _,r in new])
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise

    def claim_close(self,day,expected_basis,preflight,at):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._guard(day)
            row,h,proof,residual=self._proof(day,terminal=True)
            if self.basis(day)!=expected_basis: raise EvidenceError('management_basis_changed')
            if preflight['state']!='two_agreeing_inventory_passes':
                # Failed reads are evidence, never a closing authorisation.
                self._append(day,at,dict(kind='close_check',session=day,preflight=preflight,request_sha256=digest(json.loads(row['request'])),preset_sha256=digest(json.loads(row['preset']))))
                self.db.execute('COMMIT');return None
            self._proof(day,at,terminal=True)
            passes=preflight['passes']
            scope=self.db.execute("SELECT value FROM paper_meta WHERE key='account'").fetchone()
            if (len(passes)!=2 or scope is None or any(p['stable']['scope']!=scope[0] for p in passes)
                    or passes[0]['stable']!=passes[1]['stable']
                    or any(Decimal(p['stable']['holdings'].get('SPY','0'))!=Decimal(residual)
                           or any(o['symbol']=='SPY' for o in p['stable']['orders']) for p in passes)):
                raise EvidenceError('close_inventory_proof_invalid')
            now=datetime.fromisoformat(stamp(at))
            current_session=exchange_session(now.astimezone(NEW_YORK).date())
            if (not current_session or not current_session.opens_at<=now<current_session.closes_at
                    or any(p.get('market_open') is not True or abs(now-datetime.fromisoformat(stamp(p['broker_clock'])))>timedelta(seconds=30) for p in passes)):
                raise EvidenceError('close_clock_unavailable')
            times=[datetime.fromisoformat(stamp(p['observed_at'])) for p in passes]
            if not times[0]<=times[1]<=now or now-times[0]>timedelta(seconds=30): raise EvidenceError('close_inventory_proof_expired')
            request=dict(symbol='SPY',side='sell',qty=residual,type='market',time_in_force='day',client_order_id=row['client_id']+'-x')
            value=dict(kind='close_intent',session=day,scope=scope[0],quantity=residual,closing_sha256=digest(request),preflight=preflight,
                request_sha256=digest(json.loads(row['request'])),preset_sha256=digest(json.loads(row['preset'])),
                observation_seq=h['seq'],observation_sha256=h['fingerprint'],basis=expected_basis)
            seq=self._append(day,at,value,slots=2)
            self.db.execute('UPDATE paper_attempts SET closing=? WHERE day=?',(json.dumps(request),day))
            self.db.execute('COMMIT')
            return dict(seq=seq,request=request)
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise

    def dispatched(self,day,intent_seq,outcome,at,*,response=None,results=None):
        if outcome not in ('request_returned','outcome_unknown'):raise EvidenceError('invalid_management_outcome')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            records=self.records();intent=next((r for r in records if r['seq']==intent_seq and r['session']==day and r['kind'] in ('cancel_intent','close_intent')),None)
            if not intent:raise EvidenceError('management_intent_missing')
            if stamp(at)<intent['observed_at']:raise EvidenceError('management_clock_reversed')
            value=dict(kind='dispatch',session=day,intent_seq=intent_seq,outcome=outcome)
            if intent['kind']=='cancel_intent':
                if (not isinstance(results,list) or len(results)!=len(intent['targets'])
                        or any(not isinstance(r,dict) or r!={'id':target['id'],'outcome':r.get('outcome')} or r.get('outcome') not in ('request_returned','outcome_unknown','not_attempted') for r,target in zip(results,intent['targets']))):
                    raise EvidenceError('invalid_cancel_outcome')
                value['results']=results
            else:
                row=self.db.execute('SELECT closing FROM paper_attempts WHERE day=?',(day,)).fetchone()
                request=json.loads(row['closing'])
                normalized=None
                if outcome=='request_returned':
                    try:
                        if (not isinstance(response,dict) or response.get('client_order_id')!=request['client_order_id']
                                or numeric(response.get('qty'))!=numeric(request['qty'])):raise EvidenceError('invalid_close_response')
                        normalized=order(response,'closing_exit',stamp(at))
                    except (EvidenceError,TypeError,ValueError):pass
                value['response']=normalized
            prior=next((r for r in records if r['kind']=='dispatch' and r['intent_seq']==intent_seq),None)
            if prior:
                clean={k:v for k,v in prior.items() if k not in ('seq','observed_at')}
                if clean!=value:raise EvidenceError('management_dispatch_conflict')
                self.db.execute('COMMIT');return
            self._append(day,at,value)
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise

    def report(self):
        records=self.records();intents={};dispatches={};latest={}
        attempts={r['day']:r for r in self.db.execute('SELECT * FROM paper_attempts')}
        for r in records:
            row=attempts.get(r['session'])
            if not row:raise EvidenceError('management_attempt_missing')
            if r['kind']!='dispatch':
                if digest(json.loads(row['request']))!=r['request_sha256'] or digest(json.loads(row['preset']))!=r['preset_sha256']:
                    raise EvidenceError('management_intent_changed')
                if r['kind'] in ('cancel_intent','close_intent'):
                    scope=self.db.execute("SELECT value FROM paper_meta WHERE key='account'").fetchone()
                    if scope is None or scope[0]!=r['scope']:raise EvidenceError('management_account_scope_changed')
                    proof=self.db.execute('SELECT * FROM paper_observations WHERE seq=?',(r['observation_seq'],)).fetchone()
                    if not proof or proof['day']!=r['session'] or proof['fingerprint']!=r['observation_sha256']:raise EvidenceError('management_proof_changed')
                    verified(proof);intents[r['seq']]=r
                if r['kind']=='close_intent' and (not row['closing'] or digest(json.loads(row['closing']))!=r['closing_sha256']):
                    raise EvidenceError('management_closing_intent_changed')
            else:
                if r['intent_seq'] not in intents or intents[r['intent_seq']]['session']!=r['session'] or r['intent_seq'] in dispatches:
                    raise EvidenceError('management_dispatch_integrity_failed')
                dispatches[r['intent_seq']]=r
            latest[r['session']]=r
        rows=[]
        for day,r in sorted(latest.items(),reverse=True):
            close=next((v for v in intents.values() if v['session']==day and v['kind']=='close_intent'),None)
            canceled=[v for v in intents.values() if v['session']==day and v['kind']=='cancel_intent']
            receipt=dispatches.get(close['seq']) if close else (dispatches.get(canceled[-1]['seq']) if canceled else None)
            state=('terminal_reported' if attempts[day]['state']=='done' else
                   'closing_outcome_unknown' if close and (receipt is None or receipt['outcome']=='outcome_unknown') else
                   ('closing_response_observed' if receipt.get('response') is not None else 'closing_returned_unverified') if close else
                   r['preflight']['state'] if r['kind']=='close_check' else
                   'cancellation_outcome_unknown' if canceled and (receipt is None or receipt['outcome']=='outcome_unknown') else 'cancellation_requested_unconfirmed')
            rows.append(dict(session=day,state=state,quantity=close['quantity'] if close else None,observed_at=r['observed_at'],
                             cancel_target_count=sum(len(v['targets']) for v in canceled),receipt=receipt,
                             preflight=close['preflight'] if close else r.get('preflight')))
        legacy=[day for day,row in attempts.items() if row['closing'] and not any(v['kind']=='close_intent' and v['session']==day for v in intents.values())]
        return dict(schema_version='orb-paper-management-report-v1',attempts=rows[:100],attempts_truncated=len(rows)>100,
                    legacy_closing_sessions=sorted(legacy),
                    notice='Saved command claims and dispatch observations, not broker cancellation/fill confirmation. Unknown commands are never automatically resent. Reservations and all correction/loss gates remain; no cash reconciliation or recovery clearance.')

    def export(self,after=0,through=None):
        if type(after)is not int or after<0:raise EvidenceError('invalid_evidence_cursor')
        if through is None:through=self.db.execute('SELECT COALESCE(MAX(seq),0) FROM paper_management_log').fetchone()[0]
        if type(through)is not int or through<0:raise EvidenceError('invalid_evidence_cursor')
        rows=list(self.db.execute('SELECT * FROM paper_management_log WHERE seq>? AND seq<=? ORDER BY seq LIMIT 101',(after,through)))
        events=[dict(verified(r),seq=r['seq'],observed_at=r['observed_at']) for r in rows[:100]]
        return dict(schema_version='orb-paper-management-export-v1',events=events,through=through,
                    next_after=events[-1]['seq'] if events else after,has_more=len(rows)>100)
