#!/usr/bin/env python3
"""EK Web: a local MCP server for searching the web and reading pages. No API key or extra packages.

A search asks several free sources at once (Bing's results page, with DuckDuckGo if that fails, plus
Wikipedia's API and Google News RSS), drops results that don't match the question and reads the best
pages, because a small model rarely opens pages by itself. These aren't paid search APIs, so a source
can be blocked or change; the others still answer, and the tool says so plainly if none do.
"""

from __future__ import annotations

import base64
import concurrent.futures as cf
import email.utils
import html.parser
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from mcp.server.mcpserver import MCPServer
from safe_fetch import fetch_public

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT = 15
MAX_DOWNLOAD = 3_000_000
MAX_PAGE_CHARS = 8000
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "template", "iframe", "head"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
READ_PAGES = 3         # pages opened per search
READ_WAIT = 8          # seconds to wait for them
EXCERPT_CHARS = 700
# Words that say nothing about the topic. Bing weighs the first word most, so these must not lead the query.
FILLER = {"a", "about", "am", "an", "and", "any", "are", "at", "be", "best", "by", "can", "current", "did", "do", "does",
          "find", "for", "from", "get", "give", "has", "have", "how", "i", "in", "internet", "is", "it", "latest", "list",
          "look", "me", "most", "my", "new", "news", "newest", "now", "of", "on", "online", "or", "please", "recent",
          "search", "show", "tell", "that", "the", "their", "there", "this", "to", "today", "top", "up", "us", "was",
          "web", "were", "what", "when", "where", "which", "who", "whom", "why", "will", "with", "won", "you", "your"}
FRESH_WORDS = {"news", "today", "update", "updates", "yesterday"}
BLOCK_TAGS = {"p", "div", "li", "tr", "br", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}

mcp = MCPServer("EK Web")


# ---------- fetching ----------
def _fetch(url: str, max_bytes: int = MAX_DOWNLOAD) -> tuple[str, str, str]:
    """Returns (final url, content type, text)."""
    final, ctype, charset, raw = fetch_public(url, max_bytes, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=TIMEOUT)
    return final, ctype, raw.decode(charset or "utf-8", "replace")


