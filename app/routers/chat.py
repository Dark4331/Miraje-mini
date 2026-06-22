"""Chat + sessions endpoints (SSE streaming)."""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse

from .. import database
from ..config import DATA_DIR
from ..llm.base import get_default_model, get_provider, get_show_reasoning
from ..models import ChatRequest, SessionCreate
from ..persona_manager import get_persona_context, get_persona_system

router = APIRouter(prefix="/api", tags=["chat"])


# ---------- Helpers ----------

def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _title_from(content: str) -> str:
    clean = " ".join(content.strip().split())
    return (clean[:48] + "…") if len(clean) > 48 else (clean or "New chat")


# ---------- <think> tag handling for streamed chat ----------

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.S)
_THINK_UNCLOSED_RE = re.compile(r"<think>.*$", re.S)


def _strip_think(text: str) -> str:
    """Remove <think>…</think> blocks (and any unclosed <think>…) from text."""
    if not text:
        return ""
    text = _THINK_BLOCK_RE.sub("", text)
    text = _THINK_UNCLOSED_RE.sub("", text)
    return text


class _ThinkStreamFilter:
    """Streaming filter that separates <think> reasoning from final content.

    Feed it pieces as they arrive; it returns (token_pieces, reasoning_pieces)
    lists to emit. Anything inside <think>…</think> is routed to reasoning;
    everything else is routed to tokens. A small lookbehind avoids splitting a
    tag across chunk boundaries.
    """

    def __init__(self, show_reasoning: bool = True):
        self.show_reasoning = show_reasoning
        self.in_think = False
        self.buffer = ""

    def feed(self, piece: str) -> tuple[list[str], list[str]]:
        self.buffer += piece
        tokens: list[str] = []
        reasoning: list[str] = []
        tag_lookbehind = max(len(_THINK_OPEN), len(_THINK_CLOSE))

        while self.buffer:
            if self.in_think:
                end = self.buffer.find(_THINK_CLOSE)
                if end == -1:
                    if len(self.buffer) > tag_lookbehind:
                        chunk = self.buffer[:-tag_lookbehind]
                        if self.show_reasoning and chunk:
                            reasoning.append(chunk)
                        self.buffer = self.buffer[-tag_lookbehind:]
                    break
                chunk = self.buffer[:end]
                if self.show_reasoning and chunk:
                    reasoning.append(chunk)
                self.buffer = self.buffer[end + len(_THINK_CLOSE):]
                self.in_think = False
                continue

            start = self.buffer.find(_THINK_OPEN)
            if start == -1:
                if len(self.buffer) > tag_lookbehind:
                    chunk = self.buffer[:-tag_lookbehind]
                    if chunk:
                        tokens.append(chunk)
                    self.buffer = self.buffer[-tag_lookbehind:]
                break
            chunk = self.buffer[:start]
            if chunk:
                tokens.append(chunk)
            self.buffer = self.buffer[start + len(_THINK_OPEN):]
            self.in_think = True

        return tokens, reasoning

    def flush(self) -> tuple[list[str], list[str]]:
        tokens: list[str] = []
        reasoning: list[str] = []
        if self.buffer:
            if self.in_think:
                if self.show_reasoning:
                    reasoning.append(self.buffer)
            else:
                tokens.append(self.buffer)
            self.buffer = ""
        self.in_think = False
        return tokens, reasoning


# ---------- Persona resolution ----------

async def _resolve_persona(session: dict | None, explicit: str | None) -> str:
    """Resolve which persona to use for a chat.

    Priority: explicit (body.persona) > session-stored > active_persona setting > 'default'.
    """
    if explicit:
        return explicit
    if session and session.get("persona"):
        return session["persona"]
    from ..llm.base import get_active_persona

    active = await get_active_persona()
    if active:
        return active
    return "default"


# ---------- Sessions CRUD ----------

@router.post("/sessions")
async def create_session(body: SessionCreate):
    sid = uuid.uuid4().hex
    await database.create_session(sid, body.title, body.mode, body.model)
    return await database.get_session(sid)


@router.get("/sessions")
async def list_sessions():
    return {"sessions": await database.list_sessions()}


