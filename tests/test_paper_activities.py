"""FILL activity fixtures only; never contact a provider or submit orders."""
from copy import deepcopy
from datetime import date, timedelta
from hashlib import sha256
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.paper_activities import PaperActivities, fetch, normalize, identity
from orb.paper_evidence import EvidenceError
from orb.paper_trading import PaperEngine, PaperError
from orb.presets import PresetStore
from test_paper_evidence import seed, capture, closed, DAY
from test_paper_trading import NOW, Broker

AT = (NOW + timedelta(minutes=20)).isoformat()


def activity(id='buy-1', order='entry', side='buy', qty='10', price='100'):
    return dict(id=id, order_id=order, activity_type='FILL', type='fill', symbol='SPY',
                side=side, qty=qty, price=price, transaction_time=NOW.isoformat(),
                private_account='never-export', cum_qty=qty, leaves_qty='0')


def setup(tmp_path):
    path=tmp_path/'p.sqlite';e=PaperEngine(path);row=seed(e);capture(e,row,closed())
    return e, PaperActivities(e.db), path


def save(j, items, *, finished=True, failure=None, at=AT):
    rows=[normalize(v,j.known_orders(DAY),at) for v in items]
    rows=[v for v in rows if v]
    j.save(DAY,rows,dict(after='2026-11-01T05:00:00+00:00',until=at,scan_finished=finished,
        pages=1,received=len(items),matched=len(rows),failure=failure),at)


def full(): return [activity(), activity('sell-1','target','sell','10','102')]


def result(e): return e.evidence.report()['attempts'][0]['fill_activities']


def test_individual_events_persist_dedup_export_and_gross_is_not_net(tmp_path):
    e,j,path=setup(tmp_path)
    items=[activity(qty='4',price='99'),activity('buy-2',qty='6',price='100.6666666667'),full()[1]]
    save(j,items);save(j,items)
    r=result(e)
    assert r['state']=='closed_quantities_agree' and r['event_count']==3
    assert r['gross_price_pnl_usd']=='19.9999999998'
    assert r['fees_usd'] is r['net_pnl_usd'] is None
    assert r['orders'][0]['activity_average']=='100.00000000002'
    exported=j.export()
    assert len(exported['events'])==3 and not exported['has_more']
    assert 'never-export' not in json.dumps(exported) and 'buy-1' not in json.dumps(exported)
    e.close();e=PaperEngine(path)
    assert not e.enabled and result(e)['event_count']==3
    e.close()


@pytest.mark.parametrize('condition,state', [('none','not_observed'),('empty','quantity_mismatch'),
    ('partial','quantity_mismatch'),('failed','scan_incomplete'),('limited','scan_incomplete')])
def test_no_activity_or_failed_scan_never_invents_zero_profit(tmp_path,condition,state):
    e,j,_=setup(tmp_path)
    if condition=='empty':save(j,[])
    elif condition=='partial':save(j,[activity(qty='4')])
    elif condition=='failed':save(j,full(),finished=False,failure='activity_read_failed')
    elif condition=='limited':save(j,full(),finished=False,failure='activity_page_limit')
    r=result(e);assert r['state']==state and r['gross_price_pnl_usd'] is None
    e.close()


@pytest.mark.parametrize('field,value',[('price','99'),('qty','9'),('transaction_time',(NOW-timedelta(seconds=1)).isoformat())])
def test_same_activity_revision_is_retained_and_hold_survives_restart(tmp_path,field,value):
    e,j,path=setup(tmp_path);save(j,full());items=full();items[0][field]=value
    save(j,items,at=(NOW+timedelta(minutes=21)).isoformat())
    assert result(e)['state']=='correction_hold' and result(e)['event_count']==3
    assert e.evidence.holds()[0]['reason']=='fill_activity_changed'
    save(j,full(),at=(NOW+timedelta(minutes=22)).isoformat())
    assert result(e)['gross_price_pnl_usd'] is None
    e.close();e=PaperEngine(path)
    with pytest.raises(PaperError,match='correction hold'):e.start(PresetStore.paper('paper-balanced'))
    assert e.thread is None;e.close()


def test_missing_previous_event_holds_only_after_finished_scan(tmp_path):
    e,j,_=setup(tmp_path);save(j,full())
    save(j,[],finished=False,failure='activity_read_failed',at=(NOW+timedelta(minutes=21)).isoformat())
    assert not e.evidence.holds() and result(e)['state']=='scan_incomplete'
    save(j,[full()[0]],at=(NOW+timedelta(minutes=22)).isoformat())
    assert e.evidence.holds()[0]['reason']=='previous_activity_missing'
    assert result(e)['event_count']==2;e.close()


