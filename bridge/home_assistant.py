"""Local client for Home Assistant's exposed Assist MCP tools.

Credentials never leave this client except in authentication requests to the
configured HA origin. There is deliberately no REST service-call fallback.
"""
from __future__ import annotations

import asyncio
from ast import literal_eval
import copy
import hashlib
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

if __package__:
    from .ha_names import Target, context_targets, find_targets, name_key
else:
    from ha_names import Target, context_targets, find_targets, name_key


PROTOCOL_VERSION = "2025-03-26"
MAX_BODY = 1024 * 1024
MAX_OUTPUT = 24000
MAX_ARGUMENTS = 16384
TARGET_CACHE_LIMIT = 32
TARGET_CACHE_TTL = 3 * 86400
ALIAS_LIMIT = 32
TARGET_DOMAINS = frozenset({"light", "switch", "scene", "fan", "climate", "media_player", "script"})
SAVE_ALIAS_TOOL = "ha_SaveAlias"
FIND_TARGET_TOOL = "ha_FindTarget"
ATOMS3_TOOL = "ha_script_stackchan_atoms3_ledstrip"
ATOMS3_NAME = "AtomS3 LED Strip"
CATALOG_TTL = 45
READ_ATTEMPT_TIMEOUT = 3.0
READ_ONLY_TOOLS = frozenset({"GetLiveContext", "HassGetState"})
TOOL_DOMAINS = {"ha_HassLightSet": "light", "ha_HassClimateSetTemperature": "climate",
                "ha_HassFanSetSpeed": "fan", "ha_HassSetVolume": "media_player"}
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


def _tool_payload(value: Any) -> Any:
    if isinstance(value, dict) and value.get("success") is True and isinstance(value.get("result"), dict):
        return value["result"]
    return value


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


def _save_private_json(path: Path, data: dict[str, Any]) -> None:
    temporary = None
    try:
        if path.is_symlink():
            raise HomeAssistantError("auth_file_permissions")
        fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
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
                raise TimeoutError("Home Assistant response deadline")
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
            raise TimeoutError("Home Assistant response deadline")
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


def _target_label(value: Any) -> str | None:
    """Keep short labels, never credentials, prose, or raw tool arguments."""
    if isinstance(value, str) and re.fullmatch(r"""[\w][\w .()&+'’/"″-]{0,79}""", value):
        if diagnostic_text(value, 80) == value:
            return value
    return None


def _spoken_label(value: Any) -> str | None:
    return _target_label(" ".join(value.split())) if isinstance(value, str) else None


def _alias_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = " ".join(value.casefold().split())
    key = re.sub(r"(?<=[a-z]) (?=\d)", "", key)
    return key if re.fullmatch(r"[\w][\w -]{0,39}", key) else None


def _lookup_without_suffix(arguments: dict[str, Any]) -> dict[str, Any] | None:
    label = _target_label(arguments.get("name"))
    if not label:
        return None
    match = re.fullmatch(r"(.+?)(lampgroep|lampen|lamp|sc[eè]ne)", label, re.IGNORECASE)
    if not match:
        return None
    candidate = match[1].rstrip(" -_")
    if len(candidate) < 3 or candidate.casefold() in {"alle", "allemaal", "deze", "groep", "iedere", "welke"}:
        return None
    domain = "light" if match[2].lower().startswith("lamp") else "scene"
    if arguments.get("domain") not in (None, domain, [domain]):
        return None
    return {**arguments, "name": candidate, "domain": domain}


