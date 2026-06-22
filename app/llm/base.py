"""Abstract LLM provider + factory.

Miraje talks to models through two interchangeable providers:

* ``openai-compatible`` — anything that speaks the OpenAI Chat Completions API
  (OpenAI itself, OpenRouter, LM Studio, vLLM, llama.cpp's server, and Ollama's
  own ``/v1`` endpoint).
* ``ollama`` — Ollama's native ``/api/chat`` streaming protocol.

Both expose the same minimal interface, so the rest of the app does not care
which one is active.
"""

from __future__ import annotations

import abc
from typing import Any, AsyncIterator, Optional

import httpx

from .. import database
from ..config import get_settings


class LLMProvider(abc.ABC):
    def __init__(self, base_url: str, api_key: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @abc.abstractmethod
    async def list_models(self) -> list[str]:
        ...

    @abc.abstractmethod
    async def chat(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        ...

    @abc.abstractmethod
    async def stream_chat(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        ...

    async def chat_with_tools(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        """Send a chat request with tool definitions; return the full message dict.

        The returned dict mirrors the OpenAI message shape:
            {"role": "assistant", "content": "...", "tool_calls": [...]}

        The default implementation falls back to plain ``chat()`` and returns a
        message with no tool_calls — so providers that don't natively support
        tool calling still work (the agent will simply not get to use tools).
        """
        content = await self.chat(messages, model, temperature=temperature, max_tokens=max_tokens)
        return {"role": "assistant", "content": content, "tool_calls": []}


async def _runtime_overrides() -> dict[str, str]:
    """Merge env defaults with locally-stored overrides from the Settings panel."""
    s = get_settings()
    overrides = await get_all_settings_safe()
    return {
        "provider": overrides.get("provider", s.provider),
        "base_url": overrides.get("base_url", s.base_url),
        "api_key": overrides.get("api_key", s.api_key),
        "model": overrides.get("model", s.model),
        "ollama_url": overrides.get("ollama_url", s.ollama_url),
        "agent_max_steps": overrides.get("agent_max_steps", str(s.agent_max_steps)),
        "agent_temperature": overrides.get("agent_temperature", str(s.agent_temperature)),
        "show_reasoning": overrides.get("show_reasoning", "false"),
        "active_persona": overrides.get("active_persona", ""),
    }


async def get_all_settings_safe() -> dict[str, str]:
    try:
        return await database.get_all_settings()
    except Exception:
        return {}


async def get_provider() -> LLMProvider:
    cfg = await _runtime_overrides()
    provider = (cfg["provider"] or "openai-compatible").lower()
    if provider == "ollama":
        return OllamaProvider(
            base_url=cfg.get("ollama_url") or "http://localhost:11434",
            api_key="",
            timeout=get_settings().request_timeout,
        )
    return OpenAICompatibleProvider(
        base_url=cfg.get("base_url") or "http://localhost:11434/v1",
        api_key=cfg.get("api_key") or "ollama",
        timeout=get_settings().request_timeout,
    )


async def get_default_model() -> str:
    cfg = await _runtime_overrides()
    return cfg.get("model") or get_settings().model


async def get_show_reasoning() -> bool:
    cfg = await _runtime_overrides()
    val = (cfg.get("show_reasoning") or "false").strip().lower()
    return val in ("1", "true", "yes", "on")


async def get_active_persona() -> Optional[str]:
    cfg = await _runtime_overrides()
    val = (cfg.get("active_persona") or "").strip()
    return val or None


# Imported here to avoid a circular import at module load time.
from .ollama import OllamaProvider  # noqa: E402
from .openai_compat import OpenAICompatibleProvider  # noqa: E402