@pytest.mark.parametrize('field,value',[('side','sell'),('symbol','QQQ'),('type','trade_correct'),
    ('activity_type','DIV'),('qty','0'),('qty','NaN'),('price','0'),('transaction_time',None),
    ('transaction_time',(NOW+timedelta(days=1)).isoformat())])
def test_strict_owned_event_validation(tmp_path,field,value):
    e,j,_=setup(tmp_path);v=activity();v[field]=value
    with pytest.raises(EvidenceError):normalize(v,j.known_orders(DAY),AT)
    e.close()


def test_unknown_account_activities_not_attributed_and_price_precision_kept(tmp_path):
    e,j,_=setup(tmp_path)
    assert normalize(activity(order='external',price='secret'),j.known_orders(DAY),AT) is None
    v=normalize(activity(price='100.000000000001'),j.known_orders(DAY),AT)
    assert v['price']=='100.000000000001' and v['fees_usd'] is None
    e.close()


class Pages(Broker):
    def __init__(self,pages):super().__init__();self.pages=pages;self.reads=[]
    def request(self,method,path,payload=None,**kwargs):
        assert method=='GET', 'Read-only history must never submit or cancel'
        self.reads.append(path)
        if path.startswith('/v2/account/activities/FILL?'):
            p=self.pages.pop(0)
            if isinstance(p,Exception):raise p
            return deepcopy(p)
        return super().request(method,path,payload,**kwargs)


def test_provider_pagination_freezes_bounds_uses_raw_last_id_without_exporting_it(tmp_path):
    e,j,_=setup(tmp_path)
    page=[activity('unowned-'+str(i),order='external') for i in range(98)]+full()
    b=Pages([page,[]]);rows,scan=fetch(b,date.fromisoformat(DAY),j.known_orders(DAY),AT)
    assert len(rows)==2 and scan['scan_finished'] and scan['pages']==2 and scan['received']==100
    q=[parse_qs(urlsplit(p).query) for p in b.reads]
    assert q[0]['until']==q[1]['until']==[AT] and q[0]['after']==q[1]['after']
    assert q[1]['page_token']==['sell-1'] and q[1]['direction']==['asc']
    assert q[0]['after']==['2026-11-01T05:00:00+00:00']
    assert 'page_token' not in scan and 'sell-1' not in json.dumps(rows)
    e.close()


@pytest.mark.parametrize('kind', ['repeat','network','shape','oversize','limit'])
def test_incomplete_provider_scans_are_explicit_and_bounded(tmp_path,kind,monkeypatch):
    import orb.paper_activities as m
    e,j,_=setup(tmp_path);page=[activity(str(i),order='external') for i in range(100)]
    if kind=='repeat':pages=[page,page]
    elif kind=='network':pages=[RuntimeError('secret credential text')]
    elif kind=='shape':pages=[{'bad':'shape'}]
    elif kind=='oversize':pages=[page+[activity('extra')]]
    else:monkeypatch.setattr(m,'MAX_PAGES',2);pages=[page,[activity('n'+str(i),order='external') for i in range(100)]]
    rows,scan=fetch(Pages(pages),date.fromisoformat(DAY),j.known_orders(DAY),AT)
    assert not scan['scan_finished'] and scan['failure'] and scan['pages']<=2
    assert 'secret' not in json.dumps(scan)
    j.save(DAY,rows,scan,AT)
    assert result(e)['gross_price_pnl_usd'] is None;e.close()


def test_invalid_owned_fill_on_refresh_creates_hold_before_any_automatic_actions(tmp_path):
    e,j,_=setup(tmp_path);bad=activity();bad['side']='sell';e.client=Pages([[bad]])
    r=e.refresh_activities(DAY)
    assert r['entries_blocked'] and not e.enabled
    assert e.evidence.holds()[0]['reason']=='invalid_owned_fill_activity'
    assert not e.client.posts and not e.client.deletes;e.close()


