"""Local-first SQLite persistence.

Every chat session, message and setting lives in a single local file
(`data/miraje.db`). No external database, no cloud sync, no telemetry.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import aiosqlite

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New chat',
    mode TEXT NOT NULL DEFAULT 'chat',          -- 'chat' | 'agent'
    model TEXT,
    persona TEXT,                               -- persona id (system prompt preset)
    pinned INTEGER NOT NULL DEFAULT 0,          -- 0 | 1 (favorite)
    tags TEXT NOT NULL DEFAULT '',              -- comma-separated labels
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_viewed_at REAL NOT NULL DEFAULT 0      -- tracks recency of viewing
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,                          -- 'user' | 'assistant' | 'system' | 'tool'
    content TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}',            -- JSON: tool calls, name, etc.
    starred INTEGER NOT NULL DEFAULT 0,         -- 0 | 1 (bookmark)
    created_at REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        # Lightweight migrations: add columns to pre-existing DBs.
        try:
            cur = await db.execute("PRAGMA table_info(sessions)")
            cols = [row[1] for row in await cur.fetchall()]
            if "persona" not in cols:
                await db.execute("ALTER TABLE sessions ADD COLUMN persona TEXT")
            if "pinned" not in cols:
                await db.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            if "tags" not in cols:
                await db.execute("ALTER TABLE sessions ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
            if "last_viewed_at" not in cols:
                await db.execute("ALTER TABLE sessions ADD COLUMN last_viewed_at REAL NOT NULL DEFAULT 0")
            cur = await db.execute("PRAGMA table_info(messages)")
            mcols = [row[1] for row in await cur.fetchall()]
            if "starred" not in mcols:
                await db.execute("ALTER TABLE messages ADD COLUMN starred INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        await db.commit()


def _connect() -> aiosqlite.Connection:
    return aiosqlite.connect(DB_PATH)


# ---------- Sessions ----------

async def create_session(
    session_id: str, title: str, mode: str, model: Optional[str], persona: Optional[str] = None
) -> None:
    now = time.time()
    async with _connect() as db:
        await db.execute(
            "INSERT INTO sessions (id, title, mode, model, persona, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (session_id, title, mode, model, persona, now, now),
        )
        await db.commit()


async def set_session_persona(session_id: str, persona: Optional[str]) -> None:
    """Persist the active persona on a session so reloading restores it."""
    async with _connect() as db:
        await db.execute("UPDATE sessions SET persona = ? WHERE id = ?", (persona, session_id))
        await db.commit()


async def list_sessions() -> list[dict[str, Any]]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        # Pinned sessions float to the top, then most-recently-updated.
        cur = await db.execute("SELECT * FROM sessions ORDER BY pinned DESC, updated_at DESC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def set_session_pinned(session_id: str, pinned: bool) -> None:
    async with _connect() as db:
        await db.execute("UPDATE sessions SET pinned = ? WHERE id = ?", (1 if pinned else 0, session_id))
        await db.commit()


def _normalize_tags(raw: str) -> str:
    """Parse, trim, dedupe, lowercase tags. Returns comma-joined string."""
    parts = [t.strip().lower() for t in (raw or "").split(",") if t.strip()]
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return ",".join(seen)


async def set_session_tags(session_id: str, tags: str) -> None:
    """Set the comma-separated tags on a session."""
    async with _connect() as db:
        await db.execute("UPDATE sessions SET tags = ? WHERE id = ?", (_normalize_tags(tags), session_id))
        await db.commit()


async def touch_session_viewed(session_id: str) -> None:
    """Mark a session as just-viewed (updates last_viewed_at)."""
    async with _connect() as db:
        await db.execute("UPDATE sessions SET last_viewed_at = ? WHERE id = ?", (time.time(), session_id))
        await db.commit()


async def list_recent_sessions(limit: int = 8) -> list[dict[str, Any]]:
    """Most-recently-viewed sessions (excluding those never viewed)."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM sessions WHERE last_viewed_at > 0 ORDER BY last_viewed_at DESC LIMIT ?",
            (max(1, min(int(limit), 50)),),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def list_all_tags() -> list[dict[str, Any]]:
    """Aggregate tag → session count, across all sessions."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT tags FROM sessions WHERE tags != ''")
        rows = await cur.fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t:
                counts[t] = counts.get(t, 0) + 1
    return [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


async def search_messages(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Full-text search across all messages. Case-insensitive via LIKE.

    Returns matching messages with their session title/mode so the UI can
    group or jump to them. No external FTS5 dependency — LIKE is good enough
    for a local-first single-user app.
    """
    q = (query or "").strip()
    if not q:
        return []
    pattern = f"%{q}%"
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT m.id, m.session_id, m.role, m.content, m.created_at, "
            "s.title AS session_title, s.mode AS session_mode "
            "FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE m.content LIKE ? COLLATE NOCASE "
            "ORDER BY m.created_at DESC LIMIT ?",
            (pattern, max(1, min(int(limit), 200))),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_session(session_id: str) -> Optional[dict[str, Any]]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def touch_session(session_id: str, title: Optional[str] = None) -> None:
    now = time.time()
    async with _connect() as db:
        if title:
            await db.execute(
                "UPDATE sessions SET updated_at = ?, title = ? WHERE id = ?",
                (now, title, session_id),
            )
        else:
            await db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        await db.commit()


async def delete_session(session_id: str) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await db.commit()


# ---------- Messages ----------

