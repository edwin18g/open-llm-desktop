"""MCP client manager: connects to MCP servers and exposes their tools to LiteRT-LM.

Config format (same as Claude Desktop), file: ~/.config/ek-light-llm/mcp_servers.json
{
  "mcpServers": {
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/me"]},
    "remote":     {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer xxx"}},
    "legacy-sse": {"url": "https://example.com/sse", "transport": "sse"},
    "off":        {"command": "...", "disabled": true}
  }
}
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Callable

MAX_RESULT_CHARS = 6000

EXAMPLE_CONFIG = {
    "mcpServers": {
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", str(Path.home() / "Documents")],
            "disabled": True,
        },
        "fetch": {"command": "uvx", "args": ["mcp-server-fetch"], "disabled": True},
        "my-remote-server": {
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer YOUR_TOKEN"},
            "disabled": True,
        },
    }
}


def _safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    return s.strip("_") or "x"


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Reads a field that may be snake_case (mcp 2.x) or camelCase (mcp 1.x)."""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


_EMPTY = (None, "", "0", 0, "0000-00-00", "0000-00-00 00:00:00", [], {}, "[]", "{}", "null")
_NOISE_KEY = re.compile(r"(^|_)(id|avatar|image|thumb|deleted|password|token|hash)$|_ids?$", re.I)


def _cell(v: Any, limit: int = 80) -> str:
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    s = re.sub(r"<[^>]+>", " ", str(v))  # HTML from rich-text fields
    s = re.sub(r"\s+", " ", s).strip().replace("|", "/")
    return s[:limit] + "…" if len(s) > limit else s


