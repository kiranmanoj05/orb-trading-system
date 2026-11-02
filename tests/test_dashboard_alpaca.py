"""Provider paths stay bounded, private and read-only; source drives the chart."""
import json
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import pytest
from fastapi.testclient import TestClient
from orb.acquisition import Credentials, HTTPPage, AcquisitionError
from orb.paper_snapshot import HTTPResponse, PaperSnapshotError
from orb.dashboard_alpaca import observe_paper, connection_status, fetch_run, local_bundles
from orb.dashboard_api import create_app
from orb.ingestion import normalise_capture
from test_dashboard_import import payload

CREDS=Credentials('fixture-key-very-long', 'fixture-secret-very-long')

def paper_responses():
    return [dict(cash='10000',equity='10150',last_equity='10000',buying_power='20000',status='ACTIVE'),
            [dict(symbol='SPY',qty='2',market_value='1100',unrealized_pl='5')],
            [dict(id='fixture-order',symbol='SPY',side='buy',status='partially_filled',qty='3',filled_qty='2',
                  filled_avg_price='550.12',submitted_at='2026-10-09T14:00:00Z',filled_at='2026-10-09T14:01:00Z')]]

def test_credentials_status_is_not_connection_or_secret(monkeypatch):
    monkeypatch.delenv('APCA_API_KEY_ID',raising=False);monkeypatch.delenv('APCA_API_SECRET_KEY',raising=False)
    assert not connection_status()['configured']
    monkeypatch.setenv('APCA_API_KEY_ID',CREDS.key);monkeypatch.setenv('APCA_API_SECRET_KEY',CREDS.secret)
    value=connection_status()
    assert value['configured'] and not value['connected'] and not value['order_submission']
    assert CREDS.key not in json.dumps(value) and CREDS.secret not in json.dumps(value)

def test_paper_activity_includes_partial_fills_without_attribution():
    calls=[];responses=iter(paper_responses())
    def transport(url,headers,timeout,maximum):
        calls.append(url);assert headers['APCA-API-KEY-ID']==CREDS.key
        return HTTPResponse(200,json.dumps(next(responses)).encode())
    value=observe_paper(credentials=CREDS,transport=transport)
    assert len(calls)==3 and all(urlsplit(u).netloc=='paper-api.alpaca.markets' for u in calls)
    assert parse_qs(urlsplit(calls[-1]).query)['status']==['all']
    assert value['account']['equity']=='10150'
    assert value['orders'][0]['filled_quantity']=='2'
    assert value['orders'][0]['attribution']=='unattributed_broker_order'
    assert value['orders'][0]['average_fill_price']=='550.12'
    assert not value['orders_submitted'] and not value['execution_enabled']
    assert 'fixture-order' not in json.dumps(value)

@pytest.mark.parametrize('status',[301,401,403,429,500])
def test_paper_http_errors_stop_before_further_requests(status):
    calls=[]
    def transport(*args):calls.append(args);return HTTPResponse(status,b'private provider body')
    with pytest.raises(PaperSnapshotError) as exc:observe_paper(credentials=CREDS,transport=transport)
    assert len(calls)==1 and 'private provider body' not in str(exc.value)

@pytest.mark.parametrize('mutation',[lambda x:x[0].update(equity='NaN'),lambda x:x[2][0].update(filled_at='not-a-time'),lambda x:x[2][0].pop('filled_qty')])
def test_malformed_paper_values_are_not_published(mutation):
    values=paper_responses();mutation(values);responses=iter(values)
    with pytest.raises(PaperSnapshotError):observe_paper(credentials=CREDS,transport=lambda *args:HTTPResponse(200,json.dumps(next(responses)).encode()))

def test_secret_reflection_is_rejected():
    with pytest.raises(PaperSnapshotError,match='credential'):
        observe_paper(credentials=CREDS,transport=lambda *args:HTTPResponse(200,json.dumps({'secret':CREDS.secret}).encode()))

def test_order_limit_is_explicit():
    values=paper_responses();values[2]=[dict(values[2][0],id='order-'+str(i)) for i in range(500)];responses=iter(values)
    value=observe_paper(credentials=CREDS,transport=lambda *args:HTTPResponse(200,json.dumps(next(responses)).encode()))
    assert value['history_may_be_truncated'] and len(value['orders'])==500

