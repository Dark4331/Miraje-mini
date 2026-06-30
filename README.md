<div align="center">

<img src="logo.svg" alt="Miraje" width="120" height="120" />

# Miraje · mini

**A self-hosted, privacy-first agentic client for talking to language models.**

Chat. Autonomous agents. Local-first. No telemetry. No accounts.
Just you and your models.

[![License: MIT](https://img.shields.io/badge/License-MIT-d4a574?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12+-1a1a1f?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![Docker](https://img.shields.io/badge/Docker-Ready-1a1a1f?style=flat-square&logo=docker&logoColor=white)](https://www.docker.com)
[![Privacy](https://img.shields.io/badge/Telemetry-Never-d4a574?style=flat-square)](#-privacy-first)

</div>

---

Miraje is a single, self-contained web app you run on your own machine. Point it
at any language model — a local [Ollama](https://ollama.com) instance, an
[LM Studio](https://lmstudio.ai) server, a cloud API, or anything that speaks
the OpenAI Chat Completions protocol — and talk to it through a clean, fast,
local interface. When a simple answer isn't enough, switch to **Agent mode** and
let Miraje reason step-by-step, calling tools (web search, code execution, …)
only when it decides it needs them.

Everything — conversations, settings, keys — lives in one local SQLite file on
your host. Nothing is collected. Nothing is sent anywhere except the model
endpoint **you** configure.

> **mini** is the lightweight edition: a focused, dependency-light core that's
> easy to read, audit, and extend.

---

## ✨ Features

- **💬 Streaming chat** — token-by-token responses over SSE, with full Markdown rendering (code blocks, tables, lists, links).
- **🤖 Autonomous agents** — a transparent ReAct reasoning loop. Watch every thought, tool call and observation as it happens.
- **🧰 Built-in tools** — web search (DuckDuckGo, no API key), URL fetch, calculator, current time, and sandboxed Python execution.
- **🔌 Model-agnostic** — works with Ollama, OpenAI, OpenRouter, Together, Groq, LM Studio, vLLM, llama.cpp, and more.
- **🏠 Local-first** — all chats and settings in a single `data/miraje.db` file. Back it up, move it, delete it. It's yours.
- **🔒 Privacy-first** — zero telemetry, zero analytics, zero accounts. No data leaves your host except the prompts you send to your chosen model.
- **🐳 One-command Docker** — `docker compose up -d` and you're done.
- **🌑 Polished dark UI** — minimalist archway/mirage aesthetic, fully responsive, works offline (no CDNs, no external fonts).
- **🪶 Minimal dependencies** — FastAPI + httpx + aiosqlite. That's essentially it. Easy to audit.

---

## 🚀 Quick start (Docker)

The easiest way to run Miraje.

```bash
git clone https://github.com/Dark4331/Miraje-mini.git
cd Miraje-mini
cp .env.example .env          # optional: tweak provider/model
docker compose up -d
```

Then open **http://localhost:8000** in your browser.

That's it. Configure your model in the in-app **Settings** panel (top-left
button) and start chatting.

### Pointing it at a local model

Miraje runs in Docker, so to reach an Ollama / LM Studio server running on your
host, use `host.docker.internal` (already wired up in `docker-compose.yml`):

| Server        | `MIRAJE_PROVIDER`     | `MIRAJE_BASE_URL`                          |
| ------------- | --------------------- | ------------------------------------------ |
| Ollama        | `openai-compatible`   | `http://host.docker.internal:11434/v1`     |
| Ollama (native) | `ollama`            | — (`MIRAJE_OLLAMA_URL=http://host.docker.internal:11434`) |
| LM Studio     | `openai-compatible`   | `http://host.docker.internal:1234/v1`      |
| llama.cpp     | `openai-compatible`   | `http://host.docker.internal:8080/v1`      |
| OpenAI        | `openai-compatible`   | `https://api.openai.com/v1`                |
| OpenRouter    | `openai-compatible`   | `https://openrouter.ai/api/v1`             |

> All of these can also be set from the **Settings** panel at runtime — no
> restart required.

---


## ⚙️ Configuration

Miraje reads settings from environment variables on startup, then lets you
override them at runtime through the **Settings** panel. Runtime overrides are
stored in your local SQLite database and take precedence.

| Variable                   | Default                                            | Description                                  |
| -------------------------- | -------------------------------------------------- | -------------------------------------------- |
| `MIRAJE_PROVIDER`          | `openai-compatible`                                | `openai-compatible` or `ollama`              |
| `MIRAJE_BASE_URL`          | `http://localhost:11434/v1`                        | OpenAI-compatible endpoint                   |
| `MIRAJE_API_KEY`           | `ollama`                                           | API key (dummy is fine for local servers)    |
| `MIRAJE_MODEL`             | `llama3.1`                                         | Default model name                           |
| `MIRAJE_OLLAMA_URL`        | `http://localhost:11434`                           | Native Ollama URL (provider=ollama)          |
| `MIRAJE_AGENT_MAX_STEPS`   | `12`                                               | Max reasoning steps per agent run            |
| `MIRAJE_AGENT_TEMPERATURE` | `0.2`                                              | Sampling temperature for the agent           |
| `MIRAJE_HOST` / `PORT`     | `0.0.0.0` / `8000`                                 | Bind address                                 |
| `MIRAJE_TELEMETRY`         | `false`                                            | Always off. Exposed for transparency only.  |

See [`.env.example`](.env.example) for a ready-to-edit template.

---

## 🤖 Agent mode & tools

Switch to **New agent task** in the sidebar, describe a goal, and Miraje will
plan, act, and answer — streaming every step to the UI.

The agent uses a ReAct-style loop: at each step it emits a short *thought*,
then either **calls a tool** or **gives a final answer**. Tools run on your own
Miraje host and only fire when the agent explicitly chooses them.

| Tool             | What it does                                                       |
| ---------------- | ------------------------------------------------------------------ |
| `web_search`     | Search the web via DuckDuckGo (no API key, no account).            |
| `fetch_url`      | Download a URL and extract its text content.                       |
| `calculator`     | Safely evaluate a math expression.                                 |
| `current_time`   | Return the current date and time.                                  |
| `python_execute` | Run a Python snippet in a subprocess (10s timeout) and return output. |
| `read_file`      | Read a text file from the local workspace (`data/workspace/`).     |
| `write_file`     | Write a text file to the local workspace (sandboxed, no path escapes). |
| `list_files`     | List files in the local workspace.                                 |
| `summarize_text` | Extractive summary of arbitrary text (no model needed, TF-based).  |
| `word_count`     | Count words, sentences, characters, and lines in text.             |

The **local workspace** (`data/workspace/`) is a sandboxed directory the agent
can read from and write to — perfect for tasks that produce artifacts (notes,
drafts, generated code, reports). Paths are validated to prevent directory
traversal.

> **Security note:** `python_execute` runs code on your Miraje host. That's the
> point — it's *your* machine. For untrusted tasks, run Miraje in a container
> (the default Docker setup) and keep `max_steps` modest. Tool use is also
> per-run toggleable from the composer in Agent mode.

Adding a tool is a ~15-line edit in [`app/agents/tools.py`](app/agents/tools.py).

### Personas (system prompts)

A **persona** is a reusable system prompt that shapes how Miraje responds. Pick
one from the chip row above the composer in chat mode, and every reply inherits
its voice. Eight ship built-in:

| Persona           | Voice                                            |
| ----------------- | ------------------------------------------------ |
| ✦ Default         | Balanced, helpful assistant.                     |
| ⚡ Concise         | Short answers, no fluff.                         |
| 🎓 Socratic tutor | Teaches by asking questions.                     |
| 🛠️ Senior engineer | Pragmatic, code-first, honest about tradeoffs.   |
| ✍️ Writing coach  | Sharpens prose, cuts waste.                      |
| 💡 Brainstormer   | Generates many ideas, defers judgment.           |
| 🧒 ELI5 explainer | Simple words, vivid analogies.                   |
| 😈 Devil's advocate | Stress-tests ideas and assumptions.            |

Adding one is a single entry in [`app/personas.py`](app/personas.py). Personas
are local and static — no remote template store, no telemetry.

`GET /api/personas` lists them; `POST /api/chat` accepts a `persona` field that
injects the corresponding system prompt.

### Exporting conversations

Every session can be exported to Markdown or JSON — great for backups, sharing,
or piping into other tools:

```bash
# Markdown
curl -o chat.md   http://localhost:8000/api/sessions/<id>/export/markdown

# JSON (full session + messages + metadata)
curl -o chat.json http://localhost:8000/api/sessions/<id>/export/json
```

The same endpoints are wired into the Miraje UI's session menu.

---

## 🏗️ Architecture

```
miraje/
├── app/
│   ├── main.py              # FastAPI app + static serving
│   ├── config.py            # Env-driven settings
│   ├── database.py          # Local-first SQLite (aiosqlite)
│   ├── models.py            # Pydantic schemas
│   ├── llm/                 # Provider abstraction
│   │   ├── base.py          #   factory + interface
│   │   ├── openai_compat.py #   OpenAI-compatible (streaming SSE)
│   │   └── ollama.py        #   Native Ollama (NDJSON streaming)
│   ├── agents/
│   │   ├── autonomous.py    # ReAct reasoning loop
│   │   └── tools.py         # Built-in tools
│   └── routers/
│       ├── chat.py          # Chat + sessions (SSE)
│       ├── agents.py        # Agent runs (SSE)
│       └── system.py        # Health, settings, models, tools
├── static/                  # Offline frontend (HTML/CSS/JS, no build step)
│   ├── index.html
│   ├── css/style.css
│   ├── js/app.js
│   ├── js/markdown.js       # Minimal dependency-free Markdown renderer
│   └── logo.svg
├── data/                    # Your data: miraje.db (gitignored)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

**Why this shape?** A single-process FastAPI app talking to a local SQLite file
is the smallest possible surface that still gives you a real, persistent,
multi-session agentic client. No message queues, no Redis, no external database
to babysit. The frontend is plain HTML/CSS/JS with no build step, so the whole
thing is auditable in an afternoon.

---

## 🔌 API reference (brief)

Miraje is its own backend. If you want to script it, the JSON API is stable:

| Method | Endpoint                         | Purpose                                  |
| ------ | -------------------------------- | ---------------------------------------- |
| `GET`  | `/api/health`                    | Liveness + version.                      |
| `GET`  | `/api/settings`                  | Current settings (key masked).           |
| `PUT`  | `/api/settings`                  | Update settings.                         |
| `GET`  | `/api/models`                    | List models from the active provider.    |
| `GET`  | `/api/tools`                     | List agent tools.                        |
| `GET`  | `/api/personas`                  | List personas (system prompts).          |
| `GET`  | `/api/stats`                     | Aggregate usage stats (local, no telemetry). |
| `POST` | `/api/sessions`                  | Create a session.                        |
| `GET`  | `/api/sessions`                  | List sessions.                           |
| `GET`  | `/api/sessions/{id}`             | Session + messages.                      |
| `PATCH`| `/api/sessions/{id}?title=`      | Rename.                                  |
| `PUT`  | `/api/sessions/{id}/persona?persona=` | Set the session's persona.          |
| `PUT`  | `/api/sessions/{id}/pin?pinned=`  | Pin (favorite) or unpin a session.       |
| `PUT`  | `/api/sessions/{id}/tags?tags=`  | Set comma-separated tags on a session.   |
| `POST` | `/api/sessions/{id}/viewed`      | Mark a session as just-viewed.           |
| `GET`  | `/api/sessions/recent`           | Most-recently-viewed sessions.           |
| `GET`  | `/api/tags`                      | All tags with session counts.            |
| `GET`  | `/api/search?q=`                 | Full-text search across all messages.    |
| `DELETE`| `/api/sessions/{id}`            | Delete session + messages.               |
| `GET`  | `/api/sessions/{id}/export/markdown` | Export session as Markdown (.md).  |
| `GET`  | `/api/sessions/{id}/export/json`     | Export session as JSON (.json).    |
| `GET`  | `/api/export/all.zip`                | Back up every session as a .zip.   |
| `POST` | `/api/sessions/import/text`      | Import Markdown text as a new session.  |
| `POST` | `/api/sessions/import/json`      | Import a JSON export (lossless round-trip). |
| `DELETE`| `/api/sessions/{id}/messages/{mid}` | Delete a single message.          |
| `PUT`  | `/api/sessions/{id}/messages/{mid}/star?starred=` | Bookmark (star) a message. |
| `GET`  | `/api/messages/starred`          | All starred messages across sessions.    |
| `POST` | `/api/sessions/{id}/duplicate`   | Clone a session (metadata + messages).   |
| `PUT`  | `/api/sessions/{id}/messages/{mid}/edit` | Edit a message, drop later history, re-stream (SSE). |
| `POST` | `/api/sessions/{id}/regenerate`     | Drop the last reply and re-stream (SSE). |
| `POST` | `/api/chat`                      | Stream a chat completion (SSE).          |
| `POST` | `/api/agent/run`                 | Stream an autonomous agent run (SSE).    |

All streaming endpoints return `text/event-stream` with JSON `data:` payloads.
See [`app/routers/`](app/routers/) for the exact event shapes.

---

## 🔒 Privacy-first

- **No telemetry.** No analytics SDK, no usage tracking, no error reporting.
  `MIRAJE_TELEMETRY=false` and there's no code path to turn it on.
- **No accounts.** No login, no auth server, no cloud. It's a local app.
- **No third-party requests** except the model endpoint *you* configure. The
  frontend loads zero external resources — no CDN fonts, no tracking pixels.
- **Your data is a file.** `data/miraje.db`. Back it up, copy it, inspect it
  with `sqlite3`, delete it. It never leaves your host.

---

## ❓ FAQ

**Does Miraje send my prompts anywhere?** Only to the model endpoint you
configure. If you point it at a local Ollama, nothing leaves your machine.

**Can I use it with OpenAI / OpenRouter / Groq?** Yes — set `MIRAJE_PROVIDER=openai-compatible`,
the provider's base URL, and your API key (in `.env` or the Settings panel).

**Where are my conversations stored?** In `data/miraje.db`, a single SQLite
file. In Docker it's mounted at `./data` — back up that folder.

**How is this different from other chat UIs?** It's small, auditable, agentic
out of the box, and aggressively local-first. The whole backend is a handful of
files you can read in one sitting.

**Does it work offline?** The UI does (no external assets). Chat/agents need a
model — run Ollama locally and you're fully offline.

---

## 🗺️ Roadmap

- [ ] Multiple parallel agent tasks
- [ ] Tool: local file workspace with per-session sandbox
- [ ] RAG over local documents
- [ ] Prompt templates / preset personas
- [ ] Export conversations (Markdown / JSON)
- [ ] Multi-user / API key auth for shared deployments

---

## 🤝 Contributing

PRs welcome. Keep dependencies minimal, keep the frontend build-free, and keep
telemetry at zero. See [`LICENSE`](LICENSE) (MIT).

---

<div align="center">

**Miraje · mini** — *just you and your models.*

</div>
