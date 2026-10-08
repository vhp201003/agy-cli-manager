from __future__ import annotations

import pytest
from agy_cli_manager.proxy.token_manager import TokenManager
from agy_cli_manager.proxy.proxy_server import _extract_session_key


@pytest.fixture
def configured_token_manager(tmp_path):
    tm = TokenManager(manager_root=tmp_path)
    tm._accounts = {
        "acc1": {
            "access_token": "tok1",
            "quota": {
                "gemini": {"weekly": {"percent": 100.0, "reset_time": "2026-10-10"}, "5h": {"percent": 99.0, "reset_time": "2026-10-07"}},
                "third_party": {"weekly": {"percent": 0.52, "reset_time": "2026-10-10"}, "5h": {"percent": 0.0, "reset_time": "2026-10-07"}},
            },
        },
        "acc2": {
            "access_token": "tok2",
            "quota": {
                "gemini": {"weekly": {"percent": 88.0, "reset_time": "2026-10-10"}, "5h": {"percent": 34.0, "reset_time": "2026-10-07"}},
                "third_party": {"weekly": {"percent": 17.0, "reset_time": "2026-10-10"}, "5h": {"percent": 34.0, "reset_time": "2026-10-07"}},
            },
        },
        "acc3": {
            "access_token": "tok3",
            "quota": {
                "gemini": {"weekly": {"percent": 100.0, "reset_time": "2026-10-10"}, "5h": {"percent": 100.0, "reset_time": "2026-10-07"}},
                "third_party": {"weekly": {"percent": 47.0, "reset_time": "2026-10-10"}, "5h": {"percent": 100.0, "reset_time": "2026-10-07"}},
            },
        },
        "acc4": {
            "access_token": "tok4",
            "quota": {
                "gemini": {"weekly": {"percent": 77.0, "reset_time": "2026-10-10"}, "5h": {"percent": 100.0, "reset_time": "2026-10-07"}},
                "third_party": {"weekly": {"percent": 0.0, "reset_time": "2026-10-10"}, "5h": {"percent": 0.0, "reset_time": "2026-10-07"}},
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
