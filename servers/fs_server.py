#!/usr/bin/env python3
"""EK Files: a local MCP server for reading, writing, editing and deleting files.

Access is limited to the folders passed with --root. Deletes go to the desktop Trash
(~/.local/share/Trash) so they can be restored, unless --permanent-delete is given.

Run: python fs_server.py --root /path/one --root /path/two
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import functools
import os
import shutil
import sys
import urllib.parse
from pathlib import Path

from mcp.server.mcpserver import MCPServer

ap = argparse.ArgumentParser()
ap.add_argument("--root", action="append", default=[])
ap.add_argument("--permanent-delete", action="store_true")
ap.add_argument("--read-only", action="store_true")
ARGS = ap.parse_args()

ROOTS: list[Path] = []
for r in ARGS.root:
    for part in r.split(","):
        part = part.strip()
        if part:
            p = Path(os.path.expanduser(part)).resolve()
            p.mkdir(parents=True, exist_ok=True)
            ROOTS.append(p)
if not ROOTS:
    ROOTS.append(Path.home().resolve())

MAX_READ_CHARS = 20000
TEXT_SNIFF = 4096

mcp = MCPServer("ek-files", instructions=(
    "File tools limited to these folders: " + ", ".join(map(str, ROOTS)) +
    ". Relative paths are resolved against the first folder."
))


# ---------------- helpers ----------------
def _resolve(path: str, must_exist: bool = False) -> Path:
    raw = (path or ".").strip()
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = ROOTS[0] / p
    p = p.resolve()
    if not any(p == r or r in p.parents for r in ROOTS):
        raise ValueError(f"Access denied: {p} is outside the allowed folders ({', '.join(map(str, ROOTS))}).")
    if must_exist and not p.exists():
        raise FileNotFoundError(f"Not found: {p}")
    return p


def _writable() -> None:
    if ARGS.read_only:
        raise PermissionError("This Files plugin is read-only.")


def _is_text(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            chunk = f.read(TEXT_SNIFF)
        return b"\x00" not in chunk
    except OSError:
        return False


def _size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return str(n)


def _trash(p: Path) -> str:
    """Moves a file/folder to the freedesktop Trash so it can be restored from the file manager."""
    if ARGS.permanent_delete:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
        return f"Permanently deleted {p}"
    trash = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "Trash"
    files, info = trash / "files", trash / "info"
    files.mkdir(parents=True, exist_ok=True)
    info.mkdir(parents=True, exist_ok=True)
    name, i = p.name, 1
    while (files / name).exists() or (info / f"{name}.trashinfo").exists():
        name = f"{p.stem}.{i}{p.suffix}"
        i += 1
    (info / f"{name}.trashinfo").write_text(
        "[Trash Info]\n"
        f"Path={urllib.parse.quote(str(p))}\n"
        f"DeletionDate={_dt.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n"
    )
    shutil.move(str(p), str(files / name))
    return f"Moved {p} to Trash (restore it from the file manager's Trash)"


def tool(fn):
    """Registers a tool whose errors come back as readable text for the model."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {e}"
    return mcp.tool()(wrapper)


# ---------------- read tools ----------------
@tool
def list_allowed_folders() -> str:
    """List the folders these file tools can access."""
    return "\n".join(str(r) for r in ROOTS)


@tool
def list_directory(path: str = ".") -> str:
    """List files and folders in a directory.

    Args:
        path: Folder path (absolute, or relative to the first allowed folder).
    """
    p = _resolve(path, must_exist=True)
    if not p.is_dir():
        raise NotADirectoryError(f"Not a folder: {p}")
    rows = []
    for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        try:
            rows.append(f"[DIR]  {e.name}/" if e.is_dir() else f"[FILE] {e.name}  ({_size(e.stat().st_size)})")
        except OSError:
            rows.append(f"[?]    {e.name}")
        if len(rows) >= 500:
            rows.append("… (truncated at 500 entries)")
            break
    return f"{p}\n" + ("\n".join(rows) if rows else "(empty)")


@tool
def directory_tree(path: str = ".", max_depth: int = 3) -> str:
    """Show a folder's structure as an indented tree.

    Args:
        path: Folder to show.
        max_depth: How many levels deep to go (1-6).
    """
    root = _resolve(path, must_exist=True)
    max_depth = max(1, min(int(max_depth), 6))
    out, count = [str(root)], 0
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}

    def walk(d: Path, depth: int, prefix: str) -> None:
        nonlocal count
        if depth > max_depth or count > 400:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except OSError:
            return
        for e in entries:
            count += 1
            if count > 400:
                out.append(prefix + "…")
                return
            if e.is_dir():
                out.append(f"{prefix}{e.name}/")
                if e.name not in skip:
                    walk(e, depth + 1, prefix + "  ")
            else:
                out.append(f"{prefix}{e.name}")

    walk(root, 1, "  ")
    return "\n".join(out)


@tool
def read_file(path: str, start_line: int = 1, max_lines: int = 400) -> str:
    """Read a text file. Returns numbered lines.

    Args:
        path: File to read.
        start_line: First line to return (1-based).
        max_lines: Maximum number of lines to return.
    """
    p = _resolve(path, must_exist=True)
    if p.is_dir():
        raise IsADirectoryError(f"{p} is a folder; use list_directory")
    if not _is_text(p):
        return f"{p} is a binary file ({_size(p.stat().st_size)}); cannot show its contents."
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, int(start_line))
    chunk = lines[start - 1:start - 1 + max(1, int(max_lines))]
    body = "\n".join(f"{start + i:>5}| {l}" for i, l in enumerate(chunk))
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS] + "\n… (truncated)"
    end = start + len(chunk) - 1
    more = f"\n… {len(lines) - end} more lines (use start_line={end + 1})" if end < len(lines) else ""
    return f"{p} (lines {start}-{end} of {len(lines)})\n{body}{more}"


