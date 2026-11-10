from __future__ import annotations

import json
import time
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from agy_cli_manager.proxy.token_manager import TokenManager, ModelPoolExhaustedError
from agy_cli_manager.proxy.proxy_server import _extract_session_key


@pytest.fixture
def configured_token_manager(tmp_path):
    tm = TokenManager(manager_root=tmp_path)
    # Reset windows must still be running for the exhausted-account fixtures to
    # mean "exhausted"; a past reset time means the window already refilled.
    now = time.time()
    weekly_reset = datetime.fromtimestamp(now + 7 * 24 * 3600, tz=timezone.utc).isoformat()
    five_h_reset = datetime.fromtimestamp(now + 3600, tz=timezone.utc).isoformat()
    tm._accounts = {
        "acc1": {
            "access_token": "tok1",
            "quota": {
                "gemini": {"weekly": {"percent": 100.0, "reset_time": weekly_reset}, "5h": {"percent": 99.0, "reset_time": five_h_reset}},
                "third_party": {"weekly": {"percent": 0.52, "reset_time": weekly_reset}, "5h": {"percent": 0.0, "reset_time": five_h_reset}},
            },
        },
        "acc2": {
            "access_token": "tok2",
            "quota": {
                "gemini": {"weekly": {"percent": 88.0, "reset_time": weekly_reset}, "5h": {"percent": 34.0, "reset_time": five_h_reset}},
                "third_party": {"weekly": {"percent": 17.0, "reset_time": weekly_reset}, "5h": {"percent": 34.0, "reset_time": five_h_reset}},
            },
        },
        "acc3": {
            "access_token": "tok3",
            "quota": {
                "gemini": {"weekly": {"percent": 100.0, "reset_time": weekly_reset}, "5h": {"percent": 100.0, "reset_time": five_h_reset}},
                "third_party": {"weekly": {"percent": 47.0, "reset_time": weekly_reset}, "5h": {"percent": 100.0, "reset_time": five_h_reset}},
            },
        },
        "acc4": {
            "access_token": "tok4",
            "quota": {
                "gemini": {"weekly": {"percent": 77.0, "reset_time": weekly_reset}, "5h": {"percent": 100.0, "reset_time": five_h_reset}},
                "third_party": {"weekly": {"percent": 0.0, "reset_time": weekly_reset}, "5h": {"percent": 0.0, "reset_time": five_h_reset}},
            },
        },
    }
    return tm


def test_gemini_routing_uses_healthy_gemini_accounts(configured_token_manager):
    sessions = ["subagent-A", "subagent-B", "subagent-C", "subagent-D", "subagent-E"]
    gemini_chosen = {s: configured_token_manager.get_token_by_session(s, model_name="gemini-2.5-flash-lite")[0] for s in sessions}
    assert set(gemini_chosen.values()).issubset({"acc1", "acc2", "acc3", "acc4"})


def test_claude_routing_filters_exhausted_third_party_accounts(configured_token_manager):
    sessions = ["subagent-A", "subagent-B", "subagent-C", "subagent-D", "subagent-E"]
    claude_chosen = {s: configured_token_manager.get_token_by_session(s, model_name="claude-3-7-sonnet")[0] for s in sessions}
    for _, acc in claude_chosen.items():
        assert acc in ["acc2", "acc3"]


def test_extract_session_key_agent_request_id():
    body = '{"project": "aicode-consumers", "requestId": "agent/89ce14d5-6579-4ce5-bb2c-6789d8b62f38/1791361478857/uuid-123"}'
    assert _extract_session_key("/", {}, body) == "agent:89ce14d5-6579-4ce5-bb2c-6789d8b62f38"


def test_extract_session_key_chat_request_id():
    body = '{"project": "aicode-consumers", "requestId": "chat/be1310ed-4004-4cd4-996b-33b2ef3465cb"}'
    assert _extract_session_key("/", {}, body) == "chat:be1310ed-4004-4cd4-996b-33b2ef3465cb"