async def add_message(
    message_id: str,
    session_id: str,
    role: str,
    content: str,
    meta: Optional[dict[str, Any]] = None,
) -> None:
    now = time.time()
    async with _connect() as db:
        await db.execute(
            "INSERT INTO messages (id, session_id, role, content, meta, created_at) VALUES (?,?,?,?,?,?)",
            (message_id, session_id, role, content, json.dumps(meta or {}), now),
        )
        await db.commit()
    await touch_session(session_id)


async def get_messages(session_id: str) -> list[dict[str, Any]]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )
        rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["meta"] = json.loads(d.get("meta") or "{}")
            except json.JSONDecodeError:
                d["meta"] = {}
            out.append(d)
        return out


async def get_message(message_id: str) -> Optional[dict[str, Any]]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except json.JSONDecodeError:
            d["meta"] = {}
        return d


async def delete_message(message_id: str) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        await db.commit()


async def set_message_starred(message_id: str, starred: bool) -> None:
    """Bookmark (star) or unstar a single message."""
    async with _connect() as db:
        await db.execute("UPDATE messages SET starred = ? WHERE id = ?", (1 if starred else 0, message_id))
        await db.commit()


async def list_starred_messages(limit: int = 100) -> list[dict[str, Any]]:
    """All starred messages across every session, newest first."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT m.id, m.session_id, m.role, m.content, m.created_at, "
            "s.title AS session_title, s.mode AS session_mode "
            "FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE m.starred = 1 "
            "ORDER BY m.created_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def duplicate_session(session_id: str) -> Optional[dict[str, Any]]:
    """Clone a session (metadata + all messages) into a new session.

    The new session gets a "(copy)" suffix on the title and a fresh id.
    Starred state is preserved on the cloned messages.
    """
    src = await get_session(session_id)
    if not src:
        return None
    src_msgs = await get_messages(session_id)

    import uuid as _uuid
    new_id = _uuid.uuid4().hex
    now = time.time()
    new_title = src["title"] + " (copy)"
    async with _connect() as db:
        await db.execute(
            "INSERT INTO sessions (id, title, mode, model, persona, pinned, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (new_id, new_title, src["mode"], src.get("model"), src.get("persona"), 0, now, now),
        )
        for m in src_msgs:
            await db.execute(
                "INSERT INTO messages (id, session_id, role, content, meta, starred, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (_uuid.uuid4().hex, new_id, m["role"], m["content"],
                 json.dumps(m.get("meta") or {}), m.get("starred", 0), now + m["created_at"] - src["created_at"]),
            )
        await db.commit()
    return await get_session(new_id)


async def delete_messages_after(message_id: str) -> int:
    """Delete the given message and every message that came after it in its session.

    Used by the regenerate flow: drop the trailing assistant + its user prompt,
    then re-run. Returns the number of messages removed.
    """
    async with _connect() as db:
        cur = await db.execute("SELECT session_id, created_at FROM messages WHERE id = ?", (message_id,))
        row = await cur.fetchone()
        if not row:
            return 0
        session_id, created_at = row
        cur = await db.execute(
            "DELETE FROM messages WHERE session_id = ? AND created_at >= ?",
            (session_id, created_at),
        )
        await db.commit()
        return cur.rowcount


# ---------- Settings ----------

async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    async with _connect() as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    async with _connect() as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def get_all_settings() -> dict[str, str]:
    async with _connect() as db:
        cur = await db.execute("SELECT key, value FROM settings")
        rows = await cur.fetchall()
        return {k: v for k, v in rows}


# ---------- Stats ----------

def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars per token, common heuristic)."""
    return max(1, len(text or "") // 4)


async def get_stats() -> dict[str, Any]:
    """Aggregate usage stats across all sessions — local only, computed on demand."""
    async with _connect() as db:
        # Session counts by mode.
        cur = await db.execute("SELECT mode, COUNT(*) FROM sessions GROUP BY mode")
        by_mode = {row[0]: row[1] for row in await cur.fetchall()}

        # Message counts by role + total tokens.
        cur = await db.execute("SELECT role, COUNT(*), SUM(LENGTH(content)) FROM messages GROUP BY role")
        by_role: dict[str, dict[str, int]] = {}
        total_msgs = 0
        total_chars = 0
        for role, cnt, chars in await cur.fetchall():
            c = int(chars or 0)
            by_role[role] = {"count": int(cnt), "chars": c, "tokens": _estimate_tokens("x" * c)}
            total_msgs += int(cnt)
            total_chars += c

        # Per-session breakdown (top 10 by message count).
        cur = await db.execute(
            "SELECT s.id, s.title, s.mode, s.created_at, COUNT(m.id) AS n, SUM(LENGTH(m.content)) AS chars "
            "FROM sessions s LEFT JOIN messages m ON m.session_id = s.id "
            "GROUP BY s.id ORDER BY n DESC LIMIT 10"
        )
        top_sessions = []
        for sid, title, mode, created_at, n, chars in await cur.fetchall():
            c = int(chars or 0)
            top_sessions.append({
                "id": sid,
                "title": title,
                "mode": mode,
                "created_at": created_at,
                "messages": int(n),
                "chars": c,
                "tokens": _estimate_tokens("x" * c),
            })

        return {
            "sessions": sum(by_mode.values()),
            "sessions_by_mode": by_mode,
            "messages": total_msgs,
            "messages_by_role": by_role,
            "total_chars": total_chars,
            "total_tokens": _estimate_tokens("x" * total_chars),
            "top_sessions": top_sessions,
        }
