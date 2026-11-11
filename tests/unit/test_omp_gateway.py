import json
import pytest
from agy_cli_manager.proxy.translators.openai import (
    convert_openai_messages_to_gemini,
    convert_openai_tools_to_gemini,
    convert_gemini_response_to_openai,
)
from agy_cli_manager.proxy.translators.streaming import stream_gemini_to_openai
from importlib import import_module

fastapi_app = import_module("agy_cli_manager.proxy.fastapi_app")
from agy_cli_manager.proxy.fastapi_app import resolve_model_name, UnsupportedModelError  # noqa: E402


CATALOG = {
    "gemini-2.5-flash": {"apiProvider": "API_PROVIDER_GOOGLE_GEMINI", "maxTokens": 1048576},
    "gemini-2.5-flash-lite": {"apiProvider": "API_PROVIDER_GOOGLE_GEMINI"},
    "gemini-2.5-pro": {"apiProvider": "API_PROVIDER_GOOGLE_GEMINI"},
    "gemini-3.8-flash-tiered": {"apiProvider": "API_PROVIDER_GOOGLE_GEMINI", "maxTokens": 1048576},
    "claude-sonnet-4-6": {"apiProvider": "API_PROVIDER_ANTHROPIC_VERTEX"},
    "claude-opus-4-6-thinking": {"apiProvider": "API_PROVIDER_ANTHROPIC_VERTEX"},
    "gpt-oss-120b-medium": {"apiProvider": "API_PROVIDER_OPENAI_VERTEX"},
}


@pytest.fixture
def live_catalog(monkeypatch):
    monkeypatch.setattr(fastapi_app.tm_instance, "get_model_catalog", lambda force=False: dict(CATALOG))
    monkeypatch.setattr(fastapi_app.tm_instance, "declared_models", lambda: ["gemini-4-argon"])
    return CATALOG


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_v1_models_catalog(live_catalog):
    # Advertised ids come from the upstream per-account catalog (plus clean base
    # ids for -tiered / effort-suffixed entries), not a hardcoded list.
    from starlette.testclient import TestClient

    data = TestClient(fastapi_app.app).get("/v1/models").json()["data"]
    assert {m["id"] for m in data} == set(CATALOG) | {"gemini-3.8-flash", "gemini-4-argon", "gpt-oss-120b"}
    owners = {m["id"]: m["owned_by"] for m in data}
    assert owners["claude-sonnet-4-6"] == "anthropic"
    assert owners["gpt-oss-120b-medium"] == "openai"
    assert owners["gpt-oss-120b"] == "openai"
    assert owners["gemini-3.8-flash-tiered"] == "google"
    assert owners["gemini-3.8-flash"] == "google"


