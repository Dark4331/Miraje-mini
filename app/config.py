"""Application configuration.

All settings can be provided through environment variables (great for Docker)
or overridden at runtime through the in-app Settings panel (stored locally in
SQLite). Nothing is ever sent anywhere except the LLM endpoint you configure.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "miraje.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MIRAJE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Default LLM provider: "openai-compatible" | "ollama"
    provider: str = "openai-compatible"

    # OpenAI-compatible endpoint (OpenAI, OpenRouter, LM Studio, vLLM, llama.cpp server, Ollama's /v1, etc.)
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    model: str = "llama3.1"

    # Native Ollama endpoint (used when provider == "ollama")
    ollama_url: str = "http://localhost:11434"

    # Agent behavior
    agent_max_steps: int = 12
    agent_temperature: float = 0.2
    request_timeout: float = 120.0

    # Privacy / telemetry — hard-coded off, exposed for transparency.
    telemetry: bool = False

    def ensure_dirs(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
