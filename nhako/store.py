"""SQLite storage: conversation history, actions waiting for a confirm button, reminders, facts."""
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages (chat_id, id);

CREATE TABLE IF NOT EXISTS pending_actions (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    tool       TEXT    NOT NULL,
    args       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- due_at is UTC 'YYYY-MM-DD HH:MM:SS' so SQLite can compare it with datetime('now').
CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    due_at     TEXT    NOT NULL,
    sent       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders (sent, due_at);

-- Things the user asked Nhako to remember (Phase 6). Added to the model's system prompt.
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# A confirm button older than this is treated as expired.
PENDING_MAX_AGE_MINUTES = 30


@dataclass
class Reminder:
    id: int
    chat_id: int
    text: str
    due_at: dt.datetime  # timezone-aware UTC


def _utc_text(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


def _reminder(row) -> Reminder:
    due = dt.datetime.strptime(row[3], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.UTC)
    return Reminder(row[0], row[1], row[2], due)


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> "Store":
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        # WAL: a commit is an append instead of a rewrite, and a crash mid-write can't corrupt the file.
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=NORMAL")
        await self.db.executescript(SCHEMA)
        await self.db.commit()
        return self

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    async def add_message(self, chat_id: int, role: str, content: str) -> None:
        await self.db.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)", (chat_id, role, content))
        await self.db.commit()

    async def history(self, chat_id: int, limit: int) -> list[dict]:
        """The last `limit` messages, oldest first, in the shape Ollama expects."""
        async with self.db.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    async def save_pending(self, chat_id: int, tool: str, args: dict) -> int:
        cur = await self.db.execute(
            "INSERT INTO pending_actions (chat_id, tool, args) VALUES (?, ?, ?)",
            (chat_id, tool, json.dumps(args)))
        await self.db.commit()
        return cur.lastrowid

    async def pop_pending(self, action_id: int, chat_id: int) -> tuple[str, dict] | None:
        """Take a pending action out of the table. None if it is unknown, expired or from another chat."""
        async with self.db.execute(
            "SELECT tool, args FROM pending_actions WHERE id = ? AND chat_id = ?"
            " AND created_at >= datetime('now', ?)",
            (action_id, chat_id, f"-{PENDING_MAX_AGE_MINUTES} minutes"),
        ) as cur:
            row = await cur.fetchone()
        # Deleted whatever chat pressed it: a button pressed from the wrong chat is burned, not kept.
        await self.db.execute("DELETE FROM pending_actions WHERE id = ?", (action_id,))
        await self.db.commit()
        return (row[0], json.loads(row[1])) if row else None

    async def prune(self, keep_messages: int) -> None:
        """Daily housekeeping: only the newest messages per chat are ever read back, and an
        expired confirm button can't be pressed, so neither needs to be kept."""
        await self.db.execute(
            "DELETE FROM messages WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER "
            "(PARTITION BY chat_id ORDER BY id DESC) AS n FROM messages) WHERE n > ?)", (keep_messages,))
        await self.db.execute("DELETE FROM pending_actions WHERE created_at < datetime('now', ?)",
                              (f"-{PENDING_MAX_AGE_MINUTES} minutes",))
        await self.db.execute("DELETE FROM reminders WHERE sent = 1 AND due_at < datetime('now', '-30 days')")
        await self.db.commit()

    # --- reminders ---

    async def add_reminder(self, chat_id: int, text: str, due_at: dt.datetime) -> int:
        cur = await self.db.execute("INSERT INTO reminders (chat_id, text, due_at) VALUES (?, ?, ?)",
                                    (chat_id, text, _utc_text(due_at)))
        await self.db.commit()
        return cur.lastrowid

    async def due_reminders(self, now: dt.datetime) -> list[Reminder]:
        async with self.db.execute(
            "SELECT id, chat_id, text, due_at FROM reminders WHERE sent = 0 AND due_at <= ? ORDER BY due_at",
            (_utc_text(now),),
        ) as cur:
            return [_reminder(r) for r in await cur.fetchall()]

    async def upcoming_reminders(self, chat_id: int) -> list[Reminder]:
        async with self.db.execute(
            "SELECT id, chat_id, text, due_at FROM reminders WHERE sent = 0 AND chat_id = ? ORDER BY due_at",
            (chat_id,),
        ) as cur:
            return [_reminder(r) for r in await cur.fetchall()]

    async def mark_reminder_sent(self, reminder_id: int) -> None:
        await self.db.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
        await self.db.commit()

    async def cancel_reminder(self, reminder_id: int, chat_id: int) -> Reminder | None:
        async with self.db.execute(
            "SELECT id, chat_id, text, due_at FROM reminders WHERE id = ? AND chat_id = ? AND sent = 0",
            (reminder_id, chat_id),
        ) as cur:
            row = await cur.fetchone()
        if row:
            await self.db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            await self.db.commit()
        return _reminder(row) if row else None

    # --- facts ---

    async def add_fact(self, chat_id: int, text: str) -> bool:
        """False if the same fact (ignoring case) is already stored."""
        async with self.db.execute("SELECT 1 FROM facts WHERE chat_id = ? AND lower(text) = lower(?)",
                                   (chat_id, text)) as cur:
            if await cur.fetchone():
                return False
        await self.db.execute("INSERT INTO facts (chat_id, text) VALUES (?, ?)", (chat_id, text))
        await self.db.commit()
        return True

    async def facts(self, chat_id: int) -> list[tuple[int, str]]:
        """(id, text), oldest first."""
        async with self.db.execute("SELECT id, text FROM facts WHERE chat_id = ? ORDER BY id", (chat_id,)) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]

    async def delete_fact(self, fact_id: int, chat_id: int) -> str | None:
        async with self.db.execute("SELECT text FROM facts WHERE id = ? AND chat_id = ?", (fact_id, chat_id)) as cur:
            row = await cur.fetchone()
        if row:
            await self.db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            await self.db.commit()
        return row[0] if row else None

    # --- small key/value state (e.g. the day the morning agenda was last sent) ---

    async def get(self, key: str) -> str | None:
        async with self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set(self, key: str, value: str) -> None:
        await self.db.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        await self.db.commit()
