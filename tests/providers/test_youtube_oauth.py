import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.cli.main import main
from src.core.utils import DependencyNotFoundError
from src.youtube.account_manager import YouTubeAccountManager


@pytest.fixture
def oauth(tmp_path, monkeypatch):
    manager = YouTubeAccountManager(tmp_path)
    secrets = tmp_path / "client_secrets.json"
    secrets.write_text("{}")
    token = tmp_path / "token.json"
    token.write_text("{}")
    account = SimpleNamespace(account_name="channel-a", token_file=token, client_secrets_file=secrets)
    monkeypatch.setattr(manager, "get_account", lambda name: account)
    refresh = Mock(side_effect=RuntimeError("invalid_grant"))
    flow = Mock()
    credentials = Mock(valid=False, expired=True, refresh_token="example", refresh=refresh)
    modules = {
        "google.auth.transport.requests": SimpleNamespace(Request=Mock()),
        "google.oauth2.credentials": SimpleNamespace(Credentials=Mock(from_authorized_user_file=Mock(return_value=credentials))),
        "google_auth_oauthlib.flow": SimpleNamespace(InstalledAppFlow=Mock(from_client_secrets_file=Mock(return_value=flow))),
    }
    from importlib import import_module
    target = import_module("src.youtube.account_manager")
    monkeypatch.setattr(target, "importlib", SimpleNamespace(import_module=modules.__getitem__))
    monkeypatch.setattr("src.cli.youtube_cmd.account_manager", manager)
    return manager, flow, refresh


def test_invalid_grant_in_json_mode_returns_exit_4(oauth, monkeypatch, capsys):
    _, flow, refresh = oauth
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert main(["youtube", "auth", "--account", "channel-a", "--json"]) == 4
    message = json.loads(capsys.readouterr().out)["error"]["message"]
    assert "manga-recap youtube accounts --account-action add --account channel-a" in message
    refresh.assert_called_once()
    flow.run_local_server.assert_not_called()


def test_non_tty_disables_browser_even_when_requested(oauth, monkeypatch):
    manager, flow, _ = oauth
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(DependencyNotFoundError, match="need authorization"):
        manager.get_credentials("channel-a", allow_interactive=True)
    flow.run_local_server.assert_not_called()


def test_interactive_browser_has_five_minute_timeout(oauth, monkeypatch):
    manager, flow, _ = oauth
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    flow.run_local_server.side_effect = TimeoutError
    with pytest.raises(DependencyNotFoundError, match="did not complete"):
        manager.get_credentials("channel-a", allow_interactive=True)
    flow.run_local_server.assert_called_once_with(port=0, timeout_seconds=300)