# ---------- search ----------
class _Results(html.parser.HTMLParser):
    """Collects title, link and snippet from a results page. Subclasses say what marks each part."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self.cur: dict | None = None
        self.field: str | None = None
        self.depth = 0
        self.skip = 0

    def begin_field(self, field: str, tag: str) -> None:
        self.field, self.field_tag, self.depth = field, tag, 1

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.skip += 1
            return
        if self.field and tag == self.field_tag and tag not in VOID_TAGS:
            self.depth += 1
        elif not self.field:
            self.on_start(tag, dict(attrs))

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip = max(0, self.skip - 1)
        elif self.field and tag == self.field_tag:
            self.depth -= 1
            if not self.depth:
                self.field = None

    def handle_data(self, data):
        if self.field and self.cur is not None and not self.skip:
            self.cur[self.field] = self.cur.get(self.field, "") + data

    def on_start(self, tag: str, a: dict) -> None:
        raise NotImplementedError


class _Bing(_Results):
    def on_start(self, tag, a):
        cls = (a.get("class") or "").split()
        if tag == "li" and "b_algo" in cls:
            self.cur = {}
            self.results.append(self.cur)
        elif self.cur is None:
            return
        elif tag == "a" and "url" not in self.cur and a.get("href") and self.cur.get("_h2"):
            self.cur["url"] = a["href"]
            self.begin_field("title", "a")
        elif tag == "h2":
            self.cur["_h2"] = True
        elif tag == "p" and "snippet" not in self.cur and "url" in self.cur:
            self.begin_field("snippet", "p")


class _Ddg(_Results):
    def on_start(self, tag, a):
        cls = (a.get("class") or "").split()
        if tag == "a" and "result__a" in cls:
            self.cur = {"url": a.get("href", "")}
            self.results.append(self.cur)
            self.begin_field("title", "a")
        elif tag == "a" and "result__snippet" in cls and self.cur is not None:
            self.begin_field("snippet", "a")


def _real_url(url: str) -> str:
    """Search engines link through their own tracking redirect; this gets the real address."""
    u = urllib.parse.urlsplit(urllib.parse.urljoin("https://duckduckgo.com/", url))
    q = urllib.parse.parse_qs(u.query)
    if u.hostname and u.hostname.endswith("bing.com") and u.path.startswith("/ck/") and q.get("u"):
        b64 = q["u"][0].removeprefix("a1")
        try:
            return base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()
        except ValueError:
            return ""
    if u.hostname and u.hostname.endswith("duckduckgo.com") and q.get("uddg"):
        return q["uddg"][0]
    return urllib.parse.urlunsplit(u)


def _clean_results(raw: list[dict]) -> list[dict]:
    out, seen = [], set()
    for r in raw:
        url, title = _real_url(r.get("url", "")), " ".join(r.get("title", "").split())
        if url.startswith("http") and title and url not in seen:
            seen.add(url)
            out.append({"title": title, "url": url, "snippet": " ".join(r.get("snippet", "").split())})
    return out


# ---------- reading pages ----------
class _Text(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_tag: str | None = None
        self.skip_depth = 0
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if self.skip_tag:
            self.skip_depth += tag == self.skip_tag and tag not in VOID_TAGS
            return
        if tag == "title":
            self.in_title = True
        elif tag in SKIP_TAGS and tag != "head":
            self.skip_tag, self.skip_depth = tag, 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if not self.skip_depth:
                    self.skip_tag = None
            return
        if tag == "title":
            self.in_title = False
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip_tag:
            self.parts.append(data)


def _page_text(body: str) -> tuple[str, str]:
    p = _Text()
    p.feed(body)
    lines = [" ".join(line.split()) for line in "".join(p.parts).split("\n")]
    lines = [line for line in lines if line]
    # Menus come first and are short lines; start at the first real sentence.
    start = next((i for i, line in enumerate(lines) if len(line) >= 60), 0)
    return " ".join(p.title.split()), "\n".join(lines[start:])


@mcp.tool()
def read_page(url: str, max_chars: int = MAX_PAGE_CHARS) -> str:
    """Open a web page from a search result or a link the user gave and return its text."""
    try:
        final, ctype, body = _fetch(url)
    except urllib.error.HTTPError as e:
        return f"Couldn't open {url}: the site answered HTTP {e.code}."
    except Exception as e:  # noqa: BLE001
        return f"Couldn't open {url}: {e}"
    if ctype in ("text/html", "application/xhtml+xml"):
        title, text = _page_text(body)
    elif ctype.startswith("text/") or ctype.endswith(("json", "xml")):
        title, text = "", body
    else:
        return f"Couldn't read {url}: it is a {ctype} file, not a web page."
    if not text.strip():
        return f"{final} has no readable text (it may need JavaScript or a sign-in)."
    limit = max(500, min(max_chars, MAX_PAGE_CHARS))
    note = f"\n[Only the first {limit} of {len(text)} characters are shown.]" if len(text) > limit else ""
    return f"{title}\n{final}\n\n{text[:limit]}{note}".strip()


# ---------- search ----------
def _words(text: str) -> set[str]:
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in re.findall(r"[^\W_]+", text.lower())}


def _key_words(query: str) -> list[str]:
    seen, out = set(), []
    for w in re.findall(r"[^\W_]+", query.lower()):
        if w not in FILLER and w not in seen:
            seen.add(w)
            out.append(w)
    return out or re.findall(r"[^\W_]+", query.lower())


def _from_page(url: str, parser: _Results) -> list[dict]:
    parser.feed(_fetch(url)[2])
    return _clean_results(parser.results)


def _bing(keys: list[str]) -> list[dict]:
    # The first word counts most and which one is the real topic is unknown, so try both ends first.
    orders = [keys] if len(keys) < 2 else [keys, keys[::-1]]
    out = []
    for order in orders:
        q = urllib.parse.quote_plus(" ".join(order))
        out += _from_page(f"https://www.bing.com/search?q={q}&setlang=en", _Bing())
    return out


def _duckduckgo(keys: list[str]) -> list[dict]:
    q = urllib.parse.quote_plus(" ".join(keys))
    return _from_page(f"https://html.duckduckgo.com/html/?q={q}", _Ddg())


def _wikipedia(keys: list[str]) -> list[dict]:
    q = urllib.parse.quote_plus(" ".join(keys))
    data = json.loads(_fetch(f"https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=3&srsearch={q}")[2])
    out = []
    for r in data["query"]["search"]:
        p = _Text()
        p.feed(r["snippet"])
        out.append({"title": r["title"], "snippet": " ".join("".join(p.parts).split()),
                    "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(r["title"].replace(" ", "_"))})
    return out


def _google_news(keys: list[str]) -> list[dict]:
    q = urllib.parse.quote_plus(" ".join(keys))
    root = ET.fromstring(_fetch(f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en")[2])
    out = []
    for item in root.iterfind("./channel/item"):
        try:
            when = email.utils.parsedate_to_datetime(item.findtext("pubDate", "")).strftime("%d %b %Y")
        except (TypeError, ValueError):
            when = ""
        out.append({"title": item.findtext("title", ""), "url": item.findtext("link", ""), "snippet": "",
                    "date": when, "news": True})
    return out[:6]


def _search(query: str) -> tuple[list[dict], list[str]]:
    """Returns the matching results, best first, and any sources that failed."""
    keys = _key_words(query)
    wanted = _words(" ".join(keys))
    fresh = bool(FRESH_WORDS & set(re.findall(r"[a-z]+", query.lower())))
    sources = {"Bing": _bing, "Wikipedia": _wikipedia, "Google News": _google_news}
    found: dict[str, list[dict]] = {}
    problems = []
    with cf.ThreadPoolExecutor(len(sources)) as pool:
        futures = {name: pool.submit(fn, keys) for name, fn in sources.items()}
        for name, fut in futures.items():
            try:
                found[name] = fut.result()
            except Exception as e:  # noqa: BLE001
                found[name] = []
                problems.append(f"{name}: {e}")
    if not found["Bing"]:
        try:
            found["Bing"] = _duckduckgo(keys)
        except Exception as e:  # noqa: BLE001
            problems.append(f"DuckDuckGo: {e}")
    # A result must mention most of the question's key words, which removes dictionary pages for "best".
    need = max(1, math.ceil(len(wanted) * 0.6))
    scored, seen = [], set()
    for name, results in found.items():
        for pos, r in enumerate(results):
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            title, body = _words(r["title"]), _words(r.get("snippet", ""))
            hits = len(wanted & (title | body))
            if hits >= need:
                scored.append((-(hits + len(wanted & title) * 0.5), pos, r))
    scored.sort(key=lambda x: x[:2])
    ranked = [r for *_, r in scored]
    # Keep both kinds: pages have the facts, news has the dates. Which leads depends on the question.
    pages, news = [r for r in ranked if not r.get("news")], [r for r in ranked if r.get("news")]
    if fresh:
        return news[:3] + pages + news[3:], problems
    return pages[:3] + news + pages[3:], problems


def _excerpt(url: str, wanted: set[str]) -> str:
    """The sentences of a page that mention the question's key words, in page order."""
    _, ctype, body = _fetch(url)
    if ctype not in ("text/html", "application/xhtml+xml"):
        return ""
    _, text = _page_text(body)
    sentences = re.split(r"(?<=[.!?])\s+|\n", text)
    ranked = sorted(range(len(sentences)), key=lambda i: (-len(wanted & _words(sentences[i])), i))
    keep, used = [], 0
    for i in ranked:
        n = len(sentences[i])
        if not wanted & _words(sentences[i]):
            break
        if 30 <= n <= 400 and used + n <= EXCERPT_CHARS:
            keep.append(i)
            used += n
    return " ".join(sentences[i] for i in sorted(keep))


