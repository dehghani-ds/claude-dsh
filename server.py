#!/usr/bin/env python3
"""Local web UI for Claude Code: browse sessions, chat, manage git worktrees.

Run:  python3 server.py [--port 8765]
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
-- news inbox: Claude Code changes. Deleted notices are kept (deleted=1) so they are never re-added.
CREATE TABLE IF NOT EXISTS notices (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, source TEXT NOT NULL, title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '', version TEXT, relevant INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL, read INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
  analysis TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
        for r in c.execute("SELECT cwd, group_id FROM folders"):
            folders[r["cwd"]] = {"group": r["group_id"], "tags": []}
        for r in c.execute("SELECT cwd, tag_id FROM folder_tags"):
            folders.setdefault(r["cwd"], {"group": None, "tags": []})["tags"].append(r["tag_id"])
    return {"groups": groups, "tags": tags, "folders": folders, "colors": COLORS}


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
        # drop tags nobody uses any more
        c.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM folder_tags)")
    return {"ok": True}


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


def read_transcript(session_id):
    f = find_session_file(session_id)
    msgs = []
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
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
        w["dirty"] = bool(s and s.stdout.strip())
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
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

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
                return self._send(200, list_sessions(q["project"]))
            if u.path == "/api/session":
                return self._send(200, read_transcript(q["id"]))
            if u.path == "/api/repo":
                return self._send(200, repo_info(q["cwd"]))
            if u.path == "/api/browse":
                return self._send(200, browse(q.get("path")))
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
            if u.path == "/api/stop":
                with RUNS_LOCK:
                    p = RUNS.get(b.get("run"))
                if p:
                    kill_run(p)
                return self._send(200, {"ok": bool(p)})
            if u.path == "/api/worktree":
                return self._send(200, create_worktree(b["cwd"], b["name"], b.get("branch"),
                                                       b.get("base")))
            if u.path == "/api/worktree/remove":
                return self._send(200, remove_worktree(b["cwd"], b["path"], b.get("force")))
            if u.path == "/api/shell":
                return self._send(200, open_shell(b["cwd"]))
            if u.path == "/api/folder":
                return self._send(200, folder_set(b))
            if u.path == "/api/group":
                return self._send(200, group_save(b))
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
        run_id = uuid.uuid4().hex
        p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, bufsize=1)
        with RUNS_LOCK:
            RUNS[run_id] = p
        p.stdin.write(b["prompt"])
        p.stdin.close()

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def emit(obj):
            data = (json.dumps(obj) + "\n").encode()
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        try:
            emit({"type": "ui_run", "run": run_id})
            for line in p.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    emit(json.loads(line))
                except ValueError:
                    emit({"type": "ui_raw", "text": line})
            p.wait()
            err = p.stderr.read()
            if p.returncode != 0:
                emit({"type": "ui_error", "code": p.returncode, "text": err[-4000:]})
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            p.terminate()
        finally:
            with RUNS_LOCK:
                RUNS.pop(run_id, None)


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
