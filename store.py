"""SQLite storage for chats, messages, projects and attachments."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "ek-light-llm"
ATTACH_DIR = DATA_DIR / "attachments"

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
  instructions TEXT DEFAULT '', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS project_files(
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT, kind TEXT, size INTEGER,
  text TEXT, created REAL);
CREATE TABLE IF NOT EXISTS chats(
  id TEXT PRIMARY KEY, project_id TEXT, title TEXT, starred INTEGER DEFAULT 0,
  plugins TEXT DEFAULT '[]', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, seq INTEGER, role TEXT, text TEXT,
  attachments TEXT DEFAULT '[]', thinking TEXT DEFAULT '', tools TEXT DEFAULT '[]',
  stats TEXT DEFAULT '', plugins TEXT DEFAULT '[]', created REAL);
CREATE TABLE IF NOT EXISTS attachments(
  id TEXT PRIMARY KEY, name TEXT, kind TEXT, mime TEXT, size INTEGER, path TEXT,
  text TEXT, created REAL);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, seq);
CREATE INDEX IF NOT EXISTS idx_chat_upd ON chats(updated);
"""


def _id() -> str:
    return uuid.uuid4().hex[:16]


class Store:
    def __init__(self, path: Path | None = None) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ATTACH_DIR.mkdir(parents=True, exist_ok=True)
        self.path = path or DATA_DIR / "chats.db"
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _q(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _x(self, sql: str, args=()) -> None:
        with self._lock:
            self.db.execute(sql, args)
            self.db.commit()

    # ---------- chats ----------
    def create_chat(self, project_id: str | None = None, title: str = "New chat") -> dict:
        now = time.time()
        cid = _id()
        self._x("INSERT INTO chats(id,project_id,title,created,updated) VALUES(?,?,?,?,?)",
                (cid, project_id, title, now, now))
        return self.get_chat(cid)

    def get_chat(self, cid: str) -> dict | None:
        r = self._q("SELECT * FROM chats WHERE id=?", (cid,))
        if not r:
            return None
        c = r[0]
        c["plugins"] = json.loads(c["plugins"] or "[]")
        return c

    def list_chats(self, query: str = "", project_id: str | None = None, limit: int = 300) -> list[dict]:
        sql = ("SELECT c.id,c.title,c.starred,c.project_id,c.updated,p.name AS project_name "
               "FROM chats c LEFT JOIN projects p ON p.id=c.project_id WHERE 1=1")
        args: list = []
        if project_id:
            sql += " AND c.project_id=?"
            args.append(project_id)
        if query:
            sql += (" AND (c.title LIKE ? OR c.id IN (SELECT chat_id FROM messages WHERE text LIKE ?))")
            args += [f"%{query}%", f"%{query}%"]
        sql += " AND EXISTS(SELECT 1 FROM messages m WHERE m.chat_id=c.id) ORDER BY c.updated DESC LIMIT ?"
        args.append(limit)
        return self._q(sql, args)

    def update_chat(self, cid: str, **fields) -> None:
        allowed = {"title", "starred", "plugins", "project_id"}
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                args.append(json.dumps(v) if k == "plugins" else v)
        if sets:
            self._x(f"UPDATE chats SET {','.join(sets)} WHERE id=?", (*args, cid))

    def touch_chat(self, cid: str) -> None:
        self._x("UPDATE chats SET updated=? WHERE id=?", (time.time(), cid))

    def delete_chat(self, cid: str) -> None:
        self._x("DELETE FROM messages WHERE chat_id=?", (cid,))
        self._x("DELETE FROM chats WHERE id=?", (cid,))

    # ---------- messages ----------
    def add_message(self, chat_id: str, role: str, text: str, attachments: list | None = None,
                    thinking: str = "", tools: list | None = None, stats: str = "",
                    plugins: list | None = None) -> dict:
        mid = _id()
        with self._lock:  # reading MAX(seq) and inserting must be one step, or two threads get the same seq
            seq = (self._q("SELECT COALESCE(MAX(seq),0) AS s FROM messages WHERE chat_id=?", (chat_id,))[0]["s"]) + 1
            self._x("INSERT INTO messages(id,chat_id,seq,role,text,attachments,thinking,tools,stats,plugins,created) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, chat_id, seq, role, text, json.dumps(attachments or []), thinking,
                     json.dumps(tools or []), stats, json.dumps(plugins or []), time.time()))
            self.touch_chat(chat_id)
        return self.get_message(mid)

    def get_message(self, mid: str) -> dict | None:
        r = self._q("SELECT * FROM messages WHERE id=?", (mid,))
        return self._msg(r[0]) if r else None

    @staticmethod
    def _msg(m: dict) -> dict:
        for k in ("attachments", "tools", "plugins"):
            m[k] = json.loads(m.get(k) or "[]")
        return m

    def messages(self, chat_id: str) -> list[dict]:
        rows = self._q("SELECT * FROM messages WHERE chat_id=? ORDER BY seq", (chat_id,))
        out = []
        for m in rows:
            m = self._msg(m)
            m["attachments"] = [self.attachment_meta(a) for a in m["attachments"]]
            m["attachments"] = [a for a in m["attachments"] if a]
            out.append(m)
        return out

    def set_message_text(self, mid: str, text: str) -> None:
        self._x("UPDATE messages SET text=? WHERE id=?", (text, mid))

    def truncate_from(self, chat_id: str, seq: int) -> None:
        """Deletes the message at seq and everything after it."""
        self._x("DELETE FROM messages WHERE chat_id=? AND seq>=?", (chat_id, seq))

    # ---------- attachments ----------
    def add_attachment(self, name: str, kind: str, mime: str, size: int, path: str, text: str) -> dict:
        aid = _id()
        self._x("INSERT INTO attachments(id,name,kind,mime,size,path,text,created) VALUES(?,?,?,?,?,?,?,?)",
                (aid, name, kind, mime, size, path, text, time.time()))
        return self.attachment_meta(aid)

    def attachment(self, aid: str) -> dict | None:
        r = self._q("SELECT * FROM attachments WHERE id=?", (aid,))
        return r[0] if r else None

    def attachment_meta(self, aid: str) -> dict | None:
        a = self.attachment(aid)
        if not a:
            return None
        return {"id": a["id"], "name": a["name"], "kind": a["kind"], "mime": a["mime"], "size": a["size"],
                "path": a["path"], "chars": len(a["text"] or "")}

    # ---------- projects ----------
    def create_project(self, name: str, description: str = "", instructions: str = "") -> dict:
        now = time.time()
        pid = _id()
        self._x("INSERT INTO projects(id,name,description,instructions,created,updated) VALUES(?,?,?,?,?,?)",
                (pid, name, description, instructions, now, now))
        return self.get_project(pid)

    def get_project(self, pid: str) -> dict | None:
        r = self._q("SELECT * FROM projects WHERE id=?", (pid,))
        if not r:
            return None
        p = r[0]
        p["files"] = self._q("SELECT id,name,kind,size,LENGTH(text) AS chars,created FROM project_files "
                             "WHERE project_id=? ORDER BY created", (pid,))
        return p

    def list_projects(self) -> list[dict]:
        return self._q("SELECT p.*, (SELECT COUNT(*) FROM chats c WHERE c.project_id=p.id AND EXISTS("
                       "SELECT 1 FROM messages m WHERE m.chat_id=c.id)) AS chat_count, "
                       "(SELECT COUNT(*) FROM project_files f WHERE f.project_id=p.id) AS file_count "
                       "FROM projects p ORDER BY p.updated DESC")

    def update_project(self, pid: str, **fields) -> None:
        sets, args = [], []
        for k in ("name", "description", "instructions"):
            if k in fields:
                sets.append(f"{k}=?")
                args.append(fields[k])
        if sets:
            sets.append("updated=?")
            args.append(time.time())
            self._x(f"UPDATE projects SET {','.join(sets)} WHERE id=?", (*args, pid))

    def delete_project(self, pid: str) -> None:
        self._x("DELETE FROM project_files WHERE project_id=?", (pid,))
        self._x("UPDATE chats SET project_id=NULL WHERE project_id=?", (pid,))
        self._x("DELETE FROM projects WHERE id=?", (pid,))

    def add_project_file(self, pid: str, name: str, kind: str, size: int, text: str) -> None:
        self._x("INSERT INTO project_files(id,project_id,name,kind,size,text,created) VALUES(?,?,?,?,?,?,?)",
                (_id(), pid, name, kind, size, text, time.time()))
        self._x("UPDATE projects SET updated=? WHERE id=?", (time.time(), pid))

    def remove_project_file(self, fid: str) -> None:
        self._x("DELETE FROM project_files WHERE id=?", (fid,))

    def project_knowledge(self, pid: str) -> list[dict]:
        return self._q("SELECT name,text FROM project_files WHERE project_id=? ORDER BY created", (pid,))