def _compact(text: str, max_chars: int) -> str:
    """Turns a JSON list of records into a small markdown table: columns that are empty in every
    row and internal ids are dropped, long cells shortened, whole rows cut to fit. Small models read
    this far better than raw JSON, and it fits many more records in the context window."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    extra: list[str] = []
    rows = data
    if isinstance(data, list) and not data:
        return "0 records"
    if isinstance(data, dict):
        lists = [(k, v) for k, v in data.items() if isinstance(v, list) and all(isinstance(x, dict) for x in v)]
        if len(lists) != 1:
            kept = {k: v for k, v in data.items() if v not in _EMPTY}
            return "\n".join(f"{k}: {_cell(v, 1000)}" for k, v in kept.items()) or text
        rows = lists[0][1]
        extra = [f"{k}: {_cell(v, 300)}" for k, v in data.items() if k != lists[0][0] and v not in _EMPTY
                 and not isinstance(v, (dict, list))]
        if not rows:
            return "\n".join(extra + ["0 records"])
    if not (isinstance(rows, list) and rows and all(isinstance(x, dict) for x in rows)):
        return text
    keys: list[str] = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    def table(rs: list[dict]) -> tuple[list[str], int]:
        cols = [k for k in keys if (k == "id" or not _NOISE_KEY.search(k)) and any(r.get(k) not in _EMPTY for r in rs)]
        lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        used = sum(len(x) for x in lines + extra) + 40
        for n, r in enumerate(rs):
            line = "| " + " | ".join(_cell(r.get(k)) if r.get(k) not in _EMPTY else "" for k in cols) + " |"
            if used + len(line) > max_chars and n:
                return lines, n
            lines.append(line)
            used += len(line) + 1
        return lines, len(rs)

    lines, shown = table(rows)
    if shown < len(rows):  # columns empty in every shown row go too
        lines, shown = table(rows[:shown])
    head = f"{len(rows)} records" + (f" (showing the first {shown})" if shown < len(rows) else "")
    return "\n".join(extra + [head, *_group_counts(rows, keys), *lines])


def _check_enums(schema: dict, args: dict) -> str:
    """Small models invent option values (module "HR" when the list has "Leave") and servers often
    answer those with an empty result. Fixes case slips and explains the rest, so the model retries."""
    for key, prop in (schema.get("properties") or {}).items():
        options = prop.get("enum") if isinstance(prop, dict) else None
        value = args.get(key)
        if not options or value is None or value in options or not isinstance(value, str):
            continue
        same = [o for o in options if isinstance(o, str) and o.lower() == value.strip().lower()]
        if same:
            args[key] = same[0]
            continue
        return (f"ERROR: '{value}' is not an allowed {key}. Call the tool again with {key} set to one of: "
                + ", ".join(map(str, options)))
    return ""


_GROUP_KEY = re.compile(r"status|stage|type|priority|category|source|book|owner|assigned|department|title", re.I)


def _group_counts(rows: list[dict], keys: list[str]) -> list[str]:
    """Counts per value of a few category columns (status, type, owner…), worked out here because
    small models miscount table rows."""
    if len(rows) < 2:
        return []
    out = []
    for k in keys:
        if not _GROUP_KEY.search(k) or _NOISE_KEY.search(k):
            continue
        values = [r.get(k) for r in rows]
        if any(isinstance(v, (dict, list)) for v in values):
            continue
        counts: dict[str, int] = {}
        for v in values:
            label = "(none)" if v in _EMPTY else _cell(v, 40)
            counts[label] = counts.get(label, 0) + 1
        if 1 < len(counts) <= 8 or (len(counts) == 1 and len(rows) >= 3):
            out.append(f"By {k}: " + ", ".join(f"{v} {n}" for v, n in sorted(counts.items(), key=lambda x: -x[1])))
        if len(out) >= 4:
            break
    return out


def _result_to_text(result: Any) -> str:
    """Flattens an MCP CallToolResult into text the model can read."""
    parts: list[str] = []
    for c in getattr(result, "content", None) or []:
        ctype = getattr(c, "type", "")
        if ctype == "text":
            parts.append(c.text)
        elif ctype == "resource":
            res = getattr(c, "resource", None)
            parts.append(getattr(res, "text", None) or f"[resource {getattr(res, 'uri', '')}]")
        elif ctype == "resource_link":
            parts.append(f"[link {getattr(c, 'uri', '')}]")
        elif ctype in ("image", "audio"):
            parts.append(f"[{ctype} omitted: {_get(c, 'mime_type', 'mimeType', default='')}]")
    structured = _get(result, "structured_content", "structuredContent")
    if not parts and structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False))
    text = "\n".join(parts).strip() or "(no output)"
    if _get(result, "is_error", "isError", default=False):
        text = "ERROR: " + text
    else:
        text = _compact(text, MAX_RESULT_CHARS)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n…[truncated {len(text) - MAX_RESULT_CHARS} chars]"
    return text


_DESTRUCTIVE_WORDS = {"delete", "remove", "rm", "rmdir", "drop", "truncate", "unlink", "trash", "destroy",
                      "purge", "wipe", "erase", "uninstall", "revoke"}
_SQL_TOOL = re.compile(r"(^|_)(query|sql|execute|exec|run)(_|$)", re.I)
_SQL_WRITE = re.compile(r"\b(drop|delete|truncate|alter|update|insert|replace|grant|revoke|rename)\b", re.I)


def is_destructive(tool: Any, args: dict | None = None) -> bool:
    """True when a call to this MCP tool may delete or overwrite data, judged by the tool's own name
    (not the prefixed function name the model sees), its MCP annotations and, for SQL-style tools,
    the statement it was given."""
    name = str(getattr(tool, "name", "") or "")
    words = {w.lower() for w in re.findall(r"[A-Za-z][a-z]*|[A-Z]+(?![a-z])", name.replace("-", "_"))}
    if words & _DESTRUCTIVE_WORDS:
        return True
    ann = _get(tool, "annotations")
    # destructiveHint defaults to true in the spec, which would make every tool ask; only an explicit true counts.
    if ann is not None and _get(ann, "destructive_hint", "destructiveHint") is True \
            and _get(ann, "read_only_hint", "readOnlyHint") is not True:
        return True
    if args and _SQL_TOOL.search(name):
        text = " ".join(str(v) for v in args.values() if isinstance(v, str))
        if _SQL_WRITE.search(text):
            return True
    return False


def _flatten_error(e: BaseException) -> str:
    subs = getattr(e, "exceptions", None)
    if subs:
        return "; ".join(_flatten_error(x) for x in subs)
    return str(e) or type(e).__name__


class _Server:
    def __init__(self, name: str, spec: dict) -> None:
        self.name = name
        self.spec = spec
        self.session = None
        self.tools: list[Any] = []
        self.status = "disabled" if spec.get("disabled") else "connecting"
        self.error = ""
        self.stop_event: asyncio.Event | None = None
        self.task: asyncio.Task | None = None


# ---------- OAuth for remote servers ----------
# Remote servers that answer 401 are signed in with the MCP OAuth flow (discovery, dynamic
# client registration, PKCE): the login page opens in the browser and redirects back here.
OAUTH_CALLBACK_PORT = 33418
OAUTH_REDIRECT_URI = f"http://127.0.0.1:{OAUTH_CALLBACK_PORT}/callback"
OAUTH_LOGIN_TIMEOUT = 300
_OAUTH_DONE_PAGE = (
    "<html><body style='font-family:sans-serif;text-align:center;padding-top:80px'>"
    "<h2>{title}</h2><p>{msg}</p></body></html>"
)


class _FileTokenStorage:
    """Keeps a server's OAuth tokens and client registration in one JSON file (mode 600)."""

    def __init__(self, path: Path, server_url: str) -> None:
        self.path = path
        self.server_url = server_url

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except Exception:  # noqa: BLE001
            return {}
        return data if data.get("server_url") == self.server_url else {}

    def _write(self, key: str, value) -> None:
        data = self._read()
        data.update({"server_url": self.server_url, key: value})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.touch(mode=0o600)
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        t = self._read().get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens) -> None:
        self._write("tokens", tokens.model_dump(mode="json", exclude_none=True))
        if tokens.expires_in:
            self._write("expires_at", time.time() + tokens.expires_in)

    def expires_at(self) -> float | None:
        return self._read().get("expires_at")

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        c = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def set_client_info(self, client_info) -> None:
        self._write("client_info", client_info.model_dump(mode="json", exclude_none=True))


