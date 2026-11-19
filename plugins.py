"""Plugin catalog: curated MCP servers that can be installed with one click."""

from __future__ import annotations

import re
from pathlib import Path

HOME = str(Path.home())

# Each field: key, label, type (path|text|secret|url), placeholder/default, optional
CATALOG = [
    {
        "id": "filesystem", "title": "Files", "color": "#3b82f6", "category": "Popular",
        "description": "Read, write, edit, move and delete files in folders you choose",
        "fields": [
            {"key": "path", "label": "Folders to allow (comma-separated)", "type": "path", "default": f"{HOME}/Documents"},
            {"key": "mode", "label": "Access", "type": "select", "default": "full",
             "options": [["full", "Read & write (deletes go to Trash)"], ["readonly", "Read only"], ["permanent", "Read & write, delete permanently"]]},
        ],
        "spec": {"command": "{app_python}", "args": ["{app_dir}/servers/fs_server.py", "--root", "{path}"]},
        "needs": "nothing extra (built in)",
    },
    {
        "id": "fetch", "title": "Web Fetch", "color": "#0ea5e9", "category": "Popular",
        "description": "Open a URL and read the page as text",
        "fields": [],
        "spec": {"command": "uvx", "args": ["mcp-server-fetch"]},
        "needs": "uv (uvx)",
    },
    {
        "id": "web-search", "title": "Web Search", "color": "#f97316", "category": "Popular",
        "description": "Search the internet and read pages, no API key needed. Your searches go to Bing or DuckDuckGo",
        "fields": [],
        "spec": {"command": "{app_python}", "args": ["{app_dir}/servers/web_server.py"]},
        "needs": "nothing extra (built in)",
    },
    {
        "id": "video", "title": "Video Maker", "color": "#db2777", "category": "Popular",
        "description": "Make MP4 videos from JSON: scenes with text, images, clips, music and spoken narration",
        "fields": [],
        "spec": {"command": "{app_python}", "args": ["{app_dir}/servers/video_server.py"], "tool_timeout": 1800},
        "needs": "ffmpeg",
    },
    {
        "id": "brave-search", "title": "Brave Search", "color": "#fb542b", "category": "Popular",
        "description": "Search the web with the official Brave Search API (more dependable, needs a key)",
        "fields": [{"key": "api_key", "label": "Brave API key", "type": "secret"}],
        "spec": {"command": "npx", "args": ["-y", "@brave/brave-search-mcp-server"], "env": {"BRAVE_API_KEY": "{api_key}"}},
        "needs": "Node.js (npx)",
    },
    {
        "id": "github", "title": "GitHub", "color": "#6e7681", "category": "Popular",
        "description": "Issues, pull requests, code and repos (GitHub's hosted server)",
        "fields": [{"key": "token", "label": "Personal access token", "type": "secret"}],
        "spec": {"url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer {token}"}},
        "needs": "Internet",
    },
    {
        "id": "time", "title": "Time", "color": "#14b8a6", "category": "Popular",
        "description": "Current time and timezone conversion",
        "fields": [{"key": "tz", "label": "Local timezone", "type": "text", "default": "Asia/Kolkata"}],
        "spec": {"command": "uvx", "args": ["mcp-server-time", "--local-timezone={tz}"]},
        "needs": "uv (uvx)",
    },
    {
        "id": "mysql", "title": "MySQL", "color": "#00758f", "category": "Data",
        "description": "Query a MySQL database (read-only by default)",
        "fields": [
            {"key": "host", "label": "Host", "type": "text", "default": "127.0.0.1"},
            {"key": "port", "label": "Port", "type": "text", "default": "3306"},
            {"key": "user", "label": "User", "type": "text", "default": "root"},
            {"key": "password", "label": "Password", "type": "secret", "optional": True},
            {"key": "db", "label": "Database", "type": "text"},
        ],
        "spec": {"command": "npx", "args": ["-y", "@benborla29/mcp-server-mysql"],
                 "env": {"MYSQL_HOST": "{host}", "MYSQL_PORT": "{port}", "MYSQL_USER": "{user}",
                         "MYSQL_PASS": "{password}", "MYSQL_DB": "{db}"}},
        "needs": "Node.js (npx)",
    },
    {
        "id": "sqlite", "title": "SQLite", "color": "#2563eb", "category": "Data",
        "description": "Query and analyse a local SQLite database file",
        "fields": [{"key": "db", "label": "Database file", "type": "path", "default": f"{HOME}/data.db"}],
        "spec": {"command": "uvx", "args": ["mcp-server-sqlite", "--db-path", "{db}"]},
        "needs": "uv (uvx)",
    },
    {
        "id": "git", "title": "Git", "color": "#e5533d", "category": "Developer",
        "description": "Status, diffs, logs and commits for a local repository",
        "fields": [{"key": "repo", "label": "Repository folder", "type": "path", "default": "/var/www/html"}],
        "spec": {"command": "uvx", "args": ["mcp-server-git", "--repository", "{repo}"]},
        "needs": "uv (uvx)",
    },
    {
        "id": "playwright", "title": "Browser", "color": "#16a34a", "category": "Developer",
        "description": "Drive a real browser: open pages, click, fill forms",
        "fields": [],
        "spec": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
        "needs": "Node.js (npx)",
    },
    {
        "id": "notion", "title": "Notion", "color": "#5b5b5b", "category": "Productivity",
        "description": "Search and edit pages and databases in Notion",
        "fields": [{"key": "token", "label": "Notion integration token", "type": "secret"}],
        "spec": {"command": "npx", "args": ["-y", "@notionhq/notion-mcp-server"], "env": {"NOTION_TOKEN": "{token}"}},
        "needs": "Node.js (npx)",
    },
    {
        "id": "memory", "title": "Memory", "color": "#a855f7", "category": "Productivity",
        "description": "A local knowledge graph the model can remember facts in",
        "fields": [],
        "spec": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
        "needs": "Node.js (npx)",
    },
    {
        "id": "thinking", "title": "Step-by-step", "color": "#64748b", "category": "Productivity",
        "description": "Structured step-by-step reasoning tool",
        "fields": [],
        "spec": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"]},
        "needs": "Node.js (npx)",
    },
]

BY_ID = {c["id"]: c for c in CATALOG}


def _fill(value, inputs: dict):
    if isinstance(value, str):
        return re.sub(r"\{(\w+)\}", lambda m: str(inputs[m.group(1)]) if m.group(1) in inputs else m.group(0), value)
    if isinstance(value, list):
        return [_fill(v, inputs) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, inputs) for k, v in value.items()}
    return value


def build_spec(catalog_id: str, inputs: dict) -> tuple[str, dict]:
    """Returns (server_name, mcp spec) for a catalog plugin."""
    item = BY_ID[catalog_id]
    inputs = dict(inputs or {})
    for f in item["fields"]:
        v = str(inputs.get(f["key"], "") or f.get("default", "")).strip()
        if f["type"] == "path" and v.startswith("~"):
            v = HOME + v[1:]
        if not v and not f.get("optional"):
            raise ValueError(f"{f['label']} is required")
        inputs[f["key"]] = v
    spec = _fill(item["spec"], inputs)
    if catalog_id == "filesystem":
        spec["args"] += {"readonly": ["--read-only"], "permanent": ["--permanent-delete"]}.get(inputs.get("mode"), [])
    spec["_plugin"] = catalog_id
    spec["_title"] = item["title"]
    return catalog_id, spec


def public_catalog() -> list[dict]:
    return [{k: v for k, v in c.items() if k != "spec"} for c in CATALOG]
