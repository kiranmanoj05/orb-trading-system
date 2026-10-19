from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

import orb.acquisition as acquisition
from orb.acquisition import (AcquisitionError, Credentials, FetchLimits, HTTPPage,
                             TransportError, acquire_bundle, acquisition_plan)
from orb.ingestion import evaluate_bundle, load_bundle


NOW = datetime(2026,10,18,18,0,tzinfo=timezone.utc)
START, END = '2026-10-09T13:30:00Z', '2026-10-09T13:47:00Z'
CREDS = Credentials('dummy-test-key', 'dummy-test-secret')
FIXTURE = Path('fixtures/alpaca_capture_synthetic.json')


def responses():
    return [HTTPPage(200,json.dumps(p['response']).encode()) for p in json.loads(FIXTURE.read_bytes())['pages']]


class Transport:
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = []

    def __call__(self, url, headers, timeout, maximum):
        self.calls.append((url, dict(headers), timeout, maximum))
        item = self.sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fetch(tmp_path, transport, **kwargs):
    return acquire_bundle(START, END, tmp_path/'bundle', credentials=CREDS,
                          transport=transport, clock=lambda:NOW, sleeper=kwargs.pop('sleeper',lambda _:None),
                          **kwargs)


def test_two_page_read_only_request_becomes_valid_integrity_linked_bundle(tmp_path):
    transport = Transport(responses())
    manifest = fetch(tmp_path,transport)
    assert manifest['data_kind'] == 'historical'
    assert not manifest['provider_authenticity_verified']
    assert manifest['bars'] == 35
    assert load_bundle(tmp_path/'bundle')[1] == manifest
    for index,(url,headers,timeout,maximum) in enumerate(transport.calls):
        parsed = urlsplit(url)
        assert parsed.scheme == 'https' and parsed.netloc == 'data.alpaca.markets'
        assert parsed.path == '/v2/stocks/bars'
        params = parse_qs(parsed.query)
        assert params['symbols'] == ['SPY,QQQ'] and params['feed'] == ['iex']
        assert ('page_token' in params) == bool(index)
        assert headers['APCA-API-KEY-ID'] == CREDS.key
        assert timeout == 15 and maximum == 5_000_000
    raw = (tmp_path/'bundle'/'capture.json').read_bytes()
    assert CREDS.key.encode() not in raw and CREDS.secret.encode() not in raw
    assert evaluate_bundle(tmp_path/'bundle')['summary']['complete'] is False


def test_response_numeric_bytes_are_not_roundtripped_through_float(tmp_path):
    pages = responses()
    pages[0] = replace(pages[0],body=pages[0].body.replace(b'"o": 99,',b'"o": 99.00000000000000000001,',1))
    fetch(tmp_path,Transport(pages))
    assert b'99.00000000000000000001' in (tmp_path/'bundle'/'bars.csv').read_bytes()


def test_retry_after_and_backoff_are_bounded_and_same_page_is_retried(tmp_path):
    transport = Transport([HTTPPage(429,retry_after='2'),HTTPPage(503),*responses()])
    sleeps = []
    fetch(tmp_path,transport,sleeper=sleeps.append)
    assert sleeps == [2,2]
    assert transport.calls[0][0] == transport.calls[1][0] == transport.calls[2][0]
    assert len(transport.calls) == 4


@pytest.mark.parametrize('failure',[TransportError('sanitised'),URLError('dummy-test-secret'),TimeoutError('dummy-test-secret')])
def test_retryable_transport_failure_does_not_leak_details(tmp_path,failure):
    transport = Transport([failure,*responses()])
    sleeps = []
    fetch(tmp_path,transport,sleeper=sleeps.append)
    assert sleeps == [1]


@pytest.mark.parametrize('status',[301,302,307,308,400,401,403,404])
def test_redirects_auth_and_permanent_errors_fail_without_fallback(tmp_path,status):
    transport = Transport([HTTPPage(status,b'dummy-test-secret')])
    with pytest.raises(AcquisitionError) as error:
        fetch(tmp_path,transport)
    assert CREDS.secret not in str(error.value)
    assert len(transport.calls) == 1
    assert not (tmp_path/'bundle').exists()


