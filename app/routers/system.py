"""System endpoints: health, settings, models, tools, personas."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from .. import __version__, database
from ..agents.tools import list_tools
from ..config import get_settings
from ..llm.base import get_provider, get_default_model, _runtime_overrides
from ..persona_manager import (
    append_to_memory,
    create_persona,
    delete_persona,
    get_persona,
    get_persona_memory,
    list_personas,
    set_memory,
    update_persona,
)
from ..models import PersonaCreate, PersonaUpdate, MemoryUpdate, SettingsUpdate

router = APIRouter(prefix="/api", tags=["system"])

_KEY_MASK = "••••••••••"


# ---------- Health ----------

@router.get("/health")
async def health():
    return {
        "status": "ok",
        "app": "Miraje",
        "variant": "mini",
        "version": __version__,
        "telemetry": False,
    }


# ---------- Settings ----------

@router.get("/settings")
async def get_settings_view():
    cfg = await _runtime_overrides()
    api_key = cfg.get("api_key") or ""
    custom_sp = await database.get_setting("custom_system_prompt", "")
    show_reasoning_raw = (cfg.get("show_reasoning") or "false").strip().lower()
    return {
        "provider": cfg.get("provider"),
        "base_url": cfg.get("base_url"),
        "api_key_set": bool(api_key),
        "api_key_masked": _KEY_MASK if api_key else "",
        "model": cfg.get("model"),
        "ollama_url": cfg.get("ollama_url"),
        "agent_max_steps": int(cfg.get("agent_max_steps") or 12),
        "agent_temperature": float(cfg.get("agent_temperature") or 0.2),
        "custom_system_prompt": custom_sp or "",
        "show_reasoning": show_reasoning_raw in ("1", "true", "yes", "on"),
        "active_persona": cfg.get("active_persona") or "",
        "telemetry": False,
        "env_provider": os.environ.get("MIRAJE_PROVIDER", ""),
    }


@router.put("/settings")
async def update_settings(body: SettingsUpdate):
    if body.provider is not None:
        await database.set_setting("provider", body.provider)
    if body.base_url is not None:
        await database.set_setting("base_url", body.base_url)
    # Only overwrite the key when a real value is supplied (not the mask).
    if body.api_key is not None and body.api_key != "" and body.api_key != _KEY_MASK:
        await database.set_setting("api_key", body.api_key)
    if body.model is not None:
        await database.set_setting("model", body.model)
    if body.ollama_url is not None:
        await database.set_setting("ollama_url", body.ollama_url)
    if body.agent_max_steps is not None:
        await database.set_setting("agent_max_steps", str(body.agent_max_steps))
    if body.agent_temperature is not None:
        await database.set_setting("agent_temperature", str(body.agent_temperature))
    if body.custom_system_prompt is not None:
        await database.set_setting("custom_system_prompt", body.custom_system_prompt)
    if body.show_reasoning is not None:
        await database.set_setting("show_reasoning", "true" if body.show_reasoning else "false")
    if body.active_persona is not None:
        await database.set_setting("active_persona", body.active_persona)
    return {"status": "ok"}


# ---------- Models / Tools ----------

@router.get("/models")
async def list_models():
    provider = await get_provider()
    models = await provider.list_models()
    default = await get_default_model()
    return {"models": models, "default": default, "provider": provider.__class__.__name__}


@router.get("/tools")
async def tools():
    return {"tools": list_tools()}


# ---------- Personas (file-backed) ----------

@router.get("/personas")
async def personas():
    return {"personas": list_personas()}


@router.get("/personas/{persona_id}")
async def get_persona_route(persona_id: str):
    p = get_persona(persona_id)
    if not p:
        raise HTTPException(status_code=404, detail="persona not found")
    return {"persona": p}


@router.post("/personas")
async def create_persona_route(body: PersonaCreate):
    p = create_persona(
        name=body.name,
        system=body.system,
        icon=body.icon,
        blurb=body.blurb,
        persona_id=body.id,
    )
    return {"status": "ok", "persona": p}


@router.put("/personas/{persona_id}")
async def update_persona_route(persona_id: str, body: PersonaUpdate):
    p = update_persona(
        persona_id,
        name=body.name,
        system=body.system,
        icon=body.icon,
        blurb=body.blurb,
    )
    if not p:
        raise HTTPException(status_code=404, detail="persona not found")
    return {"status": "ok", "persona": p}


@router.delete("/personas/{persona_id}")
async def delete_persona_route(persona_id: str):
    ok = delete_persona(persona_id)
    if not ok:
        raise HTTPException(status_code=404, detail="persona not found")
    return {"status": "ok"}


# ---------- Persona memory ----------

@router.get("/personas/{persona_id}/memory")
async def get_persona_memory_route(persona_id: str):
    p = get_persona(persona_id)
    if not p:
        raise HTTPException(status_code=404, detail="persona not found")
    return {"persona_id": persona_id, "memory": get_persona_memory(persona_id)}


@router.put("/personas/{persona_id}/memory")
async def set_persona_memory_route(persona_id: str, body: MemoryUpdate):
    p = get_persona(persona_id)
    if not p:
        raise HTTPException(status_code=404, detail="persona not found")
    new_memory = set_memory(persona_id, body.content)
    return {"status": "ok", "persona_id": persona_id, "memory": new_memory or ""}


@router.post("/personas/{persona_id}/memory")
async def append_persona_memory_route(persona_id: str, body: MemoryUpdate):
    p = get_persona(persona_id)
    if not p:
        raise HTTPException(status_code=404, detail="persona not found")
    new_memory = append_to_memory(persona_id, body.content)
    return {"status": "ok", "persona_id": persona_id, "memory": new_memory or ""}


# ---------- Stats ----------

@router.get("/stats")
async def stats():
    """Aggregate usage stats — local only, computed on demand, zero telemetry."""
    return await database.get_stats()
