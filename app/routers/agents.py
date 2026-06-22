"""Autonomous agent endpoint (SSE streaming of reasoning steps)."""

from __future__ import annotations

import json
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from starlette.responses import StreamingResponse

from .. import database
from ..agents import AutonomousAgent
from ..llm.base import _runtime_overrides, get_default_model, get_provider, get_show_reasoning
from ..models import AgentRunRequest

router = APIRouter(prefix="/api", tags=["agents"])


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _title_from(task: str) -> str:
    clean = " ".join(task.strip().split())
    prefix = "Agent: "
    title = (clean[:42] + "…") if len(clean) > 42 else clean
    return prefix + (title or "Task")


@router.post("/agent/run")
async def run_agent(body: AgentRunRequest):
    cfg = await _runtime_overrides()
    provider = await get_provider()
    model = body.model or await get_default_model()
    max_steps = body.max_steps or int(cfg.get("agent_max_steps") or 12)
    temperature = float(cfg.get("agent_temperature") or 0.2)
    show_reasoning = await get_show_reasoning()

    session_id = body.session_id
    if not session_id:
        session_id = uuid.uuid4().hex
        await database.create_session(session_id, _title_from(body.task), "agent", model)
    else:
        existing = await database.get_session(session_id)
        if not existing:
            raise HTTPException(status_code=404, detail="session not found")

    task_id = uuid.uuid4().hex
    await database.add_message(task_id, session_id, "user", body.task, {"agent_task": True})

    # Pull conversation history (everything before this task) so the agent has
    # full context for follow-up questions in an existing session.
    db_messages = await database.get_messages(session_id)
    history: list[dict] = []
    for m in db_messages:
        if m["id"] == task_id:
            continue
        if m["role"] in ("user", "assistant", "system"):
            history.append({"role": m["role"], "content": m["content"]})

    agent = AutonomousAgent(
        provider=provider,
        model=model,
        max_steps=max_steps,
        temperature=temperature,
        enabled_tools=body.enabled_tools,
        show_reasoning=show_reasoning,
    )

    async def event_stream() -> AsyncIterator[str]:
        session = await database.get_session(session_id)
        yield _sse(
            {
                "type": "session",
                "session_id": session_id,
                "title": session["title"] if session else "Agent task",
                "model": model,
                "max_steps": max_steps,
                "context_messages": len(history),
                "show_reasoning": show_reasoning,
            }
        )

        final_text = ""
        try:
            async for ev in agent.run(body.task, history=history):
                yield _sse(ev)
                if ev.get("type") == "final":
                    final_text = ev.get("content", "")
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "error", "content": str(e)})
            yield _sse({"type": "done"})
            return

        # Persist the final answer as an assistant message.
        await database.add_message(
            uuid.uuid4().hex,
            session_id,
            "assistant",
            final_text or "(no final answer)",
            {"agent": True, "model": model},
        )
        yield _sse({"type": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