@router.get("/sessions/recent")
async def recent_sessions(limit: int = 8):
    """Most-recently-viewed sessions. Defined before {session_id} to avoid capture."""
    return {"sessions": await database.list_recent_sessions(limit)}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    session = await database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    messages = await database.get_messages(session_id)
    return {"session": session, "messages": messages}


@router.patch("/sessions/{session_id}")
async def rename_session(session_id: str, title: str):
    await database.touch_session(session_id, title=title)
    return await database.get_session(session_id)


@router.put("/sessions/{session_id}/persona")
async def set_persona(session_id: str, persona: str):
    """Persist the active persona on a session."""
    existing = await database.get_session(session_id)
    if not existing:
        raise HTTPException(status_code=404, detail="session not found")
    await database.set_session_persona(session_id, persona or None)
    return {"status": "ok", "persona": persona or None}


@router.put("/sessions/{session_id}/pin")
async def set_pinned(session_id: str, pinned: bool = True):
    """Pin (favorite) or unpin a session. Pinned sessions float to the top."""
    existing = await database.get_session(session_id)
    if not existing:
        raise HTTPException(status_code=404, detail="session not found")
    await database.set_session_pinned(session_id, bool(pinned))
    return {"status": "ok", "pinned": bool(pinned)}


@router.put("/sessions/{session_id}/tags")
async def set_tags(session_id: str, tags: str = ""):
    """Set the comma-separated tags on a session."""
    existing = await database.get_session(session_id)
    if not existing:
        raise HTTPException(status_code=404, detail="session not found")
    await database.set_session_tags(session_id, tags)
    return {"status": "ok", "tags": database._normalize_tags(tags)}


@router.post("/sessions/{session_id}/viewed")
async def mark_viewed(session_id: str):
    """Mark a session as just-viewed (for the recently-viewed list)."""
    existing = await database.get_session(session_id)
    if not existing:
        raise HTTPException(status_code=404, detail="session not found")
    await database.touch_session_viewed(session_id)
    return {"status": "ok"}


@router.get("/tags")
async def all_tags():
    """All tags with their session counts."""
    return {"tags": await database.list_all_tags()}


@router.get("/search")
async def search(q: str, limit: int = 50):
    """Search across all conversations (case-insensitive, message content)."""
    results = await database.search_messages(q, limit)
    return {"query": q, "count": len(results), "results": results}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    await database.delete_session(session_id)
    return {"status": "ok"}


# ---------- Chat ----------

