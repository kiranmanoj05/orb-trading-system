"""Read-only account fees: signed observations, scope, atomicity and no net claims."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
import sqlite3
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.paper_evidence import EvidenceError, digest
from orb.paper_fees import PaperFees, fetch, normalize, window
from orb.paper_trading import PaperEngine, PaperError
from orb.presets import PresetStore
from test_paper_trading import Broker, NOW

DAY = str(NOW.date())
AT = NOW.isoformat()
SCOPE = sha256(b'fixture-paper-account').hexdigest()


def fee(**changes):
    return dict(dict(id='private-fee-1', activity_type='FEE', net_amount='-1.25', currency='USD',
                     date=DAY, created_at=AT, status='executed', description='PRIVATE SECRET',
                     account_id='PRIVATE ACCOUNT', order_id='UNVERIFIED ORDER'), **changes)


class FeesBroker(Broker):
    def __init__(self):
        super().__init__(); self.account['currency']='USD'; self.pages=[[fee()]]; self.reads=[]; self.hook=None
    def request(self, method, path, payload=None, **kw):
        assert method=='GET', 'fee observation must never mutate broker'
        self.reads.append(path)
        if path.startswith('/v2/account/activities/FEE?'):
            if self.hook: self.hook(); self.hook=None
            page=self.pages.pop(0)
            if isinstance(page,Exception): raise page
            return deepcopy(page)
        return super().request(method,path,payload,**kw)


def setup(tmp_path):
    path=tmp_path/'paper.sqlite'; b=FeesBroker(); e=PaperEngine(path,b,review_clock=lambda:NOW)
    return e,PaperFees(e.db),b,path


def save(j, rows, *, finished=True, failure=None, at=AT, until=AT, day=DAY):
    after,_=window(day,NOW)
    j.save(day,[normalize(r,until) for r in rows],dict(after=after,until=until,scan_finished=finished,
        failure=failure,pages=1,received=len(rows)),at,SCOPE)


def test_signed_cash_is_unattributed_durable_private_and_does_not_submit(tmp_path):
    e,j,b,path=setup(tmp_path)
    b.pages=[[fee(),fee(id='credit',net_amount='0.25')]]
    report=e.refresh_fees(DAY); f=report['account_fees']
    assert f['observed_signed_usd_cash']=='-1' and f['state']=='observed_usd_cash_only'
    assert f['trade_fees_usd'] is f['trade_net_pnl_usd'] is None and not f['fee_coverage_verified']
    assert e.meta('account')==SCOPE and not report['attempts'] and not e.enabled
    text=json.dumps(j.export())
    for secret in ('PRIVATE','private-fee-1','UNVERIFIED','fixture-paper-account'): assert secret not in text
    assert f['events'][0]['first_observed_at']==AT and f['events'][0]['activity_date']==DAY
    assert len(b.reads)==2 and not b.posts and not b.deletes
    e.close();e=PaperEngine(path);assert e.evidence.report()['account_fees']==f;e.close()


@pytest.mark.parametrize('change', [dict(net_amount=None),dict(currency=None),dict(currency='EUR'),dict(status=None),dict(status='new_provider_status')])
def test_unknowns_do_not_become_zero_or_usd(tmp_path,change):
    e,j,_,_=setup(tmp_path);save(j,[fee(**change)])
    f=j.report();assert f['state']=='amount_currency_or_status_unavailable' and f['observed_signed_usd_cash'] is None
    assert not f['hold'];e.close()


@pytest.mark.parametrize('amount', ['NaN','Infinity','1e999999999','1e30','1e-30','1'*41,1.25,True,'garbage'])
def test_amount_validation_is_bounded_and_finite(amount):
    with pytest.raises(EvidenceError,match='invalid_fee_amount'):normalize(fee(net_amount=amount),AT)


@pytest.mark.parametrize('change', [dict(currency='usd'),dict(currency='PRIVATE'),dict(date='20261102'),dict(created_at='2026-11-02'),
    dict(created_at=(NOW+timedelta(seconds=1)).isoformat()),dict(activity_type='FILL'),dict(id='')])
def test_malformed_rows_never_expose_provider_data(change):
    b=FeesBroker();b.pages=[[fee(**change)]];after,until=window(DAY,NOW)
    events,scan=fetch(b,after,until)
    assert not events and not scan['scan_finished'] and scan['failure']
    assert 'PRIVATE' not in json.dumps(scan)


def test_empty_finished_read_is_not_actual_zero_fees(tmp_path):
    e,j,b,_=setup(tmp_path);b.pages=[[]];e.refresh_fees(DAY)
    assert j.report()['state']=='no_fee_records_observed' and j.report()['observed_signed_usd_cash'] is None
    assert j.report()['scan']['scan_finished'];e.close()


def test_offsetting_known_records_can_have_zero_observed_cash(tmp_path):
    e,j,_,_=setup(tmp_path);save(j,[fee(),fee(id='credit',net_amount='1.25')])
    assert j.report()['observed_signed_usd_cash']=='0' and j.report()['event_count']==2;e.close()


def test_query_uses_frozen_bounds_and_cursor_but_does_not_persist_raw_id():
    b=FeesBroker();b.pages=[[fee(id=f'private-{i}') for i in range(100)],[fee(id='last')]]
    after,until=window(DAY,NOW);events,scan=fetch(b,after,until)
    assert scan['scan_finished'] and scan['pages']==2 and len(events)==101
    q=[parse_qs(urlsplit(p).query) for p in b.reads]
    assert q[0]['after']==[after] and q[1]['until']==[until] and q[1]['page_token']==['private-99']
    assert 'private' not in json.dumps(events)


def test_failed_second_page_keeps_partial_evidence_and_sanitized_receipt(tmp_path):
    e,j,b,_=setup(tmp_path)
    b.pages=[[fee(id=str(i)) for i in range(100)],RuntimeError('SECRET credentials')]
    e.refresh_fees(DAY);f=j.report()
    assert f['state']=='scan_incomplete' and f['event_count']==100 and f['observed_signed_usd_cash'] is None
    assert f['scan']['failure']=='fee_read_failed' and not f['hold'] and 'SECRET' not in json.dumps(f);e.close()


def test_page_bound_never_claims_completion():
    b=FeesBroker();b.pages=[[fee(id=str(p*100+i)) for i in range(100)] for p in range(10)]
    events,scan=fetch(b,*window(DAY,NOW))
    assert len(events)==1000 and scan['failure']=='fee_page_limit' and not scan['scan_finished'] and len(b.reads)==10


@pytest.mark.parametrize('change', [dict(net_amount='-2'),dict(currency=None),dict(date='2026-11-01'),dict(status='pending'),dict(created_at=None)])
def test_revisions_are_retained_and_hold_is_sticky_across_restart(tmp_path,change):
    e,j,_,path=setup(tmp_path);save(j,[fee()]);save(j,[fee(**change)])
    assert j.report()['hold']['reason']=='fee_record_changed' and j.report()['observed_signed_usd_cash'] is None
    save(j,[fee()]);assert len([v for v in j.export()['events'] if v['kind']=='fee'])==2
    e.close();e=PaperEngine(path);assert e.evidence.holds()[0]['day']=='account_fee_evidence'
    with pytest.raises(PaperError,match='correction hold'):e.start(PresetStore.paper('paper-balanced'))
    assert e.thread is None;e.close()


def test_pending_and_missing_details_can_complete_without_erasing_versions(tmp_path):
    e,j,_,_=setup(tmp_path);save(j,[fee(net_amount=None,currency=None,status='pending',created_at=None)])
    save(j,[fee()]);assert not j.hold() and j.report()['observed_signed_usd_cash']=='-1.25'
    assert len([r for r in j.export()['events'] if r['kind']=='fee'])==2;e.close()


def test_disappearance_after_failed_read_compares_last_finished_scan(tmp_path):
    e,j,_,_=setup(tmp_path);save(j,[fee()]);save(j,[],finished=False,failure='fee_read_failed');save(j,[])
    assert j.hold()['reason']=='fee_record_missing';e.close()


def test_contradictory_duplicate_retains_both_and_blocks(tmp_path):
    e,j,b,_=setup(tmp_path);b.pages=[[fee(),fee(net_amount='-9')]];e.refresh_fees(DAY)
    assert j.report()['scan']['failure']=='fee_revision_in_scan' and j.hold()
    assert len([r for r in j.export()['events'] if r['kind']=='fee'])==2;e.close()


def test_repeated_pagination_is_not_silently_deduplicated(tmp_path):
    e,j,b,_=setup(tmp_path);b.pages=[[fee(),fee()]];e.refresh_fees(DAY)
    assert j.report()['scan']['failure']=='fee_pagination_repeated' and j.hold();e.close()


@pytest.mark.parametrize('day', ['2026-11-03','2026-01-01','20261102','bad'])
def test_window_refuses_future_old_or_noncanonical_dates_before_network(tmp_path,day):
    e,j,b,_=setup(tmp_path)
    with pytest.raises(PaperError,match='90 days'):e.refresh_fees(day)
    assert not b.reads and not j.export()['events'];e.close()


def test_weekend_posting_date_is_not_rolled_to_trading_session():
    after,_=window('2026-11-01',NOW)
    assert after=='2026-10-31T04:00:00+00:00'  # DST ends after midnight Nov 1.


@pytest.mark.parametrize('case', ['mismatch','currency','missing_id','unbound_attempt'])
def test_scope_failures_never_fetch_fee_history(tmp_path,case):
    e,j,b,_=setup(tmp_path)
    if case=='mismatch':e.meta('account','other-scope')
    if case=='currency':b.account.pop('currency')
    if case=='missing_id':b.account.pop('id')
    if case=='unbound_attempt':
        from test_paper_evidence import seed
        seed(e);e.db.execute("DELETE FROM paper_meta WHERE key='account'")
    with pytest.raises(PaperError):e.refresh_fees(DAY)
    assert not any('/activities/' in p for p in b.reads) and not j.export()['events'];e.close()


def test_account_scope_changed_during_unlocked_read_refuses_commit(tmp_path):
    e,j,b,_=setup(tmp_path)
    def change():
        from concurrent.futures import ThreadPoolExecutor
        def worker():
            with e.lock: return e.db.execute('SELECT COUNT(*) FROM paper_attempts').fetchone()[0]
        with ThreadPoolExecutor() as pool: assert pool.submit(worker).result(timeout=1)==0
        e.meta('account','other-scope')
    b.hook=change
    with pytest.raises(PaperError,match='scope changed'):e.refresh_fees(DAY)
    assert not j.export()['events'];e.close()


def test_capacity_keeps_receipt_and_global_hold(tmp_path,monkeypatch):
    e,j,_,_=setup(tmp_path);monkeypatch.setattr('orb.paper_fees.MAX_EVENTS',3)
    save(j,[fee(id=str(i)) for i in range(4)])
    assert len(j.export()['events'])==3 and j.hold()['reason']=='fee_capacity_reached'
    assert not j.report()['scan']['scan_finished'];e.close()


def test_stale_scan_refused_and_clock_reversal_held(tmp_path):
    e,j,_,_=setup(tmp_path);save(j,[fee()])
    with pytest.raises(EvidenceError,match='stale_fee_scan'):
        save(j,[],until=(NOW-timedelta(seconds=1)).isoformat())
    assert not j.hold()
    save(j,[fee()],at=(NOW-timedelta(seconds=1)).isoformat())
    assert j.hold()['reason']=='fee_clock_reversed';e.close()


def test_fixed_export_watermark_and_append_only_integrity(tmp_path):
    e,j,_,_=setup(tmp_path);save(j,[fee(id=str(i)) for i in range(105)])
    first=j.export();assert first['has_more'] and len(first['events'])==100
    save(j,[fee(id='new')],until=(NOW+timedelta(seconds=1)).isoformat())
    rest=j.export(first['next_after'],first['through']);assert len(rest['events'])==6 and not rest['has_more']
    with pytest.raises(sqlite3.IntegrityError,match='append-only'):e.db.execute('DELETE FROM paper_fee_log')
    e.db.execute('DROP TRIGGER paper_fee_log_no_update');e.db.execute("UPDATE paper_fee_log SET payload='{}' WHERE seq=1")
    with pytest.raises(EvidenceError,match='integrity'):j.report()
    with pytest.raises(EvidenceError,match='integrity'):j.export();e.close()


def test_new_fee_evidence_supersedes_review_basis_even_without_hold(tmp_path):
    from test_paper_reviews import setup as review_setup, latest
    e,_,_=review_setup(tmp_path);e.prepare_review(DAY);assert not latest(e)['stale']
    save(PaperFees(e.db),[fee()]);assert latest(e)['stale']
    assert not PaperFees(e.db).hold();e.close()


def test_v4_migration_is_atomic_preserves_intents_latches_and_requires_worker_stop(tmp_path,monkeypatch):
    e,_,_,path=setup(tmp_path);e.meta('loss-blocked:'+DAY,'yes');e.db.execute('DROP TABLE paper_management_log');e.db.execute('DROP TABLE paper_admission_log');e.db.execute('DROP TABLE paper_fee_log');e.db.execute('PRAGMA user_version=4');e.close()
    import orb.paper_trading as trading
    original=trading.install_fees
    def fail(db):original(db);raise RuntimeError('interrupted')
    monkeypatch.setattr(trading,'install_fees',fail)
    with pytest.raises(RuntimeError):PaperEngine(path)
    db=sqlite3.connect(path);assert db.execute('PRAGMA user_version').fetchone()[0]==4
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='paper_fee_log'").fetchone();db.close()
    monkeypatch.setattr(trading,'install_fees',original)
    e=PaperEngine(path);assert e.db.execute('PRAGMA user_version').fetchone()[0]==7
    assert e.meta('loss-blocked:'+DAY)=='yes' and not e.enabled;e.close()


def test_event_and_receipt_and_initial_scope_rollback_on_actual_process_exit(tmp_path):
    e,j,_,path=setup(tmp_path);e.close()
    code='''import os,sqlite3,sys
from orb.paper_fees import PaperFees
from orb.paper_evidence import digest
path,day,at,scope=sys.argv[1:]
db=sqlite3.connect(path,isolation_level=None);db.row_factory=sqlite3.Row
# Crash after event INSERT, before receipt INSERT/COMMIT.
def crash(sql):
 if "INSERT INTO paper_fee_log" in sql and "'scan'" in sql:os._exit(81)
db.set_trace_callback(crash)
PaperFees(db).save(day,[dict(activity_id='hashed',signed_cash_amount='-1',currency='USD',activity_date=day,created_at=at,status='executed')],dict(after=at,until=at,scan_finished=True,failure=None,pages=1,received=1),at,scope)
'''
    result=subprocess.run([sys.executable,'-c',code,str(path),DAY,AT,SCOPE],capture_output=True)
    assert result.returncode==81,result.stderr
    e=PaperEngine(path);assert e.meta('account') is None and not PaperFees(e.db).export()['events'];e.close()


def test_api_exact_body_origin_bounds_exports_and_no_secret_errors(tmp_path):
    e,j,b,path=setup(tmp_path);e.close()
    with TestClient(create_app(paper_db=path),base_url='http://127.0.0.1:8765') as c:
        e=c.app.state.paper_engine;e.client=b;e.review_clock=lambda:NOW
        assert c.post('/api/paper/fees/refresh',json={'session':DAY,'clear':True}).status_code==422
        assert c.post('/api/paper/fees/refresh',json={'session':DAY},headers={'Origin':'https://evil.invalid'}).status_code==403
        assert c.post('/api/paper/fees/refresh',json={'session':'2026-01-01'}).status_code==409
        assert c.get('/api/paper/fees?after=-1').status_code==422
        r=c.post('/api/paper/fees/refresh',json={'session':DAY});assert r.status_code==200
        assert r.json()['account_fees']['observed_signed_usd_cash']=='-1.25'
        data=c.get('/api/paper/fees').json();assert [v['kind'] for v in data['events']]==['fee','scan']
        assert 'PRIVATE' not in json.dumps(data)
    e.close()
