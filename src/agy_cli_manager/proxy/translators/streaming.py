import json
import time
import uuid
from typing import AsyncGenerator

try:
    from .openai import record_thought_signature
except ImportError:
    from translators.openai import record_thought_signature


async def stream_gemini_to_openai(
    gemini_stream: AsyncGenerator[bytes, None],
    model_name: str = "gemini-translated"
) -> AsyncGenerator[bytes, None]:
    buffer = b""
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created_time = int(time.time())
    tool_call_counter = 0
    has_emitted_tool_call = False
    
    async for chunk in gemini_stream:
        buffer += chunk
        if b"\r\n" in buffer:
            buffer = buffer.replace(b"\r\n", b"\n")
        
        while b"\n\n" in buffer:
            event_data, buffer = buffer.split(b"\n\n", 1)
            event_text = event_data.decode("utf-8", "ignore")
            
            data_lines = [
                line[5:].strip()
                for line in event_text.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                continue
                
            json_str = "".join(data_lines)
            if not json_str:
                continue
                
            try:
                gemini_data = json.loads(json_str)
            except json.JSONDecodeError:
                continue
                
            text_delta = ""
            thought_delta = ""
            tool_calls = []
            finish_reason = None
            
            resp_dict = gemini_data.get("response", gemini_data) if isinstance(gemini_data, dict) else {}
            candidates = resp_dict.get("candidates", [])
            if candidates:
                candidate = candidates[0]
                parts = candidate.get("content", {}).get("parts", [])
                text_delta = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
                thought_delta = "".join(p.get("text", "") for p in parts if "text" in p and p.get("thought"))

                for p in parts:
                    fc = p.get("functionCall")
                    if fc:
                        call_id = fc.get("id") or f"call_{uuid.uuid4().hex[:24]}"
                        sig = p.get("thoughtSignature") or p.get("thought_signature")
                        if sig and call_id:
                            record_thought_signature(call_id, sig)
                        tool_calls.append({
                            "index": tool_call_counter,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": fc.get("name", ""),
                                "arguments": json.dumps(fc.get("args", {})),
                            },
                        })
                        tool_call_counter += 1
                        has_emitted_tool_call = True
                
                finish_reason_map = {
                    "STOP": "stop",
                    "MAX_TOKENS": "length",
                    "SAFETY": "content_filter",
                    "RECITATION": "content_filter",
                    "TOOL_CODE_EXECUTION": "tool_calls",
                }
                gemini_fr = candidate.get("finishReason")
                if gemini_fr:
                    finish_reason = finish_reason_map.get(gemini_fr, "stop")
                    if has_emitted_tool_call and finish_reason == "stop":
                        finish_reason = "tool_calls"
                    
            usage_metadata = resp_dict.get("usageMetadata") or gemini_data.get("usageMetadata")
            
            openai_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": model_name,
                "choices": []
            }
            
            if candidates or not usage_metadata:
                choice = {
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish_reason,
                }
                if text_delta:
                    choice["delta"]["content"] = text_delta
                if thought_delta:
                    choice["delta"]["reasoning_content"] = thought_delta
                if tool_calls:
                    choice["delta"]["tool_calls"] = tool_calls
                openai_chunk["choices"].append(choice)
                
            if usage_metadata:
                openai_chunk["usage"] = {
                    "prompt_tokens": usage_metadata.get("promptTokenCount", 0),
                    "completion_tokens": usage_metadata.get("candidatesTokenCount", 0),
                    "total_tokens": usage_metadata.get("totalTokenCount", 0)
                }
            
            yield f"data: {json.dumps(openai_chunk)}\n\n".encode("utf-8")
                
    yield b"data: [DONE]\n\n"

