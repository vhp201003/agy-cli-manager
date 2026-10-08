from __future__ import annotations

from collections import deque
import gzip
import http.client
import json
import logging
import os
from pathlib import Path
import re
import select
import socket
import socketserver
import ssl
import sys
import threading
import time
from urllib.parse import urlparse
import uuid
import zlib

_current = Path(__file__).resolve().parent
_parent = _current.parent
for _p in (str(_current), str(_parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from agy_cli_manager.proxy.cert_manager import CertManager
    from agy_cli_manager.proxy.token_manager import TokenManager
except ImportError:
    try:
        from cert_manager import CertManager
        from token_manager import TokenManager
    except ImportError:
        from .cert_manager import CertManager
        from .token_manager import TokenManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Proxy] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("AgyProxy")

# Global event bus for FastAPI telemetry
MAX_LOG_HISTORY = 300
REQUEST_LOGS: deque[dict] = deque(maxlen=MAX_LOG_HISTORY)
EVENT_LISTENERS: list[callable] = []
LOG_LOCK = threading.Lock()


def register_event_listener(listener: callable) -> None:
    with LOG_LOCK:
        if listener not in EVENT_LISTENERS:
            EVENT_LISTENERS.append(listener)


def unregister_event_listener(listener: callable) -> None:
    with LOG_LOCK:
        if listener in EVENT_LISTENERS:
            EVENT_LISTENERS.remove(listener)


def _extract_model_from_request(path: str, body_str: str) -> str:
    # 1. Search in body if json
    if body_str and "model" in body_str:
        try:
            m = re.search(r'"model"\s*:\s*"([^"]+)"', body_str)
            if m:
                return m.group(1)
        except Exception:
            pass
    # 2. Search in URL path
    if "models/" in path:
        m = re.search(r"models/([^:]+)", path)
        if m:
            return m.group(1)
    # 3. Known endpoint heuristics
    if "generateContent" in path or "streamGenerateContent" in path:
        return "Gemini API"
    if "loadCodeAssist" in path:
        return "CodeAssist Auth"
    if "retrieveUserQuotaSummary" in path:
        return "Quota Telemetry"
    if "listExperiments" in path:
        return "Experiments"
    return "API Request"


def broadcast_event(event: dict) -> None:
    with LOG_LOCK:
        REQUEST_LOGS.append(event)
        listeners = list(EVENT_LISTENERS)

    for listener in listeners:
        try:
            listener(event)
        except Exception:
            pass


def _format_body_for_log(body: bytes, headers: dict[str, str] | None = None, max_len: int = 60000) -> str:
    if not body:
        return "(empty body)"

    # 1. Automatic decompression (gzip / deflate)
    data = body
    content_encoding = (headers.get("content-encoding") if headers else "") or ""
    if "gzip" in content_encoding or (len(body) > 2 and body[:2] == b"\x1f\x8b"):
        try:
            data = gzip.decompress(body)
        except Exception:
            pass
    elif "deflate" in content_encoding or "zlib" in content_encoding:
        try:
            data = zlib.decompress(body)
        except Exception:
            try:
                data = zlib.decompress(body, -zlib.MAX_WBITS)
            except Exception:
                pass

    # 2. Try JSON decode first
    try:
        decoded = data[:max_len].decode("utf-8")
        try:
            parsed = json.loads(decoded)
            return json.dumps(parsed, indent=2)
        except Exception:
            return decoded
    except UnicodeDecodeError:
        pass

    # 3. Handle binary / protobuf payloads (e.g. Google Cloud Code /log telemetry)
    # Extract readable human strings from raw protobuf stream
    try:
        raw_text = data[:max_len].decode("utf-8", "replace")
        # Filter unprintable control characters and unicode replacement diamonds
        cleaned_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffd]", " ", raw_text)
        cleaned_text = re.sub(r" {2,}", " ", cleaned_text).strip()
        if len(cleaned_text) > 20:
            return f"/* Binary / Protobuf Payload ({len(data)} bytes) - Extracted Text */\n\n{cleaned_text}"
    except Exception:
        pass

    return f"<binary data: {len(data)} bytes>"


