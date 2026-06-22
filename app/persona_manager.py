"""File-based persona management.

A persona is a reusable system prompt that shapes how Miraje responds. Each
persona lives on disk under ``data/personas/<id>/`` as a small directory:

    data/personas/<id>/
        meta.json     — structured metadata (name, icon, blurb)
        persona.md    — the system prompt (plain text)
        memory.md     — accumulated summaries / long-term notes

Personas are local, editable, and survive restarts. The built-in personas are
seeded on first launch into the same directory structure so the user can tweak
them in place — they are not special-cased in code.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from .config import DATA_DIR

PERSONAS_DIR: Path = DATA_DIR / "personas"


# ---------- Filesystem-safe ids ----------

_SAFE_ID_RE = re.compile(r"[^a-z0-9-_]+")


def _safe_id(raw: str) -> str:
    """Normalize an arbitrary string into a filesystem-safe persona id."""
    s = (raw or "").strip().lower()
    s = _SAFE_ID_RE.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "persona"


# ---------- Built-in persona content ----------

# The 8 built-in personas. Each tuple is (id, name, icon, blurb, system_prompt).
# Seeded into PERSONAS_DIR on first launch so users can edit them freely.
BUILTIN_PERSONAS: list[dict[str, str]] = [
    {
        "id": "default",
        "name": "Default",
        "icon": "✦",
        "blurb": "Balanced, helpful assistant.",
        "system": (
            "You are Miraje, a helpful, concise assistant. Answer clearly and "
            "use Markdown when it improves readability."
        ),
    },
    {
        "id": "concise",
        "name": "Concise",
        "icon": "⚡",
        "blurb": "Short answers, no fluff.",
        "system": (
            "You are a concise assistant. Keep answers as short as possible "
            "while remaining correct and complete. Prefer bullets over prose. "
            "No filler, no preamble, no apologies."
        ),
    },
    {
        "id": "socratic",
        "name": "Socratic tutor",
        "icon": "🎓",
        "blurb": "Teaches by asking questions.",
        "system": (
            "You are a patient Socratic tutor. Rather than giving answers "
            "directly, guide the user with one focused question at a time, "
            "building on what they already know. Offer hints, then let them "
            "reason. When they arrive at an insight, affirm it and connect it "
            "to the bigger picture."
        ),
    },
    {
        "id": "engineer",
        "name": "Senior engineer",
        "icon": "🛠️",
        "blurb": "Pragmatic, code-first, honest about tradeoffs.",
        "system": (
            "You are a pragmatic senior software engineer. Lead with the most "
            "practical solution, show minimal working code, and call out "
            "tradeoffs, edge cases, and when a simpler approach is better. Be "
            "honest about what you don't know. Prefer composition over "
            "inheritance, explicit over clever."
        ),
    },
    {
        "id": "writer",
        "name": "Writing coach",
        "icon": "✍️",
        "blurb": "Sharpens prose, cuts waste.",
        "system": (
            "You are an exacting writing coach. Improve clarity, rhythm, and "
            "precision. Cut unnecessary words. Explain your edits briefly. "
            "When the user shares a draft, return a revised version followed "
            "by a short list of the principles you applied."
        ),
    },
    {
        "id": "brainstorm",
        "name": "Brainstormer",
        "icon": "💡",
        "blurb": "Generates many ideas, defers judgment.",
        "system": (
            "You are an enthusiastic brainstorming partner. Generate a generous "
            "number of distinct, specific ideas. Vary the angles — practical, "
            "wild, contrarian, minimal. Don't evaluate or filter prematurely; "
            "offer a brief framing after the list for which to pursue and why."
        ),
    },
    {
        "id": "explainer",
        "name": "ELI5 explainer",
        "icon": "🧒",
        "blurb": "Simple words, vivid analogies.",
        "system": (
            "You explain things like you're talking to a curious 10-year-old. "
            "Use simple words, short sentences, and vivid analogies from "
            "everyday life. Check understanding with a gentle question at the "
            "end. Avoid jargon; when a term is unavoidable, define it inline."
        ),
    },
    {
        "id": "devils-advocate",
        "name": "Devil's advocate",
        "icon": "😈",
        "blurb": "Stress-tests ideas and assumptions.",
        "system": (
            "You are a rigorous devil's advocate. Charitably steelman the "
            "strongest objections to the user's position. Point out hidden "
            "assumptions, edge cases, and counterevidence. Be intellectually "
            "honest — concede when an objection is weak. Goal: make the user's "
            "thinking sharper, not to win."
        ),
    },
]


# ---------- Low-level disk helpers ----------

def _persona_dir(persona_id: str) -> Path:
    return PERSONAS_DIR / _safe_id(persona_id)


def _read_meta(pdir: Path) -> dict[str, Any]:
    """Read the meta.json for a persona directory, with sensible defaults."""
    meta_path = pdir / "meta.json"
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    # Fall back to deriving values from the directory name.
    return {
        "id": pdir.name,
        "name": pdir.name.replace("-", " ").title(),
        "icon": "✦",
        "blurb": "",
    }


def _write_meta(pdir: Path, meta: dict[str, Any]) -> None:
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _read_text(pdir: Path, name: str) -> str:
    p = pdir / name
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""


def _write_text(pdir: Path, name: str, text: str) -> None:
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / name).write_text(text or "", encoding="utf-8")


def _persona_view(pdir: Path) -> Optional[dict[str, Any]]:
    """Build the public persona view from a directory, or None if missing."""
    if not pdir.exists() or not (pdir / "persona.md").exists():
        return None
    meta = _read_meta(pdir)
    system = _read_text(pdir, "persona.md")
    memory = _read_text(pdir, "memory.md")
    return {
        "id": pdir.name,
        "name": meta.get("name") or pdir.name,
        "icon": meta.get("icon") or "✦",
        "blurb": meta.get("blurb") or "",
        "system": system,
        "memory": memory,
        "builtin": pdir.name in {p["id"] for p in BUILTIN_PERSONAS},
    }


# ---------- Public API ----------

def seed_builtin_personas() -> None:
    """Ensure the 8 built-in personas exist on disk.

    Existing files are NOT overwritten — the user may have customized them.
    Missing dirs (and missing persona.md files) are created from BUILTIN_PERSONAS.
    """
    PERSONAS_DIR.mkdir(parents=True, exist_ok=True)
    for p in BUILTIN_PERSONAS:
        pdir = _persona_dir(p["id"])
        pdir.mkdir(parents=True, exist_ok=True)
        # Always (re)write meta.json so name/icon/blurb stay in sync with code
        # unless the user has explicitly edited them. We detect edits via a
        # sentinel: if meta.json exists, leave it alone.
        if not (pdir / "meta.json").exists():
            _write_meta(
                pdir,
                {"name": p["name"], "icon": p["icon"], "blurb": p["blurb"]},
            )
        if not (pdir / "persona.md").exists():
            _write_text(pdir, "persona.md", p["system"])


def list_personas() -> list[dict[str, Any]]:
    """List all personas (built-in + user-created), sorted by name."""
    PERSONAS_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pdir in PERSONAS_DIR.iterdir():
        if not pdir.is_dir():
            continue
        view = _persona_view(pdir)
        if not view:
            continue
        seen.add(pdir.name)
        out.append(view)
    # Make sure every built-in persona is listed even if seeding hasn't run.
    for p in BUILTIN_PERSONAS:
        if p["id"] not in seen:
            out.append(
                {
                    "id": p["id"],
                    "name": p["name"],
                    "icon": p["icon"],
                    "blurb": p["blurb"],
                    "system": p["system"],
                    "memory": "",
                    "builtin": True,
                }
            )
    out.sort(key=lambda v: v["id"])
    return out


def get_persona(persona_id: Optional[str]) -> Optional[dict[str, Any]]:
    """Return a single persona view by id, or None if not found.

    Falls back to the 'default' persona if persona_id is None or missing —
    this matches the behaviour callers expect from the old personas module.
    """
    pid = _safe_id(persona_id or "default")
    pdir = _persona_dir(pid)
    view = _persona_view(pdir)
    if view:
        return view
    # Built-in fallback (in case seeding hasn't run yet).
    for p in BUILTIN_PERSONAS:
        if p["id"] == pid:
            return {
                "id": p["id"],
                "name": p["name"],
                "icon": p["icon"],
                "blurb": p["blurb"],
                "system": p["system"],
                "memory": "",
                "builtin": True,
            }
    # Final fallback: default persona.
    return get_persona("default")


def get_persona_system(persona_id: Optional[str]) -> str:
    """Return just the system prompt for a persona (empty string if missing)."""
    p = get_persona(persona_id)
    return (p or {}).get("system", "") or ""


def get_persona_memory(persona_id: str) -> str:
    """Return the accumulated memory for a persona (empty string if missing)."""
    pdir = _persona_dir(persona_id)
    if not pdir.exists():
        return ""
    return _read_text(pdir, "memory.md")


def get_persona_context(persona_id: Optional[str]) -> str:
    """Return persona.md + memory.md combined, as a single system prompt.

    The memory section is appended under a small "Memory:" header so the model
    can distinguish its standing instructions from its accumulated notes. If
    either piece is missing the other is returned alone.
    """
    p = get_persona(persona_id)
    if not p:
        return ""
    parts: list[str] = []
    if p.get("system"):
        parts.append(p["system"].strip())
    memory = (p.get("memory") or "").strip()
    if memory:
        parts.append("Memory:\n" + memory)
    return "\n\n".join(parts).strip()


def create_persona(
    name: str,
    system: str,
    icon: Optional[str] = None,
    blurb: Optional[str] = None,
    persona_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create a new persona on disk. Returns the new persona view.

    A filesystem-safe id is derived from `persona_id` (or `name` if not given).
    If the id already exists, a numeric suffix is appended.
    """
    PERSONAS_DIR.mkdir(parents=True, exist_ok=True)
    base_id = _safe_id(persona_id or name)
    pid = base_id
    n = 2
    while _persona_dir(pid).exists():
        pid = f"{base_id}-{n}"
        n += 1
    pdir = _persona_dir(pid)
    pdir.mkdir(parents=True, exist_ok=True)
    _write_meta(
        pdir,
        {
            "name": (name or pid).strip(),
            "icon": icon or "✦",
            "blurb": blurb or "",
        },
    )
    _write_text(pdir, "persona.md", system or "")
    view = _persona_view(pdir)
    return view or {
        "id": pid,
        "name": name,
        "icon": icon or "✦",
        "blurb": blurb or "",
        "system": system or "",
        "memory": "",
        "builtin": False,
    }


