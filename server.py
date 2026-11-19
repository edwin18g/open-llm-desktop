"""Local web server that replaces the embedded browser window.

Serves the UI, exposes the Api object as POST /api/<method>, and pushes events to the page
over Server-Sent Events (GET /events). It listens on 127.0.0.1 only, and every API call must
carry the random per-launch token, so other web pages open in the browser can't use it.
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

IDLE_SHUTDOWN_SECONDS = 20  # quit this long after the last window closed
MAX_BODY = 300 * 1024 * 1024  # uploads arrive as data URLs
CHROMIUM_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                     "microsoft-edge", "brave-browser")


class EventHub:
    """Fans events out to every open page (one queue per /events connection)."""

    def __init__(self) -> None:
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self.last_seen = time.time()
        self.ever_connected = False

    def emit(self, kind: str, **data) -> None:
        payload = json.dumps({"type": kind, **data}, default=str)
        with self._lock:
            for q in self._subs:
                q.put(payload)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.add(q)
            self.ever_connected = True
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)
            self.last_seen = time.time()

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._subs)


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, ui_dir: Path, api: Any, hub: EventHub, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.ui_dir = ui_dir.resolve()
        self.api = api
        self.hub = hub
        self.token = secrets.token_urlsafe(24)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/?t={self.token}"


class _Handler(BaseHTTPRequestHandler):
    server: AppServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # keep the terminal quiet
        pass

    def _host_ok(self) -> bool:
        # Rejects DNS-rebinding requests that reach us under another host name.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None,
              cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send(403, b"{}")
        parts = urlsplit(self.path)
        if parts.path == "/events":
            if parse_qs(parts.query).get("t", [""])[0] != self.server.token:
                return self._send(403, b"{}")
            return self._events()
        if parts.path.startswith("/att/"):  # image attachments: loaded by <img>, so the token is in the query
            if parse_qs(parts.query).get("t", [""])[0] != self.server.token:
                return self._send(403, b"{}")
            found = self.server.api._attachment_file(parts.path.removeprefix("/att/"))
            if not found:
                return self._send(404, b"Not found", "text/plain")
            path, mime = found
            # An attachment never changes once stored, so the browser may keep it.
            return self._send(200, path.read_bytes(), mime, cache="private, max-age=31536000, immutable")
        rel = parts.path.lstrip("/") or "index.html"
        path = (self.server.ui_dir / rel).resolve()
        if not path.is_file() or self.server.ui_dir not in path.parents:
            return self._send(404, b"Not found", "text/plain")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        # Read the body before any refusal: left unread, it would be taken as the start of the next request
        # on this connection (a page left open from before a restart kept breaking the new one this way).
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.close_connection = True
            return self._send(413, b'{"error":"bad request size"}')
        body = self.rfile.read(length)
        if not self._host_ok() or self.headers.get("X-Token") != self.server.token:
            self.close_connection = True
            return self._send(403, b'{"error":"forbidden"}')
        name = parts.path.removeprefix("/api/")
        fn = getattr(self.server.api, name, None)
        if not parts.path.startswith("/api/") or name.startswith("_") or not callable(fn):
            return self._send(404, b'{"error":"unknown method"}')
        try:
            args = json.loads(body or b"[]")
            result = fn(*args)
            self._send(200, json.dumps({"result": result}, default=str).encode())
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode())

    def _events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.server.hub.subscribe()
        try:
            self.wfile.write(b": hello\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.hub.unsubscribe(q)
            self.close_connection = True


def _profile_dir(browser: str, data_dir: Path) -> Path:
    # Snap browsers can't write to hidden folders in $HOME; give them one inside ~/snap.
    real = os.path.realpath(browser)
    if real.startswith("/snap/") or "/snap/bin/" in browser:
        name = Path(browser).name.removesuffix("-browser")
        return Path.home() / "snap" / name / "common" / "ek-light-llm-profile"
    return data_dir / "browser-profile"


def open_window(url: str, data_dir: Path) -> None:
    """Opens the UI as an app window in Chrome/Chromium (own profile), else the default browser."""
    choice = os.environ.get("EK_BROWSER")
    browser = shutil.which(choice) if choice else next(
        (p for p in map(shutil.which, CHROMIUM_BROWSERS) if p), None)
    if browser:
        profile = _profile_dir(browser, data_dir)
        profile.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(
                [browser, f"--app={url}", f"--user-data-dir={profile}", "--class=ek-light-llm",
                 "--window-size=1280,820", "--no-first-run", "--no-default-browser-check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
            return
        except OSError:
            traceback.print_exc()
    webbrowser.open(url)


def wait_until_closed(hub: EventHub, first_connect_timeout: float = 120) -> None:
    """Blocks while a page is open; returns once all windows have been closed for a while."""
    start = time.time()
    while True:
        time.sleep(2)
        if hub.clients:
            continue
        if not hub.ever_connected:
            if time.time() - start > first_connect_timeout:
                return
            continue
        if time.time() - hub.last_seen > IDLE_SHUTDOWN_SECONDS:
            return