def test_api_refresh_is_read_only_scoped_and_reopens_with_activity_evidence(tmp_path):
    e,j,path=setup(tmp_path);e.close()
    with TestClient(create_app(paper_db=path),base_url='http://127.0.0.1:8765') as c:
        b=Pages([full()]);c.app.state.paper_engine.client=b
        r=c.post('/api/paper/activities/refresh',json={'session':DAY})
        assert r.status_code==200
        assert r.json()['attempts'][0]['fill_activities']['state']=='closed_quantities_agree'
        assert len(b.reads)==2 and not b.posts and not b.deletes
        assert len(c.get('/api/paper/activities').json()['events'])==2
        assert c.get('/api/paper/activities?after=-1').status_code==422
        assert c.post('/api/paper/activities/refresh',json={'session':DAY,'clear':True}).status_code==422
        assert c.post('/api/paper/activities/refresh',json={'session':DAY},headers={'Origin':'http://evil.invalid'}).status_code==403
        b.account['id']='another'
        assert c.post('/api/paper/activities/refresh',json={'session':DAY}).status_code==409
        assert len(b.reads)==3
    e=PaperEngine(path);assert result(e)['event_count']==2;e.close()


def test_missing_order_evidence_or_session_does_not_probe_provider(tmp_path):
    e=PaperEngine(tmp_path/'p.sqlite',Pages([]));seed(e)
    with pytest.raises(PaperError,match='order evidence first'):e.refresh_activities(DAY)
    with pytest.raises(KeyError):e.refresh_activities('2026-10-30')
    assert not e.client.reads;e.close()


def test_append_only_integrity_and_frozen_export_cursor(tmp_path):
    e,j,_=setup(tmp_path);save(j,[activity()]);first=j.export()
    save(j,full(),at=(NOW+timedelta(minutes=21)).isoformat())
    assert not j.export(first['next_after'],first['through'])['events']
    assert len(j.export()['events'])==2
    for table in ('paper_fill_activities','paper_activity_scans'):
        with pytest.raises(sqlite3.IntegrityError,match='append-only'):e.db.execute(f'DELETE FROM {table}')
    e.db.execute('DROP TRIGGER paper_fill_activities_no_update')
    e.db.execute("UPDATE paper_fill_activities SET fingerprint='bad'")
    with pytest.raises(EvidenceError,match='integrity'):e.evidence.report()
    with pytest.raises(EvidenceError,match='integrity'):j.export()
    e.close()


def test_v2_migration_preserves_observations_holds_and_rolls_back_interruption(tmp_path,monkeypatch):
    e,j,path=setup(tmp_path)
    for table in ('paper_admission_log','paper_fee_log','paper_review_log','paper_hold_events','paper_fill_activities','paper_activity_scans'):e.db.execute(f'DROP TABLE {table}')
    e.db.execute("INSERT INTO paper_evidence_holds VALUES (?,?,?)",(DAY,'original_hold',AT))
    e.db.execute('PRAGMA user_version=2');e.close()
    original=__import__('orb.paper_trading',fromlist=['install_activities']).install_activities
    def fail(db):original(db);raise RuntimeError('interrupt')
    monkeypatch.setattr('orb.paper_trading.install_activities',fail)
    with pytest.raises(RuntimeError):PaperEngine(path)
    db=sqlite3.connect(path)
    assert db.execute('PRAGMA user_version').fetchone()[0]==2
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='paper_fill_activities'").fetchone()
    assert db.execute('SELECT COUNT(*) FROM paper_observations').fetchone()[0]==1;db.close()
    monkeypatch.setattr('orb.paper_trading.install_activities',original)
    e=PaperEngine(path)
    assert e.db.execute('PRAGMA user_version').fetchone()[0]==6
    assert e.evidence.holds()[0]['reason']=='original_hold' and result(e)['event_count']==0;e.close()


def test_activity_insert_and_scan_receipt_roll_back_on_actual_process_exit(tmp_path):
    import subprocess,sys
    e,j,path=setup(tmp_path);event=normalize(activity(),j.known_orders(DAY),AT);e.close()
    code='''import os,sqlite3,json,sys
from orb.paper_activities import PaperActivities
db=sqlite3.connect(sys.argv[1],isolation_level=None);db.row_factory=sqlite3.Row
def trace(sql):
 if sql.startswith('INSERT INTO paper_activity_scans'):os._exit(19)
db.set_trace_callback(trace)
PaperActivities(db).save(sys.argv[2],[json.loads(sys.argv[3])],json.loads(sys.argv[4]),sys.argv[5])
'''
    scan=dict(after='2026-11-01T05:00:00+00:00',until=AT,scan_finished=True,pages=1,received=1,matched=1,failure=None)
    r=subprocess.run([sys.executable,'-c',code,str(path),DAY,json.dumps(event),json.dumps(scan),AT])
    assert r.returncode==19
    e=PaperEngine(path);assert result(e)['state']=='not_observed'
    assert not PaperActivities(e.db).export()['events']
    save(PaperActivities(e.db),full());assert result(e)['event_count']==2;e.close()