def _extract_session_key(path: str, headers: dict[str, str], body_str: str, raw_body: bytes | None = None) -> str | None:
    for h in ("x-session-id", "x-conversation-id", "session-id", "conversation-id"):
        if h in headers:
            return headers[h]

    # 1. Fast regex extraction directly on raw bytes / body_str (guaranteed to match even if JSON is truncated)
    search_text = body_str or ""
    if raw_body:
        try:
            # Check first 2048 bytes where requestId always lives
            search_text = raw_body[:2048].decode("utf-8", "ignore")
        except Exception:
            pass

    if search_text:
        # Match "requestId": "agent/<agent_id>..." or "chat/<chat_id>..."
        m = re.search(r'["\']requestId["\']\s*:\s*["\'](agent|chat)/([^/\\"\s]+)', search_text)
        if m:
            return f"{m.group(1)}:{m.group(2)}"

        # Match sessionId / conversationId in JSON
        m_sess = re.search(r'["\'](sessionId|conversationId|session_id|conversation_id)["\']\s*:\s*["\']?([^"\'\s,{}]+)', search_text)
        if m_sess:
            return m_sess.group(2)

    # 2. Complete JSON parse fallback if regex didn't catch and complete payload is available
    if body_str and body_str.startswith("{"):
        try:
            data = json.loads(body_str)

            inner_req = data.get("request")
            if isinstance(inner_req, dict):
                for k in ("sessionId", "conversationId", "session_id", "conversation_id", "chatId", "taskId", "task_id"):
                    if k in inner_req and inner_req[k]:
                        return str(inner_req[k])

            for k in ("sessionId", "conversationId", "session_id", "conversation_id", "chatId", "taskId", "task_id"):
                if k in data and data[k]:
                    return str(data[k])

            meta = data.get("metadata")
            if isinstance(meta, dict):
                for k in ("conversationId", "sessionId", "taskId"):
                    if k in meta and meta[k]:
                        return str(meta[k])
        except Exception:
            pass
    return None


