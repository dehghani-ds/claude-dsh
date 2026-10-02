#!/usr/bin/env python3
"""Local web UI for Claude Code: browse sessions, chat, manage git worktrees.

Run:  python3 server.py [--port 8765]
      (or at login as a systemd user service: ./install-service.sh — see README.md)
Then open the printed URL. Binds to 127.0.0.1 only; every API call needs the
per-run token that is embedded into the served page.
"""
import argparse
import json
import os
import queue
import select
import signal
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

HOME = Path.home()
PROJECTS_DIR = HOME / ".claude" / "projects"
HERE = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(24)
CLAUDE_BIN = shutil.which("claude") or str(HOME / ".local" / "bin" / "claude")

DB_PATH = Path(os.environ.get("WORKBENCH_DB") or HERE / "workbench.db")
COLORS = ("slate", "red", "orange", "amber", "green", "teal", "blue", "violet", "pink")

RUNS = {}  # run_id -> Popen
CHAT_RUNS = {}  # run_id -> ChatRun: Claude replies keep going (and are buffered) when the page goes away
RUN_KEEP = 15 * 60  # seconds a finished run's events stay available for a page to catch up
STARTED = time.time()
RUNS_LOCK = threading.Lock()
_meta_cache = {}  # path -> (mtime, size, meta)
_types_cache = {}  # path -> {"records": {type: example line}, "blocks": {type: example line}}


# ---------------------------------------------------------------- database
# Folder groups and tags. Folders are keyed by their absolute path (cwd), so the
# data survives even if Claude Code renames its project directories.

