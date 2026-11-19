"""Downloads from public web addresses only.

A page or JSON file the model reads could point it at an internal address, so loopback and private
hosts are refused, for every redirect target too.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

MAX_REDIRECTS = 5


def check_public(url: str) -> None:
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Only http and https web addresses can be opened.")
    for info in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80)):
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise ValueError("Local and private network addresses can't be opened.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def fetch_public(url: str, max_bytes: int, headers: dict | None = None, timeout: float = 15) -> tuple[str, str, str | None, bytes]:
    """Returns (final url, content type, charset, up to max_bytes of the body)."""
    for _ in range(MAX_REDIRECTS + 1):
        check_public(url)
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with _opener.open(req, timeout=timeout) as r:
                return r.geturl(), r.headers.get_content_type(), r.headers.get_content_charset(), r.read(max_bytes)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise
    raise ValueError("Too many redirects.")
