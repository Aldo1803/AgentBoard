# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

"""AgentBoard - a single-file local server that hands cards to coding agents.

Run:
  uv run agentboard.py serve      start the board on http://127.0.0.1:4300 and sync every 60 s
  uv run agentboard.py status     print the board as text, without a server
  uv run agentboard.py selftest   run the built-in tests

Agents start at GET /agent; people look at /.
"""

import argparse
import ast
import contextlib
import http.client
import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape as esc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.2.0"
DEFAULT_PORT = 4300
DEFAULT_BASE = "main"
DEFAULT_REMOTE = "origin"
STALE_SECONDS = 4 * 3600
SYNC_SECONDS = 60
CARD_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,32}$")
COLUMNS = [
    ("waiting", "Waiting"),
    ("ready", "Ready"),
    ("claimed", "Claimed"),
    ("in_review", "In review"),
    ("blocked", "Blocked"),
    ("done", "Done"),
]
STATUS_FILTERS = {"ready", "waiting", "todo", "claimed", "in_review", "blocked", "done"}
IS_WINDOWS = sys.platform == "win32"


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class GitError(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def iso_of(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso():
    return iso_of(utcnow())


def parse_iso(value):
    if not value:
        return None
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def add_seconds(value, seconds):
    dt = parse_iso(value)
    if dt is None:
        return value or ""
    return iso_of(dt + timedelta(seconds=seconds))


def ago(value, now=None):
    dt = parse_iso(value)
    if dt is None:
        return "?"
    secs = int(((now or utcnow()) - dt).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        hours, minutes = divmod(secs // 60, 60)
        return f"{hours}h {minutes}m ago"
    return f"{secs // 86400}d ago"


def is_stale(last_event_iso, now=None):
    dt = parse_iso(last_event_iso)
    if dt is None:
        return False
    return ((now or utcnow()) - dt).total_seconds() > STALE_SECONDS


def run_git(args, cwd, timeout=60, check=False):
    cmd = ["git", *args]
    kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "cwd": str(cwd),
        "timeout": timeout,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(cmd, **kwargs)
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no output").strip()
        raise GitError(f"git {' '.join(args)} failed: {detail}")
    return proc


def find_repo_root(path):
    proc = run_git(["rev-parse", "--show-toplevel"], path)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise GitError(f"{path} is not inside a git repository")
    return Path(proc.stdout.strip()).resolve()


def shell_token(value):
    if re.search(r"\s", value):
        return f'"{value}"'
    return value


class GitFacts:
    def __init__(self, repo, base, remote):
        self.repo = Path(repo).resolve()
        self.base = base
        self.remote = remote

    def common_dir(self):
        proc = run_git(["rev-parse", "--git-common-dir"], self.repo, check=True)
        raw = proc.stdout.strip()
        path = Path(raw)
        if not path.is_absolute():
            path = self.repo / path
        return path.resolve()

    def fetch(self, *refs, timeout=120):
        run_git(["fetch", "--quiet", self.remote, *refs], self.repo, timeout=timeout, check=True)

    def fetch_card_branches(self, timeout=120):
        """Refresh every remote card/* branch, so merge detection sees heads pushed after a report.

        A wildcard refspec never fails on a missing branch, and nothing is pruned: a branch deleted
        on the remote after its merge keeps its last tracking ref, which is what detection needs.
        """
        run_git(
            ["fetch", "--quiet", "--no-prune", self.remote,
             f"+refs/heads/card/*:refs/remotes/{self.remote}/card/*"],
            self.repo, timeout=timeout, check=True,
        )

    def base_ref(self):
        return f"refs/remotes/{self.remote}/{self.base}"

    def tracking_ref(self, branch):
        return f"refs/remotes/{self.remote}/{branch}"

    def branch_on_remote(self, branch):
        try:
            proc = run_git(["ls-remote", "--heads", self.remote, branch], self.repo, timeout=30)
        except GitError:
            return False
        return proc.returncode == 0 and bool(proc.stdout.strip())

    def local_branch_exists(self, branch):
        proc = run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], self.repo)
        return proc.returncode == 0

    def rev_count(self, rev_range):
        proc = run_git(["rev-list", "--count", rev_range], self.repo, check=True)
        try:
            return int(proc.stdout.strip())
        except ValueError as exc:
            raise GitError(f"could not count commits in {rev_range}") from exc

    def is_ancestor(self, ancestor, descendant):
        proc = run_git(["merge-base", "--is-ancestor", ancestor, descendant], self.repo)
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        return None

    def last_commit(self, ref):
        proc = run_git(["log", "-1", "--format=%h|%cI|%s", ref], self.repo)
        if proc.returncode != 0:
            return None
        line = proc.stdout.strip()
        if not line:
            return None
        parts = line.split("|", 2)
        return {
            "sha": parts[0],
            "when": parts[1] if len(parts) > 1 else None,
            "subject": parts[2] if len(parts) > 2 else "",
        }


def worktree_commands(git, card_id):
    branch = f"card/{card_id}"
    worktree = f"../{git.repo.name}-worktrees/{card_id}"
    note = None
    try:
        if git.local_branch_exists(branch):
            commands = [
                f"git fetch {git.remote} {git.base}",
                f"git worktree add {shell_token(worktree)} {branch}",
            ]
        elif git.branch_on_remote(branch):
            commands = [
                f"git fetch {git.remote} {git.base} {branch}",
                f"git worktree add -b {branch} {shell_token(worktree)} {git.remote}/{branch}",
            ]
        else:
            commands = [
                f"git fetch {git.remote} {git.base}",
                f"git worktree add -b {branch} {shell_token(worktree)} {git.remote}/{git.base}",
            ]
    except GitError as exc:
        note = (
            f"could not check the remote for an existing branch ({exc}); "
            f"if card/{card_id} already exists, fetch it and add the worktree without -b"
        )
        commands = [
            f"git fetch {git.remote} {git.base}",
            f"git worktree add -b {branch} {shell_token(worktree)} {git.remote}/{git.base}",
        ]
    return branch, worktree, commands, note


SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    title TEXT NOT NULL,
    grp TEXT,
    body TEXT,
    scope TEXT,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo', 'claimed', 'in_review', 'blocked', 'done')),
    agent TEXT,
    branch TEXT,
    pr_url TEXT,
    blocked_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    done_at TEXT
);

CREATE TABLE IF NOT EXISTS deps (
    card_id TEXT NOT NULL REFERENCES cards(id),
    depends_on TEXT NOT NULL REFERENCES cards(id),
    PRIMARY KEY (card_id, depends_on)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_deps_card ON deps(card_id);
CREATE INDEX IF NOT EXISTS idx_deps_on ON deps(depends_on);
CREATE INDEX IF NOT EXISTS idx_events_card ON events(card_id, id);

CREATE TRIGGER IF NOT EXISTS trg_done_no_update
BEFORE UPDATE ON cards
WHEN OLD.status = 'done'
BEGIN
    SELECT RAISE(ABORT, 'done cards are permanent: refusing to change a done card');
END;

CREATE TRIGGER IF NOT EXISTS trg_done_no_delete
BEFORE DELETE ON cards
WHEN OLD.status = 'done'
BEGIN
    SELECT RAISE(ABORT, 'done cards are permanent: refusing to delete a done card');
END;

CREATE TRIGGER IF NOT EXISTS trg_events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only: refusing to change an event');
END;

CREATE TRIGGER IF NOT EXISTS trg_events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only: refusing to delete an event');
END;
"""


class Store:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = self.connect()
            try:
                conn.executescript(SCHEMA)
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            if "malformed database schema" in str(exc):
                raise SystemExit(
                    f"agentboard: {self.db_path} cannot be opened ({exc}). "
                    "It was created by an older version whose trigger SQL current SQLite rejects. "
                    "Delete that file (and any -wal/-shm siblings) and start the board again; "
                    "re-import your cards to rebuild the board."
                ) from exc
            raise

    def connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextlib.contextmanager
    def tx(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def add_event(self, conn, card_id, actor, kind, detail=None):
        conn.execute(
            "INSERT INTO events (card_id, at, actor, kind, detail) VALUES (?, ?, ?, ?, ?)",
            (card_id, now_iso(), actor, kind, json.dumps(detail or {}, ensure_ascii=False)),
        )

    @staticmethod
    def set_meta(conn, key, value):
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_row(self, card_id):
        conn = self.connect()
        try:
            return conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        finally:
            conn.close()

    def all_rows(self):
        conn = self.connect()
        try:
            return conn.execute("SELECT * FROM cards ORDER BY seq, id").fetchall()
        finally:
            conn.close()

    def dep_ids(self, card_id):
        conn = self.connect()
        try:
            return [r["depends_on"] for r in conn.execute(
                "SELECT depends_on FROM deps WHERE card_id = ? ORDER BY depends_on", (card_id,))]
        finally:
            conn.close()

    def dep_states(self, card_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT d.depends_on AS id, c.title AS title, c.status AS status "
                "FROM deps d JOIN cards c ON c.id = d.depends_on "
                "WHERE d.card_id = ? ORDER BY c.seq, d.depends_on",
                (card_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def in_review_branches(self):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT id, branch FROM cards WHERE status = 'in_review' AND branch IS NOT NULL"
            ).fetchall()
            return [(r["id"], r["branch"]) for r in rows]
        finally:
            conn.close()

    def last_event_at(self, card_id):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT MAX(at) AS at FROM events WHERE card_id = ?", (card_id,)
            ).fetchone()
            return row["at"] if row else None
        finally:
            conn.close()

    def events_for(self, card_id, limit=50):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE card_id = ? ORDER BY id DESC LIMIT ?",
                (card_id, limit),
            ).fetchall()
            return [self._event_dict(r) for r in rows]
        finally:
            conn.close()

    def events_since(self, since):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id ASC", (since,)
            ).fetchall()
            return [self._event_dict(r) for r in rows]
        finally:
            conn.close()

    def recent_events(self, limit=15):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._event_dict(r) for r in rows]
        finally:
            conn.close()

    def max_event_id(self):
        conn = self.connect()
        try:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
            return row["m"] if row else 0
        finally:
            conn.close()

    def get_meta(self, key, default=None):
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default
        finally:
            conn.close()

    @staticmethod
    def _event_dict(row):
        try:
            detail = json.loads(row["detail"]) if row["detail"] else {}
        except (TypeError, ValueError):
            detail = {"raw": row["detail"]}
        return {
            "id": row["id"],
            "card_id": row["card_id"],
            "at": row["at"],
            "actor": row["actor"],
            "kind": row["kind"],
            "detail": detail,
        }


def norm_opt(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def norm_scope_input(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise APIError(400, "`scope` must be a list of path strings, or one path string.")
    out = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def decode_scope(value):
    if not value:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def card_def_key(title, grp, body, scope, source, deps):
    return json.dumps(
        [title, grp, body, list(scope), source, sorted(deps)], ensure_ascii=False
    )


def find_cycle(graph):
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}
    stack = []

    def visit(node):
        color[node] = GRAY
        stack.append(node)
        for nxt in graph.get(node, ()):
            state = color.get(nxt, WHITE)
            if state == GRAY:
                idx = stack.index(nxt)
                return stack[idx:] + [nxt]
            if state == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in list(graph):
        if color.get(node, WHITE) == WHITE:
            found = visit(node)
            if found:
                return found
    return None


def card_summary(store, row, now=None):
    deps = store.dep_states(row["id"])
    unmet = [d for d in deps if d["status"] != "done"]
    status = row["status"]
    if status == "todo":
        state = "waiting" if unmet else "ready"
    else:
        state = status
    last = store.last_event_at(row["id"])
    stale = status == "claimed" and is_stale(last, now)
    return {
        "id": row["id"],
        "seq": row["seq"],
        "title": row["title"],
        "group": row["grp"],
        "status": status,
        "state": state,
        "agent": row["agent"],
        "branch": row["branch"],
        "pr": row["pr_url"],
        "blocked_reason": row["blocked_reason"],
        "stale": stale,
        "last_event_at": last,
        "done_at": row["done_at"],
        "updated_at": row["updated_at"],
        "created_at": row["created_at"],
        "source": row["source"],
        "depends_on": deps,
        "waits_on": unmet,
    }


def card_detail(store, row, now=None):
    detail = card_summary(store, row, now)
    detail["body"] = row["body"]
    detail["scope"] = decode_scope(row["scope"])
    detail["events"] = store.events_for(row["id"], limit=50)
    return detail


class Board:
    def __init__(self, store, git):
        self.store = store
        self.git = git
        self._sync_lock = threading.Lock()

    def list_cards(self, status=None):
        if status not in (None, "", "all") and status not in STATUS_FILTERS:
            raise APIError(
                400,
                f"Unknown status {status!r}; use ready, waiting, todo, claimed, "
                "in_review, blocked or done.",
            )
        out = []
        for row in self.store.all_rows():
            summary = card_summary(self.store, row)
            if status in (None, "", "all"):
                out.append(summary)
            elif status == "todo":
                if summary["status"] == "todo":
                    out.append(summary)
            elif summary["state"] == status:
                out.append(summary)
        return out

    def _fetch_card(self, conn, card_id):
        if not CARD_ID_RE.fullmatch(card_id or ""):
            raise APIError(
                404,
                f"{card_id!r} is not a card id (letters, digits and dashes, "
                "at most 32 characters). GET /cards lists every card.",
            )
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            raise APIError(404, f"No card {card_id}. GET /cards lists every card; check the id.")
        return row

    def claim(self, card_id, agent):
        agent = norm_opt(agent)
        if not agent:
            raise APIError(
                400,
                f"POST /cards/{card_id}/claim needs an agent name: pass agent=<your name> "
                "as a form field or JSON.",
            )
        with self.store.tx() as conn:
            row = self._fetch_card(conn, card_id)
            status = row["status"]
            if status == "done":
                raise APIError(
                    409,
                    f"{card_id} is done - done is permanent; take another card "
                    "(GET /cards?status=ready).",
                )
            if status == "claimed":
                holder = row["agent"] or "someone"
                raise APIError(
                    409,
                    f"{card_id} is already claimed by {holder} ({ago(row['updated_at'])}); "
                    "ask them to release it or take another ready card.",
                )
            if status == "in_review":
                raise APIError(
                    409,
                    f"{card_id} is in review ({row['pr_url'] or 'no PR'}); nothing to claim.",
                )
            if status == "blocked":
                raise APIError(
                    409,
                    f"{card_id} is blocked: {row['blocked_reason'] or 'no reason given'}. "
                    f"A person decides; unblock it with POST /cards/{card_id}/unblock and a note.",
                )
            unmet = conn.execute(
                "SELECT d.depends_on AS id, c.status AS status FROM deps d "
                "JOIN cards c ON c.id = d.depends_on "
                "WHERE d.card_id = ? AND c.status != 'done' ORDER BY c.seq",
                (card_id,),
            ).fetchall()
            if unmet:
                names = ", ".join(f"{r['id']} ({r['status']})" for r in unmet)
                raise APIError(
                    409,
                    f"{card_id} is waiting on {names}; it is not ready yet - take the next "
                    "ready card (GET /cards?status=ready).",
                )
            branch = f"card/{card_id}"
            cur = conn.execute(
                "UPDATE cards SET status = 'claimed', agent = ?, branch = ?, updated_at = ? "
                "WHERE id = ? AND status = 'todo'",
                (agent, branch, now_iso(), card_id),
            )
            if cur.rowcount != 1:
                raise APIError(409, f"{card_id} changed while claiming; try again.")
            self.store.add_event(conn, card_id, agent, "claimed", {"branch": branch})
        branch, worktree, commands, note = worktree_commands(self.git, card_id)
        result = {
            "id": card_id,
            "status": "claimed",
            "agent": agent,
            "branch": branch,
            "worktree": worktree,
            "repo_root": str(self.git.repo),
            "commands": commands,
        }
        if note:
            result["note"] = note
        return result

    def report(self, card_id, pr, note=None, agent=None):
        pr = norm_opt(pr)
        note = norm_opt(note)
        agent = norm_opt(agent)
        row = self.store.get_row(card_id)
        if row is None:
            if not CARD_ID_RE.fullmatch(card_id or ""):
                raise APIError(404, f"{card_id!r} is not a card id. GET /cards lists every card.")
            raise APIError(404, f"No card {card_id}. GET /cards lists every card; check the id.")
        status = row["status"]
        if status == "todo":
            raise APIError(
                409,
                f"{card_id} is not claimed yet; claim it first (POST /cards/{card_id}/claim).",
            )
        if status == "in_review":
            raise APIError(
                409, f"{card_id} is already in review ({row['pr_url'] or 'no PR'})."
            )
        if status == "blocked":
            raise APIError(
                409,
                f"{card_id} is blocked: {row['blocked_reason'] or 'no reason given'}; "
                f"unblock it first (POST /cards/{card_id}/unblock).",
            )
        if status == "done":
            raise APIError(409, f"{card_id} is done; no report needed.")
        holder = row["agent"]
        if agent and holder and agent != holder:
            raise APIError(
                409, f"{card_id} is held by {holder}, not {agent}; only the holder can report it."
            )
        if not pr:
            raise APIError(
                400, f"POST /cards/{card_id}/report needs the PR link: pass pr=<URL>."
            )
        branch = row["branch"] or f"card/{card_id}"
        try:
            self.git.fetch(self.git.base)
        except GitError as exc:
            raise APIError(
                502,
                f"Could not fetch {self.git.remote}/{self.git.base}: {exc}. "
                "Check the network and the remote, then report again.",
            ) from exc
        if not self.git.branch_on_remote(branch):
            raise APIError(
                409,
                f"Refused: {branch} is not on {self.git.remote} yet - push it "
                f"(git push -u {self.git.remote} {branch}) and report again.",
            )
        try:
            self.git.fetch(branch)
        except GitError as exc:
            raise APIError(
                502, f"Could not fetch {self.git.remote}/{branch}: {exc}. Try again."
            ) from exc
        count = self.git.rev_count(f"{self.git.base_ref()}..{self.git.tracking_ref(branch)}")
        if count < 1:
            raise APIError(
                409,
                f"Refused: {branch} has no commits beyond {self.git.remote}/{self.git.base} - "
                "commit and push, then report again.",
            )
        with self.store.tx() as conn:
            fresh = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
            if fresh is None or fresh["status"] != "claimed":
                current = fresh["status"] if fresh else "gone"
                raise APIError(
                    409,
                    f"{card_id} is {current} now; it must be claimed to report. "
                    f"Check GET /cards/{card_id}.",
                )
            conn.execute(
                "UPDATE cards SET status = 'in_review', pr_url = ?, updated_at = ? "
                "WHERE id = ? AND status = 'claimed'",
                (pr, now_iso(), card_id),
            )
            detail = {"pr": pr, "commits": count}
            if note:
                detail["note"] = note
            self.store.add_event(conn, card_id, holder or agent or "agent", "reported", detail)
        return {"id": card_id, "status": "in_review", "pr": pr, "commits": count, "note": note}

    def block(self, card_id, reason, agent=None):
        reason = norm_opt(reason)
        agent = norm_opt(agent)
        with self.store.tx() as conn:
            row = self._fetch_card(conn, card_id)
            status = row["status"]
            if status == "done":
                raise APIError(409, f"{card_id} is done - done is permanent.")
            if status != "claimed":
                raise APIError(
                    409, f"{card_id} is {status}, not claimed; only a claimed card can be blocked."
                )
            if agent and row["agent"] and agent != row["agent"]:
                raise APIError(409, f"{card_id} is held by {row['agent']}, not {agent}.")
            if not reason:
                raise APIError(
                    400, f"POST /cards/{card_id}/block needs a reason: pass reason=<why>."
                )
            conn.execute(
                "UPDATE cards SET status = 'blocked', blocked_reason = ?, updated_at = ? "
                "WHERE id = ? AND status = 'claimed'",
                (reason, now_iso(), card_id),
            )
            self.store.add_event(
                conn, card_id, agent or row["agent"] or "agent", "blocked", {"reason": reason}
            )
        return {"id": card_id, "status": "blocked", "reason": reason}

    def release(self, card_id, agent=None):
        agent = norm_opt(agent)
        with self.store.tx() as conn:
            row = self._fetch_card(conn, card_id)
            status = row["status"]
            if status == "done":
                raise APIError(409, f"{card_id} is done - done is permanent.")
            if status == "blocked":
                raise APIError(
                    409,
                    f"{card_id} is blocked: {row['blocked_reason'] or 'no reason given'}; "
                    f"only POST /cards/{card_id}/unblock with a person's note returns it to ready.",
                )
            if status != "claimed":
                raise APIError(409, f"{card_id} is {status}; nothing holds it.")
            if agent and row["agent"] and agent != row["agent"]:
                raise APIError(409, f"{card_id} is held by {row['agent']}, not {agent}.")
            holder = row["agent"]
            conn.execute(
                "UPDATE cards SET status = 'todo', agent = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'claimed'",
                (now_iso(), card_id),
            )
            self.store.add_event(conn, card_id, agent or "person", "released", {"holder": holder})
        return {"id": card_id, "status": "todo", "holder": holder}

    def unblock(self, card_id, note):
        note = norm_opt(note)
        with self.store.tx() as conn:
            row = self._fetch_card(conn, card_id)
            status = row["status"]
            if status == "done":
                raise APIError(409, f"{card_id} is done - done is permanent.")
            if status != "blocked":
                raise APIError(
                    409, f"{card_id} is {status}, not blocked; nothing to unblock."
                )
            if not note:
                raise APIError(
                    400,
                    f"POST /cards/{card_id}/unblock needs the person's note: pass note=<decision>.",
                )
            conn.execute(
                "UPDATE cards SET status = 'todo', blocked_reason = NULL, agent = NULL, "
                "updated_at = ? WHERE id = ? AND status = 'blocked'",
                (now_iso(), card_id),
            )
            self.store.add_event(conn, card_id, "person", "unblocked", {"note": note})
        return {"id": card_id, "status": "todo", "note": note}

    def mark_merged(self, card_id, note):
        note = norm_opt(note)
        with self.store.tx() as conn:
            row = self._fetch_card(conn, card_id)
            status = row["status"]
            if status == "done":
                raise APIError(409, f"{card_id} is done already - done is permanent.")
            if status != "in_review":
                raise APIError(
                    409,
                    f"{card_id} is {status}; only an in-review card can be marked done this way.",
                )
            if not note:
                raise APIError(
                    400,
                    f"POST /cards/{card_id}/merged needs a note: pass note=<who confirmed the merge>.",
                )
            at = now_iso()
            conn.execute(
                "UPDATE cards SET status = 'done', done_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'in_review'",
                (at, at, card_id),
            )
            self.store.add_event(
                conn, card_id, "person", "merged", {"note": note, "manual": True}
            )
        return {"id": card_id, "status": "done", "note": note}

    def sync(self, actor="board"):
        with self._sync_lock:
            self.git.fetch(self.git.base)
            if self.store.in_review_branches():
                self.git.fetch_card_branches()
            base_ref = self.git.base_ref()
            base_head = self.git.last_commit(base_ref)
            merged_ids = []
            for card_id, branch in self.store.in_review_branches():
                if self.git.is_ancestor(self.git.tracking_ref(branch), base_ref) is True:
                    merged_ids.append(card_id)
            done_now = []
            at = now_iso()
            with self.store.tx() as conn:
                for card_id in merged_ids:
                    cur = conn.execute(
                        "UPDATE cards SET status = 'done', done_at = ?, updated_at = ? "
                        "WHERE id = ? AND status = 'in_review'",
                        (at, at, card_id),
                    )
                    if cur.rowcount == 1:
                        row = conn.execute(
                            "SELECT branch FROM cards WHERE id = ?", (card_id,)
                        ).fetchone()
                        self.store.add_event(
                            conn,
                            card_id,
                            actor,
                            "merged",
                            {
                                "branch": row["branch"] if row else None,
                                "into": f"{self.git.remote}/{self.git.base}",
                            },
                        )
                        done_now.append(card_id)
                self.store.set_meta(conn, "last_sync_at", at)
                if base_head:
                    self.store.set_meta(conn, "base_head", json.dumps(base_head, ensure_ascii=False))
            return {"newly_done": done_now, "base_head": base_head, "at": at}

    def import_cards(self, payload, actor="agent"):
        items = payload
        if isinstance(items, dict) and "cards" in items:
            items = items["cards"]
            if isinstance(items, str):
                try:
                    items = json.loads(items)
                except ValueError as exc:
                    raise APIError(
                        400,
                        "PUT /cards expects a JSON list of cards "
                        "(or a form field `cards` holding one).",
                    ) from exc
        if not isinstance(items, list):
            raise APIError(400, "PUT /cards expects a JSON list of cards.")
        actor = norm_opt(actor) or "agent"

        prepared = []
        seen_ids = set()
        for index, raw in enumerate(items):
            if not isinstance(raw, dict):
                raise APIError(
                    400, f"Card #{index + 1} is not an object with at least `id` and `title`."
                )
            card_id = str(raw.get("id") or "").strip()
            if not CARD_ID_RE.fullmatch(card_id):
                raise APIError(
                    400,
                    f"Card #{index + 1}: id {card_id!r} is invalid - use letters, digits "
                    "and dashes, at most 32 characters.",
                )
            if card_id in seen_ids:
                raise APIError(400, f"Card {card_id} appears twice in one request.")
            seen_ids.add(card_id)
            title = norm_opt(raw.get("title"))
            if not title:
                raise APIError(400, f"Card {card_id} needs a title.")
            deps_raw = raw.get("depends_on") or []
            if not isinstance(deps_raw, (list, tuple)):
                raise APIError(400, f"Card {card_id}: depends_on must be a list of card ids.")
            deps = []
            for dep in deps_raw:
                dep_id = str(dep).strip()
                if not CARD_ID_RE.fullmatch(dep_id):
                    raise APIError(400, f"Card {card_id}: dependency {dep_id!r} is not a card id.")
                if dep_id not in deps:
                    deps.append(dep_id)
            done_raw = raw.get("done", False)
            if isinstance(done_raw, str):
                done = done_raw.strip().lower() in ("1", "true", "yes")
            else:
                done = bool(done_raw)
            prepared.append({
                "id": card_id,
                "title": title,
                "group": norm_opt(raw.get("group")),
                "body": norm_opt(raw.get("body")),
                "scope": norm_scope_input(raw.get("scope")),
                "source": norm_opt(raw.get("source")),
                "depends_on": deps,
                "done": done,
            })

        known = {row["id"] for row in self.store.all_rows()}
        graph = {}
        for card_id in known - seen_ids:
            graph[card_id] = self.store.dep_ids(card_id)
        for item in prepared:
            graph[item["id"]] = item["depends_on"]

        for item in prepared:
            for dep in item["depends_on"]:
                if dep not in known and dep not in seen_ids:
                    raise APIError(
                        400,
                        f"Card {item['id']} depends on {dep}, which does not exist anywhere; "
                        f"import {dep} first or fix the dependency. Nothing was saved.",
                    )

        cycle = find_cycle(graph)
        if cycle:
            raise APIError(
                400, "Dependency cycle refused: " + " -> ".join(cycle) + ". Nothing was saved."
            )

        created, updated, unchanged, refused_done = [], [], [], []
        actions = []
        with self.store.tx() as conn:
            seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM cards").fetchone()
            seq_next = seq_row["m"]
            for item in prepared:
                row = conn.execute("SELECT * FROM cards WHERE id = ?", (item["id"],)).fetchone()
                if row is not None and row["status"] == "done":
                    refused_done.append(item["id"])
                    actions.append(("refused", item))
                    continue
                if row is not None:
                    old_deps = [r["depends_on"] for r in conn.execute(
                        "SELECT depends_on FROM deps WHERE card_id = ?", (item["id"],))]
                    key_new = card_def_key(
                        item["title"], item["group"], item["body"],
                        item["scope"], item["source"], item["depends_on"],
                    )
                    key_old = card_def_key(
                        row["title"], row["grp"], row["body"],
                        decode_scope(row["scope"]), row["source"], old_deps,
                    )
                    if key_new == key_old:
                        unchanged.append(item["id"])
                        actions.append(("unchanged", item))
                        continue
                    conn.execute(
                        "UPDATE cards SET title = ?, grp = ?, body = ?, scope = ?, source = ?, "
                        "updated_at = ? WHERE id = ?",
                        (item["title"], item["group"], item["body"],
                         json.dumps(item["scope"], ensure_ascii=False),
                         item["source"], now_iso(), item["id"]),
                    )
                    updated.append(item["id"])
                    actions.append(("updated", item))
                else:
                    seq_next += 1
                    status = "done" if item["done"] else "todo"
                    at = now_iso()
                    conn.execute(
                        "INSERT INTO cards (id, seq, title, grp, body, scope, source, status, "
                        "created_at, updated_at, done_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (item["id"], seq_next, item["title"], item["group"], item["body"],
                         json.dumps(item["scope"], ensure_ascii=False), item["source"],
                         status, at, at, at if item["done"] else None),
                    )
                    created.append(item["id"])
                    actions.append(("created", item))
            for action, item in actions:
                if action not in ("created", "updated"):
                    continue
                conn.execute("DELETE FROM deps WHERE card_id = ?", (item["id"],))
                for dep in item["depends_on"]:
                    conn.execute(
                        "INSERT INTO deps (card_id, depends_on) VALUES (?, ?)",
                        (item["id"], dep),
                    )
                detail = {"source": item["source"]}
                if action == "created" and item["done"]:
                    detail["done"] = True
                kind = "imported" if action == "created" else "updated"
                self.store.add_event(conn, item["id"], actor, kind, detail)
        return {
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "refused_done": refused_done,
        }


class App:
    def __init__(self, repo, base, remote, port):
        self.git = GitFacts(repo, base, remote)
        self.store = Store(self.git.common_dir() / "agentboard" / "board.sqlite3")
        self.board = Board(self.store, self.git)
        self.port = port


def build_app(args):
    try:
        root = find_repo_root(Path(args.repo))
    except GitError as exc:
        die(
            "agentboard needs a git repository: run it from inside one, or pass --repo PATH. "
            f"(git said: {exc})"
        )
    return App(root, args.base, args.remote, args.port)


def die(message, code=2):
    print(f"agentboard: {message}", file=sys.stderr)
    raise SystemExit(code)


def render_card_line(summary):
    line = f"{summary['id']}  {summary['title']}"
    if summary["group"]:
        line += f"  [{summary['group']}]"
    state = summary["state"]
    if state == "waiting":
        line += "  waiting on " + ", ".join(
            f"{d['id']} ({d['status']})" for d in summary["waits_on"]
        )
    elif state == "ready":
        line += "  ready"
    elif state == "claimed":
        line += f"  claimed by {summary['agent'] or '?'} {ago(summary['last_event_at'])}"
        if summary["stale"]:
            line += "  STALE (no events for over 4 hours)"
    elif state == "in_review":
        line += f"  in review  {summary['pr'] or ''}".rstrip()
    elif state == "blocked":
        line += f"  blocked: {summary['blocked_reason'] or 'no reason'}"
    elif state == "done":
        line += f"  done {summary['done_at'] or ''}".rstrip()
    return line


def render_cards_text(cards):
    if not cards:
        return "(no cards)\n"
    return "\n".join(render_card_line(c) for c in cards) + "\n"


def render_event_line(event):
    detail = " ".join(f"{k}={v}" for k, v in (event["detail"] or {}).items())
    line = f"#{event['id']} {event['at']} {event['card_id'] or '-'} {event['actor']} {event['kind']}"
    if detail:
        line += f"  {detail}"
    return line


def render_card_detail_text(detail):
    lines = [f"{detail['id']}  {detail['title']}"]
    state = detail["state"]
    if state == "claimed":
        state += f" (claimed by {detail['agent']} {ago(detail['last_event_at'])})"
    lines.append(f"state: {state}")
    extras = []
    if detail["group"]:
        extras.append(f"group: {detail['group']}")
    if detail["source"]:
        extras.append(f"source: {detail['source']}")
    if extras:
        lines.append("  ".join(extras))
    if detail["scope"]:
        lines.append("scope: " + ", ".join(detail["scope"]))
    facts = [f"branch: {detail['branch'] or '(none)'}"]
    if detail["pr"]:
        facts.append(f"PR: {detail['pr']}")
    if detail["blocked_reason"]:
        facts.append(f"blocked: {detail['blocked_reason']}")
    lines.append("  ".join(facts))
    stamps = f"created: {detail['created_at']}  updated: {detail['updated_at']}"
    if detail["done_at"]:
        stamps += f"  done: {detail['done_at']}"
    lines.append(stamps)
    lines.append("")
    lines.append(detail["body"] if detail["body"] else "(no body)")
    lines.append("")
    lines.append("depends on:")
    if detail["depends_on"]:
        for dep in detail["depends_on"]:
            lines.append(f"  {dep['id']}  {dep['status']}  {dep['title'] or ''}".rstrip())
    else:
        lines.append("  (nothing)")
    lines.append("")
    lines.append("events (newest first):")
    if detail["events"]:
        for event in detail["events"]:
            lines.append("  " + render_event_line(event))
    else:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


def render_claim_text(result):
    lines = [
        f"{result['id']} is claimed by {result['agent']}.",
        f"Branch: {result['branch']}",
        f"Worktree: {result['worktree']} (beside the repo)",
        "",
        f"Run these from the repo root ({result['repo_root']}):",
    ]
    lines.extend(f"  {command}" for command in result["commands"])
    lines.append("")
    lines.append(
        "Then work in the worktree and follow the repo's AGENTS.md "
        "(setup, checks, hooks, scope)."
    )
    if result.get("note"):
        lines.append(f"Note: {result['note']}")
    return "\n".join(lines) + "\n"


def render_report_text(result):
    return (
        f"{result['id']} is in review: {result['pr']}\n"
        f"{result['commits']} commit(s) on the branch beyond the base; "
        "the repo's CI and review take over.\n"
    )


def render_block_text(result):
    return (
        f"{result['id']} is blocked: {result['reason']}\n"
        f"A person decides; to return it to ready, POST /cards/{result['id']}/unblock "
        "with a note.\n"
    )


def render_release_text(result):
    return (
        f"{result['id']} is ready again (was held by {result['holder'] or 'nobody'}). "
        "The branch stays.\n"
    )


def render_unblock_text(result):
    return f"{result['id']} is ready again. Note recorded: {result['note']}\n"


def render_merged_text(result):
    return f"{result['id']} is done (confirmed merge). Done is permanent.\n"


def render_sync_text(result):
    if result["newly_done"]:
        return "Sync: marked done " + ", ".join(result["newly_done"]) + ".\n"
    return "Sync: no new merges.\n"


def render_import_text(result):
    def part(label, ids):
        text = f"{len(ids)} {label}"
        if ids:
            text += f" ({', '.join(ids)})"
        return text

    return "Import: " + ", ".join([
        part("created", result["created"]),
        part("updated", result["updated"]),
        part("unchanged", result["unchanged"]),
        part("refused because done", result["refused_done"]),
    ]) + ".\n"


def render_agent_page(app):
    git = app.git
    base_url = f"http://127.0.0.1:{app.port}"
    ready = app.board.list_cards("ready")
    claimed = app.board.list_cards("claimed")
    lines = [
        "Agent Board - use your shell (curl.exe, Invoke-RestMethod), not a web tool: "
        "some web tools refuse localhost.",
        f"Board: {base_url}    repo: {git.repo}    base: {git.remote}/{git.base}",
        "",
        "## Rules",
        "- Read a card, summarise your plan, and wait for the person's go-ahead before you claim.",
        "- One agent per card. Claim with your name; the board replies with the branch and the "
        "git commands to create your worktree.",
        "- Work in that worktree and follow the repo's AGENTS.md (setup, checks, hooks, scope).",
        "- Commit, push the branch, open a PR, then report the card with the PR link. The board "
        "checks git facts first; only then does the card move to In review.",
        "- Blocked? POST block with a reason. A person decides; only their unblock returns it "
        "to ready.",
        "- Done is permanent, and the board owns the states: agents move a card only by claiming, "
        "reporting, blocking or releasing it; a person unblocks it or confirms a squash merge; "
        "the board detects merges.",
        "",
        "## Endpoints (plain text by default; add ?format=json or Accept: application/json)",
        f"- GET  {base_url}/cards?status=ready     ready cards, one line each (no status: every card)",
        f"- GET  {base_url}/cards/{{id}}            one full card: body, scope, deps, events",
        f"- GET  {base_url}/board                  counts, sync facts and every card (JSON with ?format=json)",
        f"- POST {base_url}/cards/{{id}}/claim      body: agent=<your name>",
        f"- POST {base_url}/cards/{{id}}/report     body: pr=<PR URL>, note=<optional>",
        f"- POST {base_url}/cards/{{id}}/block      body: reason=<why>",
        f"- POST {base_url}/cards/{{id}}/release    hand it back to ready (the branch stays)",
        f"- POST {base_url}/cards/{{id}}/unblock    a person's decision, through you: note=<decision>",
        f"- POST {base_url}/cards/{{id}}/merged     a person confirms a squash merge, through you: note=<who confirmed>",
        f"- POST {base_url}/sync                   detect merges now (the board also does it every {SYNC_SECONDS} s)",
        f"- PUT  {base_url}/cards                  import or update cards: a JSON list (see Loading cards below)",
        f"- GET  {base_url}/events?since={{id}}     what changed since an event id",
        "",
        "## Ready cards (import order - take the first one, or the id you were given)",
    ]
    if ready:
        for index, card in enumerate(ready, 1):
            lines.append(f"{index}. " + render_card_line(card))
    else:
        lines.append("(none right now)")
    lines.append("")
    lines.append("## Claimed (do not start these)")
    if claimed:
        for card in claimed:
            lines.append("- " + render_card_line(card))
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Examples (Windows PowerShell 5.1 and curl.exe; the same work in any shell)")
    lines.append(f"  Invoke-RestMethod {base_url}/agent")
    lines.append(f"  Invoke-RestMethod \"{base_url}/cards?status=ready\"")
    lines.append(
        f"  Invoke-RestMethod -Method Post {base_url}/cards/IDN-02/claim "
        "-Body @{agent='claude-code'}"
    )
    lines.append(
        f"  Invoke-RestMethod -Method Post {base_url}/cards/IDN-02/report "
        "-Body @{pr='https://github.com/org/repo/pull/12'}"
    )
    lines.append("")
    lines.append(f"  curl.exe -s {base_url}/agent")
    lines.append(f"  curl.exe -s \"{base_url}/cards?status=ready\"")
    lines.append(f"  curl.exe -s -X POST -d agent=codex {base_url}/cards/IDN-02/claim")
    lines.append(
        f"  curl.exe -s -X POST -d pr=https://github.com/org/repo/pull/12 "
        f"{base_url}/cards/IDN-02/report"
    )
    lines.extend([
        "",
        "## Loading cards (only when a person asks you to import or update cards)",
        "Write the cards to a UTF-8 file, cards.json, as a JSON list, then PUT it. Send the file,",
        "not a string: Windows PowerShell 5.1 re-encodes string bodies and loses characters.",
        "",
        "  [",
        "    {",
        '      "id": "IDN-02",                       required: letters, digits, dashes; at most 32',
        '      "title": "OIDC sign-in and callback",  required',
        '      "group": "identity",                   module or area',
        '      "depends_on": ["IDN-01"],              ids that must be done first',
        '      "scope": ["server/internal/modules/identity/**"],',
        '      "source": "docs/modules/identity.md",',
        '      "body": "markdown: what to build and the acceptance criteria",',
        '      "done": false                          true only for work finished before the board',
        "    }",
        "  ]",
        "",
        "List cards in priority order: ready cards are offered in import order. Re-importing is",
        "safe: it updates cards that are not done and never touches claims, status or history.",
        "An unknown dependency or a cycle refuses the whole request; nothing is saved.",
        "",
        f"  curl.exe -s -X PUT -H \"Content-Type: application/json\" --data-binary \"@cards.json\" \"{base_url}/cards?actor=<your name>\"",
        f"  Invoke-RestMethod -Method Put -InFile cards.json -ContentType 'application/json; charset=utf-8' \"{base_url}/cards?actor=<your name>\"",
    ])
    return "\n".join(lines) + "\n"


def render_status(app):
    store = app.store
    groups = {key: [] for key, _ in COLUMNS}
    for row in store.all_rows():
        summary = card_summary(store, row)
        groups[summary["state"]].append(summary)
    lines = [f"AgentBoard - repo: {app.git.repo}   base: {app.git.remote}/{app.git.base}"]
    lines.append(" | ".join(f"{label} {len(groups[key])}" for key, label in COLUMNS))
    bits = []
    last_sync = store.get_meta("last_sync_at")
    bits.append("last sync " + (ago(last_sync) if last_sync else "never"))
    base_head = store.get_meta("base_head")
    if base_head:
        try:
            head = json.loads(base_head)
        except ValueError:
            head = None
        if head:
            subject = str(head.get("subject") or "")[:60]
            bits.append(f'{app.git.remote}/{app.git.base} @ {head.get("sha")} "{subject}"')
    lines.append(" | ".join(bits))
    for key, label in COLUMNS:
        lines.append("")
        lines.append(label.upper())
        if not groups[key]:
            lines.append("  (none)")
        for card in groups[key]:
            lines.append("  " + render_card_line(card))
    return "\n".join(lines) + "\n"


def board_snapshot(app):
    base_head = None
    raw_head = app.store.get_meta("base_head")
    if raw_head:
        try:
            base_head = json.loads(raw_head)
        except ValueError:
            base_head = None
    cards = app.board.list_cards(None)
    counts = {key: 0 for key, _ in COLUMNS}
    for card in cards:
        counts[card["state"]] += 1
    return {
        "repo": str(app.git.repo),
        "repo_name": app.git.repo.name,
        "base": f"{app.git.remote}/{app.git.base}",
        "last_sync_at": app.store.get_meta("last_sync_at"),
        "base_head": base_head,
        "counts": counts,
        "max_event_id": app.store.max_event_id(),
        "cards": cards,
    }


PAGE_CSS = """
:root {
  --bg: #f6f7f9; --fg: #1c1e21; --card: #ffffff; --muted: #667085;
  --line: #dde1e7; --accent: #0b62d6; --warn: #b45309; --ok: #16a34a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161a; --fg: #e8eaed; --card: #1d2025; --muted: #9aa0a6;
    --line: #33373d; --accent: #7ab0ff; --warn: #fbbf24; --ok: #4ade80;
  }
}
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.45 system-ui, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--fg); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header.top { display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: baseline;
  padding: 12px 16px; border-bottom: 1px solid var(--line); background: var(--card); }
