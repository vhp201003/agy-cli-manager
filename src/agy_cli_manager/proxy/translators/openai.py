from typing import Any, Dict, List, Optional, Tuple
import json
import time
import uuid


def convert_openai_messages_to_gemini(
    messages: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    contents: List[Dict[str, Any]] = []
    system_parts: List[Dict] = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if isinstance(content, str):
                system_parts.append({"text": content})
            elif isinstance(content, list):
                for p in content:
                    if p.get("type") == "text":
                        system_parts.append({"text": p.get("text", "")})
            continue

        if role == "tool":
            if isinstance(content, str):
                try:
                    payload = json.loads(content)
                except Exception:
                    payload = {"result": content}
            elif isinstance(content, dict):
                payload = content
            else:
                payload = {"result": content}
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": {"name": msg.get("name", "unknown"), "response": {"content": payload}}}],
            })
            continue

        gemini_role = "user" if role in ("user", "system") else "model"
        parts: List[Dict] = []

        if isinstance(content, str) and content:
            parts.append({"text": content})
        elif isinstance(content, list):
            for p in content:
                t = p.get("type")
                if t == "text":
                    parts.append({"text": p.get("text", "")})
                elif t == "image_url":
                    url = p.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        try:
                            header, data = url.split(",", 1)
                            mime = header.split(";", 1)[0].replace("data:", "")
                            parts.append({"inline_data": {"mime_type": mime, "data": data}})
                        except ValueError:
                            pass

        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except Exception:
                args = {}
            parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})

        if parts:
            contents.append({"role": gemini_role, "parts": parts})

    system_instruction = None
    if system_parts:
        system_instruction = {"role": "system", "parts": [{"text": "\n".join(p["text"] for p in system_parts)}]}

    return contents, system_instruction


def convert_openai_tools_to_gemini(
    tools: List[Dict[str, Any]], tool_choice: Any = None
) -> Tuple[List[Dict], Optional[Dict]]:
    declarations = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        fn = tool.get("function", {})
        decl: Dict[str, Any] = {"name": fn.get("name", "")}
        if fn.get("description"):
            decl["description"] = fn["description"]
        if fn.get("parameters"):
            decl["parameters"] = fn["parameters"]
        declarations.append(decl)

    gemini_tools = [{"functionDeclarations": declarations}] if declarations else []

    tool_config = None
    if tool_choice and tool_choice != "auto":
        if tool_choice == "none":
            tool_config = {"functionCallingConfig": {"mode": "NONE"}}
        elif tool_choice == "required":
            tool_config = {"functionCallingConfig": {"mode": "ANY"}}
        elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            name = tool_choice.get("function", {}).get("name")
            if name:
                tool_config = {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}}

    return gemini_tools, tool_config


_FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "TOOL_CODE_EXECUTION": "tool_calls",
    "OTHER": "stop",
}


def convert_gemini_response_to_openai(
    gemini_resp: Dict[str, Any], model_name: str = "gemini-translated"
) -> Dict[str, Any]:
    choices = []
    for i, cand in enumerate(gemini_resp.get("candidates", [])):
        parts = cand.get("content", {}).get("parts", [])

        text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))

        tool_calls = []
        for p in parts:
            fc = p.get("functionCall")
            if fc:
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {"name": fc.get("name", ""), "arguments": json.dumps(fc.get("args", {}))},
                })

        finish_reason = _FINISH_REASON_MAP.get(cand.get("finishReason", "STOP"), "stop")
        if tool_calls:
            finish_reason = "tool_calls"

        msg: Dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls

        choices.append({"index": i, "message": msg, "finish_reason": finish_reason})

    usage_metadata = gemini_resp.get("usageMetadata", {})
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": choices,
        "usage": {
            "prompt_tokens": usage_metadata.get("promptTokenCount", 0),
            "completion_tokens": usage_metadata.get("candidatesTokenCount", 0),
            "total_tokens": usage_metadata.get("totalTokenCount", 0),
        },
    }


def convert_gemini_error_to_openai(gemini_error: Dict[str, Any]) -> Dict[str, Any]:
    err = gemini_error.get("error", {})
    if not err:
        return {"error": {"message": "Unknown error occurred", "type": "api_error", "param": None, "code": None}}

    code = err.get("code", 500)
    type_map = {400: "invalid_request_error", 401: "invalid_authentication", 403: "permission_denied", 429: "rate_limit_error"}
    return {
        "error": {
            "message": err.get("message", "Unknown error"),
            "type": type_map.get(code, "api_error"),
            "param": None,
            "code": err.get("status") or str(code),
        }
    }
