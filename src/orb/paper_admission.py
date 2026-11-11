"""Atomic native-paper risk checkpoints, intent claims and terminal releases.

Reservations are planned headroom, not cash debits, fills or reconciled balances.
"""
from datetime import date, datetime
from decimal import Decimal, ROUND_UP, localcontext
import json

from orb.paper_evidence import EvidenceError, PaperEvidence, TERMINAL, canonical, digest, encoded, numeric, stamp, verified
from orb.research_settings import ResearchSettings
from orb.risk import AccountSnapshot, size_long
from orb.session import exchange_session
from orb.strategy import NEW_YORK

TABLES = {'paper_admission_log'}
MAX_EVENTS = 12000


def install(db):
    db.execute('CREATE TABLE paper_admission_log (seq INTEGER PRIMARY KEY, day TEXT NOT NULL REFERENCES paper_attempts(day), '
               'observed_at TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE INDEX paper_admission_log_day ON paper_admission_log(day,seq)')
    for op in ('UPDATE','DELETE'):
        db.execute(f'CREATE TRIGGER paper_admission_log_no_{op.lower()} BEFORE {op} ON paper_admission_log '
                   "BEGIN SELECT RAISE(ABORT, 'paper admission is append-only'); END")


def number(value):
    return Decimal(numeric(str(value)))