def test_retry_exhaustion_is_finite_and_does_not_publish_partial_capture(tmp_path):
    transport = Transport([responses()[0],HTTPPage(500),HTTPPage(500),HTTPPage(500)])
    with pytest.raises(AcquisitionError,match='exhausted'):
        fetch(tmp_path,transport)
    assert len(transport.calls) == 4
    assert not (tmp_path/'bundle').exists()


@pytest.mark.parametrize('header',['100','malformed'])
def test_retry_delay_outside_policy_is_not_ignored(tmp_path,header):
    transport = Transport([HTTPPage(429,retry_after=header)])
    with pytest.raises(AcquisitionError):
        fetch(tmp_path,transport)
    assert len(transport.calls) == 1


def test_http_date_retry_after_is_respected(tmp_path):
    from email.utils import format_datetime
    sleeps=[]
    transport=Transport([HTTPPage(429,retry_after=format_datetime(NOW+timedelta(seconds=3))),*responses()])
    fetch(tmp_path,transport,sleeper=sleeps.append)
    assert sleeps == [3]


@pytest.mark.parametrize('limits,reason',[
    (FetchLimits(max_pages=1),'page budget'),
    (FetchLimits(max_page_bytes=10),'page byte'),
    (FetchLimits(max_total_bytes=10),'total byte'),
])
def test_page_and_byte_budgets_do_not_publish_truncation(tmp_path,limits,reason):
    with pytest.raises(AcquisitionError,match=reason):
        fetch(tmp_path,Transport(responses()),limits=limits)
    assert not (tmp_path/'bundle').exists()


def test_total_bytes_include_final_envelope_overhead(tmp_path):
    pages=responses()
    total=sum(len(p.body) for p in pages)
    with pytest.raises(AcquisitionError,match='envelope'):
        fetch(tmp_path,Transport(pages),limits=FetchLimits(max_total_bytes=total))
    assert not (tmp_path/'bundle').exists()


def test_repeated_token_and_invalid_payload_fail(tmp_path):
    first=responses()[0]
    with pytest.raises(AcquisitionError,match='token'):
        fetch(tmp_path,Transport([first,first]))
    with pytest.raises(AcquisitionError,match='invalid market-data'):
        fetch(tmp_path,Transport([HTTPPage(200,b'<html>dummy-test-secret</html>'.replace(b'dummy-test-secret',b'failure'))]))
    assert not (tmp_path/'bundle').exists()


def test_valid_http_but_invalid_ohlcv_fails_before_publication(tmp_path):
    pages=responses()
    pages[0]=replace(pages[0],body=pages[0].body.replace(b'"h": 100,',b'"h": 97,',1))
    with pytest.raises(AcquisitionError,match='strict validation'):
        fetch(tmp_path,Transport(pages))
    assert not (tmp_path/'bundle').exists()


def test_credential_echo_is_not_archived(tmp_path):
    with pytest.raises(AcquisitionError,match='credential material'):
        fetch(tmp_path,Transport([HTTPPage(200,b'{"message":"dummy-test-secret"}')]))
    assert not (tmp_path/'bundle').exists()
    assert CREDS.key not in repr(CREDS) and CREDS.secret not in repr(CREDS)


def test_missing_credentials_fail_before_network_and_directory_creation(tmp_path,monkeypatch):
    for name in ('APCA_API_KEY_ID','APCA_API_SECRET_KEY'):
        monkeypatch.delenv(name,raising=False)
    transport=Transport([])
    with pytest.raises(AcquisitionError,match='configure APCA'):
        acquire_bundle(START,END,tmp_path/'bundle',clock=lambda:NOW,transport=transport)
    assert not transport.calls and not (tmp_path/'bundle').exists()


def test_existing_destination_fails_before_network(tmp_path):
    (tmp_path/'bundle').mkdir()
    transport=Transport([])
    with pytest.raises(AcquisitionError,match='already exists'):
        fetch(tmp_path,transport)
    assert not transport.calls


@pytest.mark.parametrize('key,secret',[('','valid'),('bad\nkey','valid'),('valid','bad secret'),('valid','é')])
def test_bad_credentials_rejected_without_echo(key,secret):
    with pytest.raises(AcquisitionError):
        Credentials(key,secret)