def _refreshing_provider_class():
    """OAuthClientProvider that survives restarts: the SDK forgets when a saved access token expires
    and doesn't know the token endpoint, so after a restart it sends the stale token, gets a 401 and
    asks for a new browser sign-in. This restores the expiry and the server's OAuth metadata from
    storage/discovery, so an expired token is renewed with the refresh token instead."""
    from mcp.client.auth import OAuthClientProvider

    class _Provider(OAuthClientProvider):
        async def _initialize(self) -> None:
            await super()._initialize()
            ctx = self.context
            if not (ctx.current_tokens and ctx.current_tokens.refresh_token):
                return
            exp = ctx.storage.expires_at()
            # Unknown age (saved by an older version): treat as expired so it's refreshed up front.
            ctx.token_expiry_time = (exp - 60) if exp else 1.0
            if ctx.oauth_metadata is None:
                try:
                    await self._discover_metadata()
                except Exception as e:  # noqa: BLE001  (falls back to a browser sign-in)
                    print(f"[mcp] OAuth metadata discovery failed: {e}", file=sys.stderr)

        async def _discover_metadata(self) -> None:
            import httpx2
            from urllib.parse import urlsplit

            from mcp.shared.auth import OAuthMetadata

            u = urlsplit(self.context.server_url)
            origin = f"{u.scheme}://{u.netloc}"
            auth_server = origin
            async with httpx2.AsyncClient(timeout=15, follow_redirects=True) as c:
                for url in (f"{origin}/.well-known/oauth-protected-resource{u.path.rstrip('/')}",
                            f"{origin}/.well-known/oauth-protected-resource"):
                    r = await c.get(url)
                    if r.status_code == 200 and r.json().get("authorization_servers"):
                        auth_server = r.json()["authorization_servers"][0].rstrip("/")
                        self.context.auth_server_url = auth_server
                        break
                a = urlsplit(auth_server)
                a_origin, a_path = f"{a.scheme}://{a.netloc}", a.path.rstrip("/")
                for url in (f"{a_origin}/.well-known/oauth-authorization-server{a_path}",
                            f"{a_origin}/.well-known/openid-configuration{a_path}"):
                    r = await c.get(url)
                    if r.status_code == 200:
                        self.context.oauth_metadata = OAuthMetadata.model_validate(r.json())
                        return

    return _Provider