def _domains(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return ()
    choices = [value] if isinstance(value, str) else value
    if not isinstance(choices, list) or not all(isinstance(x, str) and re.fullmatch(r"[a-z_]+", x) for x in choices):
        return None
    return tuple(choices)


class HomeAssistantTools:
    def __init__(self, auth_path: Path, *, timeout: float = 8.0, scripts: tuple[str, ...] = (),
                 cache_path: Path | None = None):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise HomeAssistantError("invalid_timeout")
        self._path = Path(auth_path)
        self._auth = _read_auth(self._path)
        self._timeout = timeout
        if any(not re.fullmatch(r"[a-z0-9_]{1,54}", name) for name in scripts):
            raise ValueError("Gebruik objectnamen zonder script. in STACKCHAN_HA_SCRIPTS")
        self._scripts = frozenset(scripts)
        self._cache_path = cache_path
        self._cache_home = hashlib.sha256(self._auth["base_url"].encode()).hexdigest()
        self._cache_lock = asyncio.Lock()
        self._targets: dict[tuple[str, ...], dict[str, Any]] = {}
        self._aliases: dict[str, dict[str, Any]] = {}
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._auth_lock = asyncio.Lock()
        self._transport_lock = asyncio.Lock()
        self._session_id: str | None = None
        self._protocol = PROTOCOL_VERSION
        self._request_id = 0
        self._tools: dict[str, dict[str, Any]] = {}
        self._catalog_lock = asyncio.Lock()
        self._catalog_rows: list[Target] = []
        self._catalog_until = 0.0

    def _http(self, path: str, body: bytes, *, form: bool = False, request_id: int | None = None,
              timeout: float | None = None) -> tuple[Any, str | None]:
        headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json",
                   "Accept": "application/json, text/event-stream"}
        if not form:
            headers["Authorization"] = "Bearer " + self._auth["access_token"]
            headers["MCP-Protocol-Version"] = self._protocol
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
        request = Request(self._auth["base_url"] + path, data=body, headers=headers, method="POST")
        timeout = self._timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        try:
            with self._opener.open(request, timeout=timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id and (len(session_id) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in session_id)):
                    raise HomeAssistantError("invalid_mcp_response")
                return _decode_response(response, request_id, deadline=deadline), session_id
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
            await _finish_thread(_save_private_json, self._path, updated)
            self._auth = updated

    async def _rpc(self, method: str, params: dict[str, Any] | None = None,
                   *, notification: bool = False) -> Any:
        async with self._transport_lock:
            try:
                await self._ensure_auth()
            except HomeAssistantError as error:
                error.operation = "auth"
                raise
            tool = params.get("name") if method == "tools/call" and params else None
            read_only = tool in READ_ONLY_TOOLS
            timeout = min(self._timeout, READ_ATTEMPT_TIMEOUT) if read_only else self._timeout
            # Retry only a status read, with a fresh request ID. A write may
            # already have been applied by Home Assistant even if its reply is lost.
            for attempt in range(2 if read_only else 1):
                payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
                request_id = None
                if not notification:
                    self._request_id += 1
                    request_id = payload["id"] = self._request_id
                if params is not None:
                    payload["params"] = params
                try:
                    result, session_id = await _finish_thread(
                        self._http, "/api/mcp/assist", json.dumps(payload, allow_nan=False).encode(),
                        request_id=request_id, timeout=timeout)
                    break
                except HomeAssistantError as error:
                    if read_only and attempt == 0 and str(error) == "ha_unreachable":
                        continue
                    error.operation = "read" if read_only else "action" if tool else method
                    error.attempts = attempt + 1
                    raise
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
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    continue
                name = tool["name"]
                if name not in ASSIST_TOOLS and name not in self._scripts:
                    continue
                schema = tool.get("inputSchema")
                if not isinstance(schema, dict) or schema.get("type") != "object":
                    raise HomeAssistantError("invalid_tool_catalog")
                if len(json.dumps(schema)) > MAX_ARGUMENTS:
                    raise HomeAssistantError("invalid_tool_catalog")
                description = tool.get("description", "Home Assistant Assist device tool.")
                if not isinstance(description, str):
                    raise HomeAssistantError("invalid_tool_catalog")
                alias = "ha_" + name if name in ASSIST_TOOLS else "ha_script_" + name
                if alias.startswith("ha_script_"):
                    description = ("Runs a Home Assistant script. Use for requests to run this script. "
                                   "If the user explicitly asks for a scene, use the scene tool path instead. "
                                   + description)
                elif name == "HassTurnOn":
                    description = ("Also activates a Home Assistant scene. For a named scene, supply the exact "
                                   "scene name and domain [\"scene\"]. " + description)
                tools[alias] = {"type": "function", "name": alias,
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
        if "name" in tools.get("ha_GetLiveContext", {}).get("parameters", {}).get("properties", {}):
            tools[FIND_TARGET_TOOL] = {
                "type": "function", "name": FIND_TARGET_TOOL, "strict": False,
                "description": ("Find one exposed Home Assistant device, scene or enabled script by its spoken name, "
                                "including spacing variants and small typos. This is a read-only lookup. Use domain "
                                "script or scene when specified, and use the returned exact name or script tool."),
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string"}, "area": {"type": "string"}, "domain": {"type": "string"},
                }, "required": ["name"], "additionalProperties": False},
            }
        if self._cache_path and "ha_GetLiveContext" in tools:
            tools[SAVE_ALIAS_TOOL] = {
                "type": "function", "name": SAVE_ALIAS_TOOL, "strict": False,
                "description": ("Saves a persistent StackChan alias only when the user explicitly asks to save it. "
                                "No device action. Supply the exact Home Assistant target name. Use replace only "
                                "if the user explicitly asks to change an existing alias."),
                "parameters": {"type": "object", "properties": {
                    "alias": {"type": "string"}, "target": {"type": "string"},
                    "area": {"type": "string"},
                    "domain": {"type": "string", "enum": sorted(TARGET_DOMAINS)},
                    "replace": {"type": "boolean"},
                }, "required": ["alias", "target"], "additionalProperties": False},
            }
        self._tools = tools
        self._catalog_until = 0.0
        if self._cache_path:
            self._targets, self._aliases = await asyncio.to_thread(self._load_targets)

    def definitions(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self._tools.values()))

    def _script_targets(self) -> list[Target]:
        targets = []
        for name, definition in self._tools.items():
            if not name.startswith("ha_script_"):
                continue
            names = [name.removeprefix("ha_script_").replace("_", " ")]
            if match := re.search(r"\bAliases:\s*(\[[^\n]*\])", definition["description"]):
                try:
                    aliases = literal_eval(match[1])
                except (SyntaxError, ValueError):
                    aliases = []
                if isinstance(aliases, list):
                    names = [label for label in aliases if _target_label(label)] + names
            targets.append(Target(tuple(names), "script", tool=name))
        return targets

    async def _target_catalog(self) -> list[Target]:
        async with self._catalog_lock:
            if time.monotonic() < self._catalog_until:
                return self._catalog_rows
            result = await self._rpc("tools/call", {"name": "GetLiveContext", "arguments": {}})
            if not isinstance(result, dict) or result.get("isError"):
                return []
            rows = []
            content = result.get("content")
            for item in content if isinstance(content, list) else ():
                try:
                    payload = json.loads(item["text"])
                except (TypeError, KeyError, ValueError):
                    continue
                if isinstance(payload, dict) and payload.get("success") is True and isinstance(payload.get("result"), str):
                    rows.extend(context_targets(payload["result"]))
            if rows:
                self._catalog_rows = rows
                self._catalog_until = time.monotonic() + CATALOG_TTL
            return rows

    async def _find_target(self, name: str, domains: tuple[str, ...] = (), area: str | None = None) -> dict[str, Any]:
        candidates = [] if domains and set(domains) == {"script"} else await self._target_catalog()
        matches = find_targets(name, candidates + self._script_targets(), domains=domains, area=area)
        if len(matches) == 1:
            return {"ok": True, **matches[0].reference()}
        if matches:
            return {"ok": False, "error": "target_ambiguous", "options": [row.reference() for row in matches[:5]],
                    "feedback": "Meerdere doelen komen overeen. Vraag welk apparaat, script of welke scène is bedoeld."}
        return {"ok": False, "error": "target_not_found", "feedback": "Geen eenduidige vrijgegeven naam gevonden."}

    async def _resolve_operation(self, tool: str, arguments: dict[str, Any], label: str,
                                 domains: tuple[str, ...], area: str | None) -> dict[str, Any]:
        # Assist's overview contains no floor or device class, so leave those
        # filters to HA. Never send an action as part of name resolution.
        if arguments.keys() & {"floor", "device_class"}:
            return {}
        intrinsic = TOOL_DOMAINS.get(tool, "media_player" if tool.startswith("ha_HassMedia") else None)
        if intrinsic and domains not in ((), (intrinsic,)):
            return {}
        scope = domains or ((intrinsic,) if intrinsic else tuple(sorted(TARGET_DOMAINS - {"script"})))
        if "script" in scope:
            return {}
        return await self._find_target(label, scope, area)

    def _named_arguments(self, name: str, arguments: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        rewritten = {**arguments, "name": target["name"]}
        properties = self._tools[name]["parameters"].get("properties", {})
        if target.get("area") and "area" in properties:
            rewritten["area"] = target["area"]
        if "domain" in properties:
            rewritten["domain"] = [target["domain"]] if properties["domain"].get("type") == "array" else target["domain"]
        return rewritten

    @staticmethod
    def _target_key(entry: dict[str, Any]) -> tuple[str, ...]:
        return entry["tool"], entry["name"].casefold(), (entry["area"] or "").casefold(), entry["domain"]

    def _load_targets(self) -> tuple[dict[tuple[str, ...], dict[str, Any]], dict[str, dict[str, Any]]]:
        path = self._cache_path
        if path.is_symlink():
            return {}, {}
        try:
            with path.open("rb") as source:
                data = json.loads(source.read(131072))
        except (OSError, ValueError):
            return {}, {}
        if not isinstance(data, dict) or data.get("home") != self._cache_home:
            return {}, {}
        targets = {}
        for entry in data.get("targets", [])[-TARGET_CACHE_LIMIT:] if isinstance(data.get("targets"), list) else ():
            if (isinstance(entry, dict) and set(entry) == {"tool", "name", "area", "domain", "expires"}
                    and isinstance(entry["tool"], str) and entry["tool"] in self._tools
                    and isinstance(entry["domain"], str) and entry["domain"] in TARGET_DOMAINS
                    and _target_label(entry["name"]) and (entry["area"] is None or _target_label(entry["area"]))
                    and type(entry["expires"]) is int and time.time() < entry["expires"] <= time.time() + TARGET_CACHE_TTL):
                targets[self._target_key(entry)] = entry
        aliases = {}
        entries = data.get("aliases", [])
        for entry in entries[:ALIAS_LIMIT] if isinstance(entries, list) else ():
            if (isinstance(entry, dict) and set(entry) == {"alias", "name", "area", "domain"}
                    and (key := _alias_key(entry["alias"])) is not None and _target_label(entry["name"])
                    and (entry["area"] is None or _target_label(entry["area"]))
                    and isinstance(entry["domain"], str) and entry["domain"] in TARGET_DOMAINS):
                aliases[key] = entry
        return targets, aliases

    def saved_aliases(self) -> list[dict[str, Any]]:
        return [entry.copy() for entry in self._aliases.values()]

    async def _persist_cache(self, aliases: dict[str, dict[str, Any]]) -> None:
        await _finish_thread(_save_private_json, self._cache_path, {
            "home": self._cache_home, "targets": list(self._targets.values()), "aliases": list(aliases.values()),
        })

    def cached_targets(self) -> list[dict[str, Any]]:
        now = time.time()
        return [{key: entry[key] for key in ("tool", "name", "area", "domain")}
                for entry in self._targets.values() if entry["expires"] > now and entry["tool"] in self._tools]

    async def _update_targets(self, *, entry: dict[str, Any] | None = None,
                              remove: tuple[str, str | None] | None = None) -> None:
        if not self._cache_path:
            return
        async with self._cache_lock:
            now = time.time()
            targets = {key: value for key, value in self._targets.items() if value["expires"] > now}
            if entry is not None:
                key = self._target_key(entry)
                targets.pop(key, None)
                targets[key] = {**entry, "expires": int(now) + TARGET_CACHE_TTL}
                if len(targets) > TARGET_CACHE_LIMIT:
                    del targets[next(iter(targets))]
            elif remove is not None:
                name, area = remove
                targets = {key: value for key, value in targets.items()
                           if key[1] != name.casefold() or (area is not None and key[2] != area.casefold())}
            if targets != self._targets:
                self._targets = targets
                try:
                    await self._persist_cache(self._aliases)
                except HomeAssistantError:
                    pass  # The live call succeeded even if the optional cache file cannot be written.

    def _confirmed_target(self, name: str, arguments: dict[str, Any], response: Any) -> dict[str, Any] | None:
        if name.startswith("ha_script_"):
            label = name.removeprefix("ha_script_").replace("_", " ")
            return {"tool": name, "name": label, "area": None, "domain": "script"} if _target_label(label) else None
        if not isinstance(response, dict) or response.get("response_type") != "action_done":
            return None
        data = response.get("data")
        success = data.get("success") if isinstance(data, dict) else None
        if not isinstance(success, list) or len(success) != 1 or not isinstance(success[0], dict):
            return None
        if success[0].get("type") != "entity":
            return None
        entity = success[0].get("id")
        domain = entity.split(".", 1)[0] if isinstance(entity, str) and "." in entity else ""
        label = _target_label(arguments.get("name"))
        area = _target_label(arguments.get("area"))
        if label and domain in TARGET_DOMAINS and (arguments.get("area") is None or area):
            return {"tool": name, "name": label, "area": area, "domain": domain}
        return None

    async def _save_alias(self, arguments: dict[str, Any]) -> dict[str, Any]:
        alias = _alias_key(arguments.get("alias"))
        target = _target_label(arguments.get("target"))
        area = _target_label(arguments.get("area"))
        domain = arguments.get("domain")
        replace = arguments.get("replace", False)
        if (not alias or not target or (arguments.get("area") is not None and not area)
                or (domain is not None and (not isinstance(domain, str) or domain not in TARGET_DOMAINS))
                or not isinstance(replace, bool) or set(arguments) - {"alias", "target", "area", "domain", "replace"}):
            return {"ok": False, "error": "invalid_alias"}
        if domain == "script":
            script = "ha_script_" + re.sub(r"[ -]+", "_", target.casefold())
            if not area and script not in self._tools and FIND_TARGET_TOOL in self._tools:
                match = await self._find_target(target, ("script",))
                if match.get("error") == "target_ambiguous":
                    return match
                script = match.get("tool", script)
            if area or script not in self._tools:
                return {"ok": False, "error": "alias_target_unavailable"}
            target = script.removeprefix("ha_script_")
        else:
            query = {"name": target}
            if area:
                query["area"] = area
            if domain:
                query["domain"] = domain
            result = await self.execute("ha_GetLiveContext", query, _resolve_alias=False)
            if not result.get("ok"):
                return result
            context = result.get("text", "")
            domains = re.findall(r"^  domain: ([a-z_]+)$", context, re.M)
            if (result.get("truncated") or len(re.findall(r"^- names:", context, re.M)) != 1
                    or len(domains) != 1 or domains[0] not in TARGET_DOMAINS - {"script"}
                    or (domain and domain != domains[0])):
                return {"ok": False, "error": "alias_target_ambiguous",
                        "feedback": "Kies één vrijgegeven doel. Geef zo nodig ook het soort en de ruimte."}
            domain = domains[0]
            target = result.get("resolved_name", target)
            rooms = re.findall(r"^  areas: (.+)$", context, re.M)
            if not area and len(rooms) == 1 and "," not in rooms[0]:
                area = _target_label(rooms[0])
        entry = {"alias": alias, "name": target, "area": area, "domain": domain}
        async with self._cache_lock:
            previous = self._aliases.get(alias)
            if previous and previous != entry and not replace:
                return {"ok": False, "error": "alias_already_exists", "current_alias": previous}
            if not previous and len(self._aliases) >= ALIAS_LIMIT:
                return {"ok": False, "error": "alias_limit_reached"}
            if previous != entry:
                aliases = {**self._aliases, alias: entry}
                try:
                    await self._persist_cache(aliases)
                except asyncio.CancelledError:
                    self._targets, self._aliases = await asyncio.to_thread(self._load_targets)
                    raise
                except HomeAssistantError:
                    return {"ok": False, "error": "alias_save_failed"}
                self._aliases = aliases
        return {"ok": True, "saved_alias": entry}

    async def _execute_alias(self, tool: str, arguments: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        name, area, domain = entry["name"], entry["area"], entry["domain"]
        requested_area = arguments.get("area")
        if (arguments.get("domain") not in (None, domain, [domain])
                or (requested_area is not None and (not isinstance(requested_area, str)
                    or (area and requested_area.casefold() != area.casefold())))):
            return {"ok": False, "error": "alias_scope_mismatch", "saved_alias": entry}
        if domain == "script":
            if tool == "ha_HassTurnOn" and set(arguments) <= {"name", "domain"}:
                result = await self.execute("ha_script_" + name, {})
            else:
                return {"ok": False, "error": "use_script_tool", "script_tool": "ha_script_" + name}
        else:
            properties = self._tools[tool]["parameters"].get("properties", {})
            if "domain" not in properties:
                intrinsic_domain = TOOL_DOMAINS.get(tool, "media_player" if tool.startswith("ha_HassMedia") else None)
                if intrinsic_domain != domain:
                    return {"ok": False, "error": "alias_scope_mismatch", "saved_alias": entry}
            rewritten = {**arguments, "name": name}
            if "domain" in properties:
                rewritten["domain"] = [domain] if properties["domain"].get("type") == "array" else domain
            if area and "area" in properties:
                rewritten["area"] = area
            color_args = self._atoms3_color_arguments(tool, rewritten, entry)
            if color_args is not None:
                result = {**await self.execute(ATOMS3_TOOL, color_args), "via_tool": ATOMS3_TOOL}
            else:
                result = await self.execute(tool, rewritten, _suffix_fallback=False, _resolve_alias=False, _resolve_name=False)
        return {**result, "resolved_name": name, "resolved_domain": domain}

    def _atoms3_color_arguments(self, tool: str, arguments: dict[str, Any],
                                target: dict[str, Any]) -> dict[str, Any] | None:
        if (ATOMS3_TOOL in self._tools and tool == "ha_HassLightSet" and target["domain"] == "light"
                and name_key(target["name"]) == name_key(ATOMS3_NAME) and arguments.get("color")
                and arguments.get("brightness") != 0 and not arguments.keys() & {"floor", "device_class", "temperature"}):
            return {key: arguments[key] for key in ("color", "brightness") if key in arguments}
        return None

    async def execute(self, name: str, arguments: dict[str, Any], *, _suffix_fallback: bool = True,
                      _resolve_alias: bool = True, _resolve_name: bool = True) -> dict[str, Any]:
        if name not in self._tools:
            return {"ok": False, "error": "unknown_home_assistant_tool"}
        try:
            if not isinstance(arguments, dict) or not all(isinstance(k, str) for k in arguments):
                return {"ok": False, "error": "invalid_tool_arguments"}
            if len(json.dumps(arguments, allow_nan=False).encode()) > MAX_ARGUMENTS:
                return {"ok": False, "error": "invalid_tool_arguments"}
            if name == SAVE_ALIAS_TOOL:
                return await self._save_alias(arguments)
            label = _spoken_label(arguments.get("name"))
            area = _spoken_label(arguments.get("area"))
            domains = _domains(arguments.get("domain"))
            valid_scope = domains is not None and (arguments.get("area") is None or bool(area))
            alias = self._aliases.get(_alias_key(label)) if _resolve_alias and not name.startswith("ha_script_") else None
            if name == FIND_TARGET_TOOL:
                if not label or not valid_scope or set(arguments) - {"name", "area", "domain"}:
                    return {"ok": False, "error": "invalid_tool_arguments"}
                if alias:
                    if (domains and alias["domain"] not in domains) or (area and alias["area"] and name_key(area) != name_key(alias["area"])):
                        return {"ok": False, "error": "alias_scope_mismatch"}
                    reference = {k: alias[k] for k in ("name", "area", "domain")}
                    if alias["domain"] == "script":
                        script = "ha_script_" + alias["name"]
                        if script not in self._tools:
                            return {"ok": False, "error": "alias_target_unavailable"}
                        reference["tool"] = script
                    return {"ok": True, **reference, "resolved_name": alias["name"], "resolved_domain": alias["domain"]}
                return await self._find_target(label, domains, area)
            if alias:
                return await self._execute_alias(name, arguments, alias)
            resolved = None
            can_resolve = _resolve_name and FIND_TARGET_TOOL in self._tools and label and valid_scope
            if can_resolve and name != "ha_GetLiveContext" and not name.startswith("ha_script_"):
                candidate = await self._resolve_operation(name, arguments, label, domains, area)
                if candidate.get("error") == "target_ambiguous":
                    return candidate
                if candidate.get("ok"):
                    color_args = self._atoms3_color_arguments(name, arguments, candidate)
                    if color_args is not None:
                        return {**await self.execute(ATOMS3_TOOL, color_args), "via_tool": ATOMS3_TOOL,
                                "resolved_name": candidate["name"], "resolved_domain": "light"}
                    arguments = self._named_arguments(name, arguments, candidate)
                    resolved = candidate
            prefix = "ha_script_" if name.startswith("ha_script_") else "ha_"
            result = await self._rpc("tools/call", {"name": name[len(prefix):], "arguments": arguments})
            if not isinstance(result, dict):
                return {"ok": False, "error": "home_assistant_tool_failed"}
            content = result.get("content", [])
            if not isinstance(content, list):
                return {"ok": False, "error": "invalid_tool_result"}
            structured = result.get("structuredContent")
            structured_payload = _tool_payload(structured)
            confirmed = self._confirmed_target(name, arguments, structured_payload) if self._cache_path else None
            failure = (_semantic_failure(structured) or _semantic_failure(structured_payload)
                       or ("home_assistant_tool_failed" if result.get("isError") else None))
            detail = error_detail(structured) or error_detail(structured_payload)
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
                    payload = _tool_payload(decoded)
                    failure = failure or _semantic_failure(decoded) or _semantic_failure(payload)
                    detail = detail or error_detail(decoded) or error_detail(payload)
                    if self._cache_path and confirmed is None:
                        confirmed = self._confirmed_target(name, arguments, payload)
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
                match = None
                if detail:
                    output["ha_detail"] = detail
                    match = re.search(r"MatchFailedReason\.(NAME|AREA)\b|No exposed entities matched (name|area)\b", detail, re.I)
                    if match:
                        field = (match[1] or match[2]).lower()
                        field_label = "apparaatnaam" if field == "name" else "ruimte"
                        output["feedback"] = f"Home Assistant vond geen overeenkomst voor de opgegeven {field_label}."
                        if name == "ha_GetLiveContext" and field == "name" and _suffix_fallback and not can_resolve:
                            shortened = _lookup_without_suffix(arguments)
                            if shortened:
                                retry = await self.execute(name, shortened, _suffix_fallback=False,
                                                           _resolve_alias=_resolve_alias, _resolve_name=False)
                                if retry.get("ok") or retry.get("error") == "device_unavailable":
                                    return {**retry, "resolved_name": shortened["name"],
                                            "resolved_domain": shortened["domain"]}
                    if (match or detail == "no_valid_targets") and (target := _target_label(arguments.get("name"))):
                        await self._update_targets(remove=(target, _target_label(arguments.get("area"))))
                if (can_resolve and name == "ha_GetLiveContext"
                        and (match or detail and re.fullmatch(r"Area '.*' does not exist", detail))):
                    target = await self._find_target(label, domains, area)
                    if target.get("tool"):
                        return target  # Scripts are catalogue tools, not state entities.
                    if target.get("ok"):
                        rewritten = self._named_arguments(name, arguments, target)
                        retry = await self.execute(name, rewritten, _resolve_alias=False, _resolve_name=False,
                                                   _suffix_fallback=False)
                        return {**retry, "resolved_name": target["name"], "resolved_domain": target["domain"]}
                    if target.get("error") == "target_ambiguous":
                        return {**output, **target}
                return output
            text = "\n".join(rendered)
            if name == "ha_GetLiveContext" and label:
                rows = context_targets(text)
                if len(rows) == 1 and rows[0].state == "unavailable":
                    target = rows[0]
                    device_name = diagnostic_text(target.names[0], 80)
                    return {"ok": False, "error": "device_unavailable", "resolved_name": device_name,
                            "resolved_domain": target.domain,
                            "feedback": f"Home Assistant is bereikbaar. {device_name} zelf is onbereikbaar."}
            output: dict[str, Any] = {"ok": True, "text": text[:MAX_OUTPUT]}
            if len(text) > MAX_OUTPUT or omitted_json:
                output["truncated"] = True
                output["note"] = ("Niet alle apparaten passen in dit resultaat. Vraag ha_GetLiveContext opnieuw "
                                  "met name, area of domain voor het bedoelde apparaat. Herhaal geen bedieningsactie.")
            if isinstance(structured, dict) and len(json.dumps(structured, allow_nan=False)) + len(text) < MAX_OUTPUT:
                output["data"] = structured
            if confirmed:
                await self._update_targets(entry=confirmed)
                output["confirmed_target"] = confirmed
            if resolved:
                output.update(resolved_name=resolved["name"], resolved_domain=resolved["domain"])
            return output
        except HomeAssistantError as error:
            output = {"ok": False, "error": str(error)}
            if error.detail:
                output["ha_detail"] = error.detail
            if str(error) == "ha_unreachable":
                operation = getattr(error, "operation", None)
                output.update(ha_operation=operation, attempts=getattr(error, "attempts", None),
                              network_errno=getattr(error, "network_errno", None),
                              network_kind=getattr(error, "network_kind", None))
                output["feedback"] = {
                    "read": "Home Assistant gaf tijdelijk geen antwoord op deze opvraag.",
                    "action": "Home Assistant gaf geen bevestiging. Het is onzeker of de opdracht is uitgevoerd.",
                    "auth": "Home Assistant reageerde niet bij het vernieuwen van de aanmelding.",
                }.get(operation, "Home Assistant gaf tijdelijk geen antwoord.")
            return output
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "error": "invalid_tool_arguments"}