@tool
def search_files(pattern: str, path: str = ".") -> str:
    """Find files by name pattern, searching subfolders.

    Args:
        pattern: Name or glob pattern, e.g. "*.pdf" or "invoice".
        path: Folder to search in.
    """
    root = _resolve(path, must_exist=True)
    pat = pattern if any(c in pattern for c in "*?[") else f"*{pattern}*"
    hits = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in {".git", "node_modules", "__pycache__", ".venv"}]
        for n in dns + fns:
            if fnmatch.fnmatch(n.lower(), pat.lower()):
                hits.append(os.path.join(dp, n))
                if len(hits) >= 200:
                    return "\n".join(hits) + "\n… (first 200 matches)"
    return "\n".join(hits) or "No matches."


@tool
def search_text(query: str, path: str = ".", file_pattern: str = "*") -> str:
    """Search inside text files for a word or phrase (case-insensitive).

    Args:
        query: Text to look for.
        path: Folder (or single file) to search.
        file_pattern: Only search files matching this glob, e.g. "*.py".
    """
    root = _resolve(path, must_exist=True)
    q = query.lower()
    files = [root] if root.is_file() else (
        Path(dp) / f for dp, dns, fns in os.walk(root)
        if not any(s in Path(dp).parts for s in (".git", "node_modules", "__pycache__", ".venv"))
        for f in fns if fnmatch.fnmatch(f, file_pattern)
    )
    hits = []
    for f in files:
        try:
            if f.stat().st_size > 2_000_000 or not _is_text(f):
                continue
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if q in line.lower():
                    hits.append(f"{f}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 100:
                        return "\n".join(hits) + "\n… (first 100 matches)"
        except OSError:
            continue
    return "\n".join(hits) or "No matches."


@tool
def file_info(path: str) -> str:
    """Get size, type and modified time of a file or folder.

    Args:
        path: File or folder.
    """
    p = _resolve(path, must_exist=True)
    st = p.stat()
    kind = "folder" if p.is_dir() else ("text file" if _is_text(p) else "binary file")
    mod = _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    return f"{p}\ntype: {kind}\nsize: {_size(st.st_size)}\nmodified: {mod}"


# ---------------- write tools ----------------
@tool
def write_file(path: str, content: str) -> str:
    """Create a new file or completely replace an existing file's content.

    Args:
        path: File to write. Missing parent folders are created.
        content: The full text to write.
    """
    _writable()
    p = _resolve(path)
    if p.is_dir():
        raise IsADirectoryError(f"{p} is a folder")
    existed = p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"{'Overwrote' if existed else 'Created'} {p} ({len(content)} chars)"


@tool
def append_to_file(path: str, content: str) -> str:
    """Add text to the end of a file (creates it if missing).

    Args:
        path: File to append to.
        content: Text to add.
    """
    _writable()
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(content)
    return f"Appended {len(content)} chars to {p}"


@tool
def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    """Modify a file by replacing exact text. Read the file first to copy old_text exactly.

    Args:
        path: File to modify.
        old_text: Exact text currently in the file.
        new_text: Text to put in its place.
        replace_all: Replace every occurrence instead of requiring exactly one.
    """
    _writable()
    p = _resolve(path, must_exist=True)
    text = p.read_text(encoding="utf-8")
    n = text.count(old_text)
    if not old_text or n == 0:
        raise ValueError("old_text was not found in the file. Use read_file and copy the text exactly.")
    if n > 1 and not replace_all:
        raise ValueError(f"old_text appears {n} times. Add more surrounding text, or set replace_all=true.")
    p.write_text(text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"Edited {p}: replaced {n if replace_all else 1} occurrence(s)"


@tool
def create_folder(path: str) -> str:
    """Create a folder (and any missing parent folders).

    Args:
        path: Folder to create.
    """
    _writable()
    p = _resolve(path)
    p.mkdir(parents=True, exist_ok=True)
    return f"Folder ready: {p}"


@tool
def move_file(source: str, destination: str) -> str:
    """Move or rename a file or folder. Will not overwrite an existing destination.

    Args:
        source: Current path.
        destination: New path (or an existing folder to move into).
    """
    _writable()
    src = _resolve(source, must_exist=True)
    dst = _resolve(destination)
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise FileExistsError(f"{dst} already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {src} → {dst}"


@tool
def copy_file(source: str, destination: str) -> str:
    """Copy a file or folder. Will not overwrite an existing destination.

    Args:
        source: Path to copy.
        destination: New path (or an existing folder to copy into).
    """
    _writable()
    src = _resolve(source, must_exist=True)
    dst = _resolve(destination)
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise FileExistsError(f"{dst} already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return f"Copied {src} → {dst}"


@tool
def delete_file(path: str) -> str:
    """Delete a file (moved to Trash, restorable).

    Args:
        path: File to delete.
    """
    _writable()
    p = _resolve(path, must_exist=True)
    if p.is_dir() and not p.is_symlink():
        raise IsADirectoryError(f"{p} is a folder; use delete_folder")
    return _trash(p)


@tool
def delete_folder(path: str) -> str:
    """Delete a folder and everything inside it (moved to Trash, restorable).

    Args:
        path: Folder to delete.
    """
    _writable()
    p = _resolve(path, must_exist=True)
    if not p.is_dir():
        raise NotADirectoryError(f"{p} is not a folder; use delete_file")
    if p in ROOTS:
        raise PermissionError("Refusing to delete an allowed root folder itself.")
    return _trash(p)


if __name__ == "__main__":
    mcp.run()
