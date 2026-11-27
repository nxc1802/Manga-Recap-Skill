"""No live API calls: exercise explicit single-account selection and private credential storage."""
import json
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.cli.exit_codes import CliValidationError
from src.cloud.account_manager import KaggleAccountManager
from src.cloud.kaggle_service import KaggleCloudService
from src.providers.kaggle import KaggleAuthError
from tests.workflow.test_canonical_workflow import unit_project  # noqa: F401


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


def test_auth_failure_does_not_rotate_accounts_and_explicit_account_binds(accounts, monkeypatch):
    seen = []

    def auth(self):
        seen.append(self.api_token)
        self._api = object() if self.api_token == 'KGAT_test_2' else None

    monkeypatch.setattr(KaggleCloudService, '_authenticate', auth)
    # Default is account_1; failing auth MUST NOT silently rotate to account_2 (E05)
    service = KaggleCloudService()
    assert service.account_name == 'account_1'
    assert seen == ['KGAT_test_1']
    assert not service.is_authenticated

    # Explicit account selection binds only that configured account
    seen.clear()
    explicit_acct = KaggleCloudService(account_name='account_2')
    assert explicit_acct.account_name == 'account_2' and explicit_acct.is_authenticated
    assert seen == ['KGAT_test_2']

    with pytest.raises(KaggleAuthError, match='missing_account'):
        KaggleCloudService(account_name='missing_account')

    seen.clear()
    explicit_tok = KaggleCloudService(api_token='KGAT_explicit')
    assert seen == ['KGAT_explicit'] and not explicit_tok.is_authenticated


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


def test_dispatch_pins_single_account_and_rejects_without_rotation(accounts, unit_project, monkeypatch):
    import tarfile
    from src.cloud.kaggle_service import KaggleExecutionError, KaggleQuotaExhaustedError

    root, chapter = unit_project
    pushes = []
    quota_mode = {'quota_for_acct_1': True}

    class Api:
        def __init__(self, name): self.name = name

        def dataset_create_new(self, folder, **kwargs):
            from pathlib import Path
            data = json.loads((Path(folder) / 'dataset-metadata.json').read_text())
            assert data['id'].startswith(self.name + '/')
            assert kwargs['public'] is False

        def dataset_status(self, ref): return SimpleNamespace(status='ready')
        def dataset_list_files(self, ref): return SimpleNamespace(dataset_files=[SimpleNamespace(name='payload.tar.gz')])

        def kernels_push(self, folder):
            from pathlib import Path
            metadata = json.loads((Path(folder) / 'kernel-metadata.json').read_text())
            assert metadata['id'].startswith(self.name + '/')
            assert 'KGAT_' not in (Path(folder) / 'worker.py').read_text()
            with tarfile.open(Path(folder) / 'payload.tar.gz') as archive:
                assert not any('kaggle_accounts' in name for name in archive.getnames())
            pushes.append(self.name)
            if quota_mode['quota_for_acct_1']:
                return SimpleNamespace(error='GPU quota exceeded')
            return SimpleNamespace(error='kernel slug conflict pre-submit')

    def auth(self): self._username = self.account_name; self._api = Api(self.account_name)
    monkeypatch.setattr(KaggleCloudService, '_authenticate', auth)
    monkeypatch.setattr('src.cloud.kaggle_service.time.sleep', lambda *_: None)
    service = KaggleCloudService()
    before = (chapter / 'chapter.json').read_bytes()

    # Quota rejection stays pinned to account_1 (E05) and rolls back reservation cleanly.
    with pytest.raises(KaggleQuotaExhaustedError, match='quota'):
        service.dispatch_chapter_job('unit', '1', job_type='render', wait=False)
    assert pushes == ['account_1']
    assert not (chapter / 'cloud_job.json').exists()

    # Non-quota pre-submit rejection ALSO fails cleanly on account_1 without rotating to account_2 (E05).
    quota_mode['quota_for_acct_1'] = False
    with pytest.raises(KaggleExecutionError, match='submission'):
        service.dispatch_chapter_job('unit', '1', job_type='render', wait=False)
    assert pushes == ['account_1', 'account_1']
    assert not (chapter / 'cloud_job.json').exists()
    assert (chapter / 'chapter.json').read_bytes() == before
