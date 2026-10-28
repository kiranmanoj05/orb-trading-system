from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from decimal import Decimal as D
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from orb.dashboard import build_dashboard_run
from orb.dashboard_fixtures import scenarios
from orb.dashboard_import import import_dashboard
from orb.evaluation import _digest
from orb.presets import PresetStore, PresetConflict, REFERENCE
from orb.profile_runs import apply_preset
from orb.research_settings import ResearchSettings, settings_contract
from test_dashboard_import import csv_text


def settings(**changes):
    return {**ResearchSettings().to_dict(), **changes}


def original(name='complete_trade',**kwargs):
    r=build_dashboard_run(scenarios()[name],**kwargs)
    r['import_context']=dict(kind='example',synthetic=True,notice='synthetic')
    return r


def test_canonical_contract_and_no_binary_float_settings():
    a=ResearchSettings.from_dict(settings(fee_per_share='-0.00',risk_fraction='0.00500'))
    assert a==ResearchSettings()
    assert a.fingerprint==ResearchSettings().fingerprint
    assert a.to_dict()['risk_fraction']=='0.005'
    assert a.to_dict()['fee_per_share']=='0'
    assert set(a.engine_kwargs())=={'initial_cash','limits','daily_loss_fraction','slippage_bps','fee_per_share'}
    assert settings_contract()['unsupported']


@pytest.mark.parametrize('key,value',[('risk_fraction','0'),('risk_fraction','1.1'),('daily_loss_fraction','0'),
 ('initial_cash','-1'),('initial_cash','Infinity'),('initial_cash','1e999999999'),
 ('fee_per_share','NaN'),('fee_per_share','-0.01'),('fee_per_share','1001'),
 ('slippage_bps','10000'),('risk_fraction',0.005),('risk_fraction',True),
 ('max_position_fraction','1.01'),('max_gross_fraction','1e-13')])
def test_invalid_settings_fail_before_engine_use(key,value):
    with pytest.raises(ValueError):ResearchSettings.from_dict(settings(**{key:value}))


@pytest.mark.parametrize('extra', ['target_r','weekdays','max_session_entries','stop_policy'])
def test_unsupported_settings_are_not_ignored(extra):
    with pytest.raises(ValueError):ResearchSettings.from_dict({**settings(),extra:'3'})


def test_immutable_versions_reference_and_restart(tmp_path):
    db=tmp_path/'presets.sqlite'
    with PresetStore(db) as s:
        fixed=s.get(REFERENCE)
        with pytest.raises(PresetConflict):s.save('changed',settings(),preset_id=REFERENCE,expected_version=1)
        copy=s.duplicate(REFERENCE,1,'Research variant')
        updated=s.save('Risk study',settings(fee_per_share='.01'),preset_id=copy['id'],expected_version=1)
        assert updated['version']==2
        assert s.get(copy['id'],1)==copy
        assert len(s.history(copy['id']))==2
        with pytest.raises(PresetConflict):s.save('stale',settings(),preset_id=copy['id'],expected_version=1)
    with PresetStore(db) as s:
        assert s.get(copy['id'])==updated
        assert s.get(REFERENCE)==fixed
        assert len(s.list())==3
        # Editing returned values cannot rewrite a stored snapshot.
        updated['settings']['fee_per_share']='99'
        assert s.get(copy['id'])['settings']['fee_per_share']=='0.01'


def test_competing_connections_only_one_stale_version_can_commit(tmp_path):
    path=tmp_path/'p.sqlite'
    with PresetStore(path) as s:p=s.save('Study',settings())
    def update(name):
        with PresetStore(path) as s:
            try:return s.save(name,settings(fee_per_share='.02'),preset_id=p['id'],expected_version=1)['version']
            except PresetConflict:return 'conflict'
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(update,['a','b']))
    assert sorted(map(str,results))==['2','conflict']
    with PresetStore(path) as s:assert len(s.history(p['id']))==2