@router.post("/chat")
async def chat(body: ChatRequest):
    """Stream a chat completion. Auto-creates a session if none is given."""
    provider = await get_provider()
    model = body.model or await get_default_model()
    show_reasoning = await get_show_reasoning()

    session_id = body.session_id
    created_session = False
    if not session_id:
        session_id = uuid.uuid4().hex
        title = _title_from(body.messages[-1].content if body.messages else "New chat")
        await database.create_session(
            session_id, title, "chat", model, persona=body.persona
        )
        created_session = True
        existing = await database.get_session(session_id)
    else:
        # Always fetch the session record up-front so persona resolution works
        # regardless of which branch we took (fixes the previous `existing`
        # NameError bug when the client supplied a session_id but no persona).
        existing = await database.get_session(session_id)
        if not existing:
            raise HTTPException(status_code=404, detail="session not found")

    # Resolve the active persona: body.persona > session-stored > active_persona setting > default.
    persona_id = await _resolve_persona(existing, body.persona)
    if body.persona and body.persona != existing.get("persona"):
        await database.set_session_persona(session_id, body.persona)

    # Persist the latest user message.
    user_msg = body.messages[-1] if body.messages else None
    user_msg_id = None
    if user_msg and user_msg.role == "user":
        user_msg_id = uuid.uuid4().hex
        await database.add_message(user_msg_id, session_id, "user", user_msg.content)

    # Build the message list sent to the model.
    history = await database.get_messages(session_id)
    # Resolve the system prompt: explicit > persona context > custom (settings) > none.
    system_prompt = body.system
    if not system_prompt and persona_id:
        system_prompt = get_persona_context(persona_id)
    if not system_prompt:
        system_prompt = await database.get_setting("custom_system_prompt", "") or None
    llm_messages: list[dict] = []
    if system_prompt:
        llm_messages.append({"role": "system", "content": system_prompt})
    for m in history:
        if m["role"] in ("user", "assistant", "system"):
            llm_messages.append({"role": m["role"], "content": m["content"]})

    assistant_id = uuid.uuid4().hex

    async def event_stream() -> AsyncIterator[str]:
        # Announce session.
        session = await database.get_session(session_id)
        yield _sse(
            {
                "type": "session",
                "session_id": session_id,
                "title": session["title"] if session else "New chat",
                "persona": persona_id,
            }
        )
        yield _sse(
            {
                "type": "meta",
                "model": model,
                "user_message_id": user_msg_id,
                "show_reasoning": show_reasoning,
            }
        )

        accumulator: list[str] = []
        filter_ = _ThinkStreamFilter(show_reasoning=show_reasoning)
        try:
            async for piece in provider.stream_chat(
                llm_messages, model, temperature=body.temperature, max_tokens=body.max_tokens
            ):
                accumulator.append(piece)
                tokens, reasoning = filter_.feed(piece)
                for r in reasoning:
                    yield _sse({"type": "reasoning", "content": r})
                for t in tokens:
                    yield _sse({"type": "token", "content": t})
            # Final flush.
            tokens, reasoning = filter_.flush()
            for r in reasoning:
                yield _sse({"type": "reasoning", "content": r})
            for t in tokens:
                yield _sse({"type": "token", "content": t})
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "error", "content": str(e)})
            content = _strip_think("".join(accumulator))
            if content:
                await database.add_message(
                    assistant_id, session_id, "assistant", content, {"model": model, "error": str(e)}
                )
            yield _sse({"type": "done"})
            return

        # Persist the cleaned (think-stripped) assistant reply.
        content = _strip_think("".join(accumulator))
        await database.add_message(
            assistant_id, session_id, "assistant", content, {"model": model}
        )
        yield _sse({"type": "done", "message_id": assistant_id, "content": content})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- Export ----------

from fastapi.responses import Response  # noqa: E402


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


@router.get("/sessions/{session_id}/export/markdown")
async def export_markdown(session_id: str):
    session = await database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    messages = await database.get_messages(session_id)

    lines: list[str] = [
        f"# {session['title']}",
        "",
        f"_Exported from Miraje · mini on {_fmt_ts(time.time())}_",
        "",
        f"- **Mode:** {session['mode']}",
        f"- **Model:** {session.get('model') or '—'}",
        f"- **Created:** {_fmt_ts(session['created_at'])}",
        f"- **Messages:** {len(messages)}",
        "",
        "---",
        "",
    ]
    role_label = {"user": "You", "assistant": "Miraje", "system": "System", "tool": "Tool"}
    for m in messages:
        label = role_label.get(m["role"], m["role"].title())
        lines.append(f"## {label}")
        lines.append(f"_{_fmt_ts(m['created_at'])}_")
        lines.append("")
        lines.append(m["content"] or "_(empty)_")
        lines.append("")
    body = "\n".join(lines)
    safe_title = "".join(c if c.isalnum() or c in "-_" else "_" for c in session["title"])[:40] or "conversation"
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.md"'},
    )


@router.get("/sessions/{session_id}/export/json")
async def export_json(session_id: str):
    session = await database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    messages = await database.get_messages(session_id)
    payload = {
        "app": "Miraje",
        "variant": "mini",
        "exported_at": time.time(),
        "session": {
            "id": session["id"],
            "title": session["title"],
            "mode": session["mode"],
            "model": session.get("model"),
            "persona": session.get("persona"),
            "tags": session.get("tags", ""),
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
        },
        "messages": [
            {
                "id": m["id"],
                "role": m["role"],
                "content": m["content"],
                "meta": m["meta"],
                "starred": m.get("starred", 0),
                "created_at": m["created_at"],
            }
            for m in messages
        ],
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    safe_title = "".join(c if c.isalnum() or c in "-_" else "_" for c in session["title"])[:40] or "conversation"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.json"'},
    )


# ---------- Message actions ----------

