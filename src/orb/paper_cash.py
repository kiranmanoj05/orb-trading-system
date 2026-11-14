"""Bounded account cash/quantity bridge; never execution or hold-clearance authority."""
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
import re
from urllib.parse import urlencode

from orb.paper_activities import identity
from orb.paper_evidence import EvidenceError, canonical, digest, encoded, numeric, stamp, verified
from orb.paper_fees import normalize as fee_normalize
from orb.paper_restart import collect as inventory_collect

TABLES={'paper_cash_log'}
PAGE_SIZE, MAX_PAGES, MAX_CHECKS=100,5,200
HOLD_REASONS={'cash_activity_changed','cash_activity_missing','cash_capacity_reached','invalid_cash_activity'}


def install(db):
    db.execute('CREATE TABLE paper_cash_log (seq INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)')
    for op in ('UPDATE','DELETE'):
        db.execute(f'CREATE TRIGGER paper_cash_log_no_{op.lower()} BEFORE {op} ON paper_cash_log '
                   "BEGIN SELECT RAISE(ABORT, 'paper cash evidence is append-only'); END")


def normalize(raw,known,until):
    if not isinstance(raw,dict):raise EvidenceError('invalid_cash_activity')
    key=identity(raw.get('id'));kind=raw.get('activity_type')
    if not isinstance(kind,str) or not re.fullmatch('[A-Z]{1,16}',kind):raise EvidenceError('invalid_cash_activity')
    if kind!='FILL':
        # Reuse validated signed cash/date fields, never pretend creation time is
        # a cash posting instant. Even a known net_amount has uncertain timing.
        data=fee_normalize(dict(raw,activity_type='FEE'),until)
        return dict(activity_id=key,activity_type=kind,signed_cash_amount=data['signed_cash_amount'],
                    activity_date=data['activity_date'],created_at=data['created_at'],currency=data['currency'],
                    status=data['status'],timing='posting_time_unavailable',attribution='account_only_unattributed')
    order=identity(raw.get('order_id'));symbol=raw.get('symbol')
    if not isinstance(symbol,str) or not re.fullmatch('[A-Z0-9./-]{1,32}',symbol):raise EvidenceError('invalid_cash_activity')
    side=raw.get('side');qty,price=numeric(raw.get('qty')),numeric(raw.get('price'))
    if side not in ('buy','sell') or raw.get('type') not in ('fill','partial_fill') or Decimal(qty)<=0 or Decimal(price)<=0:
        raise EvidenceError('invalid_cash_activity')
    at=stamp(raw.get('transaction_time'))
    if at>until:raise EvidenceError('future_cash_activity')
    # The API also reports options/crypto. Do not silently assume multiplier 1.
    supported=bool(re.fullmatch(r'[A-Z]{1,5}(\.[A-Z])?',symbol))
    owner=known.get(order)
    if owner and (owner['symbol']!=symbol or owner['side']!=side):raise EvidenceError('invalid_cash_activity')
    with localcontext() as ctx:
        ctx.prec=80
        gross=canonical(Decimal(qty)*Decimal(price)*(-1 if side=='buy' else 1)) if supported else None
    return dict(activity_id=key,activity_type='FILL',order_id=order,symbol=symbol,side=side,
                quantity=qty,price=price,transaction_time=at,gross_fill_cash_usd=gross,
                timing='execution_time',attribution='matched_durable_order' if owner else 'account_only_unattributed')


def event_fingerprint(event):
    return digest({k:v for k,v in event.items() if k!='attribution'})


def fetch(client,after,until,known,elapsed):
    events=[];seen={};params=dict(after=after,until=until,direction='asc',page_size=PAGE_SIZE)
    finished=False;failure=None;pages=0
    for _ in range(MAX_PAGES):
        try:
            if elapsed()>52:raise EvidenceError('cash_read_timeout')
            page=client.request('GET','/v2/account/activities?'+urlencode(params));pages+=1
            if not isinstance(page,list) or len(page)>PAGE_SIZE:raise EvidenceError('invalid_cash_page')
            for raw in page:
                event=normalize(raw,known,until);key=event['activity_id']
                if key in seen:
                    if digest(event)!=digest(seen[key]):
                        events.append(event);raise EvidenceError('cash_activity_changed')
                    raise EvidenceError('cash_pagination_repeated')
                seen[key]=event;events.append(event)
            if len(page)<PAGE_SIZE:finished=True;break
            params['page_token']=page[-1]['id']
        except EvidenceError as exc:
            failure=str(exc) if str(exc) in {'cash_read_timeout','invalid_cash_page','cash_activity_changed','cash_pagination_repeated','future_cash_activity'} else 'invalid_cash_activity'
            break
        except Exception:failure='cash_read_failed';break
    if not finished and failure is None:failure='cash_page_limit'
    return events,dict(after=after,until=until,scan_finished=finished,pages=pages,received=len(events),failure=failure)


