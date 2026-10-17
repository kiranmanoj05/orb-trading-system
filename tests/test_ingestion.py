from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import subprocess
import sys

import pytest

from orb.ingestion import evaluate_bundle, import_capture, load_bundle, normalise_capture
from orb.replay import load_bars


FIXTURE = Path('fixtures/alpaca_capture_synthetic.json')


def capture():
    return json.loads(FIXTURE.read_bytes())


def encode(value):
    return json.dumps(value).encode()


def test_provider_symbol_order_becomes_chronological_without_gap_filling():
    bars, csv, manifest = normalise_capture(FIXTURE.read_bytes())
    assert len(bars) == manifest['bars'] == 35
    assert [b.symbol for b in bars[:4]] == ['QQQ','SPY','QQQ','SPY']
    assert bars == sorted(bars, key=lambda b:(b.start,b.symbol))
    assert bars[-1].start.isoformat() == '2026-10-09T13:47:00+00:00'  # Inclusive end.
    assert manifest['page_count'] == 2
    assert manifest['data_kind'] == 'synthetic'
    assert not manifest['provider_authenticity_verified']
    assert not manifest['point_in_time_verified']
    assert b'99.5' in csv


def test_numeric_precision_is_not_parsed_through_binary_float():
    raw = FIXTURE.read_bytes().replace(b'"o": 99,', b'"o": 99.00000000000000000001,', 1)
    bars, csv, _ = normalise_capture(raw)
    assert bars[0].open == D('99.00000000000000000001')
    assert b'99.00000000000000000001' in csv


def test_empty_intermediate_page_is_valid_only_with_continuation():
    c = capture()
    c['pages'].insert(0, {'page_token':None, 'response':{'bars':None,'next_page_token':'empty-next'}})
    c['pages'][1]['page_token'] = 'empty-next'
    assert normalise_capture(encode(c))[2]['page_count'] == 3


@pytest.mark.parametrize('field,value', [
    ('timeframe','5Min'), ('feed','boats'), ('adjustment','split'), ('asof','2026-10-09'),
    ('currency','EUR'), ('sort','desc'), ('symbols',['SPY']), ('limit',0), ('limit',10001), ('limit',True),
])
def test_explicit_baseline_request_contract(field,value):
    c = capture()
    c['request'][field] = value
    with pytest.raises(ValueError):
        normalise_capture(encode(c))


@pytest.mark.parametrize('case', ['missing_final','wrong_token','loop','extra_final','empty_extra_final'])
def test_pagination_failures_are_not_treated_as_complete(case):
    c = capture()
    if case == 'missing_final':
        c['pages'].pop()
    elif case == 'wrong_token':
        c['pages'][1]['page_token'] = 'wrong'
    elif case == 'loop':
        c['pages'][1]['response']['next_page_token'] = 'synthetic-page-2'
    else:
        if case == 'empty_extra_final':
            c['pages'] = [{'page_token':None,'response':{'bars':{},'next_page_token':None}}]
        c['pages'].append({'page_token':None,'response':{'bars':{},'next_page_token':None}})
    with pytest.raises(ValueError):
        normalise_capture(encode(c))


@pytest.mark.parametrize('case', ['duplicate','reversed_rows','symbol_regression','outside_bounds',
                                  'unfinished','nanoseconds','bad_ohlc','fractional_volume',
                                  'bool_price','string_price','unexpected_symbol','response_currency'])
def test_bad_response_data_is_not_repaired(case):
    c = capture()
    rows = c['pages'][0]['response']['bars']['QQQ']
    if case == 'duplicate':
        rows.insert(1,deepcopy(rows[0]))
    elif case == 'reversed_rows':
        rows.reverse()
    elif case == 'symbol_regression':
        a,b = c['pages']
        a['response']['bars'],b['response']['bars'] = b['response']['bars'],a['response']['bars']
    elif case == 'outside_bounds':
        c['request']['start'] = '2026-10-09T13:31:00Z'
    elif case == 'unfinished':
        c['retrieved_at'] = '2026-10-09T13:47:00Z'
    elif case == 'nanoseconds':
        rows[0]['t'] = '2026-10-09T13:30:00.000000001Z'
    elif case == 'bad_ohlc':
        rows[0]['h'] = 97
    elif case == 'fractional_volume':
        rows[0]['v'] = 100.5
    elif case == 'bool_price':
        rows[0]['o'] = True
    elif case == 'string_price':
        rows[0]['o'] = '99'
    elif case == 'unexpected_symbol':
        c['pages'][0]['response']['bars']['AAPL'] = []
    elif case == 'response_currency':
        c['pages'][0]['response']['currency'] = 'EUR'
    with pytest.raises(ValueError):
        normalise_capture(encode(c))


@pytest.mark.parametrize('raw', [b'{"provider":"alpaca","provider":"other"}', b'{"x":NaN}'])
def test_ambiguous_or_nonfinite_json_is_rejected(raw):
    with pytest.raises(ValueError):
        normalise_capture(raw)