def update_persona(
    persona_id: str,
    name: Optional[str] = None,
    system: Optional[str] = None,
    icon: Optional[str] = None,
    blurb: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Update one or more fields of an existing persona. Returns the new view."""
    pdir = _persona_dir(persona_id)
    if not pdir.exists():
        return None
    meta = _read_meta(pdir)
    if name is not None:
        meta["name"] = name
    if icon is not None:
        meta["icon"] = icon
    if blurb is not None:
        meta["blurb"] = blurb
    _write_meta(pdir, meta)
    if system is not None:
        _write_text(pdir, "persona.md", system)
    return _persona_view(pdir)


def delete_persona(persona_id: str) -> bool:
    """Delete a persona directory. Returns True if something was removed."""
    pdir = _persona_dir(persona_id)
    if not pdir.exists():
        return False
    try:
        shutil.rmtree(pdir)
        return True
    except Exception:  # noqa: BLE001
        return False


def append_to_memory(persona_id: str, text: str) -> Optional[str]:
    """Append a note to a persona's memory.md. Returns the new memory contents."""
    pdir = _persona_dir(persona_id)
    if not pdir.exists():
        return None
    existing = _read_text(pdir, "memory.md")
    addition = (text or "").strip()
    if not addition:
        return existing
    new_memory = (existing + "\n" + addition).strip() if existing else addition
    _write_text(pdir, "memory.md", new_memory + "\n")
    return new_memory


def set_memory(persona_id: str, text: str) -> Optional[str]:
    """Overwrite a persona's memory.md. Returns the new memory contents."""
    pdir = _persona_dir(persona_id)
    if not pdir.exists():
        return None
    _write_text(pdir, "memory.md", (text or "").strip() + ("\n" if text else ""))
    return _read_text(pdir, "memory.md")
