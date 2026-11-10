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


def test_fetch_account_quota_uses_bucket_id_for_gemini_family(tmp_path: Path):
    tm = TokenManager(manager_root=tmp_path)
    tm._accounts["acc"] = {
        "name": "acc",
        "access_token": "valid-token",
        "expires_at": 9999999999,
    }
    payload = {"groups": [{
        "displayName": "Google models",
        "buckets": [{
            "bucketId": "gemini-5h",
            "window": "5h",
            "remainingFraction": 0.42,
            "resetTime": "2026-11-10T12:00:00Z",
        }],
    }]}

    def fake_open(req, timeout=8):
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        return resp

    with patch("urllib.request.OpenerDirector.open", side_effect=fake_open):
        quota = tm.fetch_account_quota("acc")

    assert quota["gemini"]["5h"]["percent"] == 42.0
    assert quota["third_party"]["5h"]["percent"] == 100.0


def test_fetch_account_quota_sends_versioned_cli_user_agent_and_parses_gemini_weekly(tmp_path: Path):
    # The quota API returns the Gemini group, with the account's real weekly/5h state,
    # only to versioned antigravity/cli clients on the daily host the real app uses.
    tm = TokenManager(manager_root=tmp_path)
    tm._accounts["acc"] = {"name": "acc", "access_token": "valid-token", "expires_at": 9999999999}
    payload = {"groups": [
        {"displayName": "Gemini Models", "buckets": [
            {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.65, "resetTime": "2026-11-13T23:55:37Z"},
            {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.95, "resetTime": "2026-11-10T09:59:36Z"},
        ]},
        {"displayName": "Claude and GPT models", "buckets": [
            {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 0.24, "resetTime": "2026-11-11T21:58:09Z"},
        ]},
    ]}
    seen_agents = []
    seen_hosts = []

    def fake_open(req, timeout=8):
        seen_agents.append(req.get_header("User-agent"))
        seen_hosts.append(req.host)
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        return resp

    with patch("urllib.request.OpenerDirector.open", side_effect=fake_open):
        quota = tm.fetch_account_quota("acc")

    assert seen_hosts and seen_hosts[0] == "daily-cloudcode-pa.googleapis.com"
    assert seen_agents and seen_agents[0].startswith("antigravity/cli/")
    assert quota["gemini"]["weekly"]["percent"] == 65.0
    assert quota["gemini"]["weekly"]["reset_time"] == "2026-11-13T23:55:37Z"
    assert quota["third_party"]["weekly"]["percent"] == 24.0


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


def _fake_roster_opener(captured: dict, models: dict):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"models": models}).encode("utf-8")

    class _Opener:
        def open(self, req, timeout=None):
            captured["user_agent"] = req.get_header("User-agent")
            return _Resp()

    return _Opener()


def test_fetch_available_models_sends_versioned_client_user_agent_and_skips_disabled():
    # The backend gates newer SKUs (gemini-4-argon) on the client identity: a bare
    # "antigravity" UA gets a truncated roster and 404s on those models.
    from agy_cli_manager.manager import CODE_ASSIST_USER_AGENT

    captured: dict = {}
    roster = {
        "gemini-4-argon-low": {"thinkingLevel": 1},
        "gemini-4-argon-disabled": {"disabled": True},
        "gemini-2.5-flash": {},
    }
    with patch(
        "agy_cli_manager.proxy.token_manager.urllib.request.build_opener",
        return_value=_fake_roster_opener(captured, roster),
    ):
        models = TokenManager._fetch_available_models("tok")
    assert captured["user_agent"] == CODE_ASSIST_USER_AGENT
    assert set(models) == {"gemini-4-argon-low", "gemini-2.5-flash"}
