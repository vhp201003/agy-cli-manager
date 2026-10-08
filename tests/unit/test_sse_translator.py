import pytest
import json
from agy_cli_manager.proxy.translators.streaming import stream_gemini_to_openai

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.mark.anyio
async def test_stream_gemini_to_openai():
    async def mock_gemini_stream():
        yield b'data: {"candidates": [{"content": {"parts": [{"text": "Hello "}]}}]}\n\n'
        yield b'data: {"candidates": [{"content": {"parts": [{"text": "world"}]}}]}\n'
        yield b'\n'
        yield b'data: {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}\n\n'
        
    chunks = []
    async for chunk in stream_gemini_to_openai(mock_gemini_stream()):
        chunks.append(chunk)
        
    # Expect 4 chunks: Hello, world, usage, [DONE]
    assert len(chunks) == 4
    
    # First chunk
    data1_str = chunks[0].decode('utf-8').removeprefix('data: ').strip()
    data1 = json.loads(data1_str)
    assert data1["choices"][0]["delta"]["content"] == "Hello "
    
    # Second chunk
    data2_str = chunks[1].decode('utf-8').removeprefix('data: ').strip()
    data2 = json.loads(data2_str)
    assert data2["choices"][0]["delta"]["content"] == "world"
    
    # Usage chunk
    data3_str = chunks[2].decode('utf-8').removeprefix('data: ').strip()
    data3 = json.loads(data3_str)
    assert data3["usage"]["total_tokens"] == 15
    assert data3["choices"] == [] # Usage chunks might not have choices, or choices with empty delta
    
    # Done chunk
    assert chunks[3] == b'data: [DONE]\n\n'

@pytest.mark.anyio
async def test_stream_gemini_to_openai_fragmented_json():
    async def mock_gemini_stream():
        yield b'data: {"candidates": [{"content": {"parts": [{"t'
        yield b'ext": "Fragmented!"}]}}]}\n\n'
        
    chunks = []
    async for chunk in stream_gemini_to_openai(mock_gemini_stream()):
        chunks.append(chunk)
        
    assert len(chunks) == 2
    data1_str = chunks[0].decode('utf-8').removeprefix('data: ').strip()
    data1 = json.loads(data1_str)
    assert data1["choices"][0]["delta"]["content"] == "Fragmented!"
    assert chunks[1] == b'data: [DONE]\n\n'
