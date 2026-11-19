"""Inline browser: a hidden Chrome that the side panel shows live and the Browser plugin drives.

The panel talks to Chrome directly over the DevTools protocol (a screencast of the page, plus your
clicks and keys). Only the app's own page is allowed to connect from a browser (--remote-allow-origins),
so websites can't reach it. Like any debugging port it listens on 127.0.0.1, where other programs
running as you could also connect.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from server import CHROMIUM_BROWSERS

START_TIMEOUT = 20


class InlineBrowser:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.origin = ""  # the app page's origin; set once the HTTP server is up
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self._lock = threading.Lock()

    # ---------- process ----------
    @staticmethod
    def _binary() -> str | None:
        choice = os.environ.get("EK_BROWSER")
        if choice:
            return shutil.which(choice)
        return next((p for p in map(shutil.which, CHROMIUM_BROWSERS) if p), None)

    def _profile(self, browser: str) -> Path:
        real = os.path.realpath(browser)
        if real.startswith("/snap/") or "/snap/bin/" in browser:  # snaps can't use hidden folders in $HOME
            return Path.home() / "snap" / Path(browser).name.removesuffix("-browser") / "common" / "ek-light-llm-inline"
        return self.data_dir / "inline-browser"

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self.port is not None

    def ensure(self) -> int:
        """Starts Chrome if needed and returns its DevTools port."""
        with self._lock:
            if self.running():
                return self.port
            if not self.origin:
                raise RuntimeError("The app isn't ready yet.")
            browser = self._binary()
            if not browser:
                raise RuntimeError("The inline browser needs Google Chrome or Chromium installed.")
            profile = self._profile(browser)
            profile.mkdir(parents=True, exist_ok=True)
            self._kill_leftover(profile)
            port_file = profile / "DevToolsActivePort"
            port_file.unlink(missing_ok=True)
            self.proc = subprocess.Popen(
                [browser, "--headless=new", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                 f"--remote-allow-origins={self.origin}", f"--user-data-dir={profile}", "--no-first-run",
                 "--no-default-browser-check", "--window-size=1280,800", "--disable-features=Translate,MediaRouter",
                 "about:blank"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            (profile / "ek-light-llm.pid").write_text(str(self.proc.pid))
            deadline = time.time() + START_TIMEOUT
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError("Chrome closed right after starting.")
                try:
                    self.port = int(port_file.read_text().split()[0])
                    return self.port
                except (OSError, ValueError, IndexError):
                    time.sleep(0.1)
            self.stop()
            raise RuntimeError("Chrome didn't start in time.")

    @staticmethod
    def _kill_leftover(profile: Path) -> None:
        """A Chrome left from an app that crashed would keep the profile locked."""
        pid_file = profile / "ek-light-llm.pid"
        try:
            pid = int(pid_file.read_text())
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        except (OSError, ValueError):
            return
        if str(profile) in cmdline:
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)
            except OSError:
                pass

    def stop(self) -> None:
        proc, self.proc, self.port = self.proc, None, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ---------- pages ----------
    def _http(self, path: str, method: str = "GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"null")

    def pages(self) -> list[dict]:
        """Open tabs, newest first, with the DevTools address the panel connects to."""
        self.ensure()
        items = self._http("/json/list")
        pages = [{"id": p["id"], "title": p.get("title") or "", "url": p.get("url") or "", "ws": p.get("webSocketDebuggerUrl", "")}
                 for p in items if p.get("type") == "page"]
        if not pages:
            pages = [self.new_page("about:blank")]
        return pages

    def new_page(self, url: str = "about:blank") -> dict:
        self.ensure()
        p = self._http("/json/new?" + urllib.parse.quote(url, safe=":/?&=%#"), method="PUT")
        return {"id": p["id"], "title": p.get("title") or "", "url": p.get("url") or "", "ws": p.get("webSocketDebuggerUrl", "")}

    def close_page(self, page_id: str) -> None:
        if self.running():
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/close/{urllib.parse.quote(page_id)}", timeout=5).read()
            except OSError:
                pass
