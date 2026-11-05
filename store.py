"""SQLite persistence for WhatsApp inbox, outbox, Codex chats and jobs.

Compatible with the original bot's chats/active_chats tables. Own SQLite
connections are opened and closed for every operation (thread safe).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path: Path, default_cwd: str):
        self.path = Path(path).expanduser().resolve()
        self.default_cwd = default_cwd
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.setup()

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def setup(self):
        with self.db() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS chats (
                    sender TEXT NOT NULL, name TEXT NOT NULL, thread_id TEXT,
                    PRIMARY KEY(sender,name));
                CREATE TABLE IF NOT EXISTS active_chats (
                    sender TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inbox (
                    id TEXT PRIMARY KEY, sender TEXT NOT NULL, body TEXT NOT NULL,
                    received_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'new');
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT NOT NULL,
                    body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                    error TEXT, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, inbox_id TEXT UNIQUE,
                    sender TEXT NOT NULL, name TEXT NOT NULL, generation INTEGER NOT NULL,
                    cwd TEXT NOT NULL, prompt TEXT NOT NULL, response TEXT,
                    status TEXT NOT NULL, model TEXT, effort TEXT,
                    thread_id TEXT, submitted_at REAL NOT NULL,
                    started_at REAL, finished_at REAL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    error TEXT);
                CREATE TABLE IF NOT EXISTS menus (
                    sender TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                    options TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS server_choices (
                    sender TEXT NOT NULL, number INTEGER NOT NULL,
                    thread_id TEXT NOT NULL, title TEXT NOT NULL,
                    cwd TEXT, created_at REAL NOT NULL,
                    PRIMARY KEY(sender,number));
                CREATE INDEX IF NOT EXISTS ix_inbox_state ON inbox(state,received_at);
                CREATE INDEX IF NOT EXISTS ix_turn_status ON turns(status,submitted_at);
                CREATE INDEX IF NOT EXISTS ix_outbox_next ON outbox(status,retry_at);
            """)
            # In-place migration of original versions, without deleting history.
            fields = {r[1] for r in c.execute("PRAGMA table_info(chats)").fetchall()}
            for field, dtype in (("model", "TEXT"), ("effort", "TEXT"),
                                 ("cwd", "TEXT"), ("generation", "INTEGER NOT NULL DEFAULT 0")):
                if field not in fields:
                    c.execute(f"ALTER TABLE chats ADD COLUMN {field} {dtype}")
            c.execute("UPDATE chats SET cwd=? WHERE cwd IS NULL OR cwd=''", (self.default_cwd,))
            # Prior per-chat model selector, if present.
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_model_choices'").fetchone():
                c.execute("""UPDATE chats SET model=(SELECT model FROM chat_model_choices m
                    WHERE m.sender=chats.sender AND m.name=chats.name)
                    WHERE model IS NULL AND EXISTS (SELECT 1 FROM chat_model_choices m
                    WHERE m.sender=chats.sender AND m.name=chats.name)""")
            # Crash recovery: never re-run an already-started code-changing turn.
            c.execute("""UPDATE turns SET status='interrupted',
                error='Bot restarted during execution; check files before retrying',
                finished_at=? WHERE status='running'""", (time.time(),))
            # Commands are inexpensive, but may have been interrupted after side effects.
            # Do not replay them blindly; sender can resend if needed.
            c.execute("UPDATE inbox SET state='interrupted' WHERE state='processing'")
            # Sending may have reached Meta just before a crash; do not blindly resend.
            c.execute("UPDATE outbox SET status='uncertain' WHERE status='sending'")

    def _ensure(self, c, sender: str):
        c.execute("INSERT OR IGNORE INTO chats(sender,name,thread_id,cwd,generation) VALUES(?,?,NULL,?,0)",
                  (sender, "main", self.default_cwd))
        c.execute("INSERT OR IGNORE INTO active_chats(sender,name) VALUES(?,?)", (sender, "main"))
        current = c.execute("SELECT name FROM active_chats WHERE sender=?", (sender,)).fetchone()[0]
        if not c.execute("SELECT 1 FROM chats WHERE sender=? AND name=?", (sender, current)).fetchone():
            c.execute("UPDATE active_chats SET name='main' WHERE sender=?", (sender,))

    def current(self, sender: str) -> dict:
        with self.db() as c:
            self._ensure(c, sender)
            row = c.execute("""SELECT c.* FROM chats c JOIN active_chats a
                ON c.sender=a.sender AND c.name=a.name WHERE c.sender=?""", (sender,)).fetchone()
            return dict(row)

    def chat(self, sender: str, name: str) -> dict | None:
        with self.db() as c:
            row = c.execute("SELECT * FROM chats WHERE sender=? AND name=?", (sender, name)).fetchone()
            return dict(row) if row else None

    def chats(self, sender: str) -> list[dict]:
        with self.db() as c:
            self._ensure(c, sender)
            active = c.execute("SELECT name FROM active_chats WHERE sender=?", (sender,)).fetchone()[0]
            rows = c.execute("SELECT * FROM chats WHERE sender=? ORDER BY name COLLATE NOCASE", (sender,)).fetchall()
            return [dict(r) | {"active": r["name"] == active} for r in rows]

    def create_chat(self, sender: str, name: str, thread_id: str | None = None,
                    cwd: str | None = None) -> bool:
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._ensure(c, sender)
            try:
                c.execute("INSERT INTO chats(sender,name,thread_id,cwd,generation) VALUES(?,?,?,?,0)",
                          (sender, name, thread_id, cwd or self.default_cwd))
                c.execute("UPDATE active_chats SET name=? WHERE sender=?", (name, sender))
                c.commit()
                return True
            except sqlite3.IntegrityError:
                c.rollback()
                return False

    def use_chat(self, sender: str, name: str) -> bool:
        with self.db() as c:
            self._ensure(c, sender)
            if not c.execute("SELECT 1 FROM chats WHERE sender=? AND name=?", (sender, name)).fetchone():
                return False
            c.execute("UPDATE active_chats SET name=? WHERE sender=?", (name, sender))
            return True

    def has_pending(self, sender: str, name: str) -> bool:
        with self.db() as c:
            return bool(c.execute("""SELECT 1 FROM turns WHERE sender=? AND name=?
                AND status IN ('queued','running') LIMIT 1""", (sender, name)).fetchone())

    def rename_chat(self, sender: str, old: str, new: str) -> str:
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._ensure(c, sender)
            if c.execute("SELECT 1 FROM chats WHERE sender=? AND name=?", (sender, new)).fetchone():
                c.rollback(); return "That name already exists."
            if not c.execute("SELECT 1 FROM chats WHERE sender=? AND name=?", (sender, old)).fetchone():
                c.rollback(); return "Chat not found."
            if self._pending_in(c, sender, old):
                c.rollback(); return "Wait for queued/running tasks before renaming this chat."
            c.execute("UPDATE chats SET name=? WHERE sender=? AND name=?", (new, sender, old))
            c.execute("UPDATE turns SET name=? WHERE sender=? AND name=?", (new, sender, old))
            c.execute("UPDATE active_chats SET name=? WHERE sender=? AND name=?", (new, sender, old))
            c.execute("DELETE FROM menus WHERE sender=?", (sender,))
            c.commit()
            return f"Renamed {old} to {new}."

    @staticmethod
    def _pending_in(c, sender, name):
        return c.execute("""SELECT 1 FROM turns WHERE sender=? AND name=?
            AND status IN ('queued','running') LIMIT 1""", (sender, name)).fetchone()

    def delete_chat(self, sender: str, name: str) -> str:
        if name == "main":
            return "Cannot delete main; use /reset instead."
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._ensure(c, sender)
            if self._pending_in(c, sender, name):
                c.rollback(); return "Wait for queued/running tasks before deleting this chat."
            cur = c.execute("DELETE FROM chats WHERE sender=? AND name=?", (sender, name))
            if not cur.rowcount:
                c.rollback(); return "Chat not found."
            c.execute("DELETE FROM turns WHERE sender=? AND name=?", (sender, name))
            c.execute("UPDATE active_chats SET name='main' WHERE sender=? AND name=?", (sender, name))
            c.execute("DELETE FROM menus WHERE sender=?", (sender,))
            c.commit()
            return f"Removed {name} from this bot. The saved Codex session remains on disk."

    def reset_chat(self, sender: str) -> str:
        chat = self.current(sender)
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            if self._pending_in(c, sender, chat["name"]):
                c.rollback(); return "Wait for queued/running tasks before resetting this chat."
            c.execute("""UPDATE chats SET thread_id=NULL, generation=generation+1
                WHERE sender=? AND name=?""", (sender, chat["name"]))
            c.execute("DELETE FROM menus WHERE sender=?", (sender,))
            c.commit()
            return f"Reset {chat['name']}; next request starts a fresh Codex session."

    def set_chat_option(self, sender: str, name: str, key: str, value):
        if key not in {"model", "effort", "cwd"}:
            raise ValueError("Unknown chat property")
        with self.db() as c:
            c.execute(f"UPDATE chats SET {key}=? WHERE sender=? AND name=?", (value, sender, name))

    def put_menu(self, sender: str, name: str, kind: str, options: list):
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO menus(sender,name,kind,options,created_at) VALUES(?,?,?,?,?)",
                      (sender, name, kind, json.dumps(options), time.time()))

    def menu(self, sender: str, name: str, max_age: int = 1800) -> dict | None:
        with self.db() as c:
            row = c.execute("SELECT * FROM menus WHERE sender=? AND name=?", (sender, name)).fetchone()
            if not row or time.time() - row["created_at"] > max_age:
                return None
            return {"kind": row["kind"], "options": json.loads(row["options"])}

    def clear_menu(self, sender: str):
        with self.db() as c:
            c.execute("DELETE FROM menus WHERE sender=?", (sender,))

    def set_server_choices(self, sender: str, values: list[dict]):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM server_choices WHERE sender=?", (sender,))
            c.executemany("""INSERT INTO server_choices(sender,number,thread_id,title,cwd,created_at)
                VALUES(?,?,?,?,?,?)""",
                [(sender, i, value["id"], value["title"], value.get("cwd"), time.time())
                 for i, value in enumerate(values, 1)])
            c.commit()

    def server_choice(self, sender: str, number: int, max_age: int = 3600) -> dict | None:
        with self.db() as c:
            row = c.execute("SELECT * FROM server_choices WHERE sender=? AND number=?", (sender, number)).fetchone()
            if row and time.time() - row["created_at"] < max_age:
                return dict(row)
            return None

    def ingest(self, message_id: str, sender: str, body: str) -> bool:
        with self.db() as c:
            cur = c.execute("INSERT OR IGNORE INTO inbox(id,sender,body,received_at) VALUES(?,?,?,?)",
                            (message_id, sender, body, time.time()))
            return cur.rowcount == 1

    def take_inbox(self) -> dict | None:
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM inbox WHERE state='new' ORDER BY received_at,id LIMIT 1").fetchone()
            if row:
                c.execute("UPDATE inbox SET state='processing' WHERE id=?", (row["id"],))
            c.commit()
            return dict(row) if row else None

    def finish_inbox(self, message_id: str, state: str = "done"):
        with self.db() as c:
            c.execute("UPDATE inbox SET state=? WHERE id=?", (state, message_id))

    def reply(self, sender: str, text: str):
        """Persist individual WhatsApp-sized chunks for more reliable delivery."""
        suffix = "\n[Response truncated in WhatsApp; see /history on this chat]"
        cap = 12 * 3000
        if len(text) > cap:
            text = text[:cap-len(suffix)] + suffix
        chunks = [text[i:i+3000] for i in range(0, len(text), 3000)] or ["(Empty response)"]
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            c.executemany("""INSERT INTO outbox(recipient,body,status,created_at)
                VALUES(?,?,'pending',?)""", [(sender, chunk, time.time()) for chunk in chunks])
            c.commit()

    def take_outbox(self) -> dict | None:
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            # Preserve per-recipient message order when an earlier chunk is
            # waiting for an API retry; other senders can still proceed.
            row = c.execute("""SELECT o.* FROM outbox o WHERE o.status='pending' AND o.retry_at<=?
                AND NOT EXISTS (SELECT 1 FROM outbox p WHERE p.recipient=o.recipient
                    AND p.id<o.id AND p.status IN ('pending','sending'))
                ORDER BY o.id LIMIT 1""", (time.time(),)).fetchone()
            if row:
                c.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?", (row["id"],))
            c.commit()
            return dict(row) if row else None

    def mark_outbox(self, message_id: int, status: str, error: str = "", delay: float = 0):
        with self.db() as c:
            c.execute("UPDATE outbox SET status=?,error=?,retry_at=? WHERE id=?",
                      (status, error[:500], time.time()+delay, message_id))

    def enqueue_turn(self, message_id: str, sender: str, prompt: str) -> tuple[int, str]:
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            self._ensure(c, sender)
            chat = c.execute("""SELECT c.* FROM chats c JOIN active_chats a
                ON c.sender=a.sender AND c.name=a.name WHERE c.sender=?""", (sender,)).fetchone()
            existing = c.execute("SELECT id,name FROM turns WHERE inbox_id=?", (message_id,)).fetchone()
            if existing:
                c.commit(); return existing["id"], existing["name"]
            cur = c.execute("""INSERT INTO turns(inbox_id,sender,name,generation,cwd,prompt,
                status,model,effort,submitted_at) VALUES(?,?,?,?,?,?,'queued',?,?,?)""",
                (message_id, sender, chat["name"], chat["generation"], chat["cwd"], prompt,
                 chat["model"], chat["effort"], time.time()))
            c.commit()
            return cur.lastrowid, chat["name"]

    def claim_turn(self) -> dict | None:
        """Serialize all jobs in the same repo; allow different repos in parallel."""
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("SELECT * FROM turns WHERE status='queued' ORDER BY id LIMIT 60").fetchall()
            active_dirs = {r[0] for r in c.execute("SELECT DISTINCT cwd FROM turns WHERE status='running'")}
            active_chats = {(r[0], r[1]) for r in c.execute("SELECT sender,name FROM turns WHERE status='running'")}
            for row in rows:
                if row["cwd"] in active_dirs or (row["sender"], row["name"]) in active_chats:
                    continue
                c.execute("UPDATE turns SET status='running',started_at=? WHERE id=?", (time.time(), row["id"]))
                c.commit()
                return dict(row)
            c.commit()
            return None

    def thread_for_turn(self, turn: dict) -> str | None:
        chat = self.chat(turn["sender"], turn["name"])
        if chat and chat["generation"] == turn["generation"]:
            return chat["thread_id"]
        return None

    def finish_turn(self, turn: dict, response: str, status: str,
                    thread_id: str | None = None, input_tokens: int = 0,
                    output_tokens: int = 0, error: str = ""):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("""UPDATE turns SET response=?,status=?,thread_id=?,finished_at=?,
                input_tokens=?,output_tokens=?,error=? WHERE id=?""",
                (response, status, thread_id, time.time(), input_tokens, output_tokens, error, turn["id"]))
            if status == "done" and thread_id:
                c.execute("""UPDATE chats SET thread_id=? WHERE sender=? AND name=?
                    AND generation=?""",
                    (thread_id, turn["sender"], turn["name"], turn["generation"]))
            c.commit()

    def history(self, sender: str, name: str, count: int = 1) -> list[dict]:
        chat = self.chat(sender, name)
        if not chat:
            return []
        with self.db() as c:
            rows = c.execute("""SELECT * FROM turns WHERE sender=? AND name=? AND generation=?
                ORDER BY id DESC LIMIT ?""", (sender, name, chat["generation"], count)).fetchall()
            return [dict(x) for x in rows]

    def pending_jobs(self, sender: str, limit: int = 20) -> list[dict]:
        with self.db() as c:
            rows = c.execute("""SELECT id,name,prompt,status,submitted_at FROM turns
                WHERE sender=? AND status IN ('running','queued') ORDER BY id LIMIT ?""", (sender, limit)).fetchall()
            return [dict(r) for r in rows]

    def token_stats(self, sender: str) -> dict:
        with self.db() as c:
            r = c.execute("""SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0)
                FROM turns WHERE sender=? AND status='done'""", (sender,)).fetchone()
            return {"input": r[0], "output": r[1]}

    def get_turn(self, turn_id: int) -> dict | None:
        with self.db() as c:
            row = c.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            return dict(row) if row else None