class _OAuthBrowserLogin:
    """Opens the authorization URL in the browser and waits for the redirect on 127.0.0.1."""

    _port_lock: asyncio.Lock | None = None  # one sign-in at a time: they share the callback port

    def __init__(self, srv: "_Server", on_change: Callable[[], None]) -> None:
        self.srv = srv
        self.on_change = on_change
        self._server: asyncio.AbstractServer | None = None
        self._result: asyncio.Future | None = None

    async def redirect(self, url: str) -> None:
        import webbrowser

        if _OAuthBrowserLogin._port_lock is None:
            _OAuthBrowserLogin._port_lock = asyncio.Lock()
        await _OAuthBrowserLogin._port_lock.acquire()
        try:
            self._result = asyncio.get_running_loop().create_future()
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", OAUTH_CALLBACK_PORT)
        except OSError as e:
            _OAuthBrowserLogin._port_lock.release()
            raise RuntimeError(f"Can't listen for the sign-in callback on port {OAUTH_CALLBACK_PORT}: {e}") from e
        self.srv.error = "Sign in in your browser to finish connecting…"
        self.on_change()
        await asyncio.to_thread(webbrowser.open, url)

    async def callback(self):
        from mcp.shared.auth import AuthorizationCodeResult

        try:
            params = await asyncio.wait_for(self._result, timeout=OAUTH_LOGIN_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError("Sign-in timed out (no response from the browser)") from None
        finally:
            self._server.close()
            _OAuthBrowserLogin._port_lock.release()
            self.srv.error = ""
        if params.get("error"):
            raise RuntimeError(f"Sign-in failed: {params.get('error_description') or params['error']}")
        return AuthorizationCodeResult(code=params.get("code", ""), state=params.get("state"), iss=params.get("iss"))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        from urllib.parse import parse_qs, urlsplit

        try:
            line = (await reader.readline()).decode("latin-1")
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            target = line.split(" ")[1] if line.count(" ") >= 2 else ""
            parts = urlsplit(target)
            params = {k: v[0] for k, v in parse_qs(parts.query).items()}
            if parts.path != "/callback" or not (params.get("code") or params.get("error")):
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            ok = "code" in params
            body = _OAUTH_DONE_PAGE.format(
                title="Signed in" if ok else "Sign-in failed",
                msg="You can close this tab and return to EK Light." if ok else "Return to EK Light and try again.",
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )
            if self._result and not self._result.done():
                self._result.set_result(params)
        finally:
            await writer.drain()
            writer.close()


class MCPManager:
    def __init__(self, config_path: Path, on_change: Callable[[], None] | None = None) -> None:
        self.config_path = config_path
        self.oauth_dir = config_path.parent / "mcp_oauth"
        self.on_change = on_change or (lambda: None)
        self.spec_hook: Callable[[str, dict], dict] | None = None  # adjusts a spec just before it starts
        self.servers: dict[str, _Server] = {}
        self._cfg_lock = threading.RLock()
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="mcp-loop")
        self._thread.start()

    # ---------- config ----------
    def _atomic_write(self, data: dict) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.config_path)

    def read_config_text(self) -> str:
        with self._cfg_lock:
            if not self.config_path.exists():
                self._atomic_write({"mcpServers": {}})
            return self.config_path.read_text()

    def write_config_text(self, text: str) -> None:
        data = json.loads(text)  # raises on invalid JSON
        if not isinstance(data.get("mcpServers"), dict):
            raise ValueError('Config must contain an object "mcpServers".')
        with self._cfg_lock:
            self._atomic_write(data)

    def _load_specs(self, strict: bool = False) -> dict[str, dict]:
        with self._cfg_lock:
            try:
                return json.loads(self.read_config_text()).get("mcpServers", {}) or {}
            except Exception as e:  # noqa: BLE001
                if strict:
                    raise ValueError(f"MCP config file is invalid, fix it in Settings first: {e}") from e
                print("MCP config error:", e)
                return {}

    def _save_specs(self, specs: dict) -> None:
        with self._cfg_lock:
            self._atomic_write({"mcpServers": specs})

    def specs(self) -> dict[str, dict]:
        return self._load_specs()

    # ---------- single-plugin ops ----------
    def add_server(self, name: str, spec: dict, timeout: float = OAUTH_LOGIN_TIMEOUT + 60) -> None:
        """Saves a server to the config and (re)connects just that server."""
        with self._cfg_lock:
            specs = self._load_specs(strict=True)
            specs[name] = spec
            self._save_specs(specs)
        fut = asyncio.run_coroutine_threadsafe(self._restart_one(name, spec), self.loop)
        try:
            fut.result(timeout=timeout)
        except Exception:
            traceback.print_exc()

    def remove_server(self, name: str) -> None:
        with self._cfg_lock:
            specs = self._load_specs(strict=True)
            specs.pop(name, None)
            self._save_specs(specs)
        self._oauth_path(name).unlink(missing_ok=True)
        try:
            asyncio.run_coroutine_threadsafe(self._stop_one(name, forget=True), self.loop).result(timeout=10)
        except Exception:
            traceback.print_exc()
        self.on_change()

    async def _stop_one(self, name: str, forget: bool = False) -> None:
        srv = self.servers.get(name)
        if srv and srv.stop_event:
            srv.stop_event.set()
            if srv.task:
                await asyncio.wait([srv.task], timeout=5)
        if forget:
            self.servers.pop(name, None)

    async def _restart_one(self, name: str, spec: dict) -> None:
        await self._stop_one(name)
        srv = _Server(name, spec)
        self.servers[name] = srv
        self.on_change()
        if srv.status == "disabled":
            return
        ready = asyncio.Event()
        srv.stop_event = asyncio.Event()
        srv.task = asyncio.create_task(self._run_server(srv, ready))
        try:
            await asyncio.wait_for(ready.wait(), timeout=OAUTH_LOGIN_TIMEOUT + 40)
        except asyncio.TimeoutError:
            srv.status, srv.error = "error", "Timed out while connecting"
            srv.stop_event.set()
        self.on_change()

    # ---------- lifecycle ----------
    def reload(self, timeout: float = 60) -> None:
        """Disconnects everything and connects to all enabled servers (blocking)."""
        fut = asyncio.run_coroutine_threadsafe(self._reload(), self.loop)
        try:
            fut.result(timeout=timeout)
        except Exception:
            traceback.print_exc()

    def shutdown(self) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._stop_all(), self.loop).result(timeout=5)
        except Exception:
            pass

    async def _stop_all(self) -> None:
        for s in self.servers.values():
            if s.stop_event:
                s.stop_event.set()
        tasks = [s.task for s in self.servers.values() if s.task]
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        self.servers = {}

    async def _reload(self) -> None:
        await self._stop_all()
        specs = self._load_specs()
        waits = []
        for name, spec in specs.items():
            srv = _Server(name, spec if isinstance(spec, dict) else {})
            self.servers[name] = srv
            if srv.status == "disabled":
                continue
            ready = asyncio.Event()
            srv.stop_event = asyncio.Event()
            srv.task = asyncio.create_task(self._run_server(srv, ready))
            waits.append(ready.wait())
        self.on_change()
        if waits:
            await asyncio.wait([asyncio.create_task(w) for w in waits], timeout=120)
        for srv in self.servers.values():
            if srv.status == "connecting":
                srv.status, srv.error = "error", "Timed out while connecting"
        self.on_change()

    def _oauth_path(self, name: str) -> Path:
        return self.oauth_dir / f"{_safe_name(name)}.json"

    def _oauth(self, srv: _Server):
        """OAuth handler for a remote server; None when it sends its own Authorization header."""
        spec = srv.spec
        if spec.get("oauth") is False or any(k.lower() == "authorization" for k in (spec.get("headers") or {})):
            return None
        from mcp.shared.auth import OAuthClientMetadata

        login = _OAuthBrowserLogin(srv, self.on_change)
        return _refreshing_provider_class()(
            server_url=spec["url"],
            client_metadata=OAuthClientMetadata(
                client_name="EK Light LLM",
                redirect_uris=[OAUTH_REDIRECT_URI],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
                scope=spec.get("scope"),
            ),
            storage=_FileTokenStorage(self._oauth_path(srv.name), spec["url"]),
            redirect_handler=login.redirect,
            callback_handler=login.callback,
        )

    async def _run_server(self, srv: _Server, ready: asyncio.Event) -> None:
        from mcp import ClientSession

        spec = srv.spec
        if self.spec_hook is not None:
            try:
                spec = await asyncio.to_thread(self.spec_hook, srv.name, dict(spec))
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        try:
            async with AsyncExitStack() as stack:
                if spec.get("url"):
                    transport = spec.get("transport") or spec.get("type") or ""
                    headers = spec.get("headers") or {}
                    auth = self._oauth(srv)
                    if transport == "sse" or spec["url"].rstrip("/").endswith("/sse"):
                        from mcp.client.sse import sse_client

                        streams = await stack.enter_async_context(sse_client(spec["url"], headers=headers, auth=auth))
                    else:
                        import httpx2
                        from mcp.client.streamable_http import streamable_http_client

                        client = await stack.enter_async_context(
                            httpx2.AsyncClient(headers=headers, auth=auth, timeout=httpx2.Timeout(30, read=300))
                        )
                        streams = await stack.enter_async_context(
                            streamable_http_client(spec["url"], http_client=client)
                        )
                else:
                    from mcp import StdioServerParameters
                    from mcp.client.stdio import stdio_client

                    cmd = spec.get("command")
                    if not cmd:
                        raise ValueError('Needs "command" (local) or "url" (remote).')
                    app_dir = str(Path(__file__).resolve().parent)
                    if cmd == "{app_python}":
                        cmd = sys.executable
                    resolved = shutil.which(cmd) or cmd
                    env = {**os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()}}
                    params = StdioServerParameters(
                        command=resolved, args=[str(a).replace("{app_dir}", app_dir) for a in spec.get("args", [])],
                        env=env, cwd=spec.get("cwd"),
                    )
                    devnull = stack.enter_context(open(os.devnull, "w"))
                    streams = await stack.enter_async_context(stdio_client(params, errlog=devnull))

                read, write = streams[0], streams[1]
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                tools = await session.list_tools()
                srv.tools = list(tools.tools)
                srv.session = session
                srv.status, srv.error = "connected", ""
                ready.set()
                self.on_change()
                await srv.stop_event.wait()
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, asyncio.CancelledError):
                raise
            msg = _flatten_error(e)
            srv.status, srv.error = "error", (msg or type(e).__name__)[:300]
            print(f"MCP server '{srv.name}' failed:", srv.error)
        finally:
            srv.session = None
            if srv.status == "connected":
                srv.status = "disconnected"
            ready.set()
            self.on_change()

    def wait_connected(self, names: set[str], timeout: float = 45) -> list[str]:
        """Waits while any of these servers is still connecting (e.g. right after app start).
        Returns the names that are connected when it returns."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(s.status == "connecting" for n, s in self.servers.items() if n in names):
                break
            time.sleep(0.2)
        return [n for n, s in self.servers.items() if n in names and s.status == "connected"]

    # ---------- info ----------
    def summary(self) -> list[dict]:
        out = []
        for s in self.servers.values():
            out.append({
                "name": s.name, "status": s.status, "error": s.error,
                "tools": [{"name": t.name, "description": (t.description or "")[:200]} for t in s.tools],
                "remote": bool(s.spec.get("url")),
                "plugin": s.spec.get("_plugin"),
                "title": s.spec.get("_title") or s.name,
            })
        return out

    # ---------- tool execution ----------
    def call_tool(self, server: str, tool: str, args: dict, timeout: float = 120) -> str:
        srv = self.servers.get(server)
        if not srv or not srv.session:
            return f"ERROR: MCP server '{server}' is not connected."
        timeout = srv.spec.get("tool_timeout", timeout)  # e.g. rendering a video takes longer
        fut = asyncio.run_coroutine_threadsafe(srv.session.call_tool(tool, args or {}), self.loop)
        try:
            return _result_to_text(fut.result(timeout=timeout))
        except Exception as e:  # noqa: BLE001
            fut.cancel()
            return f"ERROR: {type(e).__name__}: {e}"

    def litert_tools(self, disabled_tools: set[str] | None = None, only: set[str] | None = None) -> list[Any]:
        """Builds LiteRT-LM Tool objects for every connected server's tools."""
        import litert_lm

        disabled_tools = disabled_tools or set()
        manager = self
        result = []
        used: set[str] = set()

        class MCPTool(litert_lm.Tool):
            def __init__(self, server: str, tool: Any, fn_name: str) -> None:
                self.server, self.tool, self.fn_name = server, tool, fn_name

            def get_tool_description(self) -> dict:
                schema = json.loads(json.dumps(_get(self.tool, "input_schema", "inputSchema") or {}))
                schema.setdefault("type", "object")
                schema.setdefault("properties", {})
                for k in ("$schema", "additionalProperties", "title"):
                    schema.pop(k, None)
                desc = (self.tool.description or self.tool.name).strip()
                return {
                    "type": "function",
                    "function": {
                        "name": self.fn_name,
                        "description": f"[{self.server}] {desc}"[:1024],
                        "parameters": schema,
                    },
                }

            def destructive(self, args: Any = None) -> bool:
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:  # noqa: BLE001
                        args = {}
                return is_destructive(self.tool, args if isinstance(args, dict) else {})

            def execute(self, param):
                args = param
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                args = dict(args or {})
                if self.tool.name == "browser_snapshot":
                    args.pop("filename", None)  # with a file name the page text goes to a file the model can't read
                problem = _check_enums(_get(self.tool, "input_schema", "inputSchema") or {}, args)
                if problem:  # the model reads this and usually retries with an allowed value
                    return problem
                return manager.call_tool(self.server, self.tool.name, args)

        for s in self.servers.values():
            if s.status != "connected" or (only is not None and s.name not in only):
                continue
            for t in s.tools:
                key = f"{s.name}/{t.name}"
                if key in disabled_tools:
                    continue
                fn = _safe_name(t.name)
                if fn in used:
                    fn = _safe_name(f"{s.name}_{t.name}")
                used.add(fn)
                result.append(MCPTool(s.name, t, fn))
        return result
