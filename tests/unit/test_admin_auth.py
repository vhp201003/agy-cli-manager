import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agy_cli_manager.proxy.token_manager import TokenManager

fastapi_app = importlib.import_module("agy_cli_manager.proxy.fastapi_app")

ADMIN_KEY = "unit-test-admin-key"


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGY_ADMIN_KEY", ADMIN_KEY)
    manager = TokenManager(manager_root=tmp_path / "root")
    monkeypatch.setattr(fastapi_app, "tm_instance", manager)
    # No lifespan context manager: the background warmup scheduler is bound to
    # the real manager root and would reach out to Google during teardown.
    yield TestClient(fastapi_app.app)


def test_admin_endpoint_without_key_returns_401(client):
    response = client.get("/api/admin/accounts")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or missing Admin API Key"


def test_admin_endpoint_with_invalid_key_returns_401(client):
    response = client.get("/api/admin/accounts", headers={"X-Admin-Key": "wrong-key"})

    assert response.status_code == 401


def test_admin_endpoint_with_valid_header_succeeds(client):
    response = client.get("/api/admin/accounts", headers={"X-Admin-Key": ADMIN_KEY})

    assert response.status_code == 200
    body = response.json()
    assert body["accounts"] == {}
    assert body["active"] is None


def test_admin_endpoint_with_bearer_token_succeeds(client):
    response = client.get("/api/admin/accounts", headers={"Authorization": f"Bearer {ADMIN_KEY}"})

    assert response.status_code == 200


def test_protected_routes_require_admin_key(client):
    assert client.post("/api/refresh-all").status_code == 401
    assert client.post("/api/warmup-all").status_code == 401
    assert client.post("/api/model-pools", json={"model_pools": {}}).status_code == 401
    assert client.post("/api/accounts/demo/clear-cooldown").status_code == 401


def test_public_routes_stay_open(client):
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/model-pools").status_code == 200