DB_LOCK = threading.Lock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  color TEXT NOT NULL DEFAULT 'slate', position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  color TEXT NOT NULL DEFAULT 'slate');
CREATE TABLE IF NOT EXISTS folders (
  cwd TEXT PRIMARY KEY, group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS folder_tags (
  cwd TEXT NOT NULL, tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (cwd, tag_id));
-- chats (sessions) share the same groups and tags as folders
CREATE TABLE IF NOT EXISTS chats (
  sid TEXT PRIMARY KEY, group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS chat_tags (
  sid TEXT NOT NULL, tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (sid, tag_id));
-- news inbox: Claude Code changes. Deleted notices are kept (deleted=1) so they are never re-added.
CREATE TABLE IF NOT EXISTS notices (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, source TEXT NOT NULL, title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '', version TEXT, relevant INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL, read INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
  analysis TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- per-chat to-dos and notes; status tracks running one as a prompt: '' | queued | running | applied | failed
CREATE TABLE IF NOT EXISTS session_notes (
  id INTEGER PRIMARY KEY, sid TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'todo', text TEXT NOT NULL,
  done INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT '', position REAL NOT NULL DEFAULT 0,
  created REAL NOT NULL, updated REAL NOT NULL, ran REAL);
CREATE INDEX IF NOT EXISTS session_notes_sid ON session_notes(sid);
"""


@contextmanager
def db():
    """Connection that commits on success, rolls back on error, and always closes."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def db_init():
    with DB_LOCK, db() as c:
        c.executescript(SCHEMA)
        # columns added after the first release: add them to existing databases
        have = {r[1] for r in c.execute("PRAGMA table_info(session_notes)")}
        for col, decl in (("check_verdict", "TEXT NOT NULL DEFAULT ''"), ("check_reason", "TEXT NOT NULL DEFAULT ''"),
                          ("check_suggest", "TEXT NOT NULL DEFAULT ''"), ("checked", "REAL")):
            if col not in have:
                c.execute(f"ALTER TABLE session_notes ADD COLUMN {col} {decl}")
        # pinned groups, folders (within their group) and chats (within their folder) are listed first
        for table in ("groups", "folders", "chats"):
            if "pinned" not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")


def _clean_name(name, what):
    name = (name or "").strip().lstrip("#").strip()
    if not name or len(name) > 40:
        raise ValueError(f"{what} name must be 1-40 characters")
    return name


def _color(color, name):
    if color in COLORS:
        return color
    return COLORS[sum(map(ord, name)) % len(COLORS)]


def meta_all():
    with DB_LOCK, db() as c:
        groups = [dict(r) for r in c.execute("SELECT * FROM groups ORDER BY position, name")]
        tags = [dict(r) for r in c.execute("SELECT * FROM tags ORDER BY name")]
        folders = {}
        for r in c.execute("SELECT cwd, group_id, pinned FROM folders"):
            folders[r["cwd"]] = {"group": r["group_id"], "tags": [], "pinned": bool(r["pinned"])}
        for r in c.execute("SELECT cwd, tag_id FROM folder_tags"):
            folders.setdefault(r["cwd"], {"group": None, "tags": [], "pinned": False})["tags"].append(r["tag_id"])
        chats = {}
        for r in c.execute("SELECT sid, group_id, pinned FROM chats"):
            chats[r["sid"]] = {"group": r["group_id"], "tags": [], "pinned": bool(r["pinned"])}
        for r in c.execute("SELECT sid, tag_id FROM chat_tags"):
            chats.setdefault(r["sid"], {"group": None, "tags": [], "pinned": False})["tags"].append(r["tag_id"])
    # grouped/tagged/pinned chats are listed on their own too, so include what's needed to show them
    for sid, ch in list(chats.items()):
        if ch["group"] is None and not ch["tags"] and not ch["pinned"]:
            del chats[sid]
            continue
        try:
            f = find_session_file(sid)
            m = session_meta(f)
            ch.update(title=m["title"], cwd=m["cwd"], mtime=m["mtime"], project=f.parent.name)
        except ValueError:
            ch.update(title="(deleted chat)", cwd=None, mtime=0, project=None, missing=True)
    return {"groups": groups, "tags": tags, "folders": folders, "chats": chats, "colors": COLORS}


def group_save(b):
    name = _clean_name(b.get("name"), "Group")
    with DB_LOCK, db() as c:
        try:
            if b.get("id"):
                c.execute("UPDATE groups SET name=?, color=? WHERE id=?",
                          (name, _color(b.get("color"), name), b["id"]))
                return {"id": b["id"]}
            pos = c.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM groups").fetchone()[0]
            cur = c.execute("INSERT INTO groups (name, color, position) VALUES (?, ?, ?)",
                            (name, _color(b.get("color"), name), pos))
            return {"id": cur.lastrowid}
        except sqlite3.IntegrityError:
            raise ValueError(f"a group named '{name}' already exists")


def group_move(gid, direction):
    with DB_LOCK, db() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM groups ORDER BY position, name")]
        i = ids.index(gid)
        j = i + (1 if direction > 0 else -1)
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        c.executemany("UPDATE groups SET position=? WHERE id=?", [(n, g) for n, g in enumerate(ids)])
    return {"ok": True}


def tag_save(b):
    name = _clean_name(b.get("name"), "Tag")
    with DB_LOCK, db() as c:
        try:
            c.execute("UPDATE tags SET name=?, color=? WHERE id=?",
                      (name, _color(b.get("color"), name), b["id"]))
        except sqlite3.IntegrityError:
            raise ValueError(f"a tag named '{name}' already exists")
    return {"ok": True}


def delete_row(table, rid):
    with DB_LOCK, db() as c:
        c.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
    return {"ok": True}


def folder_set(b):
    """Set a folder's group and/or tags. `tags` is a list of tag names (created if new)."""
    cwd = b["cwd"]
    with DB_LOCK, db() as c:
        c.execute("INSERT OR IGNORE INTO folders (cwd) VALUES (?)", (cwd,))
        if "group" in b:
            c.execute("UPDATE folders SET group_id=? WHERE cwd=?", (b["group"] or None, cwd))
        if "tags" in b:
            c.execute("DELETE FROM folder_tags WHERE cwd=?", (cwd,))
            for raw in b["tags"]:
                name = _clean_name(raw, "Tag")
                row = c.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
                tid = row[0] if row else c.execute(
                    "INSERT INTO tags (name, color) VALUES (?, ?)", (name, _color(None, name))).lastrowid
                c.execute("INSERT OR IGNORE INTO folder_tags VALUES (?, ?)", (cwd, tid))
        _drop_unused_tags(c)
    return {"ok": True}


def pin_set(b):
    """Pin or unpin a group (listed first), a folder (first in its group) or a chat (first in its folder)."""
    kind, on = b.get("kind"), int(bool(b.get("pinned")))
    with DB_LOCK, db() as c:
        if kind == "group":
            if not c.execute("UPDATE groups SET pinned=? WHERE id=?", (on, int(b["id"]))).rowcount:
                raise ValueError("group not found")
        elif kind == "folder":
            c.execute("INSERT OR IGNORE INTO folders (cwd) VALUES (?)", (b["cwd"],))
            c.execute("UPDATE folders SET pinned=? WHERE cwd=?", (on, b["cwd"]))
        elif kind == "chat":
            sid = _check_sid(b.get("sid"))
            c.execute("INSERT OR IGNORE INTO chats (sid) VALUES (?)", (sid,))
            c.execute("UPDATE chats SET pinned=? WHERE sid=?", (on, sid))
        else:
            raise ValueError("kind must be group, folder or chat")
    return {"ok": True}


NOTE_STATUSES = ("", "queued", "running", "applied", "failed")


def _check_sid(sid):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", sid or ""):
        raise ValueError("bad session id")
    return sid


def notes_list(sid):
    _check_sid(sid)
    with DB_LOCK, db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM session_notes WHERE sid=? ORDER BY position, id", (sid,))]


def notes_counts():
    """Per chat: open to-dos, notes, queued items — for the 📝 badges in the lists."""
    with DB_LOCK, db() as c:
        rows = c.execute("""SELECT sid, SUM(kind='todo' AND done=0), SUM(kind='note'), SUM(status='queued')
                            FROM session_notes GROUP BY sid""").fetchall()
    return {r[0]: {"todo": r[1] or 0, "notes": r[2] or 0, "queued": r[3] or 0} for r in rows}


def note_save(b):
    """Create ({sid, kind, text}), update ({id, text/done/status/position/ran}) or delete ({id, delete})."""
    now = time.time()
    with DB_LOCK, db() as c:
        if b.get("id"):
            nid = int(b["id"])
            if b.get("delete"):
                c.execute("DELETE FROM session_notes WHERE id=?", (nid,))
                return {"ok": True}
            sets, vals = [], []
            if "text" in b:
                text = (b["text"] or "").strip()
                if not text or len(text) > 20000:
                    raise ValueError("text must be 1-20000 characters")
                sets.append("text=?"); vals.append(text)
            if "done" in b:
                sets.append("done=?"); vals.append(int(bool(b["done"])))
            if "status" in b:
                if b["status"] not in NOTE_STATUSES:
                    raise ValueError("bad status")
                sets.append("status=?"); vals.append(b["status"])
                if b["status"] == "running":
                    sets.append("ran=?"); vals.append(now)
            if "position" in b:
                sets.append("position=?"); vals.append(float(b["position"]))
            if not sets:
                raise ValueError("nothing to change")
            c.execute(f"UPDATE session_notes SET {', '.join(sets)}, updated=? WHERE id=?", (*vals, now, nid))
            r = c.execute("SELECT * FROM session_notes WHERE id=?", (nid,)).fetchone()
            if not r:
                raise ValueError("note not found")
            return dict(r)
        sid = _check_sid(b.get("sid"))
        kind = b.get("kind") if b.get("kind") in ("todo", "note") else "todo"
        text = (b.get("text") or "").strip()
        if not text or len(text) > 20000:
            raise ValueError("text must be 1-20000 characters")
        pos = c.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM session_notes WHERE sid=?", (sid,)).fetchone()[0]
        cur = c.execute("INSERT INTO session_notes (sid, kind, text, position, created, updated) VALUES (?, ?, ?, ?, ?, ?)",
                        (sid, kind, text, pos, now, now))
        return dict(c.execute("SELECT * FROM session_notes WHERE id=?", (cur.lastrowid,)).fetchone())


def chat_set(b):
    """Set a chat's (session's) group and/or tags, like folder_set."""
    sid = b["sid"]
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", sid or ""):
        raise ValueError("bad session id")
    with DB_LOCK, db() as c:
        c.execute("INSERT OR IGNORE INTO chats (sid) VALUES (?)", (sid,))
        if "group" in b:
            c.execute("UPDATE chats SET group_id=? WHERE sid=?", (b["group"] or None, sid))
        if "tags" in b:
            c.execute("DELETE FROM chat_tags WHERE sid=?", (sid,))
            for raw in b["tags"]:
                name = _clean_name(raw, "Tag")
                row = c.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
                tid = row[0] if row else c.execute(
                    "INSERT INTO tags (name, color) VALUES (?, ?)", (name, _color(None, name))).lastrowid
                c.execute("INSERT OR IGNORE INTO chat_tags VALUES (?, ?)", (sid, tid))
        _drop_unused_tags(c)
    return {"ok": True}


def _drop_unused_tags(c):
    """Tags that no folder and no chat uses any more."""
    c.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM folder_tags UNION SELECT tag_id FROM chat_tags)")


# ---------------------------------------------------------------- sessions

def _text_of(content):
    """Plain text from a message content (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_noise(text):
    t = text.lstrip()
    return (not t or t.startswith("<command-") or t.startswith("<local-command")
            or t.startswith("<system-reminder>") or t.startswith("Caveat:"))


def _is_hidden(text):
    """Transcript filter: hide only internal notes; keep the user's commands and their output."""
    t = text.lstrip()
    return (not t or t.startswith("<local-command-caveat>") or t.startswith("<system-reminder>")
            or t.startswith("Caveat:"))


def session_meta(path: Path):
    st = path.stat()
    cached = _meta_cache.get(str(path))
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    # "mtime" is the last *message* time: resuming/reopening a session appends records
    # (mode, permission-mode, …) that must not count as activity. "fileMtime" is the raw file time.
    meta = {"id": path.stem, "title": None, "firstPrompt": None, "lastPrompt": None,
            "cwd": None, "gitBranch": None, "messages": 0, "mtime": st.st_mtime,
            "fileMtime": st.st_mtime, "size": st.st_size, "cost": None}
    last_msg = None
    ai_title = custom_title = summary = None
    types = {"records": {}, "blocks": {}}  # first example of each type, for the news checker
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            t = o.get("type")
            types["records"].setdefault(t, line[:400])
            msg = o.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                for b in msg["content"]:
                    if isinstance(b, dict):
                        types["blocks"].setdefault(b.get("type"), json.dumps(b)[:400])
            if t == "custom-title":
                custom_title = o.get("customTitle") or custom_title
            elif t == "ai-title":
                ai_title = o.get("aiTitle") or ai_title
            elif t == "summary":
                summary = o.get("summary") or summary
            elif t == "last-prompt":
                meta["lastPrompt"] = o.get("lastPrompt")
            elif t == "cost-state":
                meta["cost"] = o.get("totalCostUSD")
            elif t in ("user", "assistant") and not o.get("isSidechain"):
                last_msg = o.get("timestamp") or last_msg
                meta["cwd"] = meta["cwd"] or o.get("cwd")  # where the session started (its project)
                meta["lastCwd"] = o.get("cwd") or meta.get("lastCwd")  # where it was last working
                meta["gitBranch"] = o.get("gitBranch") or meta["gitBranch"]
                if t == "user" and not o.get("isMeta"):
                    txt = _text_of((o.get("message") or {}).get("content"))
                    if not _is_noise(txt):
                        meta["messages"] += 1
                        if not meta["firstPrompt"]:
                            meta["firstPrompt"] = txt[:300]
                elif t == "assistant":
                    meta["messages"] += 1
    meta["title"] = custom_title or ai_title or summary or meta["firstPrompt"] or "(empty session)"
    meta["mtime"] = _iso_ts(last_msg) or st.st_mtime
    _meta_cache[str(path)] = (st.st_mtime, st.st_size, meta)
    _types_cache[str(path)] = types
    return meta


def _iso_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    except ValueError:
        return None


def decode_dir_name(name):
    return "/" + name.lstrip("-").replace("-", "/")


def list_projects():
    out = []
    if not PROJECTS_DIR.is_dir():
        return out
    for d in PROJECTS_DIR.iterdir():
        if not d.is_dir():
            continue
        files = list(d.glob("*.jsonl"))
        if not files:
            continue
        metas = sorted((session_meta(f) for f in files), key=lambda m: -m["mtime"])
        latest = metas[0]["mtime"]
        cwd = next((m["cwd"] for m in metas if m["cwd"]), None)
        cwd = cwd or decode_dir_name(d.name)
        out.append({"key": d.name, "cwd": cwd, "sessions": len(files), "mtime": latest,
                    "exists": os.path.isdir(cwd),
                    "isWorktree": "/.claude/worktrees/" in cwd})
    out.sort(key=lambda p: -p["mtime"])
    return out


def project_path(key):
    p = (PROJECTS_DIR / key).resolve()
    if p.parent != PROJECTS_DIR.resolve() or not p.is_dir():
        raise ValueError("unknown project")
    return p


def list_sessions(key):
    d = project_path(key)
    items = [session_meta(f) for f in d.glob("*.jsonl")]
    items.sort(key=lambda m: -m["mtime"])
    return items


def find_session_file(session_id):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", session_id or ""):
        raise ValueError("bad session id")
    for f in PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        return f
    raise ValueError("session not found")


TRASH_DIR = HOME / ".claude" / "workbench-trash"


def session_remove(sid):
    """Remove a chat from the lists by moving its session file (and its folder of subagent files, if any) to
    ~/.claude/workbench-trash/<project>/. Nothing is deleted, so it can be restored."""
    f = find_session_file(sid)
    with RUNS_LOCK:
        if any(r.session_id == sid and not r.done for r in CHAT_RUNS.values()):
            raise ValueError("Claude is still working in this chat — stop it first")
    dest = TRASH_DIR / f.parent.name
    dest.mkdir(parents=True, exist_ok=True)
    for src in (f, f.parent / sid):
        if src.exists():
            target = dest / src.name
            if target.exists():  # an older copy in the trash: keep the newest
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(src), str(target))
    _meta_cache.pop(str(f), None)
    _types_cache.pop(str(f), None)
    return {"ok": True, "trash": str(dest)}


def session_restore(sid):
    """Undo session_remove: move the chat back from the trash."""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", sid or ""):
        raise ValueError("bad session id")
    for f in TRASH_DIR.glob(f"*/{sid}.jsonl"):
        home = PROJECTS_DIR / f.parent.name
        home.mkdir(parents=True, exist_ok=True)
        if (home / f.name).exists():
            raise ValueError("a chat with this ID is already there")
        for src in (f, f.parent / sid):
            if src.exists():
                shutil.move(str(src), str(home / src.name))
        return {"ok": True}
    raise ValueError("this chat isn't in the trash")


def rename_session(sid, title):
    """Rename a chat the way Claude Code's /rename does: append a custom-title record to its session
    file (the newest one wins), so `claude --resume` shows the new name too."""
    title = " ".join((title or "").split())
    if not title or len(title) > 200:
        raise ValueError("name must be 1-200 characters")
    f = find_session_file(sid)
    with open(f, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        ends_with_newline = True
        if fh.tell() > 0:
            fh.seek(-1, os.SEEK_END)
            ends_with_newline = fh.read(1) == b"\n"
    record = json.dumps({"type": "custom-title", "customTitle": title, "sessionId": sid}, ensure_ascii=False)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(("" if ends_with_newline else "\n") + record + "\n")
    return {"ok": True, "title": session_meta(f)["title"]}


def read_transcript(session_id):
    f = find_session_file(session_id)
    msgs = []
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            # output of a local slash command (/usage, /cost, /context…) is saved as a system record;
            # show it like the terminal does, under the command
            if o.get("type") == "system" and o.get("subtype") == "local_command" and isinstance(o.get("content"), str):
                msgs.append({"role": "user", "ts": o.get("timestamp"), "blocks": [{"type": "text", "text": o["content"]}], "model": None})
                continue
            if o.get("type") not in ("user", "assistant") or o.get("isSidechain"):
                continue
            m = o.get("message") or {}
            content = m.get("content")
            if o["type"] == "user" and o.get("isMeta"):
                continue
            if isinstance(content, str):
                if _is_hidden(content):
                    continue
                content = [{"type": "text", "text": content}]
            blocks = []
            for b in content or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    if not b.get("text", "").strip():
                        continue  # empty text blocks exist in real transcripts; nothing to show
                    if o["type"] == "user" and _is_hidden(b.get("text", "")):
                        continue
                    blocks.append({"type": "text", "text": b.get("text", "")})
                elif bt == "thinking" and b.get("thinking"):
                    blocks.append({"type": "thinking", "text": b["thinking"]})
                elif bt == "tool_use":
                    blocks.append({"type": "tool_use", "id": b.get("id"), "name": b.get("name"),
                                   "input": b.get("input")})
                elif bt == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    blocks.append({"type": "tool_result", "id": b.get("tool_use_id"),
                                   "text": (c or "")[:20000], "isError": b.get("is_error", False)})
            if blocks:
                msgs.append({"role": o["type"], "ts": o.get("timestamp"), "blocks": blocks,
                             "model": m.get("model")})
    meta = dict(session_meta(f))
    # `!` commands start where the session left off (it may have moved into a subfolder or out of a
    # worktree); fall back to where it started if that folder is gone.
    meta["shellCwd"] = next((d for d in (meta.get("lastCwd"), meta["cwd"]) if d and os.path.isdir(d)), meta["cwd"])
    if meta["cwd"] and not os.path.isdir(meta["cwd"]):  # e.g. started in a worktree that was removed since
        meta["startCwd"], meta["cwd"] = meta["cwd"], meta["shellCwd"]
    return {"meta": meta, "messages": msgs}


# ---------------------------------------------------------------- git / fs

def git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=60)
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"git {' '.join(args)} failed")
    return r