header.top h1 { font-size: 16px; margin: 0; letter-spacing: .3px; }
header.top h1 a { color: var(--fg); }
.counts span { margin-right: 12px; color: var(--muted); }
.counts b { color: var(--fg); }
.sync { color: var(--muted); font-size: 12px; }
.agents { color: var(--muted); font-size: 12px; margin-top: 2px; }
.agents .label { text-transform: uppercase; letter-spacing: .6px; font-size: 11px; margin-right: 6px; }
.agents .agent { margin-right: 14px; }
.agents .agent b { color: var(--fg); font-weight: 600; }
.agents .mini { color: var(--muted); }
.agents .none { color: var(--muted); font-style: italic; }
.activity { max-width: 980px; margin: 4px 12px 24px; padding: 10px 14px;
  background: var(--card); border: 1px solid var(--line); border-radius: 8px; }
.activity h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .6px;
  color: var(--muted); margin: 0 0 6px; font-weight: 600; }
.activity h2 .n { color: var(--fg); text-transform: none; letter-spacing: 0; font-weight: 400; }
.activity ul { list-style: none; margin: 0; padding: 0; }
.activity li { font-size: 12px; color: var(--muted); padding: 2px 0; }
.activity li b { color: var(--fg); font-weight: 600; }
.activity li a { color: var(--accent); }
.activity .mini { color: var(--muted); }
.cols { display: flex; gap: 12px; align-items: flex-start; padding: 12px; overflow-x: auto; }
.col { flex: 1 1 0; min-width: 210px; }
.col h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .6px;
  color: var(--muted); margin: 4px 2px; font-weight: 600; }