def test_fetch_reuses_capture_and_engine_without_inventing_missing_bars():
    fixture=json.loads(Path('fixtures/alpaca_capture_synthetic.json').read_text());responses=iter(fixture['pages']);calls=[]
    def transport(url,*args):calls.append(url);return HTTPPage(200,json.dumps(next(responses)['response']).encode())
    run,source=fetch_run('2026-10-09','2026-10-09','sip',credentials=CREDS,transport=transport)
    assert run['candles'] and run['import_context']['kind']=='bundle'
    assert not run['metrics_eligible']  # fixture intentionally contains a short session
    assert all(urlsplit(u).netloc=='data.alpaca.markets' for u in calls)
    assert parse_qs(urlsplit(calls[0]).query)['end']==['2026-10-09T19:59:00+00:00']
    assert json.loads(source['bundle_manifest'])['request']['feed']=='sip'

@pytest.mark.parametrize('start,end,feed',[('bad','bad','sip'),('2026-10-09','2026-10-19','sip'),('2026-10-10','2026-10-10','sip'),('2026-10-09','2026-10-09','remote-url')])
def test_invalid_fetch_never_calls_transport(start,end,feed):
    def transport(*args):pytest.fail('unexpected provider call')
    with pytest.raises(AcquisitionError):fetch_run(start,end,feed,credentials=CREDS,transport=transport)

def write_bundle(path):
    path.mkdir(parents=True);raw=Path('fixtures/alpaca_capture_synthetic.json').read_bytes()
    _,csv,manifest=normalise_capture(raw)
    (path/'capture.json').write_bytes(raw);(path/'bars.csv').write_bytes(csv);(path/'manifest.json').write_text(json.dumps(manifest))

def test_local_discovery_is_bounded_and_skips_symlinks(tmp_path):
    write_bundle(tmp_path/'good');(tmp_path/'link').symlink_to(tmp_path/'good',target_is_directory=True)
    write_bundle(tmp_path/'bad');(tmp_path/'bad'/'bars.csv').write_text('tampered')
    rows=local_bundles(tmp_path)
    assert len(rows)==2 and sum('run' in r for r in rows)==1 and sum('error' in r for r in rows)==1

def test_discovered_runs_survive_restart_without_duplicate_runs(tmp_path):
    data=tmp_path/'data';write_bundle(data/'pilot');db=tmp_path/'archive.sqlite'
    for _ in range(2):
        with TestClient(create_app(data_dir=data,research_db=db),base_url='http://127.0.0.1:8765') as client:
            rows=client.get('/api/runs').json();assert len(rows)==1
            run=client.get('/api/runs/'+rows[0]['id']).json();assert run['candles']
            assert client.get('/api/local-bundles').json()['imports'][0]['id']==rows[0]['id']

def test_new_routes_stay_same_origin_and_sanitised(monkeypatch):
    monkeypatch.delenv('APCA_API_KEY_ID',raising=False);monkeypatch.delenv('APCA_API_SECRET_KEY',raising=False)
    with TestClient(create_app(),base_url='http://127.0.0.1:8765') as c:
        assert c.post('/api/alpaca/observe').status_code==502
        assert c.post('/api/alpaca/observe',headers={'Origin':'https://evil.example'}).status_code==403
        assert c.post('/api/alpaca/fetch',json={'url':'https://evil.example'}).status_code==422
        assert c.post('/api/alpaca/observe',content='credentials').status_code==422
        assert c.get('/api/orders').status_code==404
        assert 'media-src' in c.get('/api/runs').headers['content-security-policy']

def test_fetch_route_publishes_engine_source(monkeypatch,tmp_path):
    import orb.dashboard_api as module
    from orb.dashboard_import import import_dashboard
    data=payload();run=import_dashboard(data)
    monkeypatch.setattr(module,'fetch_run',lambda *args:(run,data))
    with TestClient(create_app(research_db=tmp_path/'db.sqlite'),base_url='http://127.0.0.1:8765') as c:
        r=c.post('/api/alpaca/fetch',json={'start_session':'2026-10-09','end_session':'2026-10-09','feed':'sip'})
        assert r.status_code==201
        actual=c.get('/api/runs/'+r.json()['id']).json()
        assert actual['decisions']==run['decisions'] and actual['candles']==run['candles']


def test_nested_bracket_exit_fills_are_visible_and_deduplicated():
    values=paper_responses()
    child=dict(values[2][0],id='target-fill',side='sell',status='filled',filled_qty='2')
    values[2][0]['legs']=[child]
    values[2].append(child)
    responses=iter(values)
    result=observe_paper(credentials=CREDS,transport=lambda *a:HTTPResponse(200,json.dumps(next(responses)).encode()))
    assert len(result['orders'])==2 and result['orders'][1]['side']=='sell'
    assert result['includes_bracket_legs']
