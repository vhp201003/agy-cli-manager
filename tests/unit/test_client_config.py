import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agy_cli_manager.manager import (
    ManagerPaths,
    client_config_path,
    connect_gateway,
    disconnect_gateway,
    get_client_status,
    load_client_config,
    reset_client_config,
    resolve_proxy_endpoint,
    save_client_config,
)


@pytest.fixture
def temp_paths(tmp_path: Path) -> ManagerPaths:
    return ManagerPaths(
        root=tmp_path,
        accounts_dir=tmp_path / "accounts",
        state_file=tmp_path / "state.json",
        runtime_dir=tmp_path / "runtime",
        lock_file=tmp_path / "lock",
    )


def test_client_config_crud(temp_paths: ManagerPaths):
    assert load_client_config(temp_paths.root) == {}

    data = {"gateway_url": "http://10.0.0.1:8800", "proxy_host": "10.0.0.1", "proxy_port": 8899}
    save_client_config(data, temp_paths.root)

    loaded = load_client_config(temp_paths.root)
    assert loaded == data

    reset_client_config(temp_paths.root)
    assert load_client_config(temp_paths.root) == {}


def test_resolve_proxy_endpoint_fallback(temp_paths: ManagerPaths, monkeypatch):
    monkeypatch.delenv("AGY_PROXY_URL", raising=False)
    monkeypatch.delenv("AGY_PROXY_HOST", raising=False)
    monkeypatch.delenv("AGY_PROXY_PORT", raising=False)

    host, port = resolve_proxy_endpoint(temp_paths)
    assert host == "127.0.0.1"
    assert port == 8899

    save_client_config({"proxy_host": "192.168.1.100", "proxy_port": 9000}, temp_paths.root)
    host, port = resolve_proxy_endpoint(temp_paths)
    assert host == "192.168.1.100"
    assert port == 9000


def test_resolve_proxy_endpoint_env_override(temp_paths: ManagerPaths, monkeypatch):
    save_client_config({"proxy_host": "192.168.1.100", "proxy_port": 9000}, temp_paths.root)

    monkeypatch.setenv("AGY_PROXY_URL", "http://gateway.lan:8899")
    host, port = resolve_proxy_endpoint(temp_paths)
    assert host == "gateway.lan"
    assert port == 8899

    monkeypatch.delenv("AGY_PROXY_URL")
    monkeypatch.setenv("AGY_PROXY_HOST", "custom.lan")
    monkeypatch.setenv("AGY_PROXY_PORT", "7777")
    host, port = resolve_proxy_endpoint(temp_paths)
    assert host == "custom.lan"
    assert port == 7777


def test_connect_and_disconnect_gateway(temp_paths: ManagerPaths):
    fake_status = {
        "pool_size": 3,
        "available_accounts": 3,
        "proxy_port": 8899,
    }
    fake_ca = b"-----BEGIN CERTIFICATE-----\nFAKECA\n-----END CERTIFICATE-----\n"

    def fake_urlopen(req, timeout=5.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/api/status" in url:
            return io.BytesIO(json.dumps(fake_status).encode("utf-8"))
        if "/ca.crt" in url:
            return io.BytesIO(fake_ca)
        raise ValueError(f"Unknown URL {url}")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        cfg = connect_gateway("http://10.0.0.1:8800", paths=temp_paths)
        assert cfg["gateway_url"] == "http://10.0.0.1:8800"
        assert cfg["proxy_host"] == "10.0.0.1"
        assert cfg["proxy_port"] == 8899
        assert cfg["pool_size"] == 3

        status = get_client_status(temp_paths)
        assert status["configured"] is True
        assert status["gateway_url"] == "http://10.0.0.1:8800"

        disconnect_gateway(temp_paths)
        status_after = get_client_status(temp_paths)
        assert status_after["configured"] is False
