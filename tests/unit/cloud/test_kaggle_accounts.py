"""No live API calls: exercise sequential fallback and private credential storage."""
import argparse
import json
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.cli.exit_codes import CliValidationError
from src.cloud.account_manager import KaggleAccountManager
from src.cloud.account_fallback import with_account_fallback
from src.cloud.kaggle_service import KaggleCloudService


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.delenv('KAGGLE_API_TOKEN', raising=False)
    manager = KaggleAccountManager(tmp_path/'private/accounts.json')
    monkeypatch.setenv('KAGGLE_ACCOUNTS_FILE',str(manager.registry_file))
    for i in range(1,4): manager.add(f'account_{i}',f'KGAT_test_{i}')
    return manager


def test_registry_order_dedup_permissions_and_management(accounts):
    assert accounts.add('duplicate','KGAT_test_1')=='account_1'
    assert len(accounts.list_accounts())==3
    assert accounts.registry_file.stat().st_mode & 0o777 == 0o600
    assert 'KGAT_' not in json.dumps(accounts.list_accounts())
    accounts.set_default('account_2')
    assert [name for name,_ in accounts.candidates()]==['account_2','account_1','account_3']
    accounts.remove('account_2')
    assert accounts.list_accounts()[0]['is_default']
    with pytest.raises(CliValidationError): accounts.add('bad','wrong_token')
    with pytest.raises(CliValidationError): accounts.set_default('missing')
    with pytest.raises(CliValidationError): accounts.remove('missing')


def test_legacy_token_kept_once_and_default_respected(accounts,monkeypatch):
    monkeypatch.setenv('KAGGLE_API_TOKEN','KGAT_test_1')
    assert len(accounts.candidates())==3
    monkeypatch.setenv('KAGGLE_API_TOKEN','KGAT_legacy')
    assert len(accounts.candidates())==3  # registry is authoritative after migration
    accounts.set_default('account_3')
    assert accounts.candidates()[0][0]=='account_3'


@pytest.mark.parametrize('outcomes',[
    [False,{'status':'success'}],
    [RuntimeError('quota exceeded KGAT_test_1'),{'status':'success'}],
    [RuntimeError('2 sessions running'),False,{'status':'success'}],
    [{'status':'error'}, {'success':False}, {'status':'dispatched'}],
    [False,False,False],
    [{},None,False],
    [CliValidationError('G6 missing')],
    [ValueError('Artifact hash mismatch')],
])
def test_fallback_once_success_stop_and_validation_fail_closed(accounts,monkeypatch,outcomes,caplog):
    def auth(self): self._api=object(); self._username=self.account_name
    monkeypatch.setattr(KaggleCloudService,'_authenticate',auth)
    service=KaggleCloudService()
    calls=[]
    @with_account_fallback
    def dispatch(self):
        calls.append(self.account_name)
        result=outcomes[len(calls)-1]
        if isinstance(result,Exception): raise result
        return result
    if len(outcomes)==1:
        with pytest.raises(type(outcomes[0])): dispatch(service)
    elif outcomes[-1] is False:
        with pytest.raises(RuntimeError,match='All configured'): dispatch(service)
    else:
        assert dispatch(service)==outcomes[-1]
    assert calls==[f'account_{i+1}' for i in range(len(outcomes))]
    assert 'KGAT_' not in caplog.text


def test_auth_failure_moves_to_next_and_explicit_token_is_pinned(accounts,monkeypatch):
    seen=[]
    def auth(self):
        seen.append(self.api_token)
        self._api=object() if self.api_token=='KGAT_test_2' else None
    monkeypatch.setattr(KaggleCloudService,'_authenticate',auth)
    service=KaggleCloudService()
    assert service.account_name=='account_2' and len(seen)==2
    assert service.is_authenticated
    seen.clear()
    service=KaggleCloudService(api_token='KGAT_explicit')
    assert seen==['KGAT_explicit'] and not service.is_authenticated


def test_sdk_credentials_are_bound_and_environment_restored(accounts,monkeypatch):
    package=ModuleType('kaggle');api_package=ModuleType('kaggle.api');api_module=ModuleType('kaggle.api.kaggle_api_extended')
    class FakeApi:
        CONFIG_NAME_TOKEN='token';CONFIG_NAME_USER='username'
        def __init__(self): self.config_values={}
        def authenticate(self):
            self.config_values={'token':os.environ['KAGGLE_API_TOKEN'],'username':'owner'}
    api_module.KaggleApi=FakeApi
    for key,value in {'kaggle':package,'kaggle.api':api_package,'kaggle.api.kaggle_api_extended':api_module}.items():
        monkeypatch.setitem(sys.modules,key,value)
    monkeypatch.setenv('KAGGLE_API_TOKEN','KGAT_original')
    a=KaggleCloudService(api_token='KGAT_one');b=KaggleCloudService(api_token='KGAT_two')
    assert os.environ['KAGGLE_API_TOKEN']=='KGAT_original'
    assert a._api.config_values['token']=='KGAT_one'
    assert b._api.config_values['token']=='KGAT_two'
    monkeypatch.delenv('KAGGLE_API_TOKEN')
    KaggleCloudService(api_token='KGAT_three')
    assert 'KAGGLE_API_TOKEN' not in os.environ


