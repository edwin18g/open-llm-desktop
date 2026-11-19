"""Turns uploaded files into attachments: copies them into app storage and extracts text."""

from __future__ import annotations

import base64
import csv
import io
import json
import mimetypes
import re
import uuid
from pathlib import Path

from store import ATTACH_DIR, Store

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AUDIO_EXT = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
MAX_FILE_BYTES = 30 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".env", ".html", ".htm", ".css", ".scss", ".js", ".mjs", ".ts",
    ".tsx", ".jsx", ".vue", ".svelte", ".py", ".php", ".rb", ".go", ".rs", ".java", ".kt", ".swift", ".c",
    ".h", ".cpp", ".hpp", ".cs", ".sql", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".dart", ".lua", ".r",
    ".svg", ".tex", ".srt", ".vtt",
}


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120] or "file"


def _looks_text(data: bytes) -> bool:
    head = data[:4096]
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def extract_text(name: str, data: bytes) -> tuple[str, str]:
    """Returns (kind, text). kind: image | audio | pdf | docx | sheet | text | binary."""
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT:
        return "image", ""
    if ext in AUDIO_EXT:
        return "audio", ""
    if ext == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            pages = []
            for i, p in enumerate(reader.pages, 1):
                t = (p.extract_text() or "").strip()
                if t:
                    pages.append(f"[Page {i}]\n{t}")
            text = "\n\n".join(pages)
            return "pdf", text or "(No extractable text — this PDF may be scanned images.)"
        except Exception as e:  # noqa: BLE001
            return "pdf", f"(Could not read PDF: {e})"
    if ext == ".docx":
        try:
            return "docx", _docx_text(data)
        except Exception as e:  # noqa: BLE001
            return "docx", f"(Could not read DOCX: {e})"
    if ext in (".xlsx", ".xlsm"):
        try:
            return "sheet", _xlsx_text(data)
        except Exception as e:  # noqa: BLE001
            return "sheet", f"(Could not read spreadsheet: {e})"
    if ext in TEXT_EXT or _looks_text(data):
        text = data.decode("utf-8", errors="replace")
        if ext == ".json":
            try:
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except Exception:
                pass
        return "text", text
    return "binary", ""


# DOCX and XLSX are zipped XML; reading them with the standard library saves ~18 MB of packages.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _docx_text(data: bytes) -> str:
    import zipfile
    from xml.etree import ElementTree as ET

    body = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml")).find(f"{_W}body")
    para = lambda p: "".join(t.text or "" for t in p.iter(f"{_W}t")).strip()  # noqa: E731
    parts = []
    for el in body:
        if el.tag == f"{_W}p" and para(el):
            parts.append(para(el))
        elif el.tag == f"{_W}tbl":
            for tr in el.iter(f"{_W}tr"):
                parts.append(" | ".join(" ".join(para(p) for p in tc.iter(f"{_W}p")).strip()
                                        for tc in tr.iter(f"{_W}tc")))
    return "\n".join(parts)


def _xlsx_text(data: bytes, max_rows: int = 2000) -> str:
    import posixpath
    import zipfile
    from xml.etree import ElementTree as ET

    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    shared = []
    if "xl/sharedStrings.xml" in names:
        shared = ["".join(t.text or "" for t in si.iter(f"{_S}t"))
                  for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{_S}si")]
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = []
    for sh in ET.fromstring(z.read("xl/workbook.xml")).iter(f"{_S}sheet"):
        target = rels.get(sh.get(f"{_R}id"), "")
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        if path not in names:
            continue
        out.append(f"[Sheet: {sh.get('name')}]")
        for i, row in enumerate(ET.fromstring(z.read(path)).iter(f"{_S}row")):
            if i >= max_rows:
                out.append("… (rows truncated)")
                break
            cells: dict[int, str] = {}
            for c in row.iter(f"{_S}c"):
                col = 0
                for ch in re.match(r"[A-Z]*", c.get("r", "")).group():
                    col = col * 26 + ord(ch) - 64
                t, v = c.get("t"), c.find(f"{_S}v")
                if t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{_S}t"))
                elif v is None:
                    continue
                elif t == "s":
                    val = shared[int(v.text)]
                elif t == "b":
                    val = "TRUE" if v.text == "1" else "FALSE"
                else:
                    val = v.text or ""
                cells[(col or len(cells) + 1) - 1] = val
            if cells:
                out.append(",".join(cells.get(k, "") for k in range(max(cells) + 1)))
    return "\n".join(out)


def _prepare_image(data: bytes, ext: str) -> tuple[bytes, str]:
    """Normalises images to PNG/JPEG and caps the long side at 1536px (less memory, faster).
    Pillow is optional: the page already shrinks images before uploading them."""
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(data))
        im.load()
        changed = False
        if max(im.size) > 1536:
            im.thumbnail((1536, 1536))
            changed = True
        if ext not in (".png", ".jpg", ".jpeg") or im.mode not in ("RGB", "L", "RGBA"):
            changed = True
        if not changed:
            return data, ext
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        return buf.getvalue(), ".jpg"
    except Exception:
        return data, ext


def ingest_bytes(store: Store, name: str, data: bytes) -> dict:
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"{name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    kind, text = extract_text(name, data)
    ext = Path(name).suffix.lower()
    if kind == "image":
        data, ext = _prepare_image(data, ext)
    if kind == "binary":
        raise ValueError(f"{name}: unsupported file type (try text, code, PDF, DOCX, XLSX, CSV or an image)")
    text = text[:MAX_TEXT_CHARS]
    dest = ATTACH_DIR / f"{uuid.uuid4().hex[:10]}_{_safe(Path(name).stem)}{ext}"
    dest.write_bytes(data)
    mime = mimetypes.guess_type(dest.name)[0] or "application/octet-stream"
    return store.add_attachment(name, kind, mime, len(data), str(dest), text)


def ingest_path(store: Store, path: str) -> dict:
    p = Path(path)
    return ingest_bytes(store, p.name, p.read_bytes())


def ingest_b64(store: Store, name: str, b64: str) -> dict:
    if "," in b64[:100] and b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    return ingest_bytes(store, name, base64.b64decode(b64))


def thumbnail_data_url(path: str, size: int = 320) -> str:
    try:
        from PIL import Image

        im = Image.open(path)
        im.thumbnail((size, size))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        try:
            mime = mimetypes.guess_type(path)[0] or "image/png"
            return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode()
        except Exception:
            return ""


def csv_preview(text: str, rows: int = 5) -> str:
    try:
        r = list(csv.reader(io.StringIO(text)))[:rows]
        return "\n".join(",".join(x) for x in r)
    except Exception:
        return text[:500]