def repo_info(cwd):
    if not os.path.isdir(cwd):
        return {"isRepo": False, "error": "directory does not exist"}
    r = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if r.returncode != 0:
        return {"isRepo": False}
    main_root = str(Path(r.stdout.strip()).parent)
    wts, cur = [], {}
    for line in git(cwd, "worktree", "list", "--porcelain").stdout.splitlines() + [""]:
        if not line:
            if cur:
                wts.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        if k == "worktree":
            cur["path"] = v
        elif k == "HEAD":
            cur["head"] = v[:8]
        elif k == "branch":
            cur["branch"] = v.replace("refs/heads/", "")
        elif k in ("detached", "bare", "locked", "prunable"):
            cur[k] = True
    branches = git(cwd, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.split()
    current = git(cwd, "branch", "--show-current", check=False).stdout.strip()
    for w in wts:
        s = git(w["path"], "status", "--porcelain", check=False) if os.path.isdir(w["path"]) else None
        # the worktrees themselves live in .claude/ of the main folder: that alone isn't "changes"
        w["dirty"] = bool(s and [l for l in s.stdout.splitlines() if l.strip() and l != "?? .claude/"])
    return {"isRepo": True, "root": main_root, "worktrees": wts, "branches": branches,
            "current": current}


def create_worktree(cwd, name, branch, base):
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", name or "") or ".." in name:
        raise ValueError("invalid worktree name")
    branch = branch or f"worktree-{name.replace('/', '-')}"
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith("-"):
        raise ValueError("invalid branch name")
    info = repo_info(cwd)
    if not info.get("isRepo"):
        raise ValueError("not a git repository")
    path = Path(info["root"]) / ".claude" / "worktrees" / name
    args = ["worktree", "add"]
    if branch in info["branches"]:
        args += [str(path), branch]
    else:
        args += ["-b", branch, str(path)]
        if base:
            if base.startswith("-"):
                raise ValueError("invalid base")
            args.append(base)
    git(info["root"], *args)
    return {"path": str(path), "branch": branch}


def list_sessions_under(root):
    """Sessions of every project whose folder is `root` or inside it (the main checkout, its
    subfolders and its worktrees, including worktrees that were removed since)."""
    root = root.rstrip("/")
    out = []
    for p in list_projects():
        if p["cwd"] == root or p["cwd"].startswith(root + "/"):
            out += list_sessions(p["key"])
    out.sort(key=lambda m: -m["mtime"])
    return out


def _wt_pair(cwd, source, target):
    """-> (repo info, source worktree, target worktree) for applying `source` onto branch `target`."""
    info = repo_info(cwd)
    if not info.get("isRepo"):
        raise ValueError("not a git repository")
    src = next((w for w in info["worktrees"] if w["path"] == source), None)
    if not src or not src.get("branch"):
        raise ValueError("source worktree not found or not on a branch")
    if target == src["branch"]:
        raise ValueError("source and target are the same branch")
    dst = next((w for w in info["worktrees"] if w.get("branch") == target), None)
    if not dst:
        raise ValueError(f"branch '{target}' isn't checked out in any worktree — check it out in the main folder first")
    return info, src, dst


def worktree_preview(cwd, source, target):
    """What applying would do, without changing anything."""
    info, src, dst = _wt_pair(cwd, source, target)
    root, sb = info["root"], src["branch"]
    commits = git(root, "log", "--format=%h%x09%s", f"{target}..{sb}", "-n", "50").stdout.splitlines()
    stat = git(root, "diff", "--stat=100", f"{target}...{sb}").stdout.rstrip()
    behind = int(git(root, "rev-list", "--count", f"{sb}..{target}").stdout.strip() or 0)
    # dry-run merge: exit 1 and a list of files when it would conflict (git >= 2.38)
    mt = git(root, "merge-tree", "--write-tree", "--name-only", target, sb, check=False)
    conflicts = []
    # the merge would produce exactly the target's tree: everything is already there (e.g. squashed before)
    already = mt.returncode == 0 and mt.stdout.split("\n", 1)[0].strip() == git(root, "rev-parse", f"{target}^{{tree}}").stdout.strip()
    if mt.returncode == 1:
        for line in mt.stdout.splitlines()[1:]:
            if not line.strip():
                break
            conflicts.append(line.strip())
    src_dirty = git(src["path"], "status", "--porcelain", check=False).stdout.splitlines()
    # worktrees live in .claude/ inside the main folder: that isn't a change of its own
    dst_dirty = [l for l in git(dst["path"], "status", "--porcelain", check=False).stdout.splitlines() if l != "?? .claude/"]
    return {"source": src["path"], "sourceBranch": sb, "target": target, "targetPath": dst["path"],
            "commits": commits, "stat": stat, "behind": behind, "conflicts": conflicts, "alreadyApplied": already,
            "mergeCheck": mt.returncode in (0, 1), "sourceDirty": src_dirty[:50], "targetDirty": dst_dirty[:50]}


def worktree_apply(b):
    """Bring a worktree branch's commits into `target` (checked out in some worktree), by merge or squash.
    Optionally commits the worktree's uncommitted changes first and removes the worktree afterwards.
    On a conflict the merge is undone, so nothing is left half-done."""
    info, src, dst = _wt_pair(b["cwd"], b["source"], b["target"])
    sb, target, mode = src["branch"], b["target"], b.get("mode", "merge")
    name = os.path.basename(src["path"])
    steps = []
    if b.get("commitDirty") and git(src["path"], "status", "--porcelain", check=False).stdout.strip():
        msg = (b.get("commitMessage") or "").strip() or f"Work from worktree {name}"
        git(src["path"], "add", "-A")
        git(src["path"], "commit", "-m", msg)
        steps.append(f"committed uncommitted changes in {name}")
    if not git(info["root"], "rev-list", "-n", "1", f"{target}..{sb}").stdout.strip():
        raise ValueError(f"nothing to apply: {sb} has no commits that {target} doesn't already have")
    msg = (b.get("message") or "").strip() or (f"Merge worktree {name} ({sb})" if mode == "merge" else f"{name}: squashed changes from {sb}")
    if mode == "squash":
        r = git(dst["path"], "merge", "--squash", sb, check=False)
        if r.returncode != 0:
            git(dst["path"], "reset", "--merge", check=False)
            raise RuntimeError("squash failed, nothing changed: " + (r.stdout + r.stderr).strip()[-600:])
        if git(dst["path"], "diff", "--cached", "--quiet", check=False).returncode == 0:
            git(dst["path"], "reset", "--merge", check=False)
            raise ValueError(f"nothing to apply: {target} already has all changes from {sb}")
        c = git(dst["path"], "commit", "-m", msg, check=False)
        if c.returncode != 0:
            git(dst["path"], "reset", "--merge", check=False)
            raise RuntimeError("commit failed, nothing changed: " + (c.stdout + c.stderr).strip()[-600:])
    else:
        r = git(dst["path"], "merge", "--no-ff", "-m", msg, sb, check=False)
        if r.returncode != 0:
            git(dst["path"], "merge", "--abort", check=False)
            raise RuntimeError("merge failed, nothing changed: " + (r.stdout + r.stderr).strip()[-600:])
    steps.append(f"{'squashed' if mode == 'squash' else 'merged'} {sb} into {target}")
    head = git(dst["path"], "log", "-1", "--format=%h %s").stdout.strip()
    if b.get("removeAfter"):
        rr = git(info["root"], "worktree", "remove", src["path"], check=False)
        steps.append(f"removed worktree {name} (branch {sb} kept)" if rr.returncode == 0
                     else f"could not remove worktree {name}: {rr.stderr.strip()[-200:]}")
    return {"ok": True, "head": head, "steps": steps}


def remove_worktree(cwd, path, force):
    info = repo_info(cwd)
    if not any(w["path"] == path for w in info.get("worktrees", [])) or path == info["root"]:
        raise ValueError("not a removable worktree of this repo")
    git(info["root"], "worktree", "remove", *(["--force"] if force else []), path)
    return {"ok": True}


def browse(path):
    p = Path(os.path.expanduser(path or str(HOME))).resolve()
    if not p.is_dir():
        raise ValueError("not a directory")
    dirs = []
    try:
        for c in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if c.is_dir() and not c.name.startswith("."):
                dirs.append(c.name)
    except PermissionError:
        pass
    return {"path": str(p), "parent": str(p.parent), "dirs": dirs,
            "isRepo": (p / ".git").exists()}


# ---------------------------------------------------------------- shell
# Opens a terminal window in a folder on this machine. $WORKBENCH_TERMINAL (or $TERMINAL)
# picks the program; otherwise the first installed one below is used.

TERMINALS = (  # program, args; the folder is appended to the last arg (None = inherit the cwd)
    ("gnome-terminal", ["--working-directory="]), ("kgx", ["--working-directory="]),
    ("ptyxis", ["--new-window", "--working-directory="]), ("konsole", ["--workdir", ""]),
    ("xfce4-terminal", ["--working-directory="]), ("tilix", ["--working-directory="]),
    ("terminator", ["--working-directory="]), ("kitty", ["--directory", ""]),
    ("alacritty", ["--working-directory", ""]), ("wezterm", ["start", "--cwd", ""]),
    ("foot", ["--working-directory="]), ("x-terminal-emulator", None), ("xterm", None),
)


_desktop = {"t": 0, "ok": False}


def desktop_supported():
    """Does this claude have --desktop (open a session in the Claude Desktop app)? Checked at most every 10 min."""
    if time.time() - _desktop["t"] > 600:
        try:
            r = subprocess.run([CLAUDE_BIN, "--help"], capture_output=True, text=True, timeout=30)
            _desktop["ok"] = "--desktop" in r.stdout
        except (OSError, subprocess.SubprocessError):
            _desktop["ok"] = False
        _desktop["t"] = time.time()
    return _desktop["ok"]


def open_desktop(cwd, sid=None):
    """Open a chat (or a new one in `cwd`) in the Claude Desktop app via `claude --desktop [--resume id]`."""
    if not os.path.isdir(cwd or ""):
        raise ValueError(f"directory does not exist: {cwd}")
    cmd = [CLAUDE_BIN, "--desktop"]
    if sid:
        find_session_file(sid)
        cmd += ["--resume", sid]
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:  # it normally hands over to the app and exits; report it if it fails straight away
        if p.wait(timeout=8) != 0:
            raise RuntimeError((p.stderr.read() or "").strip()[-400:] or f"claude --desktop exited with {p.returncode}")
    except subprocess.TimeoutExpired:
        threading.Thread(target=p.stderr.read, daemon=True).start()  # still running: fine, just drain its output
    return {"ok": True}


def open_shell(cwd):
    if not os.path.isdir(cwd):
        raise ValueError(f"directory does not exist: {cwd}")
    custom = os.environ.get("WORKBENCH_TERMINAL") or os.environ.get("TERMINAL")
    if custom and shutil.which(custom.split()[0]):
        cmd = custom.split()
    else:
        for prog, args in TERMINALS:
            exe = shutil.which(prog)
            if exe:
                cmd = [exe] if args is None else [exe, *args[:-1], args[-1] + cwd]
                break
        else:
            raise RuntimeError("no terminal program found; set WORKBENCH_TERMINAL")
    subprocess.Popen(cmd, cwd=cwd, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "terminal": os.path.basename(cmd[0])}


# ---------------------------------------------------------------- news inbox
# Four checks feed the `notices` table:
#   changelog  - new Claude Code versions in the official changelog
#   version    - the installed `claude` was updated
#   cli        - `claude --help` gained or lost options (this dashboard drives the CLI)
#   format     - session files contain record/content types this dashboard doesn't know yet
# "relevant" marks notices that may need a change in this dashboard.

CHANGELOG_URL = "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"
CHANGELOG_PAGE = "https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md"
LOCAL_CHANGELOG = HOME / ".claude" / "cache" / "changelog.md"
CHECK_EVERY = 6 * 3600
FIRST_RUN_VERSIONS = 10
ANALYZE_MODEL = "haiku"

# Flags server.py passes to `claude`; losing one would break chatting.
USED_FLAGS = {"--print", "--output-format", "--verbose", "--include-partial-messages", "--resume",
              "--permission-mode", "--model", "--effort", "--tools", "--no-session-persistence"}
KNOWN_RECORDS = {"user", "assistant", "system", "summary", "ai-title", "custom-title", "last-prompt",
                 "cost-state", "mode", "permission-mode", "atis-latch", "attachment", "file-history-snapshot",
                 "file-history-delta", "queue-operation", "worktree-state", "relocated", "bridge-session",
                 "frame-link", "artifact-comment-monitor", "artifact-autoreact-ledger", "agent-name"}
KNOWN_BLOCKS = {"text", "thinking", "redacted_thinking", "tool_use", "tool_result", "image", "document"}
RELEVANT_RE = re.compile(
    r"stream-json|--output-format|--input-format|--print\b|`-p`|\bprint mode|headless mode|non-interactive|"
    r"--resume\b|--continue\b|--session-id|--no-session-persistence|include-partial|--permission-mode|"
    r"session (files?|ids?|titles?|names?|records?|storage|history|list)|transcript (file|format|jsonl)|"
    r"\.jsonl|~/\.claude/projects|\.claude/worktrees|--worktree\b|worktree (folder|path|directory|creation|cleanup)|"
    r"--model\b|/rename\b|custom title|ai[- ]title|--tools\b|--allowed-?tools|--verbose\b|cost-state|"
    r"result (event|message)|system/init|init event", re.I)

DASHBOARD_BRIEF = """\
"Claude Workbench" is a local web dashboard (server.py + index.html, Python stdlib + vanilla JS) that:
- lists projects and sessions by reading ~/.claude/projects/*/*.jsonl directly (record types user, assistant,
  ai-title, custom-title, summary, last-prompt, cost-state; content blocks text, thinking, tool_use, tool_result;
  user text tags <bash-input>, <bash-stdout>, <command-name>, <command-args>, <local-command-stdout>,
  <pasted_content>, <task-notification>, "[Request interrupted by user]");
- chats by running `claude -p --output-format stream-json --verbose --include-partial-messages
  [--resume <id>] [--permission-mode <mode>] [--model <m>] [--effort <level>]` with the prompt on stdin,
  and renders the stream events (system/init incl. model, claude_code_version, mcp_servers status and
  plugin_errors; stream_event deltas; assistant; user tool_result; result with cost/duration/denials);
- shows session "last active" from the last message timestamp, and cost from cost-state records;
- creates/removes git worktrees in <repo>/.claude/worktrees/<name>;
- opens a terminal window (gnome-terminal, konsole, … or $WORKBENCH_TERMINAL) in a project/worktree folder;
- stores folder groups, tags and this news inbox in SQLite."""

NEWS_LOCK = threading.Lock()
_news_state = {"checking": False, "last": None, "error": None}


def kv_get(key, default=None):
    with DB_LOCK, db() as c:
        r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def kv_set(key, value):
    with DB_LOCK, db() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))