def test_capacity_and_competing_worker_fail_closed(tmp_path,monkeypatch):
    import fcntl
    import orb.paper_activities as m
    e,j,path=setup(tmp_path);monkeypatch.setattr(m,'MAX_EVENTS',1)
    save(j,full())
    assert e.evidence.holds()[0]['reason']=='fill_activity_capacity_reached'
    assert result(e)['state']=='correction_hold'
    e.client=Pages([])
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(PaperError,match='Another paper worker'):e.refresh_activities(DAY)
    assert not e.client.reads;e.close()


def test_provider_read_does_not_hold_worker_lock_and_scope_is_rechecked(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    e,j,_=setup(tmp_path);entered,release=Event(),Event()
    class Slow(Pages):
        def request(self,method,path,payload=None,**kwargs):
            if '/activities/' in path:
                entered.set();assert release.wait(5)
            return super().request(method,path,payload,**kwargs)
    e.client=Slow([full()])
    with ThreadPoolExecutor() as pool:
        future=pool.submit(e.refresh_activities,DAY)
        assert entered.wait(5)
        # Separate caller gets status and can change scope while the GET waits.
        assert e.lock.acquire(timeout=1)
        try:
            assert e.status()['evidence']['attempts'][0]['fill_activities']['state']=='not_observed'
            e.meta('account','changed-during-read')
        finally:e.lock.release();release.set()
        with pytest.raises(PaperError,match='scope changed'):future.result(timeout=5)
    assert not j.export()['events'];e.close()


def test_contradictory_duplicate_in_one_page_is_retained_and_held(tmp_path):
    e,j,_=setup(tmp_path);changed=activity(price='99')
    rows,scan=fetch(Pages([[activity(),changed]]),date.fromisoformat(DAY),j.known_orders(DAY),AT)
    assert scan['failure']=='activity_revision_in_scan' and len(rows)==2
    j.save(DAY,rows,scan,AT)
    assert result(e)['state']=='correction_hold' and result(e)['event_count']==2;e.close()


def test_known_zero_fill_and_open_order_remain_distinct(tmp_path):
    e,j,_=setup(tmp_path)
    p=closed();p.update(status='canceled',filled_qty='0',filled_avg_price=None,filled_at=None)
    for leg in p['legs']:leg.update(status='canceled',filled_qty='0',filled_avg_price=None,filled_at=None)
    # Separate fresh attempt avoids introducing a deliberate quantity regression.
    e.close();path=tmp_path/'zero.sqlite';e=PaperEngine(path);row=seed(e);capture(e,row,p)
    j=PaperActivities(e.db);save(j,[])
    assert result(e)['state']=='known_no_entry' and result(e)['gross_price_pnl_usd']=='0'
    e.close()


def test_paginated_export_and_price_disagreement_do_not_claim_reconciliation(tmp_path):
    e,j,_=setup(tmp_path)
    items=[activity(str(i),qty='.1',price='101') for i in range(100)]+[full()[1]]
    save(j,items)
    r=result(e)
    assert r['state']=='closed_quantities_agree' and r['gross_price_pnl_usd']=='10'
    assert r['orders'][0]['activity_average']=='101' and r['orders'][0]['snapshot_average']=='100'
    assert r['events_truncated'] and len(r['events'])==100
    page=j.export();assert len(page['events'])==100 and page['has_more']
    end=j.export(page['next_after'],page['through']);assert len(end['events'])==1 and not end['has_more']
    assert not e.evidence.holds() and r['net_pnl_usd'] is None;e.close()


def test_late_finishing_older_query_cannot_replace_newer_scan(tmp_path):
    e,j,_=setup(tmp_path);save(j,full(),at=(NOW+timedelta(minutes=21)).isoformat())
    with pytest.raises(EvidenceError,match='stale_activity_scan'):save(j,[],at=AT)
    assert result(e)['scan']['until']==(NOW+timedelta(minutes=21)).isoformat()
    assert result(e)['event_count']==2 and not e.evidence.holds();e.close()