.col h2 .n, details summary .n { color: var(--fg); }
details.done summary { cursor: pointer; color: var(--muted); font-size: 12px;
  text-transform: uppercase; letter-spacing: .6px; margin: 4px 2px; font-weight: 600; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
  padding: 8px 10px; margin: 8px 0; }
.card .id { color: var(--muted); font-size: 12px; font-weight: 600; }
.card .title { display: block; color: var(--fg); margin: 2px 0; }
.card .meta { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.grp { display: inline-block; border: 1px solid var(--line); border-radius: 99px;
  padding: 0 7px; font-size: 11px; color: var(--muted); }
.stale { display: inline-block; background: var(--warn); color: #111;
  border-radius: 99px; padding: 0 7px; font-size: 11px; margin-left: 6px; }
.stale[hidden] { display: none; }
.empty { color: var(--muted); font-size: 12px; padding: 2px; }
.detail { max-width: 920px; padding: 16px; }
.detail h1 { font-size: 18px; margin: 0 0 6px; }
.detail h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .6px;
  color: var(--muted); margin: 20px 0 6px; }
.detail table { border-collapse: collapse; width: 100%; }
.detail td, .detail th { border-bottom: 1px solid var(--line); padding: 6px 8px;
  text-align: left; font-size: 13px; vertical-align: top; }
