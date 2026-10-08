import pytest
from httpx import AsyncClient, ASGITransport, Response, Request
import httpx
from agy_cli_manager.proxy.fastapi_app import app
from unittest.mock import patch, MagicMock

_CLOUDCODE_URL = "daily-cloudcode-pa.googleapis.com"

_SYNC_RESPONSE_ARRAY = [
    {
        "response": {
            "candidates": [{"content": {"parts": [{"text": "Hello sync"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5},
        },
        "traceId": "test-trace",
    }
]

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.mark.anyio
async def test_openai_route_sync():
    with patch("agy_cli_manager.proxy.fastapi_app.tm_instance") as mock_tm:
        mock_tm.get_token_by_session.return_value = ("test-account", "test-token")

        real_send = httpx.AsyncClient.send
        async def mock_send_side_effect(self, request, **kwargs):
            if _CLOUDCODE_URL in str(request.url):
                return Response(200, json=_SYNC_RESPONSE_ARRAY, request=request)
            return await real_send(self, request, **kwargs)

        with patch("httpx.AsyncClient.send", autospec=True, side_effect=mock_send_side_effect):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/v1/chat/completions", json={
                    "model": "gemini-1.5-flash",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": False,
                })
                assert resp.status_code == 200
                data = resp.json()
                assert "choices" in data
                assert data["choices"][0]["message"]["content"] == "Hello sync"
                assert data["usage"]["total_tokens"] == 5

@pytest.mark.anyio
async def test_openai_route_stream():
    with patch("agy_cli_manager.proxy.fastapi_app.tm_instance") as mock_tm:
        mock_tm.get_token_by_session.return_value = ("test-account", "test-token")

        real_send = httpx.AsyncClient.send
        async def mock_send_side_effect(self, request, **kwargs):
            if _CLOUDCODE_URL in str(request.url):
                async def mock_aiter_bytes():
                    yield b'data: {"candidates": [{"content": {"parts": [{"text": "Hello stream"}]}}]}\n\n'

                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.reason_phrase = "OK"
                mock_resp.aiter_bytes = mock_aiter_bytes

                async def mock_aclose():
                    pass
                mock_resp.aclose = mock_aclose
                return mock_resp
            return await real_send(self, request, **kwargs)

        with patch("httpx.AsyncClient.send", autospec=True, side_effect=mock_send_side_effect):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                async with client.stream("POST", "/v1/chat/completions", json={
                    "model": "gemini-1.5-flash",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                }) as resp:
                    assert resp.status_code == 200
                    chunks = []
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                    assert b"Hello stream" in b"".join(chunks)

@pytest.mark.anyio
async def test_openai_route_error():
    with patch("agy_cli_manager.proxy.fastapi_app.tm_instance") as mock_tm:
        mock_tm.get_token_by_session.return_value = ("test-account", "test-token")

        real_send = httpx.AsyncClient.send
        async def mock_send_side_effect(self, request, **kwargs):
            if _CLOUDCODE_URL in str(request.url):
                return Response(
                    400,
                    json={"error": {"code": 400, "message": "User location is not supported for the API use.", "status": "FAILED_PRECONDITION"}},
                    request=request,
                )
            return await real_send(self, request, **kwargs)

        with patch("httpx.AsyncClient.send", autospec=True, side_effect=mock_send_side_effect):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/v1/chat/completions", json={
                    "model": "gemini-1.5-flash",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": False,
                })
                assert resp.status_code == 400
                data = resp.json()
                assert "error" in data
                assert data["error"]["type"] == "invalid_request_error"
                assert data["error"]["message"] == "User location is not supported for the API use."