class PaperAdmission:
    def __init__(self, db): self.db = db

    def _meta(self, key, value=None):
        if value is not None: self.db.execute('INSERT OR REPLACE INTO paper_meta VALUES (?,?)',(key,str(value)))
        row = self.db.execute('SELECT value FROM paper_meta WHERE key=?',(key,)).fetchone()
        return row[0] if row else None

    def _scope(self, scope):
        saved = self._meta('account')
        if saved is not None and saved != scope: raise EvidenceError('admission_account_scope_changed')
        if saved is None and self.db.execute('SELECT 1 FROM paper_attempts').fetchone():
            raise EvidenceError('admission_missing_account_scope')
        self._meta('account',scope)

    def _loss(self, day, settings, equity):
        equity = number(equity)
        anchor, budget = self._meta('equity:'+day), self._meta('loss-budget:'+day)
        if (anchor is None) != (budget is None): raise EvidenceError('admission_incomplete_loss_checkpoint')
        anchor = number(anchor) if anchor is not None else equity
        if anchor <= 0 or equity <= 0: raise EvidenceError('admission_invalid_equity')
        requested = min(anchor, settings.initial_cash) * settings.daily_loss_fraction
        budget = min(number(budget), requested) if budget is not None else requested
        if budget <= 0: raise EvidenceError('admission_invalid_loss_budget')
        self._meta('equity:'+day,anchor)
        self._meta('loss-budget:'+day,budget)
        if equity <= anchor-budget: self._meta('loss-blocked:'+day,'yes')
        return dict(anchor_equity_usd=str(anchor),loss_budget_usd=str(budget),
                    daily_loss_blocked=self._meta('loss-blocked:'+day) is not None)

    def checkpoint(self, day, settings, equity, scope):
        """Commit anchor, tightening and irreversible loss latch as one checkpoint."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._scope(scope)
            result = self._loss(day,settings,equity)
            self.db.execute('COMMIT')
            return result
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise

    def records(self):
        result = []
        for row in self.db.execute('SELECT * FROM paper_admission_log ORDER BY seq'):
            value = verified(row)
            if value.get('kind') not in ('reserved','terminal_release') or value.get('session') != row['day']:
                raise EvidenceError('admission_integrity_failed')
            result.append(dict(value,seq=row['seq'],observed_at=row['observed_at']))
        return result

    def commitment(self):
        self.records()
        return [(r['seq'],r['fingerprint']) for r in self.db.execute('SELECT seq,fingerprint FROM paper_admission_log ORDER BY seq')]

    def _append(self, day, at, value):
        if self.db.execute('SELECT COUNT(*) FROM paper_admission_log').fetchone()[0] >= MAX_EVENTS - (1 if value['kind']=='reserved' else 0):
            raise EvidenceError('admission_capacity_reached')
        self.db.execute('INSERT INTO paper_admission_log(day,observed_at,fingerprint,payload) VALUES (?,?,?,?)',
                        (day,stamp(at),digest(value),encoded(value)))

    def reserve(self, day, client_id, request, preset, settings, scope, equity, cash, at, feed):
        """Atomically recheck gates/limits, freeze headroom and claim the attempt.

        No automatic retry: even an uncertain/failed POST keeps this reservation.
        """
        at = stamp(at)
        applied = ResearchSettings.from_dict(preset['settings'])
        if applied.fingerprint != settings.fingerprint or preset.get('settings_sha256') != applied.fingerprint:
            raise EvidenceError('admission_settings_mismatch')
        if settings.max_session_entries != 1 or settings.max_concurrent_positions != 1 or not settings.policy.uses_reference_window:
            raise EvidenceError('admission_unsupported_paper_policy')
        session = exchange_session(date.fromisoformat(day))
        when = datetime.fromisoformat(at)
        if (not session or str(when.astimezone(NEW_YORK).date()) != day or not session.range_end <= when < session.entry_cutoff
                or session.day.weekday() not in settings.weekdays or feed not in ('iex','sip')):
            raise EvidenceError('admission_ineligible_session')
        entry = number(request.get('limit_price'))
        stop = number(request.get('stop_loss',{}).get('stop_price'))
        qty = number(request.get('qty'))
        target = (entry+(entry-stop)*settings.target_r).quantize(Decimal('.01'),rounding=ROUND_UP)
        if (qty <= 0 or qty != qty.to_integral_value() or entry <= stop or stop <= 0
                or request != dict(symbol='SPY',qty=str(request['qty']),side='buy',type='limit',limit_price=request['limit_price'],
                    time_in_force='gtc',extended_hours=False,order_class='bracket',client_order_id=client_id,
                    take_profit={'limit_price':str(target)},stop_loss={'stop_price':request['stop_loss']['stop_price']})):
            raise EvidenceError('admission_invalid_order_terms')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._scope(scope)
            # All mutable risk gates are rechecked under the same database lock
            # as the intent INSERT, not trusted from earlier network-time checks.
            self.report()
            if PaperEvidence(self.db).holds(): raise EvidenceError('admission_correction_hold')
            loss = self._loss(day,settings,equity)
            if loss['daily_loss_blocked']:
                self.db.execute('COMMIT')
                return dict(accepted=False,reason='daily_loss_blocked')
            if self.db.execute('SELECT 1 FROM paper_attempts WHERE day=?',(day,)).fetchone():
                raise EvidenceError('admission_session_cap')
            if self.db.execute("SELECT 1 FROM paper_attempts WHERE state!='done'").fetchone():
                raise EvidenceError('admission_pending_account_attempt')
            allocation = min(settings.initial_cash,number(equity),number(cash))
            if allocation <= 0: raise EvidenceError('admission_no_cash')
            sized = size_long(AccountSnapshot(allocation,allocation),entry,stop,settings.engine_kwargs()['limits'],
                              slippage_bps=settings.slippage_bps,fee_per_share=settings.fee_per_share)
            if qty > sized.quantity: raise EvidenceError('admission_sizing_cap')
            # Verify whole-order headroom independently of the preflight sizing.
            buy = entry * (1 + settings.slippage_bps / Decimal(10000))
            value = dict(kind='reserved',session=day,scope=scope,request_sha256=digest(request),preset_sha256=digest(preset),
                preset_id=preset['id'],preset_name=preset['name'],preset_version=preset['version'],
                settings_sha256=applied.fingerprint,settings=applied.to_dict(),feed=feed,quantity=canonical(qty),
                entry_limit_usd=canonical(entry),stop_usd=canonical(stop),target_usd=str(target),
                allocation_usd=canonical(allocation),observed_equity_usd=canonical(number(equity)),observed_cash_usd=canonical(number(cash)),
                cash_headroom_usd=canonical(qty*(buy+settings.fee_per_share)),exposure_headroom_usd=canonical(qty*buy),
                planned_stop_risk_usd=canonical(qty*sized.risk_per_share),risk_budget_usd=canonical(sized.risk_budget),
                sizing_reason=sized.reason,**loss)
            self.db.execute('INSERT INTO paper_attempts VALUES (?,?,?,?,?,?)',
                            (day,client_id,json.dumps(request),json.dumps(preset),None,'reserved'))
            self._append(day,at,value)
            self.db.execute('COMMIT')
            return dict(accepted=True,reason='reserved')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise

    def finish(self, day, at):
        """Release planned headroom only with retained terminal quantity evidence.

        This neither clears holds nor posts realised cash/P&L to any account.
        """
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.report()
            if PaperEvidence(self.db).holds(): raise EvidenceError('admission_correction_hold')
            row = self.db.execute('SELECT * FROM paper_attempts WHERE day=?',(day,)).fetchone()
            if not row: raise EvidenceError('admission_missing_attempt')
            if row['state']=='done': self.db.execute('COMMIT');return
            h = self.db.execute('SELECT o.* FROM paper_observations o JOIN paper_observation_heads h ON h.seq=o.seq WHERE h.day=?',(day,)).fetchone()
            proof = verified(h) if h else None
            orders = proof['orders'] if proof else []
            if (not orders or h['day'] != day or any(v['status'] not in TERMINAL or v['replaced'] for v in orders)
                    or (proof['closing_expected'] and not any(v['role']=='closing_exit' for v in orders))
                    or (Decimal(orders[0]['filled_quantity'])>0 and not proof['legs_complete'])
                    or (orders[0]['status']=='filled' and orders[0]['quantity']!=orders[0]['filled_quantity'])
                    or Decimal(orders[0]['filled_quantity']) != sum((Decimal(v['filled_quantity']) for v in orders[1:]),Decimal(0))):
                raise EvidenceError('admission_terminal_proof_unavailable')
            reservation = next((v for v in self.records() if v['session']==day and v['kind']=='reserved'),None)
            if stamp(at) < h['observed_at'] or (reservation and stamp(at) < reservation['observed_at']):
                raise EvidenceError('admission_release_clock_reversed')
            value = dict(kind='terminal_release',session=day,reservation_seq=reservation['seq'] if reservation else None,
                         legacy_untracked=reservation is None,observation_seq=h['seq'],observation_sha256=h['fingerprint'],
                         request_sha256=digest(json.loads(row['request'])),preset_sha256=digest(json.loads(row['preset'])))
            self._append(day,at,value)
            self.db.execute("UPDATE paper_attempts SET state='done' WHERE day=?",(day,))
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            raise

    def report(self):
        records = self.records()
        reserved, released = {}, set()
        attempts = {r['day']:r for r in self.db.execute('SELECT * FROM paper_attempts')}
        for r in records:
            row = attempts.get(r['session'])
            if row is None or digest(json.loads(row['request'])) != r['request_sha256'] or digest(json.loads(row['preset'])) != r['preset_sha256']:
                raise EvidenceError('admission_intent_changed')
            if r['kind']=='reserved':
                if r['session'] in reserved: raise EvidenceError('admission_duplicate_reservation')
                if r['scope'] != self._meta('account'): raise EvidenceError('admission_account_scope_changed')
                reserved[r['session']] = r
            else:
                if r['session'] in released or row['state']!='done': raise EvidenceError('admission_release_state_mismatch')
                original = reserved.get(r['session'])
                if r['reservation_seq'] != (original['seq'] if original else None): raise EvidenceError('admission_release_state_mismatch')
                proof = self.db.execute('SELECT * FROM paper_observations WHERE seq=?',(r['observation_seq'],)).fetchone()
                if not proof or proof['day']!=r['session'] or proof['fingerprint']!=r['observation_sha256']:
                    raise EvidenceError('admission_release_proof_changed')
                verified(proof)
                released.add(r['session'])
        active = []
        for day,r in reserved.items():
            if day not in released:
                if attempts[day]['state']=='done': raise EvidenceError('admission_unreleased_done_attempt')
                active.append(r)
        legacy = [d for d,r in attempts.items() if d not in reserved and r['state']!='done']
        def total(key):
            with localcontext() as context:
                context.prec=80
                return None if legacy else canonical(sum((Decimal(r[key]) for r in active),Decimal(0)))
        days = sorted(k[7:] for k, in self.db.execute("SELECT key FROM paper_meta WHERE key LIKE 'equity:%'"))
        day = days[-1] if days else None
        return dict(schema_version='orb-paper-admission-report-v1',active_reservations=active,legacy_pending_sessions=legacy,
            active_count=len(active)+len(legacy),cash_headroom_usd=total('cash_headroom_usd'),
            exposure_headroom_usd=total('exposure_headroom_usd'),planned_stop_risk_usd=total('planned_stop_risk_usd'),
            session=day,session_attempt_count=int(day in attempts) if day else 0,
            anchor_equity_usd=self._meta('equity:'+day) if day else None,loss_budget_usd=self._meta('loss-budget:'+day) if day else None,
            daily_loss_blocked=self._meta('loss-blocked:'+day) is not None if day else False,
            legacy_completed_count=sum(1 for d,r in attempts.items() if d not in reserved and r['state']=='done'),
            notice='Planned admission headroom, not broker cash, fill inventory or reconciled exposure. Partial fills retain full headroom until terminal quantity evidence; uncertain submissions are never automatically resent. Fees/net P&L and account recovery clearance remain unavailable.')

    def export(self, after=0, through=None):
        if type(after) is not int or after<0: raise EvidenceError('invalid_evidence_cursor')
        if through is None: through=self.db.execute('SELECT COALESCE(MAX(seq),0) FROM paper_admission_log').fetchone()[0]
        if type(through) is not int or through<0: raise EvidenceError('invalid_evidence_cursor')
        rows=list(self.db.execute('SELECT * FROM paper_admission_log WHERE seq>? AND seq<=? ORDER BY seq LIMIT 101',(after,through)))
        events=[dict(verified(r),seq=r['seq'],observed_at=r['observed_at']) for r in rows[:100]]
        return dict(schema_version='orb-paper-admission-export-v1',events=events,through=through,
                    next_after=events[-1]['seq'] if events else after,has_more=len(rows)>100)