@pytest.mark.parametrize('kwargs',[
    {'max_pages':0},{'max_pages':101},{'max_attempts':6},{'timeout_seconds':True},
    {'max_retry_delay':31},{'max_page_bytes':10_000_001},{'max_total_bytes':100_000_001},
])
def test_invalid_safety_limits(kwargs):
    with pytest.raises(AcquisitionError):
        FetchLimits(**kwargs)


@pytest.mark.parametrize('start,end,feed',[
    ('2026-10-09',END,'iex'),(END,START,'iex'),
    ('2026-10-01T00:00:00Z',END,'iex'),(START,'2026-10-18T17:59:00Z','iex'),
    (START,END,'otc'),
])
def test_invalid_or_recent_request_does_not_need_credentials(start,end,feed):
    with pytest.raises(ValueError):
        acquisition_plan(start,end,feed=feed,now=NOW)


def test_default_transport_uses_get_and_redirect_blocking(monkeypatch):
    seen={}
    class Response(BytesIO):
        status=200
        def read1(self,n):
            return self.read(n)
    class Opener:
        def open(self,request,timeout):
            seen['request']=request
            seen['timeout']=timeout
            return Response(b'{"bars":{},"next_page_token":null}')
    def opener(handler):
        assert isinstance(handler,acquisition._NoRedirect)
        assert handler.redirect_request(None,None,302,None,None,'https://evil.invalid') is None
        return Opener()
    monkeypatch.setattr(acquisition,'build_opener',opener)
    page=acquisition._http_get(acquisition.ENDPOINT,{'APCA-API-KEY-ID':'dummy-test-key'},15,1000)
    assert page.status == 200 and seen['request'].get_method() == 'GET'
    assert seen['request'].full_url == acquisition.ENDPOINT
    assert seen['timeout'] == 15


def test_default_transport_sanitises_http_error(monkeypatch):
    class Opener:
        def open(self,*args,**kwargs):
            raise HTTPError(acquisition.ENDPOINT,403,'dummy-test-secret',{},BytesIO(b'dummy-test-secret'))
    monkeypatch.setattr(acquisition,'build_opener',lambda _:Opener())
    page=acquisition._http_get(acquisition.ENDPOINT,{},15,1000)
    assert page.status == 403 and page.body == b'' and CREDS.secret not in repr(page)


def test_default_reader_enforces_byte_limit_during_read(monkeypatch):
    class Response(BytesIO):
        status=200
        def read1(self,n):
            return self.read(n)
    class Opener:
        def open(self,*args,**kwargs):
            return Response(b'x'*1000)
    monkeypatch.setattr(acquisition,'build_opener',lambda _:Opener())
    with pytest.raises(AcquisitionError,match='page byte limit'):
        acquisition._http_get(acquisition.ENDPOINT,{},15,10)


def test_default_transport_strips_network_exception_details(monkeypatch):
    class Opener:
        def open(self,*args,**kwargs):
            raise URLError('dummy-test-secret')
    monkeypatch.setattr(acquisition,'build_opener',lambda _:Opener())
    with pytest.raises(TransportError) as error:
        acquisition._http_get(acquisition.ENDPOINT,{},15,1000)
    assert CREDS.secret not in str(error.value)


def test_cli_plan_is_offline_and_missing_keys_are_sanitised(tmp_path):
    env={k:v for k,v in os.environ.items() if k not in ('APCA_API_KEY_ID','APCA_API_SECRET_KEY')}
    command=[sys.executable,'-m','orb','fetch-alpaca','--start',START,'--end',END,'--out-dir',str(tmp_path/'bundle')]
    plan=subprocess.run(command+['--plan'],env=env,capture_output=True,text=True,check=True)
    parsed=json.loads(plan.stdout)
    assert parsed['maximum_http_attempts'] == 60 and parsed['method'] == 'GET' and not parsed['orders']
    assert 'secret' not in plan.stdout.lower()
    failed=subprocess.run(command,env=env,capture_output=True,text=True)
    assert failed.returncode == 2 and 'configure APCA_API_KEY_ID' in failed.stderr
    assert not (tmp_path/'bundle').exists()