@router.delete("/sessions/{session_id}/messages/{message_id}")
async def delete_message_route(session_id: str, message_id: str):
    existing = await database.get_session(session_id)
    if not existing:
        raise HTTPException(status_code=404, detail="session not found")
    msg = await database.get_message(message_id)
    if not msg or msg["session_id"] != session_id:
        raise HTTPException(status_code=404, detail="message not found")
    await database.delete_message(message_id)
    return {"status": "ok"}


@router.put("/sessions/{session_id}/messages/{message_id}/star")
async def star_message(session_id: str, message_id: str, starred: bool = True):
    """Bookmark (star) or unstar a single message."""
    msg = await database.get_message(message_id)
    if not msg or msg["session_id"] != session_id:
        raise HTTPException(status_code=404, detail="message not found")
    await database.set_message_starred(message_id, bool(starred))
    return {"status": "ok", "starred": bool(starred)}


@router.get("/messages/starred")
async def list_starred(limit: int = 100):
    """All starred (bookmarked) messages across every session."""
    results = await database.list_starred_messages(limit)
    return {"count": len(results), "results": results}


@router.post("/sessions/{session_id}/duplicate")
async def duplicate_session_route(session_id: str):
    """Clone a session (metadata + messages) into a new session."""
    new_session = await database.duplicate_session(session_id)
    if not new_session:
        raise HTTPException(status_code=404, detail="session not found")
    return {"status": "ok", "session": new_session}


# ---------- Regenerate ----------

@router.post("/sessions/{session_id}/regenerate")
async def regenerate(session_id: str):
    """Drop the trailing assistant reply (and anything after it) and re-stream.

    Finds the last assistant message in the session, deletes it and any later
    messages, then streams a fresh completion from the model using the remaining
    history. The persona stored on the session (if any) is re-applied as a full
    persona context (system prompt + accumulated memory).
    """
    session = await database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")

    messages = await database.get_messages(session_id)
    if not messages:
        raise HTTPException(status_code=400, detail="session has no messages")

    show_reasoning = await get_show_reasoning()

    # Find the last assistant message; drop it (and anything after).
    last_assistant = None
    for m in reversed(messages):
        if m["role"] == "assistant":
            last_assistant = m
            break
    if last_assistant:
        await database.delete_messages_after(last_assistant["id"])

    # Rebuild the LLM message list from remaining history.
    history = await database.get_messages(session_id)
    persona_id = await _resolve_persona(session, None)
    system_prompt = get_persona_context(persona_id) if persona_id else None
    if not system_prompt:
        system_prompt = await database.get_setting("custom_system_prompt", "") or None
    llm_messages: list[dict] = []
    if system_prompt:
        llm_messages.append({"role": "system", "content": system_prompt})
    for m in history:
        if m["role"] in ("user", "assistant", "system"):
            llm_messages.append({"role": m["role"], "content": m["content"]})

    provider = await get_provider()
    model = session.get("model") or await get_default_model()
    assistant_id = uuid.uuid4().hex

    async def event_stream() -> AsyncIterator[str]:
        yield _sse(
            {
                "type": "session",
                "session_id": session_id,
                "title": session["title"],
                "persona": persona_id,
            }
        )
        yield _sse(
            {
                "type": "meta",
                "model": model,
                "regenerated": True,
                "show_reasoning": show_reasoning,
            }
        )

        accumulator: list[str] = []
        filter_ = _ThinkStreamFilter(show_reasoning=show_reasoning)
        try:
            async for piece in provider.stream_chat(llm_messages, model, temperature=0.7):
                accumulator.append(piece)
                tokens, reasoning = filter_.feed(piece)
                for r in reasoning:
                    yield _sse({"type": "reasoning", "content": r})
                for t in tokens:
                    yield _sse({"type": "token", "content": t})
            tokens, reasoning = filter_.flush()
            for r in reasoning:
                yield _sse({"type": "reasoning", "content": r})
            for t in tokens:
                yield _sse({"type": "token", "content": t})
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "error", "content": str(e)})
            content = _strip_think("".join(accumulator))
            if content:
                await database.add_message(
                    assistant_id, session_id, "assistant", content, {"model": model, "error": str(e)}
                )
            yield _sse({"type": "done"})
            return

        content = _strip_think("".join(accumulator))
        await database.add_message(assistant_id, session_id, "assistant", content, {"model": model})
        yield _sse({"type": "done", "message_id": assistant_id, "content": content})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- Edit & resend ----------