.detail th { color: var(--muted); font-weight: 600; width: 110px; }
.detail pre.body { background: var(--card); border: 1px solid var(--line);
  border-radius: 8px; padding: 12px; white-space: pre-wrap; overflow-wrap: anywhere;
  font: 13px/1.5 ui-monospace, Consolas, "Courier New", monospace; margin: 0; }
.dep-done { color: var(--ok); }
.dep-open { color: var(--warn); }
code { font-family: ui-monospace, Consolas, "Courier New", monospace; font-size: 12px; }
"""

PAGE_JS = """
(function () {
  var pollTimer = null;
  var refreshTimer = null;
  var stream = null;

  function rel(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m ago";
    return Math.floor(s / 86400) + "d ago";
  }
  function updateTimes() {
    document.querySelectorAll("time[data-ts]").forEach(function (el) {
      var t = Date.parse(el.getAttribute("data-ts"));
      if (!isNaN(t)) el.textContent = rel(Date.now() - t);
    });
  }
  function updateStale() {
    document.querySelectorAll(".stale").forEach(function (el) {
      var t = Date.parse(el.getAttribute("data-after"));
      el.hidden = !(t && Date.now() >= t);
    });
  }
  function refresh() {
    refreshTimer = null;
    updateTimes();
    updateStale();
    fetch(location.pathname + location.search, { cache: "no-store" })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (!html) return;
        var doc = new DOMParser().parseFromString(html, "text/html");
        var nrev = doc.querySelector('meta[name="rev"]');
        var crev = document.querySelector('meta[name="rev"]');
        if (!nrev || !crev || nrev.content === crev.content) return;
        var open = {};
        document.querySelectorAll("details[data-key]").forEach(function (d) {
          open[d.getAttribute("data-key")] = d.open;
        });
        ["headline", "content"].forEach(function (id) {
          var old = document.getElementById(id);
          var fresh = doc.getElementById(id);
          if (old && fresh) old.innerHTML = fresh.innerHTML;
        });
        crev.content = nrev.content;
        document.querySelectorAll("details[data-key]").forEach(function (d) {
          if (open[d.getAttribute("data-key")]) d.open = true;
        });
        updateTimes();
        updateStale();
      })
      .catch(function () {});
  }
  function schedule() {
    if (refreshTimer) return;
    refreshTimer = setTimeout(refresh, 150);
  }
  function startPolling() {
    if (!pollTimer) pollTimer = setInterval(refresh, 5000);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  function connect() {
    if (!window.EventSource) { startPolling(); return; }
    try {
      stream = new EventSource("/events/stream");
    } catch (e) {
      startPolling();
      return;
    }
    stream.addEventListener("snapshot", function () { stopPolling(); schedule(); });
    stream.addEventListener("event", function () { schedule(); });
    stream.onerror = function () {
      if (stream) { stream.close(); stream = null; }
      startPolling();
      setTimeout(connect, 5000);
    };
  }
  updateTimes();
  updateStale();
  startPolling();
  connect();
})();
"""


def page_shell(title, rev, headline_html, content_html):
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta name="rev" content="{esc(rev)}">\n'
        f"<title>{esc(title)}</title>\n"
        f"<style>{PAGE_CSS}</style>\n"
        '<noscript><meta http-equiv="refresh" content="5"></noscript>\n'
        "</head>\n<body>\n"
        '<header class="top">\n'
        '<h1><a href="/">AgentBoard</a></h1>\n'
        f'<div id="headline">{headline_html}</div>\n'
        "</header>\n"
        f'<main id="content">{content_html}</main>\n'
        f"<script>{PAGE_JS}</script>\n"
        "</body>\n</html>\n"
    )


def time_html(value):
    if not value:
        return ""
    return f'<time data-ts="{esc(value)}">{esc(ago(value))}</time>'


def board_rev(app):
    base_head = app.store.get_meta("base_head") or ""
    try:
        sha = (json.loads(base_head) or {}).get("sha") or ""
    except ValueError:
        sha = ""
    return f"{app.store.max_event_id()}-{sha[:12]}"


def agents_html(cards):
    groups = {}
    for card in cards:
        if card["state"] in ("claimed", "in_review") and card["agent"]:
            groups.setdefault(card["agent"], []).append(card)
    if not groups:
        return (
            '<div class="agents"><span class="label">Agents</span> '
            '<span class="none">(none holding a card)</span></div>'
        )
    parts = []
    for agent in sorted(groups, key=lambda name: str(name).lower()):
        bits = []
        for card in groups[agent]:
            bit = f'<a href="/view/{esc(card["id"])}">{esc(card["id"])}</a>'
            if card["state"] == "claimed":
                bit += f' <span class="mini">claimed {time_html(card["last_event_at"])}</span>'
                if card["stale"]:
                    after = add_seconds(card["last_event_at"], STALE_SECONDS)
                    bit += f'<span class="stale" data-after="{esc(after)}">stale</span>'
            else:
                bit += ' <span class="mini">in review</span>'
            bits.append(bit)
        parts.append(f'<span class="agent"><b>{esc(agent)}</b> ' + " | ".join(bits) + "</span>")
    return '<div class="agents"><span class="label">Agents</span> ' + "  ".join(parts) + "</div>"


def headline_html(app, counts, cards):
    counts_html = "".join(
        f"<span>{esc(label)} <b>{counts[key]}</b></span>" for key, label in COLUMNS
    )
    bits = []
    last_sync = app.store.get_meta("last_sync_at")
    bits.append("last sync " + (time_html(last_sync) if last_sync else "never"))
    base_head = app.store.get_meta("base_head")
    if base_head:
        try:
            head = json.loads(base_head)
        except ValueError:
            head = None
        if head:
            sha = esc(str(head.get("sha") or ""))
            subject = esc(str(head.get("subject") or ""))
            bits.append(
                f'{esc(app.git.remote)}/{esc(app.git.base)} @ <code>{sha}</code> "{subject}"'
            )
    return (
        f'<div class="counts">{counts_html}</div>'
        f'<div class="sync">{" | ".join(bits)}</div>'
        + agents_html(cards)
    )


def meta_line_html(summary):
    state = summary["state"]
    if state == "claimed":
        out = f'{esc(summary["agent"] or "?")} claimed {time_html(summary["last_event_at"])}'
        hidden = "" if summary["stale"] else " hidden"
        after = add_seconds(summary["last_event_at"], STALE_SECONDS)
        out += f'<span class="stale" data-after="{esc(after)}"{hidden}>stale</span>'
        return out
    if state == "waiting":
        return "waits on " + ", ".join(esc(d["id"]) for d in summary["waits_on"])
    if state == "ready":
        return ""
    if state == "in_review":
        out = f'reported {time_html(summary["last_event_at"])}'
        if summary["pr"]:
            out += f' | <a href="{esc(summary["pr"])}" target="_blank" rel="noopener">PR</a>'
        return out
    if state == "blocked":
        return "blocked: " + esc(summary["blocked_reason"] or "no reason")
    if state == "done":
        return "done " + (time_html(summary["done_at"]) if summary["done_at"] else "")
    return ""


def board_card_html(summary):
    parts = ['<div class="card">']
    head = f'<span class="id">{esc(summary["id"])}</span>'
    if summary["group"]:
        head += f' <span class="grp">{esc(summary["group"])}</span>'
    parts.append(head)
    parts.append(
        f'<a class="title" href="/view/{esc(summary["id"])}">{esc(summary["title"])}</a>'
    )
    meta = meta_line_html(summary)
    if meta:
        parts.append(f'<div class="meta">{meta}</div>')
    parts.append("</div>")
    return "\n".join(parts)


def column_html(key, label, cards):
    if key == "done":
        items = "\n".join(board_card_html(c) for c in cards) or '<div class="empty">(none)</div>'
        return (
            f'<section class="col"><details class="done" data-key="done">'
            f'<summary>Done <span class="n">{len(cards)}</span></summary>'
            f"{items}</details></section>"
        )
    items = "\n".join(board_card_html(c) for c in cards) or '<div class="empty">(none)</div>'
    return (
        f'<section class="col"><h2>{esc(label)} <span class="n">{len(cards)}</span></h2>'
        f"{items}</section>"
    )


def activity_html(store):
    events = store.recent_events(14)
    if not events:
        return '<section class="activity"><h2>Recent activity</h2><div class="empty">(none)</div></section>'
    groups = []
    for event in events:
        key = (event["card_id"], event["actor"], event["kind"])
        if groups and groups[-1]["key"] == key:
            groups[-1]["count"] += 1
            continue
        groups.append({"key": key, "event": event, "count": 1})
    items = []
    for group in groups:
        event = group["event"]
        if event["card_id"]:
            card = f'<a href="/view/{esc(event["card_id"])}">{esc(event["card_id"])}</a>'
        else:
            card = "board"
        extra = ""
        if event["kind"] == "merged" and (event["detail"] or {}).get("manual"):
            extra = ' <span class="mini">manual</span>'
        count = f' <span class="mini">x{group["count"]}</span>' if group["count"] > 1 else ""
        items.append(
            f'<li>{time_html(event["at"])} | <b>{esc(event["actor"])}</b> '
            f'{esc(event["kind"])} {card}{count}{extra}</li>'
        )
    return (
        '<section class="activity"><h2>Recent activity '
        f'<span class="n">last {len(events)}</span></h2><ul>'
        + "".join(items)
        + "</ul></section>"
    )


def render_board_html(app):
    cards = app.board.list_cards(None)
    groups = {key: [] for key, _ in COLUMNS}
    for card in cards:
        groups[card["state"]].append(card)
    counts = {key: len(groups[key]) for key, _ in COLUMNS}
    columns = "\n".join(
        column_html(key, label, groups[key]) for key, label in COLUMNS
    )
    return page_shell(
        f"AgentBoard - {app.git.repo.name}",
        board_rev(app),
        headline_html(app, counts, cards),
        f'<div class="cols">{columns}</div>' + activity_html(app.store),
    )


def facts_table_html(rows):
    body = "\n".join(f"<tr><th>{esc(key)}</th><td>{value}</td></tr>" for key, value in rows)
    return f'<table class="facts">{body}</table>'


def render_card_page_html(app, card_id):
    row = app.store.get_row(card_id)
    if row is None:
        raise APIError(404, f"No card {card_id}. GET /cards lists every card; check the id.")
    detail = card_detail(app.store, row)
    state = detail["state"]
    if state == "claimed":
        state_text = (
            f'claimed by {esc(detail["agent"] or "?")} '
            f"{time_html(detail['last_event_at'])}"
        )
    else:
        state_text = esc(state)
    head = (
        f'<div class="counts"><span>id <b>{esc(detail["id"])}</b></span>'
        f'<span>state <b>{state_text}</b></span></div>'
        f'<div class="sync">'
        f'<a href="/">back to the board</a></div>'
    )
    facts = []
    if detail["group"]:
        facts.append(("group", esc(detail["group"])))
    if detail["source"]:
        facts.append(("source", esc(detail["source"])))
    if detail["scope"]:
        facts.append(("scope", esc(", ".join(detail["scope"]))))
    facts.append(("branch", esc(detail["branch"] or "-")))
    if detail["pr"]:
        facts.append(
            ("PR", f'<a href="{esc(detail["pr"])}" target="_blank" rel="noopener">'
                   f'{esc(detail["pr"])}</a>')
        )
    if detail["blocked_reason"]:
        facts.append(("blocked", esc(detail["blocked_reason"])))
    facts.append(("created", esc(detail["created_at"])))
    facts.append(("updated", esc(detail["updated_at"])))
    if detail["done_at"]:
        facts.append(("done", esc(detail["done_at"])))
    parts = ['<article class="detail">']
    parts.append(f'<h1>{esc(detail["id"])} - {esc(detail["title"])}</h1>')
    parts.append(facts_table_html(facts))
    parts.append("<h2>Body</h2>")
    parts.append(f'<pre class="body">{esc(detail["body"] or "(no body)")}</pre>')
    parts.append("<h2>Depends on</h2>")
    if detail["depends_on"]:
        dep_rows = []
        for dep in detail["depends_on"]:
            klass = "dep-done" if dep["status"] == "done" else "dep-open"
            dep_rows.append(
                f'<tr><td><a href="/view/{esc(dep["id"])}">{esc(dep["id"])}</a></td>'
                f'<td class="{klass}">{esc(dep["status"])}</td>'
                f'<td>{esc(dep["title"] or "")}</td></tr>'
            )
        parts.append('<table class="deps">' + "\n".join(dep_rows) + "</table>")
    else:
        parts.append('<div class="empty">(nothing)</div>')
    parts.append("<h2>Events</h2>")
    if detail["events"]:
        event_rows = []
        for event in detail["events"]:
            detail_json = json.dumps(event["detail"], ensure_ascii=False) if event["detail"] else ""
            event_rows.append(
                f'<tr><td>#{event["id"]}</td>'
                f"<td>{time_html(event['at'])}</td>"
                f'<td>{esc(event["actor"])}</td>'
                f'<td>{esc(event["kind"])}</td>'
                f'<td>{esc(detail_json)}</td></tr>'
            )
        parts.append('<table class="events">' + "\n".join(event_rows) + "</table>")
    else:
        parts.append('<div class="empty">(none)</div>')
    parts.append("</article>")
    return page_shell(
        f"{detail['id']} - AgentBoard",
        board_rev(app),
        head,
        "\n".join(parts),
    )


def decode_body(raw, content_type=None):
    """Request bytes to text without silently losing characters.

    UTF-8 first (a leading BOM is dropped). Windows PowerShell 5.1 sends a string body as
    ISO-8859-1 unless told otherwise, so a declared charset is honoured next, and Windows-1252
    (a superset of ISO-8859-1's printable range) is the last resort.
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    match = re.search(r"charset=([\w.:-]+)", content_type or "", re.IGNORECASE)
    if match:
        try:
            return raw.decode(match.group(1))
        except (LookupError, UnicodeDecodeError):
            pass
    return raw.decode("cp1252", "replace")


def make_server(app, port):
    BoardHTTP.app = app
    httpd = ThreadingHTTPServer(("127.0.0.1", port), BoardHTTP)
    httpd.daemon_threads = True
    return httpd


class BoardHTTP(BaseHTTPRequestHandler):
    server_version = f"AgentBoard/{VERSION}"
    protocol_version = "HTTP/1.1"
    timeout = 60
    app = None

    def log_message(self, fmt, *args):
        message = fmt % args
        if " 200 " in message or " 204 " in message:
            return
        sys.stderr.write(f"[agentboard] {self.address_string()} {message}\n")

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        self._query = {}
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path or "/"
            if len(path) > 1 and path.endswith("/"):
                path = path.rstrip("/")
            self._query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if method == "GET":
                return self.route_get(path)
            if method == "POST":
                return self.route_post(path)
            if method == "PUT":
                return self.route_put(path)
            raise APIError(
                404, f"No route {method} {path}. Agents start at GET /agent; the board is at GET /."
            )
        except APIError as exc:
            self.respond_error(exc)
        except GitError as exc:
            self.respond_error(APIError(502, str(exc)))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.respond_error(
                APIError(500, "Internal error; see the server log on the machine running the board.")
            )

    def param(self, name, default=None):
        values = self._query.get(name)
        if values:
            return values[0]
        return default

    def wants_json(self):
        fmt = (self.param("format") or "").lower()
        if fmt == "json":
            return True
        if fmt == "text":
            return False
        return "application/json" in (self.headers.get("Accept") or "")

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        text = decode_body(raw, self.headers.get("Content-Type"))
        stripped = text.lstrip()
        if ctype == "application/json" or (
            stripped[:1] in ("{", "[") and ctype != "application/x-www-form-urlencoded"
        ):
            try:
                return json.loads(text)
            except ValueError:
                if ctype == "application/json":
                    raise APIError(400, "The request body is not valid JSON; fix it and retry.")
        parsed = urllib.parse.parse_qs(text, keep_blank_values=True)
        return {key: (value[0] if len(value) == 1 else value) for key, value in parsed.items()}

    def body_str(self, body, key):
        if not isinstance(body, dict):
            return None
        value = body.get(key)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def respond(self, status, text, data=None):
        if self.wants_json() and data is not None:
            self.send_bytes(
                status,
                json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )
        else:
            self.send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def send_html(self, html_text):
        self.send_bytes(200, html_text.encode("utf-8"), "text/html; charset=utf-8")

    def send_bytes(self, status, body, content_type):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def respond_error(self, exc):
        self.respond(exc.status, exc.message + "\n", {"error": exc.message})

    def write_sse(self, event_name, data):
        payload = json.dumps(data, ensure_ascii=False)
        chunk = f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")
        self.wfile.write(chunk)
        self.wfile.flush()

    def stream_events(self):
        app = self.app
        since_raw = self.param("since") or "0"
        last_id = int(since_raw) if since_raw.isdigit() else 0
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            snapshot = board_snapshot(app)
            last_id = max(last_id, snapshot["max_event_id"])
            self.write_sse("snapshot", snapshot)
            idle = 0
            while True:
                events = app.store.events_since(last_id)
                if events:
                    for event in events:
                        self.write_sse("event", event)
                        last_id = max(last_id, event["id"])
                    idle = 0
                else:
                    idle += 1
                    if idle >= 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        idle = 0
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def route_get(self, path):
        app = self.app
        if path == "/":
            return self.send_html(render_board_html(app))
        if path == "/favicon.ico":
            return self.send_bytes(204, b"", "image/x-icon")
        if path == "/agent":
            text = render_agent_page(app)
            data = {
                "board": f"http://127.0.0.1:{app.port}",
                "repo": str(app.git.repo),
                "base": f"{app.git.remote}/{app.git.base}",
                "ready": app.board.list_cards("ready"),
                "claimed": app.board.list_cards("claimed"),
            }
            return self.respond(200, text, data)
        if path == "/cards":
            cards = app.board.list_cards(self.param("status"))
            return self.respond(200, render_cards_text(cards), {"cards": cards})
        if path == "/board":
            return self.respond(200, render_status(app), board_snapshot(app))
        if path == "/events/stream":
            return self.stream_events()
        match = re.fullmatch(r"/cards/([^/]+)", path)
        if match:
            card_id = urllib.parse.unquote(match.group(1))
            row = app.store.get_row(card_id)
            if row is None:
                raise APIError(404, f"No card {card_id}. GET /cards lists every card; check the id.")
            detail = card_detail(app.store, row)
            return self.respond(200, render_card_detail_text(detail), detail)
        if path == "/events":
            since_raw = self.param("since") or "0"
            since = int(since_raw) if since_raw.isdigit() else 0
            events = app.store.events_since(since)
            if events:
                text = "\n".join(render_event_line(e) for e in events) + "\n"
            else:
                text = "(no events)\n"
            return self.respond(200, text, {"events": events})
        if path.startswith("/view/"):
            card_id = urllib.parse.unquote(path[len("/view/"):])
            if not card_id:
                raise APIError(404, "No card in /view/. Use /view/<card id>, like /view/IDN-02.")
            return self.send_html(render_card_page_html(app, card_id))
        raise APIError(404, f"No route GET {path}. Agents start at GET /agent; the board is at GET /.")

    def route_post(self, path):
        app = self.app
        match = re.fullmatch(r"/cards/([^/]+)/(claim|report|block|release|unblock|merged)", path)
        if match:
            card_id = urllib.parse.unquote(match.group(1))
            action = match.group(2)
            body = self.read_body()
            if action == "claim":
                result = app.board.claim(card_id, self.body_str(body, "agent"))
                return self.respond(200, render_claim_text(result), result)
            if action == "report":
                result = app.board.report(
                    card_id,
                    self.body_str(body, "pr"),
                    self.body_str(body, "note"),
                    self.body_str(body, "agent"),
                )
                return self.respond(200, render_report_text(result), result)
            if action == "block":
                result = app.board.block(
                    card_id, self.body_str(body, "reason"), self.body_str(body, "agent")
                )
                return self.respond(200, render_block_text(result), result)
            if action == "release":
                result = app.board.release(card_id, self.body_str(body, "agent"))
                return self.respond(200, render_release_text(result), result)
            if action == "unblock":
                result = app.board.unblock(card_id, self.body_str(body, "note"))
                return self.respond(200, render_unblock_text(result), result)
            if action == "merged":
                result = app.board.mark_merged(card_id, self.body_str(body, "note"))
                return self.respond(200, render_merged_text(result), result)
        if path == "/sync":
            try:
                result = app.board.sync()
            except GitError as exc:
                raise APIError(
                    502,
                    f"Sync failed: {exc}. Nothing changed; check the remote and the network, "
                    "then retry.",
                ) from exc
            return self.respond(200, render_sync_text(result), result)
        raise APIError(404, f"No route POST {path}. Agents start at GET /agent; the board is at GET /.")

    def route_put(self, path):
        if path != "/cards":
            raise APIError(404, f"No route PUT {path}. Import cards with PUT /cards (a JSON list).")
        app = self.app
        body = self.read_body()
        actor = self.param("actor")
        if not actor and isinstance(body, dict):
            actor = self.body_str(body, "actor")
        payload = body
        if isinstance(body, dict) and isinstance(body.get("cards"), str):
            try:
                payload = json.loads(body["cards"])
            except ValueError as exc:
                raise APIError(
                    400, "The `cards` form field is not valid JSON; send the list unchanged."
                ) from exc
        result = app.board.import_cards(payload, actor=actor or "agent")
        return self.respond(200, render_import_text(result), result)


def cmd_serve(args):
    app = build_app(args)
    try:
        httpd = make_server(app, args.port)
    except OSError as exc:
        die(f"could not listen on 127.0.0.1:{args.port} ({exc}); pass --port with a free port")
    app.port = httpd.server_address[1]
    port = app.port
    print(f"AgentBoard on http://127.0.0.1:{port}")
    print(f"  repo: {app.git.repo}")
    print(f"  base: {app.git.remote}/{app.git.base}")
    print(f"  db:   {app.store.db_path}")
    try:
        result = app.board.sync()
        if result["newly_done"]:
            print(f"  initial sync: marked done {', '.join(result['newly_done'])}")
        else:
            print("  initial sync: no new merges")
    except GitError as exc:
        print(f"  initial sync failed (the board still serves): {exc}", file=sys.stderr)
    print(f"  people: http://127.0.0.1:{port}/")
    print(f"  agents: http://127.0.0.1:{port}/agent")
    stop = threading.Event()

    def sync_loop():
        while not stop.wait(SYNC_SECONDS):
            try:
                app.board.sync()
            except Exception as exc:
                print(f"[agentboard] sync failed: {exc}", file=sys.stderr)

    worker = threading.Thread(target=sync_loop, daemon=True)
    worker.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
    print("\nAgentBoard stopped.")
    return 0


def cmd_status(args):
    try:
        root = find_repo_root(Path(args.repo))
    except GitError as exc:
        die(
            "agentboard needs a git repository: run it from inside one, or pass --repo PATH. "
            f"(git said: {exc})"
        )
    git = GitFacts(root, args.base, args.remote)
    db_path = git.common_dir() / "agentboard" / "board.sqlite3"
    if not db_path.exists():
        print(f"No board yet in {db_path}.\nStart it with: uv run agentboard.py serve")
        return 0
    app = App(root, args.base, args.remote, args.port)
    sys.stdout.write(render_status(app))
    return 0


GIT_IDENTITY = [
    "-c", "user.name=AgentBoard Test",
    "-c", "user.email=agentboard@example.invalid",
    "-c", "init.defaultBranch=main",
]
SELFTEST_TESTS = []


def selftest(fn):
    SELFTEST_TESTS.append(fn)
    return fn


def gitx(args, cwd=None):
    proc = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


class Env:
    def __init__(self, root):
        self.root = Path(root)
        self.origin = self.root / "origin.git"
        gitx(["init", "--bare", str(self.origin)])
        self.seed = self.root / "seed"
        gitx(["clone", str(self.origin), str(self.seed)])
        (self.seed / "README.md").write_text("# test repo\n", encoding="utf-8")
        gitx(["add", "-A"], self.seed)
        gitx(["commit", "-m", "initial"], self.seed)
        gitx(["push", "-u", "origin", "main"], self.seed)
        self.clone = self.root / "work"
        gitx(["clone", str(self.origin), str(self.clone)])
        self.app = App(self.clone, "main", "origin", 0)
        self.httpd = make_server(self.app, 0)
        self.app.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.app.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method, path, data=None, json_body=None, headers=None):
        url = self.base_url + path
        body = None
        hdrs = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8"), dict(exc.headers)

    def json(self, method, path, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Accept"] = "application/json"
        code, text, _ = self.request(method, path, headers=headers, **kwargs)
        try:
            data = json.loads(text)
        except ValueError:
            raise AssertionError(f"{method} {path} did not return JSON ({code}): {text[:300]}")
        return code, data


@contextlib.contextmanager
def temp_env():
    with tempfile.TemporaryDirectory(prefix="agentboard-selftest-") as tmp:
        env = Env(tmp)
        try:
            yield env
        finally:
            env.close()


def import_cards(env, cards, actor="tester"):
    code, data = env.json("PUT", f"/cards?actor={actor}", json_body=cards)
    assert code == 200, f"import failed: {code} {data}"
    return data


def run_commands(env, commands, cwd=None):
    for command in commands:
        proc = subprocess.run(
            shlex.split(command),
            cwd=str(cwd or env.clone),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            raise AssertionError(f"command failed: {command}\n{(proc.stderr or proc.stdout).strip()}")


def write_commit_push(worktree, branch, message="work"):
    worktree = Path(worktree)
    (worktree / "change.txt").write_text(f"{message}\n{time.time()}\n", encoding="utf-8")
    gitx(["add", "-A"], worktree)
    gitx(["commit", "-m", message], worktree)
    gitx(["push", "-u", "origin", branch], worktree)


def claim_and_finish(env, card_id, agent="tester"):
    code, data = env.json("POST", f"/cards/{card_id}/claim?format=json", data={"agent": agent})
    assert code == 200, data
    run_commands(env, data["commands"])
    worktree = env.root / "work-worktrees" / card_id
    write_commit_push(worktree, f"card/{card_id}", f"{card_id} work")
    code, data = env.json(
        "POST", f"/cards/{card_id}/report", data={"pr": f"https://example.invalid/{card_id}"}
    )
    assert code == 200, data
    return worktree


@selftest
def t01_schema_and_triggers():
    with temp_env() as env:
        import_cards(env, [{"id": "TRG-01", "title": "done card", "done": True}])
        conn = sqlite3.connect(str(env.app.store.db_path))
        try:
            attempts = [
                "UPDATE cards SET title = 'x' WHERE id = 'TRG-01'",
                "DELETE FROM cards WHERE id = 'TRG-01'",
                "UPDATE events SET kind = 'x'",
                "DELETE FROM events",
            ]
            for statement in attempts:
                try:
                    conn.execute(statement)
                    conn.commit()
                except sqlite3.IntegrityError:
                    conn.rollback()
                    continue
                raise AssertionError(f"the database allowed: {statement}")
        finally:
            conn.close()


@selftest
def t02_import_create_update_unchanged_refused():
    with temp_env() as env:
        result = import_cards(env, [
            {"id": "IMP-00", "title": "already done", "done": True},
            {"id": "IMP-01", "title": "first", "group": "core", "depends_on": ["IMP-00"],
             "scope": ["src/**"], "source": "spec.md", "body": "Body one"},
        ])
        assert result["created"] == ["IMP-00", "IMP-01"], result
        code, data = env.json("GET", "/cards?status=ready")
        assert [c["id"] for c in data["cards"]] == ["IMP-01"], data
        result = import_cards(env, [
            {"id": "IMP-00", "title": "already done CHANGED", "done": True},
            {"id": "IMP-01", "title": "first", "group": "core", "depends_on": ["IMP-00"],
             "scope": ["src/**"], "source": "spec.md", "body": "Body one"},
        ])
        assert result["refused_done"] == ["IMP-00"], result
        assert result["unchanged"] == ["IMP-01"], result
        assert env.app.store.get_row("IMP-00")["title"] == "already done"
        result = import_cards(env, [
            {"id": "IMP-01", "title": "first renamed", "group": "core",
             "depends_on": ["IMP-00"], "scope": ["src/**"], "source": "spec.md",
             "body": "Body one"},
        ])
        assert result["updated"] == ["IMP-01"], result
        assert env.app.store.get_row("IMP-01")["title"] == "first renamed"
        code, _ = env.json("PUT", "/cards", json_body=[{"id": "IMP-01", "title": "back", "done": True}])
        assert code == 200
        row = env.app.store.get_row("IMP-01")
        assert row["title"] == "back" and row["status"] == "todo", dict(row)
        code, data = env.json("PUT", "/cards", json_body=[{"id": "IMP-02", "title": "x", "depends_on": ["NOPE-9"]}])
        assert code == 400 and "NOPE-9" in data["error"], (code, data)
        assert env.app.store.get_row("IMP-02") is None
        code, data = env.json("PUT", "/cards", json_body=[
            {"id": "CYC-1", "title": "a", "depends_on": ["CYC-2"]},
            {"id": "CYC-2", "title": "b", "depends_on": ["CYC-1"]},
        ])
        assert code == 400 and "CYC-1" in data["error"] and "CYC-2" in data["error"], (code, data)
        assert env.app.store.get_row("CYC-1") is None and env.app.store.get_row("CYC-2") is None
        code, data = env.json("PUT", "/cards", json_body=[{"id": "bad id", "title": "x"}])
        assert code == 400, (code, data)
        code, data = env.json("PUT", "/cards", json_body=[{"id": "IMP-03"}])
        assert code == 400, (code, data)


@selftest
def t03_claim_commands_and_worktree():
    with temp_env() as env:
        import_cards(env, [{"id": "CMD-01", "title": "commands"}])
        before = gitx(["worktree", "list", "--porcelain"], env.clone) + gitx(["branch", "--list"], env.clone)
        code, data = env.json("POST", "/cards/CMD-01/claim", data={"agent": "tester"})
        assert code == 200, data
        assert data["branch"] == "card/CMD-01", data
        assert data["worktree"] == "../work-worktrees/CMD-01", data
        after = gitx(["worktree", "list", "--porcelain"], env.clone) + gitx(["branch", "--list"], env.clone)
        assert before == after, "claim changed the repo on disk"
        run_commands(env, data["commands"])
        worktree = env.root / "work-worktrees" / "CMD-01"
        assert worktree.is_dir(), "the worktree was not created"
        head = gitx(["rev-parse", "--abbrev-ref", "HEAD"], worktree).strip()
        assert head == "card/CMD-01", head
        write_commit_push(worktree, "card/CMD-01", "cmd work")
        gitx(["worktree", "remove", "--force", str(worktree)], env.clone)
        gitx(["branch", "-D", "card/CMD-01"], env.clone)
        code, data = env.json("POST", "/cards/CMD-01/release", data={"agent": "tester"})
        assert code == 200, data
        code, data = env.json("POST", "/cards/CMD-01/claim", data={"agent": "tester"})
        assert code == 200, data
        assert any("card/CMD-01" in command for command in data["commands"]), data
        run_commands(env, data["commands"])
        head = gitx(["rev-parse", "--abbrev-ref", "HEAD"], worktree).strip()
        assert head == "card/CMD-01", head


@selftest
def t04_concurrent_claims():
    with temp_env() as env:
        import_cards(env, [{"id": "RACE-01", "title": "race"}])
        results = []
        barrier = threading.Barrier(2)

        def worker(name):
            barrier.wait()
            code, text, _ = env.request("POST", "/cards/RACE-01/claim", data={"agent": name})
            results.append((name, code, text))

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("alice", "bob")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        winners = [r for r in results if r[1] == 200]
        losers = [r for r in results if r[1] == 409]
        assert len(winners) == 1, results
        assert len(losers) == 1, results
        assert winners[0][0] in losers[0][2], f"409 did not name the holder: {results}"


@selftest
def t05_report_git_facts():
    with temp_env() as env:
        import_cards(env, [{"id": "REP-01", "title": "report"}])
        code, data = env.json("POST", "/cards/REP-01/report", data={"pr": "https://example.invalid/1"})
        assert code == 409 and "claim" in data["error"].lower(), (code, data)
        code, data = env.json("POST", "/cards/REP-01/claim", data={"agent": "tester"})
        assert code == 200, data
        code, data = env.json("POST", "/cards/REP-01/report", data={"pr": "https://example.invalid/1"})
        assert code == 409 and "not on" in data["error"], (code, data)
        gitx(["branch", "card/REP-01", "origin/main"], env.clone)
        gitx(["push", "origin", "card/REP-01"], env.clone)
        code, data = env.json("POST", "/cards/REP-01/report", data={"pr": "https://example.invalid/1"})
        assert code == 409 and "no commits beyond" in data["error"], (code, data)
        worktree = env.root / "rep-worktree"
        gitx(["worktree", "add", str(worktree), "card/REP-01"], env.clone)
        write_commit_push(worktree, "card/REP-01", "rep work")
        code, data = env.json(
            "POST", "/cards/REP-01/report",
            data={"pr": "https://example.invalid/1", "note": "ready for review"},
        )
        assert code == 200 and data["status"] == "in_review", (code, data)
        assert data["commits"] >= 1, data
        row = env.app.store.get_row("REP-01")
        assert row["status"] == "in_review" and row["pr_url"].endswith("/1"), dict(row)
        code, data = env.json("POST", "/cards/REP-01/report", data={"pr": "https://example.invalid/2"})
        assert code == 409 and "already in review" in data["error"], (code, data)


@selftest
def t06_merge_sync_dependents():
    with temp_env() as env:
        import_cards(env, [
            {"id": "FLOW-01", "title": "first"},
            {"id": "FLOW-02", "title": "second", "depends_on": ["FLOW-01"]},
        ])
        code, data = env.json("GET", "/cards?status=ready")
        assert [c["id"] for c in data["cards"]] == ["FLOW-01"], data
        code, data = env.json("POST", "/cards/FLOW-02/claim", data={"agent": "tester"})
        assert code == 409 and "FLOW-01" in data["error"], (code, data)
        claim_and_finish(env, "FLOW-01")
        gitx(["fetch", "origin"], env.seed)
        gitx(["checkout", "main"], env.seed)
        gitx(["merge", "--no-ff", "origin/card/FLOW-01", "-m", "merge FLOW-01"], env.seed)
        gitx(["push", "origin", "main"], env.seed)
        code, data = env.json("POST", "/sync")
        assert code == 200 and data["newly_done"] == ["FLOW-01"], (code, data)
        assert env.app.store.get_row("FLOW-01")["status"] == "done"
        code, data = env.json("GET", "/cards?status=ready")
        assert [c["id"] for c in data["cards"]] == ["FLOW-02"], data
        events = env.app.store.events_for("FLOW-01", limit=5)
        assert any(e["kind"] == "merged" and e["actor"] == "board" for e in events), events


@selftest
def t07_squash_manual_merged():
    with temp_env() as env:
        import_cards(env, [{"id": "SQ-01", "title": "squash"}])
        claim_and_finish(env, "SQ-01")
        gitx(["fetch", "origin"], env.seed)
        gitx(["checkout", "main"], env.seed)
        gitx(["merge", "--squash", "origin/card/SQ-01"], env.seed)
        gitx(["commit", "-m", "squash SQ-01"], env.seed)
        gitx(["push", "origin", "main"], env.seed)
        code, data = env.json("POST", "/sync")
        assert code == 200 and data["newly_done"] == [], data
        assert env.app.store.get_row("SQ-01")["status"] == "in_review"
        code, data = env.json(
            "POST", "/cards/SQ-01/merged", data={"note": "squashed, confirmed in GitHub"}
        )
        assert code == 200 and data["status"] == "done", data
        assert env.app.store.get_row("SQ-01")["done_at"]
        code, data = env.json("POST", "/cards/SQ-01/merged", data={"note": "again"})
        assert code == 409, (code, data)
        events = env.app.store.events_for("SQ-01", limit=5)
        assert any(
            e["kind"] == "merged" and e["actor"] == "person" and "squash" in json.dumps(e["detail"])
            for e in events
        ), events


@selftest
def t08_block_release_unblock():
    with temp_env() as env:
        import_cards(env, [{"id": "BLK-01", "title": "block"}])
        code, data = env.json("POST", "/cards/BLK-01/release", data={})
        assert code == 409, (code, data)
        code, data = env.json("POST", "/cards/BLK-01/claim", data={"agent": "tester"})
        assert code == 200, data
        code, data = env.json("POST", "/cards/BLK-01/block", data={"reason": "waiting for API keys"})
        assert code == 200 and data["status"] == "blocked", data
        row = env.app.store.get_row("BLK-01")
        assert row["blocked_reason"] == "waiting for API keys", dict(row)
        code, data = env.json("POST", "/cards/BLK-01/claim", data={"agent": "other"})
        assert code == 409 and "blocked" in data["error"], (code, data)
        code, data = env.json("POST", "/cards/BLK-01/release", data={})
        assert code == 409 and "unblock" in data["error"], (code, data)
        code, data = env.json("POST", "/cards/BLK-01/unblock", data={"note": "keys rotated, go"})
        assert code == 200 and data["status"] == "todo", data
        row = env.app.store.get_row("BLK-01")
        assert row["blocked_reason"] is None and row["agent"] is None, dict(row)
        assert row["branch"] == "card/BLK-01", dict(row)
        code, data = env.json("POST", "/cards/BLK-01/claim", data={"agent": "other"})
        assert code == 200, data
        code, data = env.json("POST", "/cards/BLK-01/release", data={"agent": "other"})
        assert code == 200, data
        row = env.app.store.get_row("BLK-01")
        assert row["status"] == "todo" and row["agent"] is None, dict(row)
        assert row["branch"] == "card/BLK-01", dict(row)


@selftest
def t09_done_permanent_api():
    with temp_env() as env:
        import_cards(env, [{"id": "DONE-01", "title": "finished", "done": True}])
        calls = [
            ("/cards/DONE-01/claim", {"agent": "x"}),
            ("/cards/DONE-01/report", {"pr": "https://example.invalid/1"}),
            ("/cards/DONE-01/block", {"reason": "x"}),
            ("/cards/DONE-01/release", {}),
            ("/cards/DONE-01/unblock", {"note": "x"}),
            ("/cards/DONE-01/merged", {"note": "x"}),
        ]
        for path, payload in calls:
            code, data = env.json("POST", path, data=payload)
            assert code == 409, (path, code, data)
            assert "done" in data["error"].lower(), (path, data)
        result = import_cards(env, [{"id": "DONE-01", "title": "changed"}])
        assert result["refused_done"] == ["DONE-01"], result
        assert env.app.store.get_row("DONE-01")["title"] == "finished"


@selftest
def t10_events_and_since():
    with temp_env() as env:
        import_cards(env, [{"id": "EV-01", "title": "events"}])
        code, data = env.json("POST", "/cards/EV-01/claim", data={"agent": "tester"})
        assert code == 200
        code, data = env.json("GET", "/events")
        events = data["events"]
        assert all(e["actor"] and e["at"] and e["kind"] for e in events), events
        kinds = [e["kind"] for e in events]
        assert kinds == ["imported", "claimed"], kinds
        mid = events[0]["id"]
        code, data = env.json("GET", f"/events?since={mid}")
        assert [e["kind"] for e in data["events"]] == ["claimed"], data
        code, data = env.json("POST", "/cards/EV-01/block", data={"reason": "x"})
        assert code == 200
        code, data = env.json("GET", f"/events?since={mid}")
        assert [e["kind"] for e in data["events"]] == ["claimed", "blocked"], data


@selftest
def t11_agent_page_selfsufficient():
    with temp_env() as env:
        import_cards(env, [{"id": "AG-01", "title": "agent page"}])
        code, text, _ = env.request("GET", "/agent")
        assert code == 200
        first_line = text.splitlines()[0]
        assert "shell" in first_line and "web tool" in first_line, first_line
        for token in ("Invoke-RestMethod", "curl.exe", "AG-01", "/claim", "/report",
                      "AGENTS.md", "go-ahead", env.base_url, "PUT", "/merged", "/sync",
                      "depends_on", "--data-binary", "-InFile"):
            assert token in text, f"/agent is missing {token!r}"
        code, data = env.json("GET", "/agent")
        assert code == 200 and data["ready"], data


@selftest
def t12_html_readonly():
    with temp_env() as env:
        import_cards(env, [{"id": "UI-01", "title": "page"}])
        code, text, _ = env.request("GET", "/")
        assert code == 200
        lowered = text.lower()
        for banned in ("<form", "<button", "<input", "<select", "onclick=", "<textarea"):
            assert banned not in lowered, f"the page contains {banned}"
        for needed in ("Waiting", "Ready", "Claimed", "In review", "Blocked", "Done",
                       "UI-01", "prefers-color-scheme", "setInterval", "5000"):
            assert needed in text, f"the page is missing {needed}"
        code, text, _ = env.request("GET", "/view/UI-01")
        assert code == 200 and "UI-01" in text and "Events" in text
        code, text, _ = env.request("GET", "/view/NOPE-99")
        assert code == 404, (code, text)


@selftest
def t13_errors_and_json():
    with temp_env() as env:
        code, text, _ = env.request("GET", "/cards/GHOST-1")
        assert code == 404 and "GET /cards" in text, (code, text)
        code, text, _ = env.request("GET", "/cards/GHOST-1?format=json")
        assert code == 404 and json.loads(text)["error"], (code, text)
        code, text, _ = env.request("GET", "/cards?status=bogus")
        assert code == 400, (code, text)
        import_cards(env, [{"id": "J-01", "title": "json"}])
        code, text, _ = env.request("GET", "/cards", headers={"Accept": "application/json"})
        data = json.loads(text)
        assert data["cards"][0]["id"] == "J-01", data
        code, text, _ = env.request("POST", "/cards/J-01/claim", data={})
        assert code == 400 and "agent" in text, (code, text)
        code, text, _ = env.request("GET", "/nope")
        assert code == 404, (code, text)


@selftest
def t14_status_render():
    with temp_env() as env:
        import_cards(env, [{"id": "ST-01", "title": "status"}])
        text = render_status(env.app)
        for token in ("ST-01", "Ready", "Done", "Waiting", "Claimed", "In review", "Blocked"):
            assert token in text, f"status is missing {token}"


@selftest
def t15_source_stdlib_and_packaging():
    source = Path(__file__).read_text(encoding="utf-8")
    lines = source.splitlines()
    assert lines[0].startswith("# /// script"), lines[0]
    header = "\n".join(lines[:10])
    assert 'requires-python = ">=3.12"' in header, header
    assert "dependencies = []" in header, header
    forbidden = "shell" + "=True"
    assert forbidden not in source
    tree = ast.parse(source)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    third_party = {module for module in imports if module not in sys.stdlib_module_names}
    assert not third_party, f"third-party imports: {third_party}"


@selftest
def t16_stale_marker():
    global STALE_SECONDS
    with temp_env() as env:
        import_cards(env, [{"id": "STALE-01", "title": "stale"}])
        code, data = env.json("POST", "/cards/STALE-01/claim", data={"agent": "slowpoke"})
        assert code == 200, data
        saved = STALE_SECONDS
        STALE_SECONDS = 0
        try:
            code, text, _ = env.request("GET", "/agent")
            assert "STALE" in text, text
            code, text, _ = env.request("GET", "/")
            assert "STALE" in text.upper(), text
            code, data = env.json("GET", "/cards?status=claimed")
            assert data["cards"][0]["stale"] is True, data
        finally:
            STALE_SECONDS = saved
        code, data = env.json("GET", "/cards?status=claimed")
        assert data["cards"][0]["stale"] is False, data


@selftest
def t17_body_encodings():
    with temp_env() as env:
        def put_raw(raw, content_type):
            req = urllib.request.Request(
                env.base_url + "/cards?actor=tester&format=json", data=raw, method="PUT",
                headers={"Content-Type": content_type},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status
        text = '[{"id": "ENC-01", "title": "Configuración de importación"}]'
        assert put_raw(text.encode("cp1252"), "application/json") == 200
        assert env.app.store.get_row("ENC-01")["title"] == "Configuración de importación"
        text = '[{"id": "ENC-02", "title": "Señal"}]'
        assert put_raw(text.encode("iso-8859-1"), "application/json; charset=iso-8859-1") == 200
        assert env.app.store.get_row("ENC-02")["title"] == "Señal"
        text = '[{"id": "ENC-03", "title": "Ruta → paso · listo"}]'
        assert put_raw(b"\xef\xbb\xbf" + text.encode("utf-8"), "application/json") == 200
        assert env.app.store.get_row("ENC-03")["title"] == "Ruta → paso · listo"


@selftest
def t18_merge_detected_after_rewrite_elsewhere():
    with temp_env() as env:
        import_cards(env, [{"id": "FP-01", "title": "rewritten after review"}])
        claim_and_finish(env, "FP-01")
        # The review fix is pushed from another clone (a reviewer, another machine), rewriting the
        # branch; the board's clone has never seen the new head before it is merged.
        gitx(["fetch", "origin"], env.seed)
        gitx(["checkout", "-B", "review-fix", "origin/card/FP-01"], env.seed)
        (env.seed / "change.txt").write_text("rewritten after review\n", encoding="utf-8")
        gitx(["add", "-A"], env.seed)
        gitx(["commit", "--amend", "-m", "FP-01 work, rewritten"], env.seed)
        gitx(["push", "--force", "origin", "HEAD:card/FP-01"], env.seed)
        code, data = env.json("POST", "/sync")  # a regular sync while the PR is open
        assert code == 200 and data["newly_done"] == [], (code, data)
        # Merged with a merge commit, and the branch deleted on merge, as GitHub can do.
        gitx(["fetch", "origin"], env.seed)
        gitx(["checkout", "main"], env.seed)
        gitx(["merge", "--no-ff", "origin/card/FP-01", "-m", "merge FP-01"], env.seed)
        gitx(["push", "origin", "main"], env.seed)
        gitx(["push", "origin", "--delete", "card/FP-01"], env.seed)
        code, data = env.json("POST", "/sync")
        assert code == 200 and data["newly_done"] == ["FP-01"], (code, data)


def read_sse_event(resp):
    fields = {}
    while True:
        raw = resp.readline()
        if not raw:
            raise AssertionError("the event stream closed unexpectedly")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if fields.get("event"):
                return fields
            fields = {}
            continue
        if line.startswith(":"):
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key] = value.lstrip()


@selftest
def t19_board_snapshot_and_sse():
    with temp_env() as env:
        import_cards(env, [{"id": "SSE-01", "title": "stream"}])
        code, data = env.json("GET", "/board")
        assert code == 200, data
        assert data["base"] == "origin/main", data
        assert [c["id"] for c in data["cards"]] == ["SSE-01"], data
        assert data["counts"]["ready"] == 1, data
        conn = http.client.HTTPConnection("127.0.0.1", env.app.port, timeout=15)
        try:
            conn.request("GET", "/events/stream")
            resp = conn.getresponse()
            assert resp.status == 200
            assert "text/event-stream" in (resp.getheader("Content-Type") or "")
            first = read_sse_event(resp)
            assert first["event"] == "snapshot", first
            snapshot = json.loads(first["data"])
            assert snapshot["cards"][0]["id"] == "SSE-01", snapshot
            code, _ = env.json("POST", "/cards/SSE-01/claim", data={"agent": "tester"})
            assert code == 200
            second = read_sse_event(resp)
            assert second["event"] == "event", second
            event = json.loads(second["data"])
            assert event["kind"] == "claimed" and event["actor"] == "tester", event
        finally:
            conn.close()


def run_selftest():
    print(f"AgentBoard selftest ({len(SELFTEST_TESTS)} tests)")
    failed = 0
    started = time.time()
    for fn in SELFTEST_TESTS:
        name = fn.__name__
        try:
            fn()
            print(f"ok   {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    total = len(SELFTEST_TESTS)
    print(f"\n{total - failed}/{total} passed in {time.time() - started:.1f}s")
    return 1 if failed else 0


def cmd_selftest(args):
    return run_selftest()


def cmd_tui(args):
    script = Path(__file__).resolve().with_name("agentboard-tui.py")
    if not script.is_file():
        die("agentboard-tui.py is missing next to agentboard.py; keep the two files together.")
    uv = shutil.which("uv")
    if not uv:
        die("the console runs through uv; install uv or run agentboard-tui.py directly.")
    command = [uv, "run", "--script", str(script)]
    if args.selftest:
        command.append("--selftest")
        if args.url:
            command.extend(["--url", args.url])
    else:
        command.extend(["--url", args.url or f"http://127.0.0.1:{DEFAULT_PORT}"])
    return subprocess.call(command)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw:
        raw = ["serve"]
    elif raw[0].startswith("-") and raw[0] not in ("-h", "--help", "--version"):
        raw.insert(0, "serve")
    parser = argparse.ArgumentParser(
        prog="agentboard",
        description="AgentBoard - a shared list of cards that coding agents read, claim and report on.",
    )
    parser.add_argument("--version", action="version", version=f"AgentBoard {VERSION}")
    sub = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("serve", "start the board on 127.0.0.1 and sync every 60 seconds (default)"),
        ("status", "print the board as text, without a server"),
        ("selftest", "run the daemon's built-in tests"),
        ("tui", "open the terminal console against a running board"),
    ):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        if name == "tui":
            sp.add_argument(
                "--url",
                default=None,
                help=f"the board's address (default: http://127.0.0.1:{DEFAULT_PORT})",
            )
            sp.add_argument(
                "--selftest", action="store_true", help="run the console's headless checks instead"
            )
        else:
            sp.add_argument("--repo", default=".", help="the repository the board serves (default: .)")
            sp.add_argument("--base", default=DEFAULT_BASE, help=f"base branch (default: {DEFAULT_BASE})")
            sp.add_argument("--remote", default=DEFAULT_REMOTE, help=f"git remote (default: {DEFAULT_REMOTE})")
            sp.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default: {DEFAULT_PORT})")
    args = parser.parse_args(raw)
    if args.command in (None, "serve"):
        return cmd_serve(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "tui":
        return cmd_tui(args)
    return cmd_selftest(args)


if __name__ == "__main__":
    raise SystemExit(main())
