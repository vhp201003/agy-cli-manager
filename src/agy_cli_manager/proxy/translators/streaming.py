import json
import time
import uuid
from typing import AsyncGenerator

async def stream_gemini_to_openai(gemini_stream: AsyncGenerator[bytes, None]) -> AsyncGenerator[bytes, None]:
    buffer = b""
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created_time = int(time.time())
    
    async for chunk in gemini_stream:
        buffer += chunk
        
        while b"\n\n" in buffer:
            event_data, buffer = buffer.split(b"\n\n", 1)
            event_text = event_data.decode("utf-8")
            
            if not event_text.startswith("data: "):
                continue
                
            json_str = event_text.removeprefix("data: ").strip()
            if not json_str:
                continue
                
            try:
                gemini_data = json.loads(json_str)
            except json.JSONDecodeError:
                continue
                
            text_delta = ""
            finish_reason = None
            
            candidates = gemini_data.get("candidates", [])
            if candidates:
                candidate = candidates[0]
                parts = candidate.get("content", {}).get("parts", [])
                text_delta = "".join(p.get("text", "") for p in parts if "text" in p)
                
                finish_reason_map = {
                    "STOP": "stop",
                    "MAX_TOKENS": "length",
                    "SAFETY": "content_filter",
                    "RECITATION": "content_filter"
                }
                gemini_fr = candidate.get("finishReason")
                if gemini_fr:
                    finish_reason = finish_reason_map.get(gemini_fr, "stop")
                    
            usage_metadata = gemini_data.get("usageMetadata")
            
            openai_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": "gemini-translated",
                "choices": []
            }
            
            if candidates or not usage_metadata:
                choice = {
                    "index": 0,
                    "delta": {}
                }
                if text_delta:
                    choice["delta"]["content"] = text_delta
                if finish_reason:
                    choice["finish_reason"] = finish_reason
                openai_chunk["choices"].append(choice)
                
            if usage_metadata:
                openai_chunk["usage"] = {
                    "prompt_tokens": usage_metadata.get("promptTokenCount", 0),
                    "completion_tokens": usage_metadata.get("candidatesTokenCount", 0),
                    "total_tokens": usage_metadata.get("totalTokenCount", 0)
                }
            
            yield f"data: {json.dumps(openai_chunk)}\n\n".encode("utf-8")
                
    yield b"data: [DONE]\n\n"