def test_v1_models_falls_back_when_catalog_unavailable(monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setattr(fastapi_app.tm_instance, "get_model_catalog", lambda force=False: {})
    monkeypatch.setattr(fastapi_app.tm_instance, "declared_models", lambda: [])
    ids = {m["id"] for m in TestClient(fastapi_app.app).get("/v1/models").json()["data"]}
    assert ids == {"gemini-2.5-flash", "gemini-2.5-flash-lite"}


def test_resolve_accepts_catalog_ids(live_catalog, monkeypatch):
    assert resolve_model_name("gemini-2.5-flash") == "gemini-2.5-flash"
    assert resolve_model_name("gemini-2.5-pro") == "gemini-2.5-pro"
    assert resolve_model_name("claude-sonnet-4-6") == "claude-sonnet-4-6"
    # Effort suffixes and bare tiered family names map onto the advertised id.
    assert resolve_model_name("gemini-3.8-flash-tiered-high") == "gemini-3.8-flash-tiered"
    assert resolve_model_name("gemini-3.8-flash") == "gemini-3.8-flash-tiered"
    assert resolve_model_name("gemini-3.8-flash-high") == "gemini-3.8-flash-tiered"
    assert resolve_model_name("gemini-2.5-flash-latest") == "gemini-2.5-flash"

    # Base model IDs map onto effort-suffixed upstream entries when effort is supplied
    cat_with_tiered_skus = {
        **CATALOG,
        "claude-sonnet-5-5-low": {"apiProvider": "API_PROVIDER_ANTHROPIC_VERTEX"},
        "claude-sonnet-5-5-medium": {"apiProvider": "API_PROVIDER_ANTHROPIC_VERTEX"},
        "claude-sonnet-5-5-high": {"apiProvider": "API_PROVIDER_ANTHROPIC_VERTEX"},
        "gemini-4-carbon-low": {"apiProvider": "API_PROVIDER_GOOGLE_GEMINI", "maxTokens": 1048576, "maxOutputTokens": 262144},
        "gemini-4-carbon-medium": {"apiProvider": "API_PROVIDER_GOOGLE_GEMINI", "maxTokens": 1048576, "maxOutputTokens": 262144},
        "gemini-4-carbon-high": {"apiProvider": "API_PROVIDER_GOOGLE_GEMINI", "maxTokens": 1048576, "maxOutputTokens": 262144},
    }
    monkeypatch.setattr(fastapi_app.tm_instance, "get_model_catalog", lambda force=False: dict(cat_with_tiered_skus))
    assert resolve_model_name("claude-sonnet-5-5", effort="low") == "claude-sonnet-5-5-low"
    assert resolve_model_name("claude-sonnet-5-5", effort="medium") == "claude-sonnet-5-5-medium"
    assert resolve_model_name("claude-sonnet-5-5", effort="high") == "claude-sonnet-5-5-high"
    assert resolve_model_name("claude-sonnet-5-5") == "claude-sonnet-5-5-high"
    assert resolve_model_name("gemini-4-carbon", effort="low") == "gemini-4-carbon-low"
    assert resolve_model_name("gemini-4-carbon", effort="medium") == "gemini-4-carbon-medium"
    assert resolve_model_name("gemini-4-carbon", effort="high") == "gemini-4-carbon-high"
    assert resolve_model_name("gemini-4-carbon") == "gemini-4-carbon-high"

def test_resolve_accepts_operator_declared_pool_models(live_catalog):
    # An explicit model_pools entry declares the model served by a chosen account set.
    assert resolve_model_name("gemini-4-argon") == "gemini-4-argon"
    assert resolve_model_name("gemini-4-argon-low") == "gemini-4-argon"
    assert resolve_model_name("gemini-4-argon-high") == "gemini-4-argon"


def test_resolve_rejects_ids_outside_catalog(live_catalog):
    # Names that are neither advertised nor declared are rejected; never swapped.
    for bad in ("gemini-4-argon-tiered",
                "claude-opus-4-6", "claude-sonnet-5-5", "unknown-agent-model", None, ""):
        try:
            resolve_model_name(bad)
        except UnsupportedModelError:
            continue
        raise AssertionError(f"expected UnsupportedModelError for {bad!r}")


def test_tool_response_resolution_and_turn_merging():
    messages = [
        {"role": "user", "content": "Check project files"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_001",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": '{"dir": "."}'},
                },
                {
                    "id": "call_002",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": {"path": "package.json"}},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_001", "content": '["package.json", "index.ts"]'},
        {"role": "tool", "tool_call_id": "call_002", "content": '{"name": "my-app", "version": "1.0.0"}'},
        {"role": "user", "content": "Now please test the app"},
    ]

    contents, system_instruction = convert_openai_messages_to_gemini(messages)
    assert system_instruction is None

    assert len(contents) == 4
    assert contents[0]["role"] == "user"
    assert contents[0]["parts"][0]["text"] == "Check project files"

    assert contents[1]["role"] == "model"
    assert len(contents[1]["parts"]) == 2
    assert contents[1]["parts"][1]["functionCall"]["args"] == {"path": "package.json"}
    assert contents[1]["parts"][1]["thoughtSignature"] == "skip_thought_signature_validator"

    assert contents[2]["role"] == "user"
    tool_parts = contents[2]["parts"]
    assert len(tool_parts) == 2
    assert tool_parts[0]["functionResponse"]["name"] == "list_files"
    assert tool_parts[0]["functionResponse"]["response"]["content"] == {"result": ["package.json", "index.ts"]}
    assert tool_parts[1]["functionResponse"]["name"] == "read_file"
    assert tool_parts[1]["functionResponse"]["response"]["content"] == {"name": "my-app", "version": "1.0.0"}

    assert contents[3]["role"] == "user"
    assert contents[3]["parts"][0]["text"] == "Now please test the app"


@pytest.mark.anyio
async def test_streaming_tool_calls_and_thought_suppression():
    async def mock_stream():
        yield b'data: {"candidates": [{"content": {"parts": [{"text": "Thinking about bash command...", "thought": true}]}}]}\r\n\r\n'
        yield b'data:{"response": {"candidates": [{"content": {"parts": [{"functionCall": {"name": "bash", "args": {"command": "ls -la"}}}]}}]}}\n\n'
        yield b'data: {"candidates": [{"finishReason": "STOP"}]}\n\n'

    chunks = []
    async for chunk in stream_gemini_to_openai(mock_stream(), model_name="claude-sonnet-4-6"):
        chunks.append(chunk)

    assert len(chunks) == 4

    thought_chunk = json.loads(chunks[0].decode("utf-8").removeprefix("data: ").strip())
    assert thought_chunk["model"] == "claude-sonnet-4-6"
    assert "content" not in thought_chunk["choices"][0]["delta"]
    assert thought_chunk["choices"][0]["delta"]["reasoning_content"] == "Thinking about bash command..."
    assert thought_chunk["choices"][0]["finish_reason"] is None

    tool_chunk = json.loads(chunks[1].decode("utf-8").removeprefix("data: ").strip())
    tc = tool_chunk["choices"][0]["delta"]["tool_calls"][0]
    assert tc["function"]["name"] == "bash"
    assert json.loads(tc["function"]["arguments"]) == {"command": "ls -la"}
    assert tc["type"] == "function"
    assert tool_chunk["choices"][0]["finish_reason"] is None

    finish_chunk = json.loads(chunks[2].decode("utf-8").removeprefix("data: ").strip())
    assert finish_chunk["choices"][0]["finish_reason"] == "tool_calls"

    assert chunks[3] == b"data: [DONE]\n\n"