def test_extract_session_key_nested_session_id():
    body = '{"project": "aicode-consumers", "request": {"sessionId": -3750763034362895579}}'
    assert _extract_session_key("/", {}, body) == "-3750763034362895579"


def test_per_model_pool_routing_and_strict_exhaustion(tmp_path):
    tm = TokenManager(manager_root=tmp_path)
    now = time.time()
    future_reset = datetime.fromtimestamp(now + 3600, tz=timezone.utc).isoformat()
    tm._accounts = {
        "acc1": {
            "name": "acc1",
            "access_token": "tok1",
            "expires_at": now + 3600,
            "quota": {
                "gemini": {"weekly": {"percent": 100.0, "reset_time": future_reset}, "5h": {"percent": 100.0, "reset_time": future_reset}},
                "third_party": {"weekly": {"percent": 100.0, "reset_time": future_reset}, "5h": {"percent": 100.0, "reset_time": future_reset}},
            },
        },
        "acc2": {
            "name": "acc2",
            "access_token": "tok2",
            "expires_at": now + 3600,
            "quota": {
                "gemini": {"weekly": {"percent": 100.0, "reset_time": future_reset}, "5h": {"percent": 100.0, "reset_time": future_reset}},
                "third_party": {"weekly": {"percent": 100.0, "reset_time": future_reset}, "5h": {"percent": 100.0, "reset_time": future_reset}},
            },
        },
        "acc3": {
            "name": "acc3",
            "access_token": "tok3",
            "expires_at": now + 3600,
            "quota": {
                "gemini": {"weekly": {"percent": 100.0, "reset_time": future_reset}, "5h": {"percent": 100.0, "reset_time": future_reset}},
                "third_party": {"weekly": {"percent": 100.0, "reset_time": future_reset}, "5h": {"percent": 100.0, "reset_time": future_reset}},
            },
        },
    }

    # Configure model pools:
    # - gemini-2.5-pro -> acc2 only
    # - claude* -> acc3 only
    # - * -> acc1, acc2
    tm.set_model_pools({
        "gemini-2.5-pro": ["acc2"],
        "claude*": ["acc3"],
        "*": ["acc1", "acc2"],
    })

    # Persistence check in config.json
    saved_cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved_cfg["model_pools"]["gemini-2.5-pro"] == ["acc2"]
    assert saved_cfg["model_pools"]["claude*"] == ["acc3"]

    # 1. Exact match routes to acc2
    acc_pro, tok_pro = tm.get_token_by_session(model_name="gemini-2.5-pro")
    assert acc_pro == "acc2"
    assert tok_pro == "tok2"

    # 2. Wildcard match routes claude-sonnet-4-6 to acc3
    acc_claude, tok_claude = tm.get_token_by_session(model_name="claude-sonnet-4-6")
    assert acc_claude == "acc3"
    assert tok_claude == "tok3"

    # 3. Fallback '*' routes gemini-2.5-flash to acc1/acc2
    acc_flash, _ = tm.get_token_by_session(model_name="gemini-2.5-flash")
    assert acc_flash in ("acc1", "acc2")

    # 4. Strict mode: when acc3 is in 429 cooldown, claude-sonnet-4-6 MUST raise ModelPoolExhaustedError
    # and NOT spill over to acc1 or acc2!
    tm.mark_429("acc3", cooldown_seconds=600)
    with pytest.raises(ModelPoolExhaustedError):
        tm.get_token_by_session(model_name="claude-sonnet-4-6")

    # 5. Clear cooldown, then quarantine acc3 -> still raises ModelPoolExhaustedError
    tm.clear_cooldown("acc3")
    tm.quarantine_account("acc3", reason="auth_error", duration_seconds=600)
    with pytest.raises(ModelPoolExhaustedError):
        tm.get_token_by_session(model_name="claude-sonnet-4-6")


