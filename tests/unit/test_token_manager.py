from pathlib import Path
import json
import urllib.error
import urllib.parse
from unittest.mock import patch, MagicMock

from agy_cli_manager.proxy.token_manager import (
    _resolve_oauth_credentials,
    TokenManager,
)
from agy_cli_manager.manager import (
    build_paths,
    install_client_ca,
    resolve_ca_bundle_path,
    save_client_config,
)


def test_resolve_oauth_credentials_from_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_CLIENT_ID=file_id\nGOOGLE_CLIENT_SECRET=file_sec\n", encoding="utf-8")
    cid, csec = _resolve_oauth_credentials(tmp_path)
    assert cid == "file_id"
    assert csec == "file_sec"


def test_resolve_oauth_credentials_from_config_json(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"google_client_id": "cfg_id", "google_client_secret": "cfg_sec"}), encoding="utf-8")
    cid, csec = _resolve_oauth_credentials(tmp_path)
    assert cid == "cfg_id"
    assert csec == "cfg_sec"


def test_refresh_account_token_precedence(tmp_path: Path):
    tm = TokenManager(manager_root=tmp_path)
    tm._accounts["custom"] = {
        "name": "custom",
        "client_id": "custom-id-value",
        "client_secret": "custom-secret-value",
        "refresh_token": "dummy-refresh-token",
        "access_token": "old-token",
        "expires_at": 0,
    }

    captured_payload = {}

    def fake_open(req, timeout=15):
        nonlocal captured_payload
        data = urllib.parse.parse_qs(req.data.decode("utf-8"))
        captured_payload = {k: v[0] for k, v in data.items()}
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = json.dumps({"access_token": "new-tok", "expires_in": 3600}).encode("utf-8")
        return resp

    with patch("urllib.request.OpenerDirector.open", side_effect=fake_open):
        ok, msg = tm.refresh_account_token("custom")
        assert ok is True
        assert captured_payload["client_id"] == "custom-id-value"
        assert captured_payload["client_secret"] == "custom-secret-value"


def test_fetch_account_quota_recursion_guard(tmp_path: Path):
    tm = TokenManager(manager_root=tmp_path)
    tm._accounts["acc"] = {
        "name": "acc",
        "access_token": "valid-token",
        "expires_at": 9999999999,
        "quota": {"dummy": 1},
    }

    call_count = 0

    def fake_open(req, timeout=8):
        nonlocal call_count
        call_count += 1
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with patch("urllib.request.OpenerDirector.open", side_effect=fake_open), \
         patch.object(tm, "refresh_account_token", return_value=(True, "Refreshed")):
        res = tm.fetch_account_quota("acc")
        assert call_count == 2
        assert res == {"dummy": 1}


def test_resolve_ca_bundle_precedence(tmp_path: Path):
    paths = build_paths(tmp_path)
    custom_bundle = tmp_path / "custom_bundle.crt"
    custom_bundle.write_bytes(b"CERT")

    save_client_config({"ca_bundle": str(custom_bundle)}, root=tmp_path)
    resolved = resolve_ca_bundle_path(paths)
    assert resolved == custom_bundle


def test_install_client_ca_creates_bundle(tmp_path: Path, monkeypatch):
    paths = build_paths(tmp_path)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: type("Res", (), {"stdout": "True"})())
    bundle, ok = install_client_ca(paths, b"-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----")
    assert bundle.is_file()
    assert ok is True