def test_non_streaming_thought_and_tool_conversion():
    gemini_resp = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Internal reasoning chain", "thought": True},
                        {"functionCall": {"name": "git_status", "args": {}}},
                    ],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ]
    }
    openai_resp = convert_gemini_response_to_openai(gemini_resp, model_name="gpt-oss-120b-medium")
    choice = openai_resp["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["reasoning_content"] == "Internal reasoning chain"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "git_status"
    assert openai_resp["model"] == "gpt-oss-120b-medium"


def test_tools_schema_sanitization():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "grep",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "properties": {
                        "skip": {"type": ["number", "null"], "description": "offset"},
                        "flag": {"type": ["boolean", "null"]},
                        "model": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string"}},
                            ],
                            "description": "string or list",
                        },
                    },
                },
            },
        }
    ]
    gemini_tools, _ = convert_openai_tools_to_gemini(tools)
    params = gemini_tools[0]["functionDeclarations"][0]["parameters"]
    assert "additionalProperties" not in params
    assert "$schema" not in params
    assert params["properties"]["skip"]["type"] == "number"
    assert params["properties"]["skip"]["nullable"] is True
    assert params["properties"]["flag"]["type"] == "boolean"
    assert params["properties"]["flag"]["nullable"] is True
    model_param = params["properties"]["model"]
    assert "anyOf" not in model_param
    assert model_param["type"] == "string"
    assert model_param["description"] == "string or list"


def test_thinking_budget_mapping():
    from starlette.testclient import TestClient
    from agy_cli_manager.proxy.fastapi_app import app
    from unittest.mock import patch, MagicMock, AsyncMock

    client = TestClient(app)

    captured_payloads = []
    async def mock_send(self, req, stream=False):
        import json
        body_dict = json.loads(req.content.decode("utf-8"))
        captured_payloads.append(body_dict)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.reason_phrase = "OK"
        mock_resp.aread = AsyncMock(return_value=json.dumps([{"response": {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}}]).encode("utf-8"))
        mock_resp.aclose = AsyncMock()
        return mock_resp

    with patch("httpx.AsyncClient.send", new=mock_send), patch("agy_cli_manager.proxy.token_manager.TokenManager.get_token_by_session", return_value=("test_acc", "fake_token")):
        client.post("/v1/chat/completions", json={"model": "gemini-2.5-flash-low", "messages": [{"role": "user", "content": "hi"}]})
        assert captured_payloads[-1]["request"]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 1024

        client.post("/v1/chat/completions", json={"model": "gemini-2.5-flash", "reasoning_effort": "high", "messages": [{"role": "user", "content": "hi"}]})
        assert captured_payloads[-1]["request"]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 24576

        client.post("/v1/chat/completions", json={"model": "gemini-2.5-flash", "reasoning_effort": "none", "messages": [{"role": "user", "content": "hi"}]})
        assert captured_payloads[-1]["request"]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0


def test_thought_signature_capture_and_injection():
    from agy_cli_manager.proxy.translators.openai import record_thought_signature, convert_openai_messages_to_gemini

    record_thought_signature("call_custom_123", "real_encrypted_sig_abc")

    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call_custom_123", "type": "function", "function": {"name": "test_fn", "arguments": "{}"}},
                {"id": "call_other_456", "type": "function", "function": {"name": "other_fn", "arguments": "{}"}},
            ],
        },
    ]
    contents, _ = convert_openai_messages_to_gemini(messages)
    parts = contents[1]["parts"]
    assert parts[0]["thoughtSignature"] == "real_encrypted_sig_abc"
    assert parts[1]["thoughtSignature"] == "skip_thought_signature_validator"


def test_unsupported_model_returns_400_without_token_call(live_catalog):
    from starlette.testclient import TestClient
    from unittest.mock import patch
    from agy_cli_manager.proxy.fastapi_app import app

    client = TestClient(app)
    with patch("agy_cli_manager.proxy.token_manager.TokenManager.get_token_by_session") as tok:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsupported_model"
    tok.assert_not_called()

    with patch("agy_cli_manager.proxy.token_manager.TokenManager.get_token_by_session") as tok:
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "missing_model"
    tok.assert_not_called()
def test_gcp_tos_host_resolution():
    from agy_cli_manager.proxy.fastapi_app import _get_cloudcode_host, tm_instance
    tm_instance._accounts["test_gcp"] = {"is_gcp_tos": True}
    tm_instance._accounts["test_standard"] = {"is_gcp_tos": False}
    assert _get_cloudcode_host("test_gcp") == "cloudcode-pa.googleapis.com"
    assert _get_cloudcode_host("test_standard") == "daily-cloudcode-pa.googleapis.com"
    assert _get_cloudcode_host(None) == "daily-cloudcode-pa.googleapis.com"
    tm_instance._accounts.pop("test_gcp", None)
    tm_instance._accounts.pop("test_standard", None)
