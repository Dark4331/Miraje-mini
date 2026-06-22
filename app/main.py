"""Miraje application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import STATIC_DIR, get_settings
from .database import init_db
from .persona_manager import seed_builtin_personas
from .routers import agents, chat, system


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Seed the 8 built-in personas onto disk on first launch (idempotent).
    seed_builtin_personas()
    yield


app = FastAPI(
    title="Miraje",
    version=__version__,
    description="A self-hosted, privacy-first agentic LLM client.",
    lifespan=lifespan,
)

# API routers
app.include_router(system.router)
app.include_router(chat.router)
app.include_router(agents.router)

# Static assets
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/favicon.ico")
async def favicon():
    p = STATIC_DIR / "logo.svg"
    if p.exists():
        return FileResponse(str(p), media_type="image/svg+xml")
    return FileResponse(str(STATIC_DIR / "index.html"))


def main() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, reload=False)


if __name__ == "__main__":
    main()
