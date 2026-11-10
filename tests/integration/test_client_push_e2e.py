import importlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from agy_cli_manager.manager import (
    build_paths,
    delete_gateway_account,
    list_gateway_accounts,
    push_account_to_gateway,
    save_client_config,
    update_gateway_account,
)

fastapi_app = importlib.import_module("agy_cli_manager.proxy.fastapi_app")

ADMIN_KEY = "integration-admin-key"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_mock_gemini_home(root: Path, email: str = "developer.alice@gmail.com") -> Path:
    home = root / ".gemini" / "antigravity-cli"
    home.mkdir(parents=True, exist_ok=True)
    (home / "antigravity-oauth-token").write_text(
        json.dumps(
            {
                "email": email,
                "name": "Alice Developer",
                "token": {
                    "access_token": "ya29.integration-access",
                    "refresh_token": "1//integration-refresh",
                    "expiry_timestamp": 1893456000.0,
                },
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch):
    """Run the real FastAPI gateway on a local port with an isolated manager root."""
    monkeypatch.setenv("AGY_ADMIN_KEY", ADMIN_KEY)
    manager_root = tmp_path / "server-root"
    monkeypatch.setattr(fastapi_app, "tm_instance", fastapi_app.TokenManager(manager_root=manager_root))
    monkeypatch.setattr(fastapi_app.tm_instance, "fetch_account_quota", lambda *a, **k: None)

    port = _free_port()
    config = uvicorn.Config(fastapi_app.app, host="127.0.0.1", port=port, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        pytest.fail("gateway did not start in time")

    try:
        yield f"http://127.0.0.1:{port}", manager_root
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    

def test_client_push_end_to_end(gateway, tmp_path):
    url, manager_root = gateway
    client_paths = build_paths(tmp_path / "client-root")
    source = _write_mock_gemini_home(tmp_path / "home")

    response = push_account_to_gateway(
        client_paths,
        name="developer.alice",
        source_dir=source,
        gateway_url=url,
        admin_key=ADMIN_KEY,
    )

    assert response["name"] == "developer.alice"
    assert response["account"]["email"] == "developer.alice@gmail.com"

    state = json.loads((manager_root / "state.json").read_text(encoding="utf-8"))
    assert "developer.alice" in state["accounts"]
    token_file = manager_root / "accounts" / "developer.alice" / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    assert token_file.is_file()

    status = json.loads(_http_get(f"{url}/api/status"))
    assert status["pool_size"] == 1


def test_client_account_list_redacts_tokens(gateway, tmp_path):
    url, _ = gateway
    client_paths = build_paths(tmp_path / "client-root")
    source = _write_mock_gemini_home(tmp_path / "home")

    push_account_to_gateway(
        client_paths,
        name="developer.alice",
        source_dir=source,
        gateway_url=url,
        admin_key=ADMIN_KEY,
    )

    listing = list_gateway_accounts(client_paths, gateway_url=url, admin_key=ADMIN_KEY)
    body = json.dumps(listing)

    assert listing["total"] == 1
    assert listing["accounts"]["developer.alice"]["email"] == "developer.alice@gmail.com"
    assert "access_token" not in body
    assert "refresh_token" not in body
    assert "ya29.integration-access" not in body


def test_client_account_delete_removes_account(gateway, tmp_path):
    url, manager_root = gateway
    client_paths = build_paths(tmp_path / "client-root")
    source = _write_mock_gemini_home(tmp_path / "home")

    push_account_to_gateway(
        client_paths,
        name="developer.alice",
        source_dir=source,
        gateway_url=url,
        admin_key=ADMIN_KEY,
    )

    result = delete_gateway_account(client_paths, "developer.alice", gateway_url=url, admin_key=ADMIN_KEY)

    assert result["name"] == "developer.alice"
    listing = list_gateway_accounts(client_paths, gateway_url=url, admin_key=ADMIN_KEY)
    assert listing["total"] == 0
    assert not (manager_root / "accounts" / "developer.alice").exists()


def test_client_push_without_admin_key_is_rejected(gateway, tmp_path):
    url, _ = gateway
    client_paths = build_paths(tmp_path / "client-root")
    source = _write_mock_gemini_home(tmp_path / "home")

    with pytest.raises(ValueError) as excinfo:
        push_account_to_gateway(
            client_paths,
            name="developer.alice",
            source_dir=source,
            gateway_url=url,
            admin_key="wrong-key",
        )

    assert "401" in str(excinfo.value)


def test_client_account_patch_updates_enabled_state(gateway, tmp_path):
    url, _ = gateway
    client_paths = build_paths(tmp_path / "client-root")
    source = _write_mock_gemini_home(tmp_path / "home")

    push_account_to_gateway(
        client_paths,
        name="developer.alice",
        source_dir=source,
        gateway_url=url,
        admin_key=ADMIN_KEY,
    )

    updated = update_gateway_account(client_paths, "developer.alice", enabled=False, gateway_url=url, admin_key=ADMIN_KEY)
    assert updated["account"]["enabled"] is False

    listing = list_gateway_accounts(client_paths, gateway_url=url, admin_key=ADMIN_KEY)
    assert listing["accounts"]["developer.alice"]["enabled"] is False


def test_disabled_account_leaves_the_routing_pool(gateway, tmp_path):
    url, _ = gateway
    client_paths = build_paths(tmp_path / "client-root")
    source = _write_mock_gemini_home(tmp_path / "home")

    push_account_to_gateway(
        client_paths,
        name="developer.alice",
        source_dir=source,
        gateway_url=url,
        admin_key=ADMIN_KEY,
    )
    assert json.loads(_http_get(f"{url}/api/status"))["pool_size"] == 1

    update_gateway_account(client_paths, "developer.alice", enabled=False, gateway_url=url, admin_key=ADMIN_KEY)

    status = json.loads(_http_get(f"{url}/api/status"))
    assert status["pool_size"] == 0
    assert "developer.alice" not in status["accounts"]


def test_push_without_refresh_token_is_rejected(gateway, tmp_path):
    url, _ = gateway
    body = json.dumps(
        {
            "name": "developer.alice",
            "payload": {
                "format_version": "1.0",
                "identity": {"email": "developer.alice@gmail.com"},
                "token_data": {"access_token": "ya29.integration-access"},
            },
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        f"{url}/api/admin/accounts/push",
        data=body,
        headers={"Content-Type": "application/json", "X-Admin-Key": ADMIN_KEY},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)

    assert excinfo.value.code == 400
    assert "refresh_token" in excinfo.value.read().decode("utf-8")


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8")


def test_cli_push_and_account_commands(gateway, tmp_path, capsys):
    from agy_cli_manager.cli import handle_client_account_list, handle_client_push

    url, _ = gateway
    client_paths = build_paths(tmp_path / "client-root")
    save_client_config({"gateway_url": url, "admin_key": ADMIN_KEY}, client_paths.root)
    source = _write_mock_gemini_home(tmp_path / "home")

    exit_code = handle_client_push(client_paths, name="developer.alice", source_dir=str(source))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "[+] Detected Google Identity: developer.alice@gmail.com" in output
    assert "[OK] Account 'developer.alice' successfully registered on Gateway Server." in output

    exit_code = handle_client_account_list(client_paths)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "developer.alice" in output
    assert "developer.alice@gmail.com" in output


def test_cli_push_without_local_session_reports_login_hint(gateway, tmp_path, capsys):
    from agy_cli_manager.cli import handle_client_push

    url, _ = gateway
    client_paths = build_paths(tmp_path / "client-root")
    save_client_config({"gateway_url": url, "admin_key": ADMIN_KEY}, client_paths.root)
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()

    exit_code = handle_client_push(client_paths, source_dir=str(empty_home))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Please run 'agy login' first." in output