def test_unknown_fields_prevent_archiving_headers_and_credential_metadata(tmp_path):
    c = capture()
    c['headers'] = {'APCA-API-KEY-ID':'do-not-archive'}
    source = tmp_path/'source.json'
    source.write_bytes(encode(c))
    with pytest.raises(ValueError):
        import_capture(source,tmp_path/'bundle')
    assert not (tmp_path/'bundle').exists()


def test_page_limit_and_empty_dataset_fail_explicitly():
    c = capture()
    c['request']['limit'] = 1
    with pytest.raises(ValueError, match='limit'):
        normalise_capture(encode(c))
    c = capture()
    c['pages'] = [{'page_token':None,'response':{'bars':None,'next_page_token':None}}]
    with pytest.raises(ValueError, match='no bars'):
        normalise_capture(encode(c))


def test_bundle_reimport_is_reproducible_and_existing_target_not_overwritten(tmp_path):
    a,b = tmp_path/'a',tmp_path/'b'
    manifest = import_capture(FIXTURE,a)
    assert manifest == import_capture(FIXTURE,b)
    assert all((a/n).read_bytes() == (b/n).read_bytes() for n in ('capture.json','bars.csv','manifest.json'))
    bars,loaded = load_bundle(a)
    assert bars == load_bars(a/'bars.csv') and loaded == manifest
    with pytest.raises(FileExistsError):
        import_capture(FIXTURE,a)
    assert load_bundle(a)[1] == manifest


@pytest.mark.parametrize('filename', ['capture.json','bars.csv','manifest.json'])
def test_bundle_tampering_rejected(tmp_path,filename):
    destination = tmp_path/'bundle'
    import_capture(FIXTURE,destination)
    file = destination/filename
    if filename == 'capture.json':
        file.write_bytes(file.read_bytes().replace(b'"v": 100',b'"v": 101',1))
    elif filename == 'bars.csv':
        file.write_bytes(file.read_bytes()+b'\n')
    else:
        m = json.loads(file.read_bytes())
        m['provider_authenticity_verified'] = True
        file.write_bytes(encode(m))
    with pytest.raises(ValueError,match='integrity'):
        load_bundle(destination)


def test_symlink_and_partial_bundle_are_rejected(tmp_path):
    destination = tmp_path/'bundle'
    destination.mkdir()
    (destination/'capture.json').symlink_to(FIXTURE.resolve())
    with pytest.raises(ValueError,match='symlink'):
        load_bundle(destination)
    (destination/'capture.json').unlink()
    (destination/'capture.json').write_bytes(FIXTURE.read_bytes())
    with pytest.raises(OSError):
        load_bundle(destination)


def test_bundle_identity_binds_request_and_raw_capture_not_just_bars(tmp_path):
    a = tmp_path/'a'
    import_capture(FIXTURE,a)
    first = evaluate_bundle(a,daily_loss_fraction=D('.001'))
    c = capture()
    c['request']['feed'] = 'sip'
    source = tmp_path/'source.json'
    source.write_bytes(encode(c))
    b = tmp_path/'b'
    import_capture(source,b)
    second = evaluate_bundle(b,daily_loss_fraction=D('.001'))
    assert first['manifest']['dataset']['sha256'] == second['manifest']['dataset']['sha256']
    assert first['evaluation_id'] != second['evaluation_id']
    assert not first['manifest']['dataset']['provenance_verified']
    assert first['manifest']['dataset']['source_bundle']['integrity_verified']
    assert first['summary']['trade_status_counts'] == {'closed':1,'unresolved':0,'unfilled':0,'rejected':1}
    assert D(first['portfolio']['account']['cash']) == D('9968.8')


def test_cli_import_evaluate_repeatability_and_bundle_output_protection(tmp_path):
    bundle = tmp_path/'bundle'
    create = [sys.executable,'-m','orb','import-alpaca',str(FIXTURE),'--out-dir',str(bundle)]
    subprocess.run(create,capture_output=True,text=True,check=True)
    out,report = tmp_path/'evaluation.json',tmp_path/'evaluation.md'
    command = [sys.executable,'-m','orb','evaluate-bundle',str(bundle),'--daily-loss-fraction','.001',
               '--output',str(out),'--report',str(report)]
    first = subprocess.run(command,capture_output=True,text=True,check=True)
    second = subprocess.run(command,capture_output=True,text=True,check=True)
    assert first.stdout == second.stdout == out.read_text()
    assert 'provider authenticity and point-in-time availability unverified' in report.read_text()
    original = (bundle/'capture.json').read_bytes()
    invalid = subprocess.run(command+['--output',str(bundle/'capture.json')],capture_output=True,text=True)
    assert invalid.returncode == 2
    assert (bundle/'capture.json').read_bytes() == original
    assert subprocess.run(create,capture_output=True).returncode == 2
