import json
import pytest
from agy_cli_manager.proxy.translators.openai import (
    convert_openai_messages_to_gemini,
    convert_openai_tools_to_gemini,
    convert_gemini_response_to_openai,
)
from agy_cli_manager.proxy.translators.streaming import stream_gemini_to_openai
from agy_cli_manager.proxy.fastapi_app import _SUPPORTED_MODELS, resolve_model_name


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_v1_models_catalog():
    model_ids = {m["id"] for m in _SUPPORTED_MODELS}
    expected = {
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-low",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
    }
    assert expected.issubset(model_ids)


def test_v1_models_retrieve():
    from starlette.testclient import TestClient
    from agy_cli_manager.proxy.fastapi_app import app

    client = TestClient(app)
    resp = client.get("/v1/models/claude-sonnet-4-6")
    assert resp.status_code == 200
    assert resp.json()["id"] == "claude-sonnet-4-6"

    resp_404 = client.get("/v1/models/non-existent-model")
    assert resp_404.status_code == 404


def test_model_preservation_for_claude_and_gpt():
    assert resolve_model_name("claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert resolve_model_name("claude-opus-4-6-thinking") == "claude-opus-4-6-thinking"
    assert resolve_model_name("gpt-oss-120b-medium") == "gpt-oss-120b-medium"
    assert resolve_model_name("gemini-2.5-flash") == "gemini-2.5-flash"
    assert resolve_model_name("gemini-2.5-pro") == "gemini-2.5-pro"
    assert resolve_model_name("gemini-3.8-flash-high") == "gemini-2.5-flash"
    assert resolve_model_name("gemini-3.8-flash-medium") == "gemini-2.5-flash"
    assert resolve_model_name("gemini-3.8-flash-low") == "gemini-2.5-flash"
    assert resolve_model_name("unknown-agent-model") == "gemini-2.5-flash"
    assert resolve_model_name(None) == "gemini-2.5-flash"
    assert resolve_model_name("") == "gemini-2.5-flash"


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