class MITMProxyHandler(socketserver.BaseRequestHandler):
    cert_manager: CertManager
    token_manager: TokenManager

    def handle(self) -> None:
        try:
            req_line = self._read_line(self.request)
            if not req_line:
                return

            parts = req_line.split()
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]

            # Read headers
            headers = {}
            while True:
                line = self._read_line(self.request)
                if not line:
                    break
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            if method.upper() == "CONNECT":
                self._handle_connect(target)
            else:
                self._handle_plain_http(method, target, headers)

        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            logger.debug(f"Exception handling request: {exc}")

    def _read_line(self, sock: socket.socket) -> str:
        line_bytes = bytearray()
        while True:
            ch = sock.recv(1)
            if not ch:
                break
            if ch == b"\n":
                break
            if ch != b"\r":
                line_bytes.extend(ch)
        return line_bytes.decode("latin1", "replace")

    def _read_inbound_body(self, sock: socket.socket, headers: dict[str, str]) -> bytes:
        te = headers.get("transfer-encoding", "").lower()
        if "chunked" in te:
            body = bytearray()
            while True:
                line = self._read_line(sock)
                if not line:
                    break
                chunk_size_str = line.split(";")[0].strip()
                if not chunk_size_str:
                    continue
                try:
                    chunk_size = int(chunk_size_str, 16)
                except ValueError:
                    break
                if chunk_size == 0:
                    # Read trailing headers
                    while True:
                        trailer = self._read_line(sock)
                        if not trailer:
                            break
                    break
                # Read chunk data
                remaining = chunk_size
                while remaining > 0:
                    buf = sock.recv(min(remaining, 16384))
                    if not buf:
                        break
                    body.extend(buf)
                    remaining -= len(buf)
                self._read_line(sock)  # consume trailing \r\n
            return bytes(body)
        else:
            content_len = int(headers.get("content-length", 0))
            if content_len <= 0:
                return b""
            body = bytearray()
            remaining = content_len
            while remaining > 0:
                buf = sock.recv(min(remaining, 16384))
                if not buf:
                    break
                body.extend(buf)
                remaining -= len(buf)
            return bytes(body)

    def _handle_connect(self, target: str) -> None:
        if ":" in target:
            host, port_str = target.split(":", 1)
            port = int(port_str)
        else:
            host, port = target, 443

        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        # Intercept Google Cloud Code & OAuth endpoints
        if "googleapis.com" in host.lower():
            self._intercept_google_tls(host, port)
        else:
            self._blind_passthrough(host, port)

    def _blind_passthrough(self, host: str, port: int) -> None:
        try:
            remote = socket.create_connection((host, port), timeout=15)
        except Exception as exc:
            logger.debug(f"Could not connect to {host}:{port}: {exc}")
            return

        try:
            sockets = [self.request, remote]
            while True:
                r, _, _ = select.select(sockets, [], [], 30)
                if not r:
                    break
                for s in r:
                    other = remote if s is self.request else self.request
                    data = s.recv(16384)
                    if not data:
                        return
                    other.sendall(data)
        finally:
            remote.close()

    def _intercept_google_tls(self, host: str, port: int) -> None:
        ssl_ctx = self.cert_manager.get_ssl_context_for_host(host)
        try:
            tls_client = ssl_ctx.wrap_socket(self.request, server_side=True)
        except Exception as exc:
            logger.debug(f"TLS handshake with client failed for {host}: {exc}")
            return

        try:
            while True:
                req_line = self._read_line(tls_client)
                if not req_line:
                    break
                parts = req_line.split()
                if len(parts) < 3:
                    break
                req_method, req_path, _ = parts[0], parts[1], parts[2]

                headers: dict[str, str] = {}
                while True:
                    hdr_line = self._read_line(tls_client)
                    if not hdr_line:
                        break
                    if ":" in hdr_line:
                        k, v = hdr_line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()

                # Read complete request body (including chunked requests from Go net/http)
                body = self._read_inbound_body(tls_client, headers)

                # Forward with token routing, real-time response streaming & failover
                self._dispatch_with_retry(tls_client, host, port, req_method, req_path, headers, body)

                if headers.get("connection", "").lower() == "close":
                    break
        finally:
            try:
                tls_client.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            tls_client.close()

    def _dispatch_with_retry(
        self,
        client_sock: ssl.SSLSocket,
        host: str,
        port: int,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        max_attempts = 4
        inbound_auth = headers.get("authorization", "")
        inbound_preview = inbound_auth[:18] + "..." if inbound_auth else "None"
        last_error = None

        body_str = _format_body_for_log(body, headers)
        body_size = len(body) if body else 0
        model_name = _extract_model_from_request(path, body_str)
        session_key = _extract_session_key(path, headers, body_str, raw_body=body)

        for attempt in range(max_attempts):
            acc_name, access_token = self.token_manager.get_token_by_session(session_key, model_name=model_name)
            out_headers = dict(headers)
            out_headers["host"] = host
            out_headers["authorization"] = f"Bearer {access_token}"

            # Strip inbound chunked encoding and set explicit length so upstream never hangs
            out_headers.pop("transfer-encoding", None)
            if body:
                out_headers["content-length"] = str(len(body))
            elif "content-length" in out_headers:
                out_headers["content-length"] = "0"

            overridden_preview = f"Bearer {access_token[:10]}...{access_token[-5:]}"
            event_id = str(uuid.uuid4())[:8]

            try:
                t0 = time.time()
                upstream_ctx = ssl.create_default_context()
                # 180s timeout to allow long LLM thinking & streaming turns without timing out
                conn = http.client.HTTPSConnection(host, port, context=upstream_ctx, timeout=180)
                conn.request(method, path, body=body if body else None, headers=out_headers)
                resp = conn.getresponse()
                latency_ms = round((time.time() - t0) * 1000, 1)

                # 1. Handle 401 Token Expired -> Auto-refresh OAuth token immediately and retry
                if resp.status == 401 and attempt < max_attempts - 1:
                    err_preview = resp.read(512).decode("utf-8", "ignore")
                    logger.warning(
                        f"[401 Token Expired] Account '{acc_name}' returned 401 on {path}. Auto-refreshing OAuth token..."
                    )
                    self.token_manager.refresh_account_token(acc_name)
                    broadcast_event({
                        "id": event_id,
                        "time": time.strftime("%H:%M:%S"),
                        "method": method,
                        "path": path,
                        "host": host,
                        "account": acc_name,
                        "inbound_token": inbound_preview,
                        "overridden_token": overridden_preview,
                        "status": 401,
                        "status_text": "401 Expired (Auto-refreshed)",
                        "latency_ms": latency_ms,
                        "body": body_str,
                        "body_size": body_size,
                        "retried": True,
                    })
                    conn.close()
                    continue

                # 2. Handle 429 Quota Exceeded -> Cooldown 10 mins and retry with standby
                if resp.status == 429 and attempt < max_attempts - 1:
                    err_preview = resp.read(512).decode("utf-8", "ignore")
                    logger.warning(
                        f"[429 Quota Exceeded] Account '{acc_name}' hit rate limit on {path}. Routing to standby account..."
                    )
                    self.token_manager.mark_429(acc_name, cooldown_seconds=600)
                    broadcast_event({
                        "id": event_id,
                        "time": time.strftime("%H:%M:%S"),
                        "method": method,
                        "path": path,
                        "host": host,
                        "account": acc_name,
                        "inbound_token": inbound_preview,
                        "overridden_token": overridden_preview,
                        "status": 429,
                        "status_text": "429 Quota Limit (Switched account)",
                        "latency_ms": latency_ms,
                        "body": body_str,
                        "body_size": body_size,
                        "retried": True,
                    })
                    conn.close()
                    continue

                # 3. Handle 403 Forbidden (e.g. Account has no valid license) -> Cooldown and switch
                if resp.status == 403 and attempt < max_attempts - 1:
                    err_preview = resp.read(512).decode("utf-8", "ignore")
                    logger.warning(
                        f"[403 Forbidden] Account '{acc_name}' forbidden on {path}: {err_preview[:100]}. Routing to standby..."
                    )
                    self.token_manager.mark_429(acc_name, cooldown_seconds=1800)
                    broadcast_event({
                        "id": event_id,
                        "time": time.strftime("%H:%M:%S"),
                        "method": method,
                        "path": path,
                        "host": host,
                        "account": acc_name,
                        "inbound_token": inbound_preview,
                        "overridden_token": overridden_preview,
                        "status": 403,
                        "status_text": "403 Forbidden (Account no license, switched)",
                        "latency_ms": latency_ms,
                        "body": body_str,
                        "body_size": body_size,
                        "retried": True,
                    })
                    conn.close()
                    continue

                # 4. Stream response back to client in real-time
                resp_line = f"HTTP/1.1 {resp.status} {resp.reason}\r\n".encode("latin1")
                client_sock.sendall(resp_line)

                for k, v in resp.getheaders():
                    if k.lower() in ("transfer-encoding", "content-length", "connection"):
                        continue
                    client_sock.sendall(f"{k}: {v}\r\n".encode("latin1"))

                # Use chunked transfer encoding so tokens stream dynamically to agy without delay
                client_sock.sendall(b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")

                total_streamed = 0
                resp_chunks: list[bytes] = []
                resp_sample_size = 0
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    total_streamed += len(chunk)
                    if resp_sample_size < 16384:
                        resp_chunks.append(chunk)
                        resp_sample_size += len(chunk)
                    client_sock.sendall(f"{len(chunk):X}\r\n".encode("latin1") + chunk + b"\r\n")
                client_sock.sendall(b"0\r\n\r\n")

                conn.close()

                # Format response sample preview
                resp_preview = ""
                if resp_chunks:
                    try:
                        raw_combined = b"".join(resp_chunks)
                        resp_preview = _format_body_for_log(raw_combined, dict(resp.getheaders()))
                    except Exception:
                        resp_preview = f"<streamed response: {total_streamed} bytes>"

                # Broadcast successful completion to dashboard
                broadcast_event({
                    "id": event_id,
                    "time": time.strftime("%H:%M:%S"),
                    "method": method,
                    "path": path,
                    "host": host,
                    "account": acc_name,
                    "model": model_name,
                    "inbound_token": inbound_preview,
                    "overridden_token": overridden_preview,
                    "status": resp.status,
                    "status_text": f"{resp.status} {resp.reason}",
                    "latency_ms": latency_ms,
                    "body": body_str,
                    "body_size": body_size,
                    "response_preview": resp_preview,
                    "response_size": total_streamed,
                    "retried": attempt > 0,
                })

                logger.info(
                    f"[{acc_name}] {method} {path} -> HTTP {resp.status} ({latency_ms}ms, {total_streamed} bytes)"
                )
                return

            except (ConnectionResetError, BrokenPipeError) as e:
                logger.debug(f"Client disconnected during {method} {path}: {e}")
                return
            except OSError as e:
                if getattr(e, "winerror", None) == 10053:
                    logger.debug(f"Client aborted connection during {method} {path}")
                    return
                last_error = e
                logger.error(f"Error forwarding {method} {path} to upstream {host} via {acc_name}: {e}")
                time.sleep(0.3)
            except Exception as e:
                last_error = e
                logger.error(f"Error forwarding {method} {path} to upstream {host} via {acc_name}: {e}")
                time.sleep(0.3)

        # Fallback if all retry attempts failed
        err_body = f'{{"error": "Upstream proxy forwarding failed after retries: {last_error}"}}'.encode()
        client_sock.sendall(
            b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(err_body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + err_body
        )

    def _handle_plain_http(self, method: str, target: str, headers: dict) -> None:
        parsed = urlparse(target)
        host = parsed.hostname or headers.get("host", "127.0.0.1")
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        try:
            conn = http.client.HTTPConnection(host, port, timeout=15)
            conn.request(method, path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()

            self.request.sendall(f"HTTP/1.1 {resp.status} {resp.reason}\r\n".encode("latin1"))
            for k, v in resp.getheaders():
                self.request.sendall(f"{k}: {v}\r\n".encode("latin1"))
            self.request.sendall(b"\r\n" + body)
            conn.close()
        except Exception as exc:
            logger.debug(f"Plain HTTP forward error: {exc}")


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_proxy_server(port: int = 8899, token_manager: TokenManager | None = None, cert_manager: CertManager | None = None) -> tuple[ThreadedTCPServer, threading.Thread]:
    cm = cert_manager or CertManager()
    tm = token_manager or TokenManager()

    handler_cls = MITMProxyHandler
    handler_cls.cert_manager = cm
    handler_cls.token_manager = tm

    # Main dispatcher server (port 8899)
    server = ThreadedTCPServer(("127.0.0.1", port), handler_cls)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info(f"AGY Multi-Account Token Proxy running on http://127.0.0.1:{port}")

    return server, t