@pytest.mark.parametrize('response',[False,None,{}, {'error':'quota exceeded'},SimpleNamespace(error='session limit')])
def test_rejected_push_is_never_dispatched(response):
    with pytest.raises(RuntimeError,match='rejected'):
        KaggleCloudService._require_kernel_submission(response)


def test_cli_accounts_needs_no_series_setup_or_auth(accounts,monkeypatch,capsys):
    from src.cli.main import main
    monkeypatch.setattr('src.cli.cloud_cmd.KaggleCloudService',MagicMock(side_effect=AssertionError('No API on management')))
    assert main(['cloud','accounts','--json'])==0
    response=json.loads(capsys.readouterr().out)
    assert len(response['data']['accounts'])==3
    assert 'KGAT_' not in json.dumps(response)
    assert main(['cloud','accounts','--account-action','default','--account','account_3','--json'])==0
    capsys.readouterr()
    assert accounts.candidates()[0][0]=='account_3'



def test_first_add_preserves_existing_and_remove_is_effective(tmp_path,monkeypatch):
    monkeypatch.setenv('KAGGLE_API_TOKEN','KGAT_old')
    manager=KaggleAccountManager(tmp_path/'accounts.json')
    manager.add('new','KGAT_new')
    assert manager.candidates()==[('existing','KGAT_old'),('new','KGAT_new')]
    manager.remove('existing')
    assert manager.candidates()==[('new','KGAT_new')]
    manager.remove('new')
    assert manager.candidates()==[]


def test_reused_service_tries_all_accounts_without_persisting_failure_state(accounts,monkeypatch):
    monkeypatch.setattr(KaggleCloudService,'_authenticate',lambda self:setattr(self,'_api',object()))
    service=KaggleCloudService()
    service._select_account(2)
    seen=[]
    @with_account_fallback
    def operation(self):
        seen.append(self.account_name)
        return False if self.account_name=='account_3' else {'status':'success'}
    assert operation(service)['status']=='success'
    assert seen==['account_3','account_1']
    assert set(json.loads(accounts.registry_file.read_text()))=={'accounts','default_account'}


# The canonical fixture contains isolated image/PCM inputs, not live production evidence.
from unit.test_canonical_workflow import unit_project


def test_real_dispatch_fallback_on_rejected_submission(accounts,unit_project,monkeypatch):
    import tarfile
    root,chapter=unit_project
    pushes=[]
    class Api:
        def __init__(self,name): self.name=name
        def dataset_create_new(self,folder,**kwargs):
            from pathlib import Path
            data=json.loads((Path(folder)/'dataset-metadata.json').read_text())
            assert data['id'].startswith(self.name+'/')
            assert kwargs['public'] is False
        def dataset_status(self,ref): return SimpleNamespace(status='ready')
        def dataset_list_files(self,ref): return SimpleNamespace(dataset_files=[SimpleNamespace(name='payload.tar.gz')])
        def kernels_push(self,folder):
            from pathlib import Path
            metadata=json.loads((Path(folder)/'kernel-metadata.json').read_text())
            assert metadata['id'].startswith(self.name+'/')
            assert 'KGAT_' not in (Path(folder)/'worker.py').read_text()
            with tarfile.open(Path(folder)/'payload.tar.gz') as archive:
                assert not any('kaggle_accounts' in name for name in archive.getnames())
            pushes.append(self.name)
            if self.name=='account_1': return SimpleNamespace(error='GPU quota exceeded')
            return SimpleNamespace(ref=self.name+'/accepted',error='')
    def auth(self): self._username=self.account_name;self._api=Api(self.account_name)
    monkeypatch.setattr(KaggleCloudService,'_authenticate',auth)
    monkeypatch.setattr('src.cloud.kaggle_service.time.sleep',lambda *_:None)
    service=KaggleCloudService()
    before=(chapter/'chapter.json').read_bytes()
    result=service.dispatch_chapter_job('unit','1',job_type='render',wait=False)
    assert pushes==['account_1','account_2']
    assert result['status']=='dispatched' and result['kernel_ref']=='account_2/accepted'
    assert (chapter/'chapter.json').read_bytes()==before
