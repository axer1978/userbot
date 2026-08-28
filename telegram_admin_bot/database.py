"""SQLite persistence for conversations and messages.

Uses aiosqlite so that both the Telethon event loop and the FastAPI request
handlers (which share one asyncio loop) can write without blocking it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

# Message.direction
DIR_IN = "in"
DIR_OUT = "out"
DIR_SYSTEM = "system"

# Message.status
STATUS_RECEIVED = "received"
STATUS_SENT = "sent"
STATUS_PENDING = "pending_approval"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"

# Outreach.status — messages we start, rather than reply to
OUT_QUEUED = "queued"
OUT_DRAFTED = "drafted"
OUT_SENT = "sent"
OUT_FAILED = "failed"
OUT_CANCELLED = "cancelled"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    chat_id              INTEGER PRIMARY KEY,
    display_name         TEXT    NOT NULL DEFAULT '',
    username             TEXT,
    is_bot               INTEGER NOT NULL DEFAULT 0,
    access_hash          INTEGER,
    automation_paused    INTEGER NOT NULL DEFAULT 0,
    unread               INTEGER NOT NULL DEFAULT 0,
    last_message_at      TEXT,
    last_message_preview TEXT    NOT NULL DEFAULT '',
    created_at           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    telegram_id INTEGER,
    direction   TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    text        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS outreach (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    display_name TEXT    NOT NULL DEFAULT '',
    goal         TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    message      TEXT,
    error        TEXT,
    -- messages.id of the pending draft, so approving or rejecting it can
    -- settle this row rather than leaving it stuck on "drafted".
    draft_id     INTEGER,
    created_at   TEXT    NOT NULL,
    sent_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach (status, id);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_tg
    ON messages (chat_id, telegram_id) WHERE telegram_id IS NOT NULL;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._conn: Optional[aiosqlite.Connection] = None
        # Guards multi-statement sequences that must stay consistent.
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been awaited yet")
        return self._conn

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    async def upsert_conversation(
        self,
        chat_id: int,
        display_name: str,
        username: Optional[str],
        is_bot: bool,
        access_hash: Optional[int] = None,
    ) -> dict[str, Any]:
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO conversations (chat_id, display_name, username, is_bot,
                                           access_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    username     = excluded.username,
                    is_bot       = excluded.is_bot,
                    access_hash  = COALESCE(excluded.access_hash, conversations.access_hash)
                """,
                (chat_id, display_name, username, int(is_bot), access_hash, utcnow()),
            )
            await self.conn.commit()
        return await self.get_conversation(chat_id)  # type: ignore[return-value]

    async def get_conversation(self, chat_id: int) -> Optional[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM conversations WHERE chat_id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        return _conversation(row) if row else None

    async def list_conversations(self) -> list[dict[str, Any]]:
        async with self.conn.execute(
            """
            SELECT * FROM conversations
            ORDER BY COALESCE(last_message_at, created_at) DESC
            """
        ) as cur:
            rows = await cur.fetchall()
        return [_conversation(r) for r in rows]

    async def set_paused(self, chat_id: int, paused: bool) -> Optional[dict[str, Any]]:
        await self.conn.execute(
            "UPDATE conversations SET automation_paused = ? WHERE chat_id = ?",
            (int(paused), chat_id),
        )
        await self.conn.commit()
        return await self.get_conversation(chat_id)

    async def get_access_hash(self, chat_id: int) -> Optional[int]:
        """Cached access_hash, used to rebuild an InputPeer if resolution fails."""
        async with self.conn.execute(
            "SELECT access_hash FROM conversations WHERE chat_id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["access_hash"] if row else None

    async def mark_read(self, chat_id: int) -> Optional[dict[str, Any]]:
        await self.conn.execute(
            "UPDATE conversations SET unread = 0 WHERE chat_id = ?", (chat_id,)
        )
        await self.conn.commit()
        return await self.get_conversation(chat_id)

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def record_message(
        self,
        chat_id: int,
        direction: str,
        status: str,
        text: str,
        telegram_id: Optional[int] = None,
        bump_preview: bool = True,
        mark_unread: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Insert a message and refresh the conversation's preview.

        Returns None when the message is a duplicate of one already stored
        under the same Telegram message id (we send via Telethon *and* watch
        outgoing events, so the same message can arrive twice).
        """
        now = utcnow()
        async with self._lock:
            cur = await self.conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (chat_id, telegram_id, direction, status, text, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, telegram_id, direction, status, text, now),
            )
            if cur.rowcount == 0:
                await self.conn.commit()
                return None
            message_id = cur.lastrowid

            if bump_preview:
                await self.conn.execute(
                    """
                    UPDATE conversations
                       SET last_message_at      = ?,
                           last_message_preview = ?,
                           unread               = CASE WHEN ? THEN unread + 1 ELSE unread END
                     WHERE chat_id = ?
                    """,
                    (now, _preview(text), int(mark_unread), chat_id),
                )
            await self.conn.commit()

        return await self.get_message(message_id)

    async def get_message(self, message_id: int) -> Optional[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ) as cur:
            row = await cur.fetchone()
        return _message(row) if row else None

    async def find_by_telegram_id(
        self, chat_id: int, telegram_id: Optional[int]
    ) -> Optional[dict[str, Any]]:
        if telegram_id is None:
            return None
        async with self.conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND telegram_id = ?",
            (chat_id, telegram_id),
        ) as cur:
            row = await cur.fetchone()
        return _message(row) if row else None

    async def get_messages(self, chat_id: int, limit: int = 300) -> list[dict[str, Any]]:
        async with self.conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_message(r) for r in rows]

    async def get_history_for_ai(self, chat_id: int, limit: int = 30) -> list[dict[str, str]]:
        """Recent exchange as OpenAI-style role/content pairs.

        Drafts that were never approved, rejections and error rows are left
        out — only what actually crossed the wire is context for the model.
        """
        async with self.conn.execute(
            """
            SELECT * FROM (
                SELECT direction, text, id FROM messages
                 WHERE chat_id = ?
                   AND status IN (?, ?)
                   AND direction IN (?, ?)
                   AND TRIM(text) <> ''
                 ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (chat_id, STATUS_RECEIVED, STATUS_SENT, DIR_IN, DIR_OUT, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"role": "user" if r["direction"] == DIR_IN else "assistant", "content": r["text"]}
            for r in rows
        ]

    async def update_message(
        self,
        message_id: int,
        *,
        text: Optional[str] = None,
        status: Optional[str] = None,
        telegram_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        sets: list[str] = []
        params: list[Any] = []
        if text is not None:
            sets.append("text = ?")
            params.append(text)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if telegram_id is not None:
            sets.append("telegram_id = ?")
            params.append(telegram_id)
        if not sets:
            return await self.get_message(message_id)
        params.append(message_id)
        await self.conn.execute(
            f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", params
        )
        await self.conn.commit()
        return await self.get_message(message_id)

    async def pending_drafts(self, chat_id: Optional[int] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE status = ?"
        params: list[Any] = [STATUS_PENDING]
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        sql += " ORDER BY id ASC"
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_message(r) for r in rows]

    async def reject_pending(self, chat_id: int) -> list[int]:
        """Drop still-pending drafts for a chat; returns the ids affected."""
        drafts = await self.pending_drafts(chat_id)
        for d in drafts:
            await self.update_message(d["id"], status=STATUS_REJECTED)
        return [d["id"] for d in drafts]

    async def set_conversation_preview(self, chat_id: int, text: str) -> None:
        await self.conn.execute(
            "UPDATE conversations SET last_message_at = ?, last_message_preview = ? WHERE chat_id = ?",
            (utcnow(), _preview(text), chat_id),
        )
        await self.conn.commit()


    # ------------------------------------------------------------------
    # Outreach — messages we start, rather than reply to
    # ------------------------------------------------------------------

    async def queue_outreach(
        self, recipients: Iterable[tuple[int, str]], goal: str
    ) -> list[dict[str, Any]]:
        """Queue one message per (chat_id, display_name).

        Skips anyone who already has a message waiting — queued, or drafted and
        sitting unapproved — so a person never ends up with two unsent openers.
        """
        now = utcnow()
        created: list[int] = []
        async with self._lock:
            for chat_id, name in recipients:
                async with self.conn.execute(
                    "SELECT 1 FROM outreach WHERE chat_id = ? AND status IN (?, ?)",
                    (chat_id, OUT_QUEUED, OUT_DRAFTED),
                ) as cur:
                    if await cur.fetchone():
                        continue
                cur = await self.conn.execute(
                    """
                    INSERT INTO outreach (chat_id, display_name, goal, status, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chat_id, name, goal, OUT_QUEUED, now),
                )
                created.append(cur.lastrowid)
            await self.conn.commit()
        return [row for row in [await self.get_outreach(i) for i in created] if row]

    async def get_outreach(self, outreach_id: int) -> Optional[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM outreach WHERE id = ?", (outreach_id,)
        ) as cur:
            row = await cur.fetchone()
        return _outreach(row) if row else None

    async def list_outreach(self, limit: int = 200) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM (SELECT * FROM outreach ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [_outreach(r) for r in rows]

    async def next_queued_outreach(self) -> Optional[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM outreach WHERE status = ? ORDER BY id ASC LIMIT 1", (OUT_QUEUED,)
        ) as cur:
            row = await cur.fetchone()
        return _outreach(row) if row else None

    async def update_outreach(
        self,
        outreach_id: int,
        *,
        status: Optional[str] = None,
        message: Optional[str] = None,
        error: Optional[str] = None,
        draft_id: Optional[int] = None,
        mark_sent: bool = False,
    ) -> Optional[dict[str, Any]]:
        sets: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("message", message),
            ("error", error),
            ("draft_id", draft_id),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        if mark_sent:
            sets.append("sent_at = ?")
            params.append(utcnow())
        if not sets:
            return await self.get_outreach(outreach_id)
        params.append(outreach_id)
        await self.conn.execute(
            f"UPDATE outreach SET {', '.join(sets)} WHERE id = ?", params
        )
        await self.conn.commit()
        return await self.get_outreach(outreach_id)

    async def outreach_for_draft(self, draft_id: int) -> Optional[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM outreach WHERE draft_id = ?", (draft_id,)
        ) as cur:
            row = await cur.fetchone()
        return _outreach(row) if row else None

    async def cancel_queued_outreach(self) -> int:
        """Stop everything not yet acted on. Returns how many were cancelled."""
        cur = await self.conn.execute(
            "UPDATE outreach SET status = ? WHERE status = ?", (OUT_CANCELLED, OUT_QUEUED)
        )
        await self.conn.commit()
        return cur.rowcount

    async def outreach_sent_since(self, iso_timestamp: str) -> int:
        """How many outreach messages actually went out since a given moment."""
        async with self.conn.execute(
            "SELECT COUNT(*) AS n FROM outreach WHERE status = ? AND sent_at >= ?",
            (OUT_SENT, iso_timestamp),
        ) as cur:
            row = await cur.fetchone()
        return row["n"] if row else 0


def _preview(text: str, limit: int = 90) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _conversation(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "chat_id": row["chat_id"],
        "display_name": row["display_name"],
        "username": row["username"],
        "is_bot": bool(row["is_bot"]),
        "automation_paused": bool(row["automation_paused"]),
        "unread": row["unread"],
        "last_message_at": row["last_message_at"],
        "last_message_preview": row["last_message_preview"],
    }


def _outreach(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "display_name": row["display_name"],
        "goal": row["goal"],
        "status": row["status"],
        "message": row["message"],
        "error": row["error"],
        "draft_id": row["draft_id"],
        "created_at": row["created_at"],
        "sent_at": row["sent_at"],
    }


def _message(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "telegram_id": row["telegram_id"],
        "direction": row["direction"],
        "status": row["status"],
        "text": row["text"],
        "created_at": row["created_at"],
    }