def collect(client,scope,anchor,known,now,elapsed):
    snapshot,_=inventory_collect(client,[],scope,now,elapsed)
    passes=snapshot['passes'];valid=snapshot['state'] in ('flat_inventory_agrees','unattributed_inventory') and len(passes)==2
    if valid and passes[0]['observed_cash_usd']!=passes[1]['observed_cash_usd']:
        snapshot['state']='cash_changed_during_snapshot';valid=False
    result=dict(snapshot=snapshot,snapshot_valid=valid,started_at=stamp(now.isoformat()),events=[],scan=None)
    if not valid or anchor is None:return result
    opening=anchor['snapshot']['passes'][-1]['observed_at'];closing=passes[-1]['observed_at']
    gap=datetime.fromisoformat(closing)-datetime.fromisoformat(opening)
    if gap<=timedelta(0) or gap>timedelta(days=90):
        result['window_failure']='invalid_cash_window';return result
    # Creation-time lookback; never claimed to capture every late/backfilled event.
    after=(datetime.fromisoformat(anchor['started_at'])-timedelta(days=1)).isoformat()
    result['events'],result['scan']=fetch(client,after,closing,known,elapsed)
    return result


def bridge(anchor,current):
    if not current['snapshot_valid']:return dict(state='inventory_unavailable')
    if anchor is None:return dict(state='opening_anchor_saved')
    if current.get('window_failure'):return dict(state=current['window_failure'])
    first=anchor['snapshot']['passes'][-1];last=current['snapshot']['passes'][-1]
    opening_lo,opening_hi=anchor['started_at'],first['observed_at']
    closing_lo,closing_hi=current['started_at'],last['observed_at']
    with localcontext() as ctx:
        ctx.prec=80
        cash=Decimal(0);quantities={};uncertain=0;included=0;outside=0;nontrade=[]
        for event in current['events']:
            if event['activity_type']!='FILL':
                # Settlement dates and creation dates do not locate the cash effect
                # within our two snapshots. Retain every returned nontrade record.
                nontrade.append(event);uncertain+=1;continue
            at=event['transaction_time']
            if at<opening_lo:outside+=1;continue
            if at<=opening_hi or at>=closing_lo or event['gross_fill_cash_usd'] is None:
                uncertain+=1;continue
            included+=1;cash+=Decimal(event['gross_fill_cash_usd'])
            quantities[event['symbol']]=quantities.get(event['symbol'],Decimal(0))+Decimal(event['quantity'])*(-1 if event['side']=='sell' else 1)
        observed=Decimal(last['observed_cash_usd'])-Decimal(first['observed_cash_usd'])
        before,after=first['stable']['holdings'],last['stable']['holdings'];positions=[]
        for symbol in sorted(set(before)|set(after)|set(quantities)):
            delta=Decimal(after.get(symbol,'0'))-Decimal(before.get(symbol,'0'));expected=quantities.get(symbol,Decimal(0))
            positions.append(dict(symbol=symbol,observed_quantity_delta=canonical(delta),fill_quantity_delta=canonical(expected),residual_quantity=canonical(delta-expected)))
        scan=current['scan'];state='observed_deltas_agree'
        if not scan or not scan['scan_finished']:state='activity_scan_incomplete'
        elif uncertain:state='activity_timing_or_effect_unknown'
        elif observed!=cash or any(Decimal(p['residual_quantity']) for p in positions):state='unexplained_difference'
        eligible=bool(scan and scan['scan_finished'] and not uncertain)
        return dict(state=state,arithmetic_eligible=eligible,observed_cash_delta_usd=canonical(observed),modeled_gross_fill_cash_usd=canonical(cash),
                    observed_minus_included_fill_cash_usd=canonical(observed-cash),unexplained_cash_usd=canonical(observed-cash) if eligible else None,positions=positions,included_fills=included,
                    excluded_pre_anchor_fills=outside,uncertain_records=uncertain,nontrade_records=len(nontrade),
                    opening_at=opening_hi,closing_at=closing_hi)