from pydantic import BaseModel  # noqa: E402


class EditRequest(BaseModel):
    content: str


@router.put("/sessions/{session_id}/messages/{message_id}/edit")
async def edit_and_resend(session_id: str, message_id: str, body: EditRequest):
    """Edit a message's content, drop everything after it, and re-stream.

    Works on any message role. After updating the message text, every message
    that came after it is deleted, the LLM context is rebuilt from the remaining
    history (with the session's persona re-applied as full persona context), and
    a fresh completion is streamed back. This is the classic "edit a user prompt
    and resend" flow.
    """
    session = await database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    msg = await database.get_message(message_id)
    if not msg or msg["session_id"] != session_id:
        raise HTTPException(status_code=404, detail="message not found")

    new_content = (body.content or "").strip()
    if not new_content:
        raise HTTPException(status_code=400, detail="content must not be empty")

    show_reasoning = await get_show_reasoning()

    # Update the edited message's content, then delete everything after it.
    async with database._connect() as db:
        await db.execute(
            "UPDATE messages SET content = ? WHERE id = ?", (new_content, message_id)
        )
        await db.execute(
            "DELETE FROM messages WHERE session_id = ? AND created_at > ?",
            (session_id, msg["created_at"]),
        )
        await db.commit()

    # Rebuild the LLM message list.
    history = await database.get_messages(session_id)
    persona_id = await _resolve_persona(session, None)
    system_prompt = get_persona_context(persona_id) if persona_id else None
    if not system_prompt:
        system_prompt = await database.get_setting("custom_system_prompt", "") or None
    llm_messages: list[dict] = []
    if system_prompt:
        llm_messages.append({"role": "system", "content": system_prompt})
    for m in history:
        if m["role"] in ("user", "assistant", "system"):
            llm_messages.append({"role": m["role"], "content": m["content"]})

    provider = await get_provider()
    model = session.get("model") or await get_default_model()
    assistant_id = uuid.uuid4().hex

    async def event_stream() -> AsyncIterator[str]:
        yield _sse(
            {
                "type": "session",
                "session_id": session_id,
                "title": session["title"],
                "persona": persona_id,
            }
        )
        yield _sse(
            {
                "type": "meta",
                "model": model,
                "edited": True,
                "edited_message_id": message_id,
                "show_reasoning": show_reasoning,
            }
        )

        accumulator: list[str] = []
        filter_ = _ThinkStreamFilter(show_reasoning=show_reasoning)
        try:
            async for piece in provider.stream_chat(llm_messages, model, temperature=0.7):
                accumulator.append(piece)
                tokens, reasoning = filter_.feed(piece)
                for r in reasoning:
                    yield _sse({"type": "reasoning", "content": r})
                for t in tokens:
                    yield _sse({"type": "token", "content": t})
            tokens, reasoning = filter_.flush()
            for r in reasoning:
                yield _sse({"type": "reasoning", "content": r})
            for t in tokens:
                yield _sse({"type": "token", "content": t})
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "error", "content": str(e)})
            content = _strip_think("".join(accumulator))
            if content:
                await database.add_message(
                    assistant_id, session_id, "assistant", content, {"model": model, "error": str(e)}
                )
            yield _sse({"type": "done"})
            return

        content = _strip_think("".join(accumulator))
        await database.add_message(assistant_id, session_id, "assistant", content, {"model": model})
        yield _sse({"type": "done", "message_id": assistant_id, "content": content})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- Export all (zip) ----------

import io  # noqa: E402
import zipfile  # noqa: E402


