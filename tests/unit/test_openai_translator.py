import pytest
from agy_cli_manager.proxy.translators.openai import (
    convert_openai_messages_to_gemini,
    convert_gemini_response_to_openai
)

def test_convert_openai_messages_to_gemini_standard():
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"}
    ]
    contents, system_instruction = convert_openai_messages_to_gemini(messages)
    
    assert system_instruction is None
    assert len(contents) == 2
    assert contents[0]["role"] == "user"
    assert contents[0]["parts"][0]["text"] == "Hello"
    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][0]["text"] == "Hi there!"

def test_convert_openai_messages_to_gemini_with_system():
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"}
    ]
    contents, system_instruction = convert_openai_messages_to_gemini(messages)
    
    assert system_instruction is not None
    assert system_instruction["parts"][0]["text"] == "You are a helpful assistant."
    assert system_instruction["role"] == "system"
    
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    assert contents[0]["parts"][0]["text"] == "Hello"

def test_convert_openai_messages_to_gemini_multiple_system_prompts():
    messages = [
        {"role": "system", "content": "Rule 1."},
        {"role": "system", "content": "Rule 2."},
        {"role": "user", "content": "Hello"}
    ]
    contents, system_instruction = convert_openai_messages_to_gemini(messages)
    
    assert system_instruction is not None
    assert system_instruction["parts"][0]["text"] == "Rule 1.\nRule 2."
    
    assert len(contents) == 1

def test_convert_openai_messages_to_gemini_vision():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
            ]
        }
    ]
    contents, system_instruction = convert_openai_messages_to_gemini(messages)
    
    assert system_instruction is None
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    
    parts = contents[0]["parts"]
    assert len(parts) == 2
    assert parts[0]["text"] == "What is in this image?"
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert parts[1]["inline_data"]["data"] == "iVBORw0KGgo="

def test_convert_gemini_response_to_openai_sync():
    gemini_resp = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Hello world!"}],
                    "role": "model"
                },
                "finishReason": "STOP"
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 5,
            "totalTokenCount": 15
        }
    }
    
    openai_resp = convert_gemini_response_to_openai(gemini_resp)
    
    assert openai_resp["object"] == "chat.completion"
    assert len(openai_resp["choices"]) == 1
    assert openai_resp["choices"][0]["message"]["content"] == "Hello world!"
    assert openai_resp["choices"][0]["message"]["role"] == "assistant"
    assert openai_resp["choices"][0]["finish_reason"] == "stop"
    
    assert "usage" in openai_resp
    assert openai_resp["usage"]["prompt_tokens"] == 10
    assert openai_resp["usage"]["completion_tokens"] == 5
    assert openai_resp["usage"]["total_tokens"] == 15
