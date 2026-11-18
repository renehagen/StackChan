"""Local client for Home Assistant's exposed Assist MCP tools.

Credentials never leave this client except in authentication requests to the
configured HA origin. There is deliberately no REST service-call fallback.
"""
from __future__ import annotations

import asyncio
import copy
from http.client import HTTPException
import ipaddress
import json
import math
import os
import re
from pathlib import Path
import stat
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


PROTOCOL_VERSION = "2025-03-26"
MAX_BODY = 1024 * 1024
MAX_OUTPUT = 24000
MAX_ARGUMENTS = 16384
# These are device/read intents. Administrative, messaging and security tools
# are not added merely because a future server offers them.
ASSIST_TOOLS = frozenset({
    "GetLiveContext", "HassGetState", "HassTurnOn", "HassTurnOff", "HassLightSet",
    "HassClimateSetTemperature", "HassSetVolume", "HassMediaPause",
    "HassMediaUnpause", "HassMediaNext", "HassMediaPrevious",
    "HassMediaSearchAndPlay", "HassFanSetSpeed",
})


class HomeAssistantError(Exception):
    """Keep a stable machine code separate from the sanitized HA explanation."""
    def __init__(self, code: str, detail: str | None = None):
        super().__init__(code)
        self.detail = detail


def diagnostic_text(value: Any, limit: int = 320) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    text = re.sub(r"(?i)https?://\S+|(?:bearer|basic)\s+\S+|(?:access_token|refresh_token|password|api_key)\s*[:=]\s*\S+|[A-Za-z0-9_.-]{40,}", "[afgeschermd]", text)
    return text[:limit] or None


def error_detail(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    err = value.get("error")
    data = value.get("data")
    for item in (err, err.get("message") if isinstance(err, dict) else None,
                 value.get("message"), data.get("code") if isinstance(data, dict) else None):
        if text := diagnostic_text(item):
            return text
    if isinstance(data, dict) and isinstance(data.get("failed"), list):
        codes = [diagnostic_text(x.get("code")) for x in data["failed"] if isinstance(x, dict)]
        return diagnostic_text(", ".join(x for x in codes if x))
    return None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HomeAssistantError("redirect_refused")


async def _finish_thread(function, *args, **kwargs):
    """Do not release an owning async lock while its HTTP/file thread runs.

    Cancellation cannot unsend a device command. Finish the already bounded
    operation once, then propagate cancellation without starting another one.
    Repeated cancellation during shutdown must not detach the thread either.
    """
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(operation)
            break
        except asyncio.CancelledError:
            cancelled = True
            if operation.cancelled():
                raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


def _semantic_failure(value: Any) -> str | None:
    """HA wraps its intent results as JSON text, even when MCP isError is false."""
    if not isinstance(value, dict):
        return None
    if (value.get("success") is False or value.get("ok") is False
            or value.get("error") or value.get("response_type") == "error"):
        return "home_assistant_tool_failed"
    data = value.get("data")
    if value.get("failed") or (isinstance(data, dict) and data.get("failed")):
        return "home_assistant_partial_failure"
    return None


def _base_url(value: Any) -> str:
    if not isinstance(value, str):
        raise HomeAssistantError("invalid_auth_config")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise HomeAssistantError("invalid_auth_config") from None
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username
            or url.password or url.query or url.fragment or url.path not in {"", "/"}
            or any(ord(c) < 33 for c in value) or (port is not None and port == 0)):
        raise HomeAssistantError("invalid_auth_config")
    if url.scheme == "http":
        host = url.hostname.lower()
        try:
            address = ipaddress.ip_address(host)
            local = address.is_private or address.is_loopback
        except ValueError:
            local = host == "localhost" or host.endswith(".local")
        if not local:
            raise HomeAssistantError("https_required")
    return value.rstrip("/")