def add_notice(key, source, title, body, version=None, relevant=False, read=False):
    """Insert once; returns True if new. Keys of deleted notices stay, so they never come back."""
    with DB_LOCK, db() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO notices (key, source, title, body, version, relevant, created, read)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, source, title, body, version, int(relevant), time.time(), int(read)))
        return cur.rowcount == 1


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


def parse_changelog(text):
    """-> [(version, [bullet, ...])] newest first."""
    out = []
    for m in re.finditer(r"^## +\[?([0-9][^\s\]]*)\]?.*?$(.*?)(?=^## |\Z)", text, re.M | re.S):
        bullets = [b.strip() for b in re.findall(r"^[-*] +(.+(?:\n(?![-*] |#).+)*)", m.group(2), re.M)]
        out.append((m.group(1), [" ".join(b.split()) for b in bullets]))
    out.sort(key=lambda x: _vtuple(x[0]), reverse=True)
    return out


def fetch_changelog():
    """Newest of GitHub and Claude Code's own cached copy. -> (text, source label)"""
    candidates = []
    try:
        with urlopen(Request(CHANGELOG_URL, headers={"User-Agent": "claude-workbench"}), timeout=10) as r:
            candidates.append((r.read().decode("utf-8", "replace"), "GitHub"))
    except (URLError, OSError, TimeoutError):
        pass
    if LOCAL_CHANGELOG.is_file():
        candidates.append((LOCAL_CHANGELOG.read_text(errors="replace"), "local cache"))
    if not candidates:
        raise RuntimeError("changelog unavailable (offline and no local cache)")
    return max(candidates, key=lambda c: max((_vtuple(v) for v, _ in parse_changelog(c[0])), default=()))