@router.get("/export/all.zip")
async def export_all():
    """Back up every session as a single zip: one .md + one .json per session,
    plus a top-level index.json manifest. Runs entirely locally."""
    sessions = await database.list_sessions()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = []
        for s in sessions:
            msgs = await database.get_messages(s["id"])
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in s["title"])[:40] or "session"
            folder = f"{safe}_{s['id'][:8]}"
            # Markdown
            md_lines = [
                f"# {s['title']}",
                "",
                f"_Exported from Miraje · mini on {_fmt_ts(s['created_at'])}_",
                "",
                f"- **Mode:** {s['mode']}",
                f"- **Model:** {s.get('model') or '—'}",
                f"- **Persona:** {s.get('persona') or 'default'}",
                f"- **Messages:** {len(msgs)}",
                "",
                "---",
                "",
            ]
            role_label = {"user": "You", "assistant": "Miraje", "system": "System", "tool": "Tool"}
            for m in msgs:
                md_lines.append(f"## {role_label.get(m['role'], m['role'].title())}")
                md_lines.append(f"_{_fmt_ts(m['created_at'])}_")
                md_lines.append("")
                md_lines.append(m["content"] or "_(empty)_")
                md_lines.append("")
            zf.writestr(f"{folder}/conversation.md", "\n".join(md_lines))
            # JSON
            jpayload = {
                "session": s,
                "messages": [
                    {"id": m["id"], "role": m["role"], "content": m["content"], "meta": m["meta"], "created_at": m["created_at"]}
                    for m in msgs
                ],
            }
            zf.writestr(f"{folder}/conversation.json", json.dumps(jpayload, indent=2, ensure_ascii=False))
            manifest.append({"folder": folder, "title": s["title"], "id": s["id"], "messages": len(msgs)})
        zf.writestr("index.json", json.dumps({"app": "Miraje", "variant": "mini", "exported_at": time.time(), "sessions": manifest}, indent=2, ensure_ascii=False))
    body = buf.getvalue()
    return Response(
        content=body,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="miraje-backup.zip"'},
    )


# ---------- Import markdown ----------

import re as _re_import  # noqa: E402


_MD_HEADING = _re_import.compile(r"^#{1,3}\s+(.*)$")
_MD_HEADING2 = _re_import.compile(r"^##\s+(.*)$")


@router.post("/sessions/import/text")
async def import_markdown_text(body: dict):
    """Import Markdown text as a new session.

    Request body: {"title": "...", "mode": "chat", "model": "...", "markdown": "..."}
    Parses the markdown into messages:
      - A line starting with "# " sets the session title.
      - A line starting with "## Role:" (e.g. "## You", "## Miraje") starts a new message with that role.
      - Everything until the next "##" heading is that message's content.
    """
    md = (body.get("markdown") or "").strip()
    if not md:
        raise HTTPException(status_code=400, detail="markdown must not be empty")

    title = (body.get("title") or "").strip()
    mode = (body.get("mode") or "chat").strip()
    model = (body.get("model") or "").strip() or None

    # Parse.
    messages: list[dict] = []
    cur_role = None
    cur_lines: list[str] = []
    lines = md.split("\n")

    def flush():
        nonlocal cur_role, cur_lines
        if cur_role and cur_lines:
            content = "\n".join(cur_lines).strip()
            if content:
                messages.append({"role": cur_role, "content": content})
        cur_lines = []

    for line in lines:
        m1 = _MD_HEADING.match(line)
        if m1 and not line.startswith("##"):
            # "# Title" — top-level heading sets session title.
            if not title:
                title = m1.group(1).strip()
            continue
        m2 = _MD_HEADING2.match(line)
        if m2:
            flush()
            role_label = m2.group(1).strip().lower()
            role_map = {"you": "user", "miraje": "assistant", "assistant": "assistant",
                        "user": "user", "system": "system", "tool": "tool"}
            cur_role = role_map.get(role_label, "assistant")
            continue
        cur_lines.append(line)
    flush()

    if not title:
        title = "Imported conversation"

    # Create session + messages.
    sid = uuid.uuid4().hex
    await database.create_session(sid, title, mode, model)
    for m in messages:
        await database.add_message(uuid.uuid4().hex, sid, m["role"], m["content"])
    return {"status": "ok", "session": await database.get_session(sid), "messages_imported": len(messages)}