def test_pool_rule_matches_effort_suffixed_model_ids(tmp_path):
    """A thinking-level suffix is a request parameter, not a different model."""
    tm = TokenManager(manager_root=tmp_path)
    now = time.time()
    tm._accounts = {
        name: {"name": name, "access_token": tok, "expires_at": now + 3600}
        for name, tok in (("acc1", "tok1"), ("acc2", "tok2"))
    }
    tm.set_model_pools({"gemini-4-argon": ["acc2"]})

    for requested in ("gemini-4-argon", "gemini-4-argon-low", "gemini-4-argon-high", "gemini-4-argon-xhigh"):
        acc, tok = tm.get_token_by_session(model_name=requested)
        assert (acc, tok) == ("acc2", "tok2"), requested

    # The routing key the gateway builds is "<requested>|<canonical>"; the
    # canonical half must match too, whichever order the parts arrive in.
    assert tm.resolve_pool_for_model("gemini-4-argon-high|gemini-4-argon")[0] == "gemini-4-argon"


def test_model_pools_api_endpoints(monkeypatch, tmp_path):
    import sys
    import agy_cli_manager.proxy.fastapi_app
    fastapi_module = sys.modules["agy_cli_manager.proxy.fastapi_app"]

    tm = TokenManager(manager_root=tmp_path)
    tm._accounts = {
        "accA": {"name": "accA", "access_token": "tokA", "expires_at": time.time() + 3600},
        "accB": {"name": "accB", "access_token": "tokB", "expires_at": time.time() + 3600},
    }
    monkeypatch.setattr(fastapi_module, "tm_instance", tm)
    monkeypatch.setenv("AGY_ADMIN_KEY", "router-test-admin-key")
    client = TestClient(fastapi_module.app)
    admin_headers = {"X-Admin-Key": "router-test-admin-key"}

    # 1. GET initial empty model pools
    r1 = client.get("/api/model-pools")
    assert r1.status_code == 200
    assert r1.json()["model_pools"] == {}
    assert set(r1.json()["available_accounts"]) == {"accA", "accB"}

    # 2. POST update model pools
    r2 = client.post("/api/model-pools", headers=admin_headers, json={
        "model_pools": {
            "gemini-2.5-flash": ["accA"],
            "claude*": ["accB"],
        }
    })
    assert r2.status_code == 200
    assert r2.json()["model_pools"] == {
        "gemini-2.5-flash": ["accA"],
        "claude*": ["accB"],
    }

    # 3. DELETE a single rule
    r3 = client.delete("/api/model-pools", headers=admin_headers, params={"pattern": "claude*"})
    assert r3.status_code == 200
    assert r3.json()["model_pools"] == {"gemini-2.5-flash": ["accA"]}

    # 4. Verify /v1/chat/completions returns 429 when strict pool is in cooldown
    tm.mark_429("accA", cooldown_seconds=600)
    r4 = client.post("/v1/chat/completions", json={
        "model": "gemini-2.5-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    })
    assert r4.status_code == 429
    assert r4.json()["error"]["type"] == "rate_limit_error"



def test_routing_prefers_accounts_that_advertise_the_model(tmp_path):
    """A healthy account without the model 404s, so capability decides first."""
    tm = TokenManager(manager_root=tmp_path)
    now = time.time()
    tm._accounts = {
        name: {"name": name, "access_token": f"tok{name[-1]}", "expires_at": now + 3600}
        for name in ("acc1", "acc2")
    }
    tm.set_model_pools({})
    tm._per_account_catalog = {
        "acc1": {"gemini-3.8-flash-tiered", "gemini-2.5-flash"},
        "acc2": {"claude-sonnet-5-5-high", "gemini-2.5-flash"},
    }
    tm._catalog_ts = time.time()

    acc, _ = tm.get_token_by_session(model_name="claude-sonnet-5-5-high")
    assert acc == "acc2"

    # Unknown to every account: keep the normal pool instead of failing here.
    acc_any, _ = tm.get_token_by_session(model_name="gemini-4-argon")
    assert acc_any in ("acc1", "acc2")