def changelog_body(bullets):
    rel = [b for b in bullets if RELEVANT_RE.search(b)]
    parts = []
    if rel:
        parts.append("#### 🔧 May affect this dashboard\n" + "\n".join(f"- {b}" for b in rel))
    parts.append("#### All changes\n" + "\n".join(f"- {b}" for b in bullets))
    return "\n\n".join(parts), len(rel)


def check_changelog():
    text, src = fetch_changelog()
    versions = parse_changelog(text)
    if not versions:
        return 0
    seeded = kv_get("changelog_seeded", False)
    newest_known = kv_get("changelog_newest")
    added = 0
    for v, bullets in (versions[:FIRST_RUN_VERSIONS] if not seeded else versions):
        if seeded and newest_known and _vtuple(v) <= _vtuple(newest_known):
            break
        body, nrel = changelog_body(bullets)
        title = f"Claude Code {v}" + (f" — {nrel} change{'s' if nrel != 1 else ''} may affect the dashboard" if nrel else "")
        body += f"\n\n<sub>Source: {src} · [full changelog]({CHANGELOG_PAGE})</sub>"
        added += add_notice(f"changelog:{v}", "changelog", title, body, v, nrel > 0)
    kv_set("changelog_seeded", True)
    kv_set("changelog_newest", versions[0][0])
    return added


def claude_version():
    r = subprocess.run([CLAUDE_BIN, "--version"], capture_output=True, text=True, timeout=30)
    m = re.search(r"\d+\.\d+\.\d+\S*", r.stdout)
    return m.group(0) if m else None


def check_version():
    cur, prev = claude_version(), kv_get("cli_version")
    if not cur:
        return 0
    kv_set("cli_version", cur)
    if not prev or prev == cur:
        return 0
    newer = _vtuple(cur) > _vtuple(prev)
    body = (f"Installed Claude Code changed from **{prev}** to **{cur}**.\n\n"
            + (f"See the changelog notices for versions after {prev}." if newer else "This is a downgrade."))
    return add_notice(f"version:{prev}->{cur}", "version",
                      f"Claude Code {'updated' if newer else 'changed'}: {prev} → {cur}", body, cur, False)


def cli_flags():
    r = subprocess.run([CLAUDE_BIN, "--help"], capture_output=True, text=True, timeout=30)
    flags = {}
    for line in r.stdout.splitlines():
        m = re.match(r"^\s{2,}(?:-\w, )?(--[a-zA-Z][\w-]*)(.*)$", line)
        if m:
            flags[m.group(1)] = line.strip()
    return flags


def check_cli_flags():
    cur, prev = cli_flags(), kv_get("cli_flags")
    if not cur:
        return 0
    kv_set("cli_flags", sorted(cur))
    if prev is None:
        return 0
    new, gone = sorted(set(cur) - set(prev)), sorted(set(prev) - set(cur))
    added = 0
    ver = kv_get("cli_version")
    if new:
        body = "New options in `claude --help`:\n\n" + "\n".join(f"- `{cur[f]}`" for f in new)
        added += add_notice(f"cli-new:{','.join(new)}", "cli", f"New CLI option{'s' if len(new) > 1 else ''}: {', '.join(new)}",
                            body, ver, any(RELEVANT_RE.search(cur[f]) for f in new))
    if gone:
        broken = [f for f in gone if f in USED_FLAGS]
        body = "Options no longer in `claude --help`:\n\n" + "\n".join(f"- `{f}`" for f in gone)
        if broken:
            body += "\n\n⚠ **This dashboard uses " + ", ".join(f"`{f}`" for f in broken) + "** — chatting may break."
        added += add_notice(f"cli-gone:{','.join(gone)}", "cli",
                            ("⚠ " if broken else "") + f"Removed CLI option{'s' if len(gone) > 1 else ''}: {', '.join(gone)}",
                            body, ver, True)
    return added


def check_session_format():
    """Notice record/content types the dashboard doesn't know. Only reports each type once."""
    for d in PROJECTS_DIR.glob("*/"):
        for f in d.glob("*.jsonl"):
            session_meta(f)
    seen = {"records": {}, "blocks": {}}
    for path, t in _types_cache.items():
        for kind in seen:
            for typ, ex in t[kind].items():
                if typ and typ not in seen[kind]:
                    seen[kind][typ] = (ex, path)
    reported = set(kv_get("format_reported", []))
    added = 0
    for kind, known in (("records", KNOWN_RECORDS), ("blocks", KNOWN_BLOCKS)):
        for typ, (ex, path) in sorted(seen[kind].items()):
            key = f"{kind}:{typ}"
            if typ in known or key in reported:
                continue
            reported.add(key)
            what = "session record type" if kind == "records" else "message content block type"
            body = (f"Claude Code wrote a {what} **`{typ}`** that this dashboard doesn't handle yet.\n\n"
                    f"First seen in `{tilde_path(path)}`:\n\n```json\n{ex}\n```\n\n"
                    + ("It is ignored today; consider showing it in the transcript." if kind == "blocks"
                       else "It is ignored today; check whether it carries info worth showing (titles, costs, status…)."))
            added += add_notice(f"format:{key}", "format", f"New {what}: {typ}", body, kv_get("cli_version"), True)
    kv_set("format_reported", sorted(reported))
    return added


def tilde_path(p):
    return str(p).replace(str(HOME), "~", 1)


def check_news():
    """Run every check; errors in one don't stop the others."""
    if not NEWS_LOCK.acquire(blocking=False):
        return {"added": 0, "busy": True}
    _news_state["checking"] = True
    added, errors = 0, []
    try:
        for fn in (check_version, check_changelog, check_cli_flags, check_session_format):
            try:
                added += fn()
            except Exception as e:  # keep going; report at the end
                errors.append(f"{fn.__name__}: {e}")
        _news_state.update(last=time.time(), error="; ".join(errors) or None)
        kv_set("news_last_check", _news_state["last"])
    finally:
        _news_state["checking"] = False
        NEWS_LOCK.release()
    return {"added": added, "errors": errors}


def news_loop():
    time.sleep(3)
    while True:
        last = kv_get("news_last_check", 0) or 0
        if time.time() - last >= CHECK_EVERY - 60:
            check_news()
        time.sleep(600)