@router.post("/sessions/import/json")
async def import_json(body: dict):
    """Import a JSON export as a new session (lossless round-trip with /export/json).

    Request body is the exact shape produced by GET /api/sessions/{id}/export/json:
    {"session": {...}, "messages": [...]}.
    A new session is created with a fresh id; the title gets "(imported)" suffix
    to distinguish it. Starred state is preserved on imported messages.
    """
    sess = body.get("session") or {}
    msgs = body.get("messages") or []
    if not sess and not msgs:
        raise HTTPException(status_code=400, detail="body must contain 'session' and/or 'messages'")

    title = (sess.get("title") or "Imported session").strip()
    if not title.endswith("(imported)"):
        title = title + " (imported)"
    mode = (sess.get("mode") or "chat").strip()
    model = sess.get("model") or None
    persona = sess.get("persona") or None
    tags = sess.get("tags") or ""

    sid = uuid.uuid4().hex
    await database.create_session(sid, title, mode, model, persona=persona)
    if tags:
        await database.set_session_tags(sid, tags)

    imported = 0
    for m in msgs:
        starred = 1 if m.get("starred") else 0
        mid = uuid.uuid4().hex
        async with database._connect() as db:
            await db.execute(
                "INSERT INTO messages (id, session_id, role, content, meta, starred, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (mid, sid, m.get("role", "user"), m.get("content", ""),
                 json.dumps(m.get("meta") or {}), starred, m.get("created_at") or time.time()),
            )
            await db.commit()
        imported += 1
    return {"status": "ok", "session": await database.get_session(sid), "messages_imported": imported}


# ---------- Session uploads + workspace downloads ----------
#
# We don't depend on python-multipart, so file uploads are handled as a raw
# request body with the filename passed in via the ``filename`` query param.
# Files are stored under ``data/sessions/{session_id}/uploads/`` and surfaced
# back to the UI (and to the autonomous agent) by path.

import os  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi.responses import FileResponse  # noqa: E402


def _sanitize_filename(name: str) -> str:
    """Strip path separators and unsafe characters from a user-supplied filename."""
    # Take only the basename in case the caller passed a path.
    name = os.path.basename(name or "")
    # Keep alphanumerics, dash, underscore, dot. Replace anything else with _.
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name).strip("._")
    return cleaned or "upload"


def _session_uploads_dir(session_id: str) -> Path:
    """Return (and create) the uploads directory for a given session."""
    uploads = DATA_DIR / "sessions" / session_id / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    return uploads


@router.post("/sessions/{session_id}/upload")
async def upload_file(session_id: str, request: Request, filename: str = "upload"):
    """Upload a file to a session's upload directory.

    The file body is sent as the raw request body (no multipart); the filename
    is supplied via the ``filename`` query parameter. Stored under
    ``data/sessions/{session_id}/uploads/``.
    """
    session = await database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty request body")

    safe_name = _sanitize_filename(filename)
    uploads_dir = _session_uploads_dir(session_id)

    # Avoid clobbering an existing file by appending a short suffix if needed.
    target = uploads_dir / safe_name
    if target.exists():
        stem = target.stem or "upload"
        suffix = target.suffix
        i = 1
        while target.exists():
            target = uploads_dir / f"{stem}_{i}{suffix}"
            i += 1

    target.write_bytes(body)

    return {
        "status": "ok",
        "filename": target.name,
        "path": str(target),
        "size": len(body),
    }


@router.get("/sessions/{session_id}/uploads")
async def list_session_uploads(session_id: str):
    """List all files uploaded to a session."""
    session = await database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")

    uploads_dir = DATA_DIR / "sessions" / session_id / "uploads"
    files: list[dict] = []
    if uploads_dir.exists():
        for entry in sorted(uploads_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not entry.is_file():
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            files.append(
                {
                    "filename": entry.name,
                    "path": str(entry),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                }
            )
    return {"status": "ok", "session_id": session_id, "files": files}


@router.get("/workspace/downloads/{filename}")
async def serve_workspace_download(filename: str):
    """Serve a file from ``data/workspace/downloads/`` for the user to download.

    The autonomous agent's ``download_file`` tool writes assets into that
    directory; this endpoint exposes them over HTTP so the UI can offer a
    "download" link directly from the chat.
    """
    safe_name = _sanitize_filename(filename)
    if safe_name != filename:
        # If the caller tried a path-ish filename, refuse rather than serve
        # an unexpected file.
        raise HTTPException(status_code=400, detail="invalid filename")

    target = DATA_DIR / "workspace" / "downloads" / safe_name
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    return FileResponse(
        path=str(target),
        filename=safe_name,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )
