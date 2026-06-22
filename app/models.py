"""Request / response schemas."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: str
    name: Optional[str] = None


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    messages: list[ChatMessage]
    model: Optional[str] = None
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    system: Optional[str] = None
    persona: Optional[str] = None


class SessionCreate(BaseModel):
    title: str = "New chat"
    mode: str = "chat"
    model: Optional[str] = None


class AgentRunRequest(BaseModel):
    session_id: Optional[str] = None
    task: str
    model: Optional[str] = None
    max_steps: Optional[int] = None
    enabled_tools: Optional[list[str]] = None


class SettingsUpdate(BaseModel):
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    ollama_url: Optional[str] = None
    agent_max_steps: Optional[int] = None
    agent_temperature: Optional[float] = None
    custom_system_prompt: Optional[str] = None
    show_reasoning: Optional[bool] = None
    active_persona: Optional[str] = None


class PersonaCreate(BaseModel):
    name: str
    system: str
    icon: Optional[str] = None
    blurb: Optional[str] = None
    id: Optional[str] = None


class PersonaUpdate(BaseModel):
    name: Optional[str] = None
    system: Optional[str] = None
    icon: Optional[str] = None
    blurb: Optional[str] = None


class MemoryUpdate(BaseModel):
    content: str