class PaperCash:
    def __init__(self,db):self.db=db

    def hold(self):
        meta=dict(self.db.execute("SELECT key,value FROM paper_meta WHERE key IN ('cash-hold','cash-hold-at')"))
        if not meta:return None
        if set(meta)!={'cash-hold','cash-hold-at'} or meta['cash-hold'] not in HOLD_REASONS:raise EvidenceError('cash_hold_integrity_failed')
        return dict(day='account_cash_evidence',reason=meta['cash-hold'],observed_at=stamp(meta['cash-hold-at']))

    def commitment(self):
        rows=list(self.db.execute('SELECT * FROM paper_cash_log ORDER BY seq'))
        for row in rows:verified(row)
        return [(r['seq'],r['fingerprint']) for r in rows]

    def anchor(self):
        for row in self.db.execute('SELECT * FROM paper_cash_log ORDER BY seq'):
            value=verified(row)
            if value['anchor_created']:return dict(value['capture'],seq=row['seq'])
        return None

    def save(self,expected,capture,at):
        from orb.paper_restart import basis
        at=stamp(at)
        if at<capture['started_at']:raise EvidenceError('cash_clock_reversed')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if basis(self.db)!=expected:raise EvidenceError('cash_basis_changed')
            self.commitment();anchor=self.anchor()
            latest=self.db.execute('SELECT * FROM paper_cash_log ORDER BY seq DESC LIMIT 1').fetchone()
            if latest and verified(latest)['capture']['started_at']>capture['started_at']:raise EvidenceError('stale_cash_scan')
            def hold(reason):
                if not self.hold():self.db.executemany('INSERT INTO paper_meta VALUES (?,?)',[('cash-hold',reason),('cash-hold-at',at)])
            if self.db.execute('SELECT COUNT(*) FROM paper_cash_log').fetchone()[0]>=MAX_CHECKS:
                hold('cash_capacity_reached');self.db.execute('COMMIT');return
            scope=capture['snapshot']['scope'];bound=self.db.execute("SELECT value FROM paper_meta WHERE key='account'").fetchone()
            if bound and scope!=bound[0]:raise EvidenceError('cash_scope_changed')
            if capture['snapshot_valid'] and scope is not None:
                if not bound and self.db.execute('SELECT 1 FROM paper_attempts').fetchone():raise EvidenceError('cash_saved_scope_missing')
                self.db.execute("INSERT OR IGNORE INTO paper_meta VALUES ('account',?)",(scope,))
            scan=capture['scan']
            if scan and scan['failure'] in ('invalid_cash_activity','cash_activity_changed','future_cash_activity'):
                hold('invalid_cash_activity' if scan['failure']!='cash_activity_changed' else 'cash_activity_changed')
            current={e['activity_id']:event_fingerprint(e) for e in capture['events']}
            # Compare with all retained versions, including before interrupted scans.
            for old in self.db.execute('SELECT * FROM paper_cash_log ORDER BY seq'):
                previous=verified(old)['capture']
                for event in previous['events']:
                    key=event['activity_id']
                    if key in current and event_fingerprint(event)!=current[key]:hold('cash_activity_changed')
                if (scan and scan['scan_finished'] and previous['scan'] and previous['scan']['scan_finished']
                        and previous['scan']['after']==scan['after'] and previous['scan']['until']<=scan['until']
                        and any(e['activity_id'] not in current for e in previous['events'])):hold('cash_activity_missing')
            result=bridge(anchor,capture)
            expired=datetime.fromisoformat(at)-datetime.fromisoformat(capture['started_at'])>timedelta(seconds=60)
            if expired:
                result=dict(result,state='cash_capture_expired',arithmetic_eligible=False,unexplained_cash_usd=None)
            if self.hold():result=dict(result,state='correction_hold',arithmetic_eligible=False,unexplained_cash_usd=None)
            receipt=dict(schema_version='orb-paper-cash-check-v1',anchor_seq=anchor['seq'] if anchor else None,
                         anchor_created=anchor is None and capture['snapshot_valid'] and not expired,
                         basis_before=expected,capture=capture,result=result,attribution='account_wide_not_strategy_profit',
                         recovery_cleared=False,settled_cash_verified=False,fee_coverage_verified=False,net_pnl_usd=None)
            self.db.execute('INSERT INTO paper_cash_log(observed_at,fingerprint,payload) VALUES (?,?,?)',(at,digest(receipt),encoded(receipt)))
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise

    def report(self):
        self.commitment();row=self.db.execute('SELECT * FROM paper_cash_log ORDER BY seq DESC LIMIT 1').fetchone()
        latest=dict(verified(row),seq=row['seq'],observed_at=row['observed_at']) if row else None
        if latest:
            capture=latest['capture'];latest=dict(latest,capture=dict(capture,events=capture['events'][:100],events_truncated=len(capture['events'])>100))
        return dict(schema_version='orb-paper-cash-report-v1',latest=latest,anchor_seq=(self.anchor() or {}).get('seq'),hold=self.hold(),
                    notice='Account-wide cash/quantity arithmetic only. Creation-time scans and dated postings cannot prove completeness or settled cash. No net strategy profit, hold clearance, worker start or order authority.')

    def export(self,after=0,through=None):
        if type(after)is not int or after<0:raise EvidenceError('invalid_evidence_cursor')
        if through is None:through=self.db.execute('SELECT COALESCE(MAX(seq),0) FROM paper_cash_log').fetchone()[0]
        if type(through)is not int or through<0:raise EvidenceError('invalid_evidence_cursor')
        rows=list(self.db.execute('SELECT * FROM paper_cash_log WHERE seq>? AND seq<=? ORDER BY seq LIMIT 101',(after,through)))
        events=[dict(verified(r),seq=r['seq'],observed_at=r['observed_at']) for r in rows[:100]]
        return dict(schema_version='orb-paper-cash-export-v1',events=events,through=through,next_after=events[-1]['seq'] if events else after,has_more=len(rows)>100)
