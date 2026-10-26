import json
from pathlib import Path

import pytest

pytest.importorskip('fastapi')
pytest.importorskip('httpx')
from fastapi.testclient import TestClient
from orb.dashboard_api import create_app
from orb.dashboard_import import MAX_BYTES
from test_dashboard_import import payload


@pytest.fixture
def client():
    with TestClient(create_app(examples=True),base_url='http://127.0.0.1:8765') as c:
        yield c


def test_examples_and_exact_engine_evidence(client):
    listing=client.get('/api/runs')
    assert listing.status_code==200
    assert len(listing.json())==3
    assert all(r['synthetic'] is True for r in listing.json())
    for item in listing.json():
        result=client.get('/api/runs/'+item['id'])
        run=result.json()
        assert run['metrics_eligible']==item['complete']
        assert run['evaluation_id']==run['evidence']['evaluation_id']
        assert not any(run['capabilities'].values())
    assert client.get('/api/runs/missing').status_code==404


def test_import_get_and_server_restart_semantics(client):
    imported=client.post('/api/import',json=payload('missing_entry'))
    assert imported.status_code==201
    run=client.get('/api/runs/'+imported.json()['id']).json()
    assert not run['metrics_eligible']
    assert run['evidence']['summary']['total_return'] is None
    assert len(client.get('/api/runs').json())==4
    restarted=TestClient(create_app(),base_url='http://127.0.0.1:8765')
    assert restarted.get('/api/runs').json()==[]


@pytest.mark.parametrize('headers',[{'Host':'evil.example:8765'},
    {'Origin':'https://evil.example'},{'Origin':'http://localhost:8765'},
    {'Host':'127.0.0.1:9999'}])
def test_cross_origin_and_dns_rebinding_hosts_are_rejected(client,headers):
    assert client.get('/api/runs',headers=headers).status_code==403
    assert client.post('/api/import',headers=headers,json=payload()).status_code==403


@pytest.mark.parametrize('body',['{"kind":"csv","kind":"capture"}', '{"x":NaN}', 'not json', '{}', 'null'])
def test_malformed_import_is_sanitised_and_does_not_add_a_run(client,body):
    r=client.post('/api/import',content=body,headers={'Content-Type':'application/json'})
    assert r.status_code==422
    assert 'not json' not in r.text
    assert len(client.get('/api/runs').json())==3


def test_body_type_size_and_preflight_guards(client):
    assert client.post('/api/import',content='{}').status_code==415
    assert client.post('/api/import',content='x'*(MAX_BYTES+1),headers={'Content-Type':'application/json'}).status_code==413
    assert client.options('/api/import',headers={'Origin':'https://evil.example','Access-Control-Request-Method':'POST'}).status_code==403
    assert 'access-control-allow-origin' not in client.get('/api/runs').headers


def test_session_capacity_and_no_order_endpoint(client):
    for i in range(9):
        assert client.post('/api/import',json={**payload(),'label':f'run {i}'}).status_code==201
    assert client.post('/api/import',json=payload()).status_code==409
    assert client.post('/api/orders',json={}).status_code==404
    assert client.get('/docs').status_code==404


def test_static_asset_allowlist_and_headers(tmp_path):
    (tmp_path/'index.html').write_text('<html>research</html>')
    (tmp_path/'app.js').write_text('console.log("local")')
    (tmp_path/'secret.env').write_text('do not serve')
    c=TestClient(create_app(web_dir=tmp_path),base_url='http://127.0.0.1:8765')
    assert c.get('/').text=='<html>research</html>'
    assert c.get('/assets/app.js').status_code==200
    assert c.get('/assets/secret.env').status_code==404
    assert c.get('/assets/../secret.env').status_code==404
    assert c.get('/').headers['cache-control']=='no-store'
    assert "frame-ancestors 'none'" in c.get('/').headers['content-security-policy']