def _read_top(results: list[dict], wanted: set[str]) -> None:
    """Fills in page excerpts for the first few results. A slow site is skipped, not waited for."""
    readable = [r for r in results if not r.get("news")][:READ_PAGES]
    pool = cf.ThreadPoolExecutor(max(1, len(readable)))
    futures = {pool.submit(_excerpt, r["url"], wanted): r for r in readable}
    done, _ = cf.wait(futures, timeout=READ_WAIT)
    for fut in done:
        if not fut.exception() and fut.result():
            futures[fut]["excerpt"] = fut.result()
    pool.shutdown(wait=False, cancel_futures=True)


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """Search the public internet for news, current events, prices, people, companies, products or facts you may not know. Returns matching results with the relevant text from the best pages. Not for the user's own company records."""
    results, problems = _search(query)
    if not results:
        why = f" Sources that failed: {'; '.join(problems)}." if problems else ""
        return f"No web results matched: {query}.{why}"
    results = results[:max(1, min(max_results, 10))]
    _read_top(results, _words(" ".join(_key_words(query))))
    lines = [f"Web results for: {query}"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}" + (f" ({r['date']})" if r.get("date") else ""))
        if not r.get("news"):  # news links are long opaque redirects: costly for the model, and it can't open them
            lines.append(f"   {r['url']}")
        text = r.get("excerpt") or r.get("snippet")
        if text:
            lines.append(f"   {text}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