def _read_auth(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise HomeAssistantError("auth_file_permissions")
        if info.st_size > 32768:
            raise HomeAssistantError("invalid_auth_config")
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HomeAssistantError("not_configured") from None
    except (OSError, ValueError):
        raise HomeAssistantError("invalid_auth_config") from None
    if not isinstance(data, dict):
        raise HomeAssistantError("invalid_auth_config")
    for key in ("client_id", "refresh_token", "access_token"):
        value = data.get(key)
        if not isinstance(value, str) or not value or len(value) > 16384 or any(ord(c) < 32 for c in value):
            raise HomeAssistantError("invalid_auth_config")
    expiry = data.get("expires_at")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
        raise HomeAssistantError("invalid_auth_config")
    data["base_url"] = _base_url(data.get("base_url"))
    return data


def _save_auth(path: Path, data: dict[str, Any]) -> None:
    temporary = None
    try:
        if path.is_symlink():
            raise HomeAssistantError("auth_file_permissions")
        fd, temporary = tempfile.mkstemp(prefix=".ha-auth-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, separators=(",", ":"), allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError:
        raise HomeAssistantError("auth_save_failed") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _decode_response(response: Any, request_id: int | None, *, deadline: float | None = None) -> Any:
    """Read JSON or a finite matching SSE event, not an entire open SSE stream."""
    if response.status in {202, 204}:
        return None
    media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media_type == "text/event-stream":
        total = 0
        parts: list[bytes] = []
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise HomeAssistantError("ha_unreachable")
            line = response.readline(min(65537, MAX_BODY - total + 1))
            total += len(line)
            if total > MAX_BODY or len(line) > 65536:
                raise HomeAssistantError("response_too_large")
            if not line or line in (b"\n", b"\r\n"):
                if parts:
                    try:
                        event = json.loads(b"\n".join(parts))
                    except (ValueError, UnicodeDecodeError):
                        raise HomeAssistantError("invalid_mcp_response") from None
                    parts.clear()
                    if isinstance(event, dict) and event.get("id") == request_id:
                        return event
                if not line:
                    raise HomeAssistantError("invalid_mcp_response")
            elif line.startswith(b"data:"):
                parts.append(line[5:].lstrip(b" ").rstrip(b"\r\n"))
    if media_type not in {"application/json", ""}:
        raise HomeAssistantError("invalid_mcp_response")
    chunks: list[bytes] = []
    total = 0
    while True:
        if deadline is not None and time.monotonic() > deadline:
            raise HomeAssistantError("ha_unreachable")
        chunk = response.read1(min(65536, MAX_BODY - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_BODY:
            raise HomeAssistantError("response_too_large")
    raw = b"".join(chunks)
    if len(raw) > MAX_BODY:
        raise HomeAssistantError("response_too_large")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HomeAssistantError("invalid_mcp_response") from None


class HomeAssistantTools:
    def __init__(self, auth_path: Path, *, timeout: float = 8.0):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise HomeAssistantError("invalid_timeout")
        self._path = Path(auth_path)
        self._auth = _read_auth(self._path)
        self._timeout = timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._auth_lock = asyncio.Lock()
        self._transport_lock = asyncio.Lock()
        self._session_id: str | None = None
        self._protocol = PROTOCOL_VERSION
        self._request_id = 0
        self._tools: dict[str, dict[str, Any]] = {}

    def _http(self, path: str, body: bytes, *, form: bool = False,
              request_id: int | None = None) -> tuple[Any, str | None]:
        headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json",
                   "Accept": "application/json, text/event-stream"}
        if not form:
            headers["Authorization"] = "Bearer " + self._auth["access_token"]
            headers["MCP-Protocol-Version"] = self._protocol
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
        request = Request(self._auth["base_url"] + path, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id and (len(session_id) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in session_id)):
                    raise HomeAssistantError("invalid_mcp_response")
                return _decode_response(response, request_id, deadline=time.monotonic() + self._timeout), session_id
        except HTTPError as error:
            code = {401: "authentication_required", 403: "permission_denied",
                    404: "assist_mcp_not_configured", 429: "rate_limited"}.get(error.code, "ha_http_error")
            raise HomeAssistantError(code) from None
        except (URLError, OSError, TimeoutError, HTTPException) as error:
            failure = HomeAssistantError("ha_unreachable")
            cause = error.reason if isinstance(error, URLError) else error
            # Fixed metadata helps distinguish DNS/OS permission failures without
            # leaking request URLs, headers, credentials or exception messages.
            failure.network_errno = getattr(cause, "errno", None)
            failure.network_kind = type(cause).__name__
            raise failure from None

    async def _ensure_auth(self) -> None:
        async with self._auth_lock:
            if self._auth["expires_at"] > time.time() + 60:
                return
            body = urlencode({"grant_type": "refresh_token", "client_id": self._auth["client_id"],
                              "refresh_token": self._auth["refresh_token"]}).encode()
            result, _ = await _finish_thread(self._http, "/auth/token", body, form=True)
            if not isinstance(result, dict):
                raise HomeAssistantError("invalid_auth_response")
            token, expiry = result.get("access_token"), result.get("expires_in")
            if (not isinstance(token, str) or not token or len(token) > 16384
                    or any(ord(c) < 32 for c in token) or isinstance(expiry, bool)
                    or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= 0):
                raise HomeAssistantError("invalid_auth_response")
            updated = {**self._auth, "access_token": token, "expires_at": time.time() + expiry}
            await _finish_thread(_save_auth, self._path, updated)
            self._auth = updated

    async def _rpc(self, method: str, params: dict[str, Any] | None = None,
                   *, notification: bool = False) -> Any:
        async with self._transport_lock:
            await self._ensure_auth()
            payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            request_id = None
            if not notification:
                self._request_id += 1
                request_id = self._request_id
                payload["id"] = request_id
            if params is not None:
                payload["params"] = params
            result, session_id = await _finish_thread(
                self._http, "/api/mcp/assist", json.dumps(payload, allow_nan=False).encode(), request_id=request_id)
            if session_id:
                self._session_id = session_id
            if notification:
                return None
            if not isinstance(result, dict) or result.get("id") != request_id or result.get("jsonrpc") != "2.0":
                raise HomeAssistantError("invalid_mcp_response")
            if "error" in result:
                raise HomeAssistantError("mcp_request_failed", error_detail(result))
            if "result" not in result:
                raise HomeAssistantError("invalid_mcp_response")
            return result["result"]

    async def initialize(self) -> None:
        self._tools = {}
        result = await self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "StackChan", "version": "1.0"},
        })
        if not isinstance(result, dict) or result.get("protocolVersion") not in {PROTOCOL_VERSION, "2024-11-05", "2025-06-18"}:
            raise HomeAssistantError("unsupported_mcp_protocol")
        self._protocol = result["protocolVersion"]
        await self._rpc("notifications/initialized", notification=True)
        tools: dict[str, dict[str, Any]] = {}
        cursor = None
        for _ in range(8):
            page = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(page, dict) or not isinstance(page.get("tools"), list):
                raise HomeAssistantError("invalid_tool_catalog")
            for tool in page["tools"]:
                if not isinstance(tool, dict) or tool.get("name") not in ASSIST_TOOLS:
                    continue
                name = tool["name"]
                schema = tool.get("inputSchema")
                if not isinstance(schema, dict) or schema.get("type") != "object":
                    raise HomeAssistantError("invalid_tool_catalog")
                if len(json.dumps(schema)) > MAX_ARGUMENTS:
                    raise HomeAssistantError("invalid_tool_catalog")
                description = tool.get("description", "Home Assistant Assist device tool.")
                if not isinstance(description, str):
                    raise HomeAssistantError("invalid_tool_catalog")
                tools["ha_" + name] = {"type": "function", "name": "ha_" + name,
                    "description": description[:4000], "parameters": copy.deepcopy(schema), "strict": False}
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise HomeAssistantError("invalid_tool_catalog")
        else:
            raise HomeAssistantError("tool_catalog_too_large")
        if not tools:
            raise HomeAssistantError("no_exposed_assist_tools")
        self._tools = tools

    def definitions(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self._tools.values()))

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self._tools:
            return {"ok": False, "error": "unknown_home_assistant_tool"}
        try:
            if not isinstance(arguments, dict) or not all(isinstance(k, str) for k in arguments):
                return {"ok": False, "error": "invalid_tool_arguments"}
            if len(json.dumps(arguments, allow_nan=False).encode()) > MAX_ARGUMENTS:
                return {"ok": False, "error": "invalid_tool_arguments"}
            result = await self._rpc("tools/call", {"name": name[3:], "arguments": arguments})
            if not isinstance(result, dict):
                return {"ok": False, "error": "home_assistant_tool_failed"}
            content = result.get("content", [])
            if not isinstance(content, list):
                return {"ok": False, "error": "invalid_tool_result"}
            structured = result.get("structuredContent")
            failure = _semantic_failure(structured) or ("home_assistant_tool_failed" if result.get("isError") else None)
            detail = error_detail(structured)
            rendered: list[str] = []
            omitted_json = False
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    try:
                        decoded = json.loads(item["text"])
                    except ValueError:
                        if result.get("isError") and not detail:
                            detail = diagnostic_text(item["text"])
                        rendered.append(item["text"])
                        continue
                    failure = failure or _semantic_failure(decoded)
                    detail = detail or error_detail(decoded)
                    # GetLiveContext returns JSON containing a large YAML/text
                    # result. Decode the complete envelope before shortening
                    # the actual context; never send half a JSON document.
                    if isinstance(decoded, dict) and decoded.get("success") is True and isinstance(decoded.get("result"), str):
                        rendered.append(decoded["result"])
                    elif len(item["text"]) <= MAX_OUTPUT:
                        rendered.append(item["text"])
                    else:
                        omitted_json = True
            if failure:
                output = {"ok": False, "error": failure}
                if detail:
                    output["ha_detail"] = detail
                    match = re.search(r"MatchFailedReason\.(NAME|AREA)\b|No exposed entities matched (name|area)\b", detail, re.I)
                    if match:
                        field = (match[1] or match[2]).lower()
                        label = "apparaatnaam" if field == "name" else "ruimte"
                        output["feedback"] = f"Home Assistant vond geen overeenkomst voor de opgegeven {label}."
                return output
            text = "\n".join(rendered)
            output: dict[str, Any] = {"ok": True, "text": text[:MAX_OUTPUT]}
            if len(text) > MAX_OUTPUT or omitted_json:
                output["truncated"] = True
                output["note"] = ("Niet alle apparaten passen in dit resultaat. Vraag ha_GetLiveContext opnieuw "
                                  "met name, area of domain voor het bedoelde apparaat. Herhaal geen bedieningsactie.")
            if isinstance(structured, dict) and len(json.dumps(structured, allow_nan=False)) + len(text) < MAX_OUTPUT:
                output["data"] = structured
            return output
        except HomeAssistantError as error:
            return {"ok": False, "error": str(error), **({"ha_detail": error.detail} if error.detail else {})}
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": "invalid_tool_arguments"}