def list_notices():
    with DB_LOCK, db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, source, title, body, version, relevant, created, read, analysis FROM notices"
            " WHERE deleted=0 ORDER BY created DESC, id DESC")]
    rows.sort(key=lambda r: (r["created"] // 60, _vtuple(r["version"]), r["id"]), reverse=True)
    return {"items": rows, "unread": sum(1 for r in rows if not r["read"]),
            "lastCheck": _news_state["last"] or kv_get("news_last_check"),
            "checking": _news_state["checking"], "error": _news_state["error"]}


def notice_update(b):
    ids = [int(i) for i in (b.get("ids") or [b["id"]])]
    marks = ",".join("?" * len(ids))
    with DB_LOCK, db() as c:
        if "read" in b:
            c.execute(f"UPDATE notices SET read=? WHERE id IN ({marks})", (int(bool(b["read"])), *ids))
        if b.get("delete"):
            c.execute(f"UPDATE notices SET deleted=1, read=1 WHERE id IN ({marks})", ids)
        if b.get("restore"):  # undo a delete
            c.execute(f"UPDATE notices SET deleted=0 WHERE id IN ({marks})", ids)
    return {"ok": True}


CHECK_VERDICTS = ("fits", "caution", "conflict", "duplicate")


def session_context(sid, max_chars=7000):
    """Recent conversation of a chat, plus what Claude is doing right now if a reply is running."""
    lines = []
    try:
        for m in read_transcript(sid)["messages"][-40:]:
            who = "USER" if m["role"] == "user" else "CLAUDE"
            for b in m["blocks"]:
                if b["type"] == "text" and b.get("text", "").strip():
                    lines.append(f"{who}: {b['text'].strip()[:800]}")
                elif b["type"] == "tool_use":
                    lines.append(f"CLAUDE used {b.get('name')}: {json.dumps(b.get('input'))[:220]}")
    except ValueError:
        pass
    history = "\n".join(lines)[-max_chars:]
    live = ""
    with RUNS_LOCK:
        run = next((r for r in CHAT_RUNS.values() if r.session_id == sid and not r.done), None)
    if run:
        with run.cond:
            events = list(run.events)
        parts, partial = [], ""
        for ev in events:
            t = ev.get("type")
            if t == "assistant":
                partial = ""
                for b in (ev.get("message") or {}).get("content") or []:
                    if b.get("type") == "text" and b.get("text", "").strip():
                        parts.append("CLAUDE: " + b["text"].strip()[:800])
                    elif b.get("type") == "tool_use":
                        parts.append(f"CLAUDE is using {b.get('name')}: {json.dumps(b.get('input'))[:220]}")
            elif t == "stream_event":
                d = (ev.get("event") or {}).get("delta") or {}
                if d.get("type") == "text_delta":
                    partial += d.get("text", "")
        if partial.strip():
            parts.append("CLAUDE (writing now): " + partial.strip()[-800:])
        live = f"The session is RUNNING right now. The user asked: {run.prompt[:600]!r}\n" + "\n".join(parts)[-3000:]
    return history, live


def note_check(nid):
    """Ask Claude (Haiku, no tools, not saved) whether running this to-do next would clash with what the
    chat is doing or has decided. Stores and returns {verdict, reason, suggestion}."""
    with DB_LOCK, db() as c:
        n = c.execute("SELECT * FROM session_notes WHERE id=?", (nid,)).fetchone()
        if not n:
            raise ValueError("note not found")
        queued = [r["text"] for r in c.execute(
            "SELECT text FROM session_notes WHERE sid=? AND status='queued' AND id<>? ORDER BY position, id", (n["sid"], nid))]
    history, live = session_context(n["sid"])
    prompt = (
        "A developer keeps a to-do list next to an ongoing Claude Code session and wants to send one item to that "
        "session as its NEXT prompt. Judge whether that would clash with what the session is doing or has decided.\n\n"
        f"## Recent conversation (oldest first, shortened)\n{history or '(no messages yet)'}\n\n"
        + (f"## In progress right now\n{live}\n\n" if live else "")
        + ("## Already queued to run before it\n" + "\n".join(f"{i + 1}. {q[:300]}" for i, q in enumerate(queued)) + "\n\n" if queued else "")
        + f"## The to-do to check\n{n['text'][:2000]}\n\n"
        "Answer with ONLY a JSON object, no other text:\n"
        '{"verdict": "fits" | "caution" | "conflict" | "duplicate", "reason": "<one short sentence>", "suggestion": "<one short sentence, or empty>"}\n'
        "- fits: independent of the current work, or its natural next step\n"
        "- caution: depends on work that isn't finished, or should wait until the current turn ends\n"
        "- conflict: contradicts, undoes or competes with the current direction or a decision made in the conversation "
        "(e.g. changes the same code another way, reverses an agreed choice)\n"
        "- duplicate: the session already did this or is doing it now")
    p = subprocess.run([CLAUDE_BIN, "-p", "--model", ANALYZE_MODEL, "--tools", "", "--no-session-persistence"],
                       input=prompt, capture_output=True, text=True, timeout=120, cwd=str(HERE))
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError((p.stderr or p.stdout).strip()[-400:] or "claude failed")
    m = re.search(r"\{.*\}", p.stdout, re.S)
    try:
        ans = json.loads(m.group(0)) if m else {}
    except ValueError:
        ans = {}
    verdict = ans.get("verdict") if ans.get("verdict") in CHECK_VERDICTS else "caution"
    reason = str(ans.get("reason") or ("Couldn't read Claude's answer: " + p.stdout.strip()[:200]))[:400]
    suggestion = str(ans.get("suggestion") or "")[:400]
    with DB_LOCK, db() as c:
        c.execute("UPDATE session_notes SET check_verdict=?, check_reason=?, check_suggest=?, checked=? WHERE id=?",
                  (verdict, reason, suggestion, time.time(), nid))
        return dict(c.execute("SELECT * FROM session_notes WHERE id=?", (nid,)).fetchone())


def notice_analyze(nid):
    """Ask Claude (no tools, not saved as a session) what a notice means for this dashboard."""
    with DB_LOCK, db() as c:
        r = c.execute("SELECT title, body FROM notices WHERE id=?", (nid,)).fetchone()
    if not r:
        raise ValueError("notice not found")
    prompt = (f"{DASHBOARD_BRIEF}\n\nHere is a Claude Code change notice:\n\n### {r['title']}\n{r['body']}\n\n"
              "For the developer of this dashboard, answer in short markdown bullets under two headings:\n"
              "**Needs attention** — anything that could break the dashboard or require a code change.\n"
              "**Could add** — new features the dashboard could use or show.\n"
              "Only mention items that genuinely relate to what the dashboard does. "
              "If nothing is relevant, reply exactly: No impact on the dashboard.")
    p = subprocess.run([CLAUDE_BIN, "-p", "--model", ANALYZE_MODEL, "--tools", "", "--no-session-persistence"],
                       input=prompt, capture_output=True, text=True, timeout=180, cwd=str(HERE))
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError((p.stderr or p.stdout).strip()[-500:] or "claude failed")
    text = p.stdout.strip()
    with DB_LOCK, db() as c:
        c.execute("UPDATE notices SET analysis=? WHERE id=?", (text, nid))
    return {"analysis": text}


class ChatRun:
    """One `claude -p` reply. A background thread reads its output into `events`; any number of page
    connections can follow it (from any point), and it keeps running if they all disconnect."""

    def __init__(self, proc, cwd, session_id, prompt):
        self.id, self.proc, self.cwd, self.session_id = uuid.uuid4().hex, proc, cwd, session_id
        self.prompt, self.started, self.ended, self.done = prompt[:300], time.time(), None, False
        self.events, self.cond = [], threading.Condition()

    def add(self, ev):
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def pump(self):
        err = []
        threading.Thread(target=lambda: err.append(self.proc.stderr.read()), daemon=True).start()
        self.add({"type": "ui_run", "run": self.id})
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                ev = {"type": "ui_raw", "text": line}
            if ev.get("type") == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
                self.session_id = ev["session_id"]
            self.add(ev)
        self.proc.wait()
        time.sleep(0.05)
        if self.proc.returncode != 0:
            self.add({"type": "ui_error", "code": self.proc.returncode, "text": "".join(err)[-4000:]})
        with self.cond:
            self.done, self.ended = True, time.time()
            self.cond.notify_all()
        with RUNS_LOCK:
            RUNS.pop(self.id, None)

    def info(self):
        return {"run": self.id, "sessionId": self.session_id, "cwd": self.cwd, "prompt": self.prompt,
                "started": self.started, "ended": self.ended, "done": self.done, "events": len(self.events)}


def list_runs():
    """Runs still going, plus ones that finished in the last RUN_KEEP seconds (older ones are forgotten)."""
    now = time.time()
    with RUNS_LOCK:
        for rid in [r for r, run in CHAT_RUNS.items() if run.done and now - run.ended > RUN_KEEP]:
            del CHAT_RUNS[rid]
        return [run.info() for run in CHAT_RUNS.values()]


_usage = {"t": 0, "data": None, "error": None}
_usage_lock = threading.Lock()


def profile_info(force=False):
    """Who is logged in to Claude Code (from ~/.claude.json) and the plan usage limits from /usage.
    /usage is answered locally by Claude Code (no model call), so it costs nothing; cached for a minute."""
    acct = {}
    try:
        a = json.loads((HOME / ".claude.json").read_text()).get("oauthAccount") or {}
        acct = {"name": a.get("displayName") or a.get("fullName"), "fullName": a.get("fullName"), "email": a.get("emailAddress"),
                "org": a.get("organizationName"), "role": a.get("organizationRole"), "billing": a.get("billingType"),
                "extraUsage": a.get("hasExtraUsageEnabled")}
    except (OSError, ValueError):
        pass
    with _usage_lock:
        if force or time.time() - _usage["t"] > 60:
            data, err = None, None
            try:
                p = subprocess.run([CLAUDE_BIN, "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence"],
                                   input="/usage", capture_output=True, text=True, timeout=60, cwd=str(HOME))
                auth = None
                for line in p.stdout.splitlines():
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("type") == "system" and ev.get("subtype") == "init":
                        auth = ev.get("apiKeySource")
                    rep = ev.get("usage_report")
                    if rep:
                        rl = rep.get("rate_limits") or {}
                        data = {"limits": rl.get("limits") or [], "extra": rl.get("extra_usage"), "auth": auth}
                if data is None:
                    err = (p.stderr or p.stdout).strip()[-300:] or "no usage information in /usage"
            except (OSError, subprocess.SubprocessError) as e:
                err = str(e)
            _usage.update(t=time.time(), data=data or _usage["data"], error=err)
        return {**acct, **(_usage["data"] or {"limits": []}), "usageAt": _usage["t"], "usageError": _usage["error"]}


_tok_cache = {}  # path -> (mtime, size, rows)


def _usage_rows(path: Path):
    """One row per model call in a transcript: (time, message id, model, input, output, cache write, cache read).
    Claude Code writes one record per content block, all carrying the same usage, so each message id counts once."""
    st = path.stat()
    c = _tok_cache.get(str(path))
    if c and c[0] == st.st_mtime and c[1] == st.st_size:
        return c[2]
    rows, seen = [], set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"usage"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            m = o.get("message")
            if o.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                continue
            mid = m.get("id") or o.get("requestId") or o.get("uuid")
            ts = _iso_ts(o.get("timestamp"))
            if mid in seen or not ts or m.get("model") == "<synthetic>":
                continue
            seen.add(mid)
            u = m["usage"]
            rows.append((ts, mid, m.get("model") or "?", int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                         int(u.get("cache_creation_input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0)))
    _tok_cache[str(path)] = (st.st_mtime, st.st_size, rows)
    return rows


def token_usage(sid=None):
    """Tokens used across every session (subagents included) in three windows: the plan's current 5-hour window,
    today, and the plan's current week. The windows follow the reset times from /usage when known, so they line up
    with the limit percentages; otherwise they roll (last 5 hours, last 7 days). Also a per-day series for 7 days."""
    now = time.time()
    lims = {l.get("kind"): _iso_ts(l.get("resets_at")) for l in ((_usage["data"] or {}).get("limits") or [])}
    end5, endw = lims.get("session"), lims.get("weekly_all")
    a5, aw = bool(end5 and end5 > now), bool(endw and endw > now)
    s5 = end5 - 5 * 3600 if a5 else now - 5 * 3600
    sw = endw - 7 * 86400 if aw else now - 7 * 86400
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    sd = today.timestamp()
    day_starts = [datetime.fromtimestamp(sd - i * 86400).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() for i in range(6, -1, -1)]
    wins = {"session": {"from": s5, "to": end5 if a5 else None, "aligned": a5},
            "today": {"from": sd, "to": sd + 86400, "aligned": True},
            "week": {"from": sw, "to": endw if aw else None, "aligned": aw}}
    for w in wins.values():
        w.update(input=0, output=0, cacheWrite=0, cacheRead=0, calls=0, models={}, sessions={})
    days = [{"day": t, "input": 0, "output": 0, "cacheWrite": 0, "cacheRead": 0} for t in day_starts]
    earliest = min(sw, day_starts[0])
    seen = set()
    if PROJECTS_DIR.is_dir():
        for d in PROJECTS_DIR.iterdir():
            if not d.is_dir():
                continue
            for f in [*d.glob("*.jsonl"), *d.glob("*/subagents/*.jsonl")]:
                try:
                    if f.stat().st_mtime < earliest:
                        continue
                    rows = _usage_rows(f)
                except OSError:
                    continue
                owner = f.stem if f.parent == d else f.parent.parent.name  # subagent tokens count for their chat
                for ts, mid, model, i, o, cw, cr in rows:
                    if ts < earliest or mid in seen:  # a forked session repeats its parent's messages
                        continue
                    seen.add(mid)
                    for w in wins.values():
                        if ts >= w["from"]:
                            w["input"] += i; w["output"] += o; w["cacheWrite"] += cw; w["cacheRead"] += cr; w["calls"] += 1
                            w["models"][model] = w["models"].get(model, 0) + i + o + cw + cr
                            ss = w["sessions"].setdefault(owner, {"id": owner, "project": d.name, "tokens": 0, "output": 0})
                            ss["tokens"] += i + o + cw + cr; ss["output"] += o
                    for day in reversed(days):
                        if ts >= day["day"]:
                            day["input"] += i; day["output"] += o; day["cacheWrite"] += cw; day["cacheRead"] += cr
                            break
    out = {}
    for k, w in wins.items():
        ranked = sorted(w["sessions"].values(), key=lambda x: -x["tokens"])
        top = ranked[:8] + [x for x in ranked[8:] if x["id"] == sid]  # keep the chat on screen even when it's small
        for x in top:
            try:
                m = session_meta(PROJECTS_DIR / x["project"] / (x["id"] + ".jsonl"))
                x.update(title=m["title"], cwd=m["cwd"])
            except OSError:
                x.update(title=None, cwd=decode_dir_name(x["project"]))
            x["rank"] = ranked.index(x) + 1
        out[k] = {**{f: w[f] for f in ("from", "to", "aligned", "input", "output", "cacheWrite", "cacheRead", "calls")},
                  "total": w["input"] + w["output"] + w["cacheWrite"] + w["cacheRead"],
                  "models": sorted(({"model": a, "tokens": b} for a, b in w["models"].items()), key=lambda x: -x["tokens"]),
                  "sessions": top, "sessionCount": len(ranked)}
    return {"windows": out, "days": days, "at": now}


MODEL_CATALOG_DIR = HOME / ".claude" / "cache" / "model-catalog"


def model_catalog():
    """The models Claude Code offers (its own cached catalog, the same list as /model), with descriptions,
    badges and each model's effort levels."""
    data = None
    for f in sorted(MODEL_CATALOG_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text())
            models = data["catalog"]["config"]["models"]
            break
        except (OSError, ValueError, KeyError, TypeError):
            data = None
    if not data:
        return {"models": [], "error": "Claude Code hasn't downloaded its model list yet — run claude once"}
    ver = _vtuple(kv_get("cli_version") or "")
    out = []
    for m in models:
        if m.get("min_claude_code_version") and ver and _vtuple(m["min_claude_code_version"]) > ver:
            continue  # needs a newer Claude Code than the one installed
        th = m.get("thinking") or {}
        out.append({
            "id": m["id"], "name": m.get("name") or m["id"], "short": m.get("short_name") or (m.get("name") or m["id"]).split()[0],
            "description": m.get("description") or "", "section": m.get("section") or "overflow",
            "badge": (m.get("badge") or {}).get("message"), "tip": (m.get("tooltip") or {}).get("content"),
            "notice": (m.get("notice") or {}).get("text"), "effortHelp": th.get("description"),
            "efforts": [{"id": o["id"], "name": o.get("name") or o["id"], "tip": (o.get("tooltip") or {}).get("content"),
                         "recommended": (o.get("badge") or {}).get("message") == "Recommended"} for o in th.get("effort_options") or []],
            "fast": bool(m.get("fast_mode")),
        })
    default = None
    try:
        default = json.loads((HOME / ".claude" / "settings.json").read_text()).get("model")
    except (OSError, ValueError):
        pass
    return {"models": out, "default": default, "fetchedAt": data.get("fetchedAt")}


def server_status():
    """For the ⏻ button: when this process started and whether server.py changed since (restart needed)."""
    code_mtime = os.path.getmtime(__file__)
    with RUNS_LOCK:
        running = len(RUNS)
    return {"started": STARTED, "pid": os.getpid(), "codeChanged": code_mtime > STARTED, "codeTime": code_mtime,
            "running": running, "service": _is_service(), "desktop": desktop_supported()}


def _is_service():
    """True when this process is the main process of the `workbench` systemd user service."""
    try:
        r = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", "workbench"],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() == str(os.getpid())
    except (OSError, subprocess.SubprocessError):
        return False


def restart_soon():
    """Replace this process with a fresh copy of itself: same PID, port and terminal, so it works the same when
    started by hand or as the systemd service. Running chats and commands are stopped first."""
    def go():
        time.sleep(0.4)  # let the HTTP response go out
        with RUNS_LOCK:
            runs = list(RUNS.values())
        for p in runs:
            kill_run(p)
        print("Restarting Claude Workbench…", flush=True)
        os.execv(sys.executable, [sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]])
    threading.Thread(target=go, daemon=True).start()


def kill_run(p):
    """Stop a chat or shell run; shell runs have their own process group, so kill all of it."""
    try:
        if os.getpgid(p.pid) == p.pid:
            os.killpg(p.pid, signal.SIGTERM)
        else:
            p.terminate()
    except (ProcessLookupError, PermissionError):
        pass


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the page went away (reload/close) before the answer arrived: nothing to do

    def _auth(self):
        if not self._host_ok() or not secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN):
            self._send(403, {"error": "forbidden"})
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            html = (HERE / "index.html").read_text().replace("__TOKEN__", TOKEN)
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if not self._auth():
            return
        try:
            if u.path == "/api/projects":
                return self._send(200, list_projects())
            if u.path == "/api/sessions":
                if q.get("root"):
                    return self._send(200, list_sessions_under(q["root"]))
                return self._send(200, list_sessions(q["project"]))
            if u.path == "/api/worktree/preview":
                return self._send(200, worktree_preview(q["cwd"], q["source"], q["target"]))
            if u.path == "/api/session":
                return self._send(200, read_transcript(q["id"]))
            if u.path == "/api/repo":
                return self._send(200, repo_info(q["cwd"]))
            if u.path == "/api/browse":
                return self._send(200, browse(q.get("path")))
            if u.path == "/api/profile":
                return self._send(200, profile_info(q.get("force") == "1"))
            if u.path == "/api/tokens":
                return self._send(200, token_usage(q.get("sid")))
            if u.path == "/api/models":
                return self._send(200, model_catalog())
            if u.path == "/api/status":
                return self._send(200, server_status())
            if u.path == "/api/notes":
                return self._send(200, notes_list(q.get("sid")))
            if u.path == "/api/notes/counts":
                return self._send(200, notes_counts())
            if u.path == "/api/runs":
                return self._send(200, list_runs())
            if u.path == "/api/run/stream":
                run = CHAT_RUNS.get(q.get("run", ""))
                if not run:
                    raise ValueError("run not found (finished more than 15 minutes ago?)")
                return self._follow(run, int(q.get("from", 0)))
            if u.path == "/api/meta":
                return self._send(200, meta_all())
            if u.path == "/api/notices":
                return self._send(200, list_notices())
            self._send(404, {"error": "not found"})
        except (ValueError, KeyError, RuntimeError, OSError) as e:
            self._send(400, {"error": str(e)})

    def do_POST(self):
        if not self._auth():
            return
        u = urlparse(self.path)
        try:
            b = self._body()
            if u.path == "/api/chat":
                return self._chat(b)
            if u.path == "/api/shell":
                return self._shell(b)
            if u.path == "/api/restart":
                restart_soon()
                return self._send(200, {"ok": True})
            if u.path == "/api/stop":
                with RUNS_LOCK:
                    p = RUNS.get(b.get("run"))
                if p:
                    kill_run(p)
                return self._send(200, {"ok": bool(p)})
            if u.path == "/api/worktree":
                return self._send(200, create_worktree(b["cwd"], b["name"], b.get("branch"),
                                                       b.get("base")))
            if u.path == "/api/worktree/apply":
                return self._send(200, worktree_apply(b))
            if u.path == "/api/worktree/remove":
                return self._send(200, remove_worktree(b["cwd"], b["path"], b.get("force")))
            if u.path == "/api/open-desktop":
                return self._send(200, open_desktop(b.get("cwd"), b.get("sessionId")))
            if u.path == "/api/open-terminal":  # "open shell" button: a terminal window in that folder
                return self._send(200, open_shell(b["cwd"]))
            if u.path == "/api/session/remove":
                return self._send(200, session_remove(b.get("id")))
            if u.path == "/api/session/restore":
                return self._send(200, session_restore(b.get("id")))
            if u.path == "/api/session/rename":
                return self._send(200, rename_session(b.get("id"), b.get("title")))
            if u.path == "/api/note":
                return self._send(200, note_save(b))
            if u.path == "/api/note/check":
                return self._send(200, note_check(int(b["id"])))
            if u.path == "/api/chatmeta":
                return self._send(200, chat_set(b))
            if u.path == "/api/folder":
                return self._send(200, folder_set(b))
            if u.path == "/api/group":
                return self._send(200, group_save(b))
            if u.path == "/api/pin":
                return self._send(200, pin_set(b))
            if u.path == "/api/group/move":
                return self._send(200, group_move(int(b["id"]), int(b["dir"])))
            if u.path == "/api/group/delete":
                return self._send(200, delete_row("groups", int(b["id"])))
            if u.path == "/api/tag":
                return self._send(200, tag_save(b))
            if u.path == "/api/tag/delete":
                return self._send(200, delete_row("tags", int(b["id"])))
            if u.path == "/api/notices/check":
                return self._send(200, {**check_news(), **list_notices()})
            if u.path == "/api/notice":
                return self._send(200, notice_update(b))
            if u.path == "/api/notice/analyze":
                return self._send(200, notice_analyze(int(b["id"])))
            self._send(404, {"error": "not found"})
        except (ValueError, KeyError, RuntimeError, OSError, sqlite3.Error) as e:
            self._send(400, {"error": str(e)})

    def _stream_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def emit(obj):
            data = (json.dumps(obj) + "\n").encode()
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()
        return emit

    def _shell(self, b):
        """`!command` from the chat box: run it in bash in the chat's folder, like Claude Code's
        shell mode, and stream stdout/stderr. No terminal is attached, so interactive programs
        (sudo password prompts, vim, less) can't be used."""
        cwd, command = b["cwd"], b.get("cmd", "")
        if not os.path.isdir(cwd):
            raise ValueError(f"directory does not exist: {cwd}")
        if not command.strip():
            raise ValueError("empty command")
        env = {**os.environ, "TERM": "dumb", "NO_COLOR": "1", "PAGER": "cat", "GIT_PAGER": "cat"}
        # report the folder the command ended in, so `cd` carries over to the next command like a real shell
        cwd_r, cwd_w = os.pipe()
        script = f"trap 'pwd >&{cwd_w}' EXIT\n{command}"
        p = subprocess.Popen(["bash", "-c", script], cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, pass_fds=(cwd_w,),
                             start_new_session=True)  # own process group, so Stop kills children too
        os.close(cwd_w)
        run_id = uuid.uuid4().hex
        with RUNS_LOCK:
            RUNS[run_id] = p
        q = queue.Queue()

        def pump(stream, name):
            for chunk in iter(lambda: stream.read1(8192), b""):
                q.put((name, chunk.decode("utf-8", "replace")))
            q.put((name, None))

        for s, n in ((p.stdout, "stdout"), (p.stderr, "stderr")):
            threading.Thread(target=pump, args=(s, n), daemon=True).start()
        emit = self._stream_start()
        started, sent, cap, open_streams = time.time(), 0, 200_000, 2
        try:
            emit({"type": "ui_run", "run": run_id})
            while open_streams:
                name, text = q.get()
                if text is None:
                    open_streams -= 1
                elif sent < cap:
                    text = text[:cap - sent]
                    sent += len(text)
                    emit({"type": "shell_out", "stream": name, "text": text})
                    if sent >= cap:
                        emit({"type": "shell_out", "stream": "stderr", "text": "\n… output truncated (200 KB shown)\n"})
            code = p.wait()
            end_cwd = cwd
            if select.select([cwd_r], [], [], 0.3)[0]:  # a background job may still hold the pipe: don't block
                end_cwd = os.read(cwd_r, 4096).decode("utf-8", "replace").strip() or cwd
            emit({"type": "shell_exit", "code": code, "seconds": round(time.time() - started, 2),
                  "cwd": end_cwd if os.path.isdir(end_cwd) else cwd})
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            kill_run(p)
        finally:
            os.close(cwd_r)
            with RUNS_LOCK:
                RUNS.pop(run_id, None)

    def _chat(self, b):
        cwd = b["cwd"]
        if not os.path.isdir(cwd):
            raise ValueError(f"directory does not exist: {cwd}")
        cmd = [CLAUDE_BIN, "-p", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages"]
        if b.get("sessionId"):
            find_session_file(b["sessionId"])
            cmd += ["--resume", b["sessionId"]]
        mode = b.get("permissionMode")
        if mode in ("default", "acceptEdits", "auto", "plan", "bypassPermissions", "dontAsk"):
            cmd += ["--permission-mode", mode]
        model = b.get("model")
        if model and re.fullmatch(r"[A-Za-z0-9._\[\]-]+", model):
            cmd += ["--model", model]
        if b.get("effort") in ("low", "medium", "high", "xhigh", "max"):
            cmd += ["--effort", b["effort"]]
        p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, bufsize=1)
        p.stdin.write(b["prompt"])
        p.stdin.close()
        run = ChatRun(p, cwd, b.get("sessionId"), b["prompt"])
        with RUNS_LOCK:
            RUNS[run.id] = p
            CHAT_RUNS[run.id] = run
        threading.Thread(target=run.pump, daemon=True).start()
        self._follow(run, 0)

    def _follow(self, run, start):
        """Stream a run's events from `start` until it ends. If the page goes away, only this connection
        ends — Claude keeps working and a reloaded page can follow the run again."""
        emit = self._stream_start()
        i = max(0, start)
        try:
            while True:
                with run.cond:
                    while i >= len(run.events) and not run.done:
                        run.cond.wait(timeout=15)
                    batch, finished = run.events[i:], run.done
                for ev in batch:
                    emit(ev)
                i += len(batch)
                if finished and i >= len(run.events):
                    break
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    db_init()
    threading.Thread(target=news_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"Claude UI running at http://127.0.0.1:{a.port}/  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
