import pytest
from agy_cli_manager.proxy.translators.openai import convert_gemini_error_to_openai

def test_convert_gemini_error_to_openai():
    gemini_error = {
        "error": {
            "code": 400,
            "message": "User location is not supported for the API use.",
            "status": "FAILED_PRECONDITION"
        }
    }
    
    openai_error = convert_gemini_error_to_openai(gemini_error)
    
    assert "error" in openai_error
    assert openai_error["error"]["message"] == "User location is not supported for the API use."
    assert openai_error["error"]["type"] == "invalid_request_error"
    assert openai_error["error"]["code"] == "FAILED_PRECONDITION"

def test_fallback_error():
    openai_error = convert_gemini_error_to_openai({"other": "format"})
    
    assert "error" in openai_error
    assert openai_error["error"]["message"] == "Unknown error occurred"
    assert openai_error["error"]["type"] == "api_error"