def test_failed_write_rolls_back_without_changing_head(tmp_path):
    with PresetStore(tmp_path/'p.sqlite') as s:
        p=s.save('Study',settings())
        s.db.execute("CREATE TRIGGER fail_new BEFORE INSERT ON preset_versions BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(sqlite3.IntegrityError):s.save('Fail',settings(),preset_id=p['id'],expected_version=1)
        assert s.get(p['id'])==p
        assert len(s.history(p['id']))==1


def test_unknown_databases_and_corrupt_settings_are_not_adopted(tmp_path):
    path=tmp_path/'foreign.sqlite'
    c=sqlite3.connect(path);c.execute('CREATE TABLE secrets(value)');c.commit();c.close()
    before=path.read_bytes()
    with pytest.raises(ValueError):PresetStore(path)
    assert path.read_bytes()==before
    with PresetStore(tmp_path/'ours.sqlite') as s:
        p=s.save('Study',settings())
        s.db.execute('UPDATE preset_versions SET fingerprint=?',('bad',))
        with pytest.raises(ValueError,match='integrity'):s.get(p['id'])


@pytest.mark.parametrize('name',['complete_trade','known_skip','missing_entry'])
def test_apply_is_new_frozen_run_with_current_metrics_and_original_unchanged(name):
    source=original(name);before=deepcopy(source)
    with PresetStore() as s:
        p=s.save('Cost study',settings(fee_per_share='.01',initial_cash='12000'))
        result=apply_preset(source,p)
        s.save('Later',settings(fee_per_share='.2'),preset_id=p['id'],expected_version=1)
    assert source==before
    assert result['evaluation_id']!=source['evaluation_id']
    assert result['evaluation_id']==_digest(result['evidence']['manifest'])
    assert result['applied_settings']['preset']['version']==1
    assert result['applied_settings']['values']['fee_per_share']=='0.01'
    assert result['applied_settings']['values']['initial_cash']=='12000'
    assert result['evidence']['manifest']['dataset']['sha256']==source['evidence']['manifest']['dataset']['sha256']
    assert result['metrics_eligible']==source['metrics_eligible']
    assert result['capabilities']['order_submission'] is False
    if name=='complete_trade':
        # 12k capital admits 29 shares; 2.60 gross/share less 0.01 per side.
        assert D(result['evidence']['summary']['closed_subset']['net_pnl'])==D('74.82')
        assert result['decisions'][0]['trade']['quantity']==29
        assert D(result['decisions'][0]['trade']['fees'])==D('.58')
    if name=='missing_entry':assert result['evidence']['summary']['total_return'] is None


def test_apply_preserves_explicit_missing_day_and_bundle_identity():
    source=original(start_session=date(2026,10,8),end_session=date(2026,10,9))
    with PresetStore() as s:r=apply_preset(source,s.get(REFERENCE,1))
    assert len(r['evidence']['coverage'])==4
    assert not r['metrics_eligible']
    capture=Path('fixtures/alpaca_capture_synthetic.json').read_text()
    source=import_dashboard(dict(kind='capture',source=capture))
    with PresetStore() as s:r=apply_preset(source,s.get(REFERENCE,1))
    assert r['evidence']['manifest']['dataset']['source_bundle']==source['evidence']['manifest']['dataset']['source_bundle']
    again=import_dashboard(dict(kind='capture',source=capture,report=r['evidence']))
    assert again['applied_settings']['preset']==r['applied_settings']['preset']
    assert not again['metrics_eligible']


def test_cli_uses_same_contract_and_exclusive_outputs(tmp_path):
    def cli(*args):return subprocess.run([sys.executable,'-m','orb.presets','--db',str(tmp_path/'p.sqlite'),*args],capture_output=True,text=True)
    template=cli('template');assert template.returncode==0
    file=tmp_path/'settings.json';file.write_text(json.dumps(settings(fee_per_share='.01')))
    created=cli('create','--name','CLI study','--settings',str(file));assert created.returncode==0,created.stderr
    p=json.loads(created.stdout)
    csv=tmp_path/'bars.csv';csv.write_text(csv_text(scenarios()['complete_trade']))
    output=tmp_path/'run.json'
    args=('run','--id',p['id'],'--version','1','--csv',str(csv),'--output',str(output))
    run=cli(*args);assert run.returncode==0,run.stderr
    data=json.loads(output.read_text())
    assert data['applied_settings']['preset']['settings_sha256']==p['settings_sha256']
    before=output.read_bytes();assert cli(*args).returncode==2;assert output.read_bytes()==before
    assert len(json.loads(cli('list').stdout))==3


@pytest.mark.parametrize('committed',[False,True])
def test_abrupt_subprocess_exit_preserves_transaction_boundary(tmp_path,committed):
    path=tmp_path/'p.sqlite'
    with PresetStore(path) as s:p=s.save('Study',settings())
    code='''
import os,sys
from orb.presets import PresetStore
p,id,committed=sys.argv[1:]
s=PresetStore(p)
if committed=='True':
 s.save('Committed',s.get(id)['settings'],preset_id=id,expected_version=1)
else:
 s.db.execute('BEGIN IMMEDIATE')
 s.db.execute("INSERT INTO preset_versions SELECT preset_id,2,'not committed',settings,fingerprint,created_at FROM preset_versions WHERE preset_id=? AND version=1",(id,))
os._exit(19)
'''
    r=subprocess.run([sys.executable,'-c',code,str(path),p['id'],str(committed)])
    assert r.returncode==19
    with PresetStore(path) as s:
        assert s.get(p['id'])['version']==(2 if committed else 1)


@pytest.mark.parametrize('key,value',[('version',None),('version',True),('name',{}),('settings_sha256','wrong')])
def test_invalid_profile_snapshot_cannot_be_imported_as_applied_metadata(key,value):
    source=original()
    with PresetStore() as s:r=apply_preset(source,s.get(REFERENCE,1))
    saved=deepcopy(r['evidence'])
    saved['manifest']['profile'][key]=value
    saved['evaluation_id']=_digest(saved['manifest'])
    with pytest.raises(ValueError):import_dashboard(dict(kind='csv',source=csv_text(scenarios()['complete_trade']),report=saved))
