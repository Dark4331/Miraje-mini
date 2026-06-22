"""Autonomous agent — OpenAI-style tool-calling reasoning loop.

The agent sends the model a list of available tools (OpenAI function-calling
schema) and lets the model decide at each step whether to call a tool or
produce a final answer. Every step is streamed to the UI so you can watch the
agent reason in real time. Nothing about the loop is hidden.

Falls back gracefully to plain ``chat()`` if the provider doesn't support
``chat_with_tools()`` — the agent will still answer, just without tools.
"""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Optional

from ..llm.base import LLMProvider
from .tools import TOOLS, ToolResult, call_tool, list_tools


SYSTEM_PROMPT = """You are Miraje's autonomous agent — a careful, tool-using assistant running on the user's own private server.

You solve a task through a small number of reasoning steps. At each step you may either:
  1. Call one of the provided tools (use the standard tool-call mechanism), or
  2. Reply with your final answer as plain text.

Rules:
- Use tools to gather information before answering when useful. Do not invent facts.
- Keep tool arguments minimal, well-formed, and matching each tool's schema.
- When you have enough to answer, reply with plain text (no tool call) and stop.
- You may think briefly before each tool call — wrap private reasoning in <think>…</think> tags if you wish; that content is shown to the user as reasoning, not as the final answer.

CRITICAL — PATHS AND URLS:
- When the user gives you a file path, use THAT EXACT PATH. Do NOT replace it with a different path. Do NOT hallucinate paths. Copy the user's path character-for-character into the tool argument.
- When the user gives you a URL, use THAT EXACT URL. Do NOT add paths, filenames, or extensions to it. Do NOT modify it in any way.
- If the user does NOT give you a path, save files to the workspace: data/workspace/downloads/ (the default download location). Do NOT invent paths like /home/user or C:\\Users\\anything. If you need a path and the user didn't provide one, use the workspace.
- If you are unsure what path to use, ASK the user. Never guess.

You may be given conversation history for context. Treat it as background; the user's latest task is the one to solve.

Files and the host machine:
- You can access files on the user's machine using list_directory, read_local_file, and write_local_file. Always use absolute paths for these tools.
- write_local_file automatically creates parent directories if they don't exist.
- system_info should ONLY be called when the user explicitly asks about their system (e.g. "what OS do I have?", "how much RAM?"). Do NOT call system_info for general tasks or to discover paths.
- Files the user has uploaded into the current chat are stored under data/sessions/{{session_id}}/uploads/. You can read them with read_local_file by passing the full path.
- Downloaded files go to data/workspace/downloads/ by default. The user can download them from the chat interface.

Downloading from the web — CRITICAL RULES:
1. NEVER guess or invent URLs. Do NOT add "/image.png" or any path to a URL the user gave you. Always scan the actual page first.
2. When the user asks to download something from a URL:
   a. FIRST call find_downloadable_assets with the EXACT URL the user gave you.
   b. This deep-scans the HTML and finds: images (including those with URLs in alt/onclick/data attributes), files, download links, ALL buttons (even without onclick), clickable images, redirects, form actions, CSS background images, and more.
   c. Present ALL findings to the user in a clear list, including the HTML class and element type so the user can identify what to download.
   d. Ask the user which ones they want to download.
3. If the user asks for a specific type (e.g. "the zip file", "the image"):
   - Match their request to the found assets by file extension, description, or HTML class.
   - If a direct file URL matches, use download_file with that URL.
   - If a clickable image leads to another page, use follow_link on the link_url to find the actual full-resolution image URL, then use download_file.
   - If a download button or link was found, use follow_link on the URL to see where it leads, then download from the final URL.
   - If a redirect was found, use follow_link to check where it leads, then download from the final URL.
4. If the user says "download everything" or "all of them" or "both":
   - Download each asset one by one using download_file.
   - Report progress after each download.
5. ALWAYS use the EXACT URL returned by find_downloadable_assets or follow_link — do NOT modify it.
6. If find_downloadable_assets finds nothing or the user says something is missing:
   - Use fetch_url to read the full HTML text of the page.
   - Look through the HTML manually for ANY element that could be an image, download button, or link — check alt attributes, onclick handlers, data attributes, class names, etc.
   - Report ALL HTML elements you found (with their tag, class, and attributes) to the user so they can identify what to download.
7. After downloading, tell the user the file is ready and they can download it from the chat interface.
8. If the user asks to save the downloaded file to a specific location, use read_local_file to read it from the workspace, then write_local_file to write it to the user's requested path.
9. LEARN from the user: when the user tells you which element to download (e.g. "the one with class 'download-btn'"), remember that pattern. Use fetch_url + read the saved learning file at data/workspace/download_patterns.json to check if you've learned patterns from previous interactions.

Available tools:
{tools}"""


# ---------- <think> tag handling ----------

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

    Feed it pieces as they arrive; it emits (token_pieces, reasoning_pieces)
    pairs. Anything inside <think>…</think> is routed to reasoning; everything
    else is routed to tokens. A small lookbehind avoids splitting a tag across
    chunk boundaries.
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
                    # Still inside think; emit everything (held back by lookbehind).
                    if len(self.buffer) > tag_lookbehind:
                        chunk = self.buffer[:-tag_lookbehind]
                        if self.show_reasoning and chunk:
                            reasoning.append(chunk)
                        self.buffer = self.buffer[-tag_lookbehind:]
                    break
                # Found the end tag.
                chunk = self.buffer[:end]
                if self.show_reasoning and chunk:
                    reasoning.append(chunk)
                self.buffer = self.buffer[end + len(_THINK_CLOSE):]
                self.in_think = False
                continue

            # Not in think — look for an opening tag.
            start = self.buffer.find(_THINK_OPEN)
            if start == -1:
                # Hold back the last few chars in case a tag is mid-stream.
                if len(self.buffer) > tag_lookbehind:
                    chunk = self.buffer[:-tag_lookbehind]
                    if chunk:
                        tokens.append(chunk)
                    self.buffer = self.buffer[-tag_lookbehind:]
                break
            # Emit whatever came before the tag, then enter think mode.
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


# ---------- Tool definition builder ----------

# Map the loose type strings in our tool schema to JSON-Schema types.
_TYPE_MAP = {
    "string": "string",
    "str": "string",
    "int": "integer",
    "integer": "integer",
    "number": "number",
    "float": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


def _parse_schema_field(spec: str) -> tuple[str, str, bool]:
    """Parse a schema field spec like 'int (optional, default 6)'.

    Returns (json_type, description, required).
    """
    s = (spec or "").strip()
    required = True
    desc = ""
    # Split off any parenthetical note.
    m = re.match(r"^([^(]+)(?:\((.*)\))?$", s)
    if not m:
        # Fall back to a plain string.
        return "string", s, True
    type_part = m.group(1).strip().lower()
    note = (m.group(2) or "").strip()
    if "optional" in note.lower() or "default" in note.lower():
        required = False
    if note:
        desc = note
    json_type = _TYPE_MAP.get(type_part, "string")
    return json_type, desc, required


def _build_tool_definitions(enabled_tools: Optional[list[str]] = None) -> list[dict]:
    """Build OpenAI-style function tool definitions from the TOOLS registry."""
    enabled = enabled_tools or list(TOOLS.keys())
    out: list[dict] = []
    for name in enabled:
        spec = TOOLS.get(name)
        if not spec:
            continue
        schema = spec.get("schema", {}) or {}
        properties: dict[str, Any] = {}
        required: list[str] = []
        for field_name, field_spec in schema.items():
            if isinstance(field_spec, dict):
                # Already structured — pass through (best effort).
                properties[field_name] = field_spec
                if field_spec.get("required", True):
                    required.append(field_name)
                continue
            json_type, desc, is_required = _parse_schema_field(str(field_spec))
            prop: dict[str, Any] = {"type": json_type}
            if desc:
                prop["description"] = desc
            properties[field_name] = prop
            if is_required:
                required.append(field_name)
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.get("description", ""),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
        )
    return out


def _build_system_prompt(enabled_tools: Optional[list[str]] = None) -> str:
    """Build the agent's system prompt, listing each tool and its schema."""
    lines = []
    enabled = enabled_tools or list(TOOLS.keys())
    for t in list_tools():
        if t["name"] not in enabled:
            continue
        schema = ", ".join(f'"{k}": {v}' for k, v in t["schema"].items()) or "(no arguments)"
        lines.append(f'- {t["name"]}: {t["description"]} Schema: {{{schema}}}')
    return SYSTEM_PROMPT.format(tools="\n".join(lines))


class AutonomousAgent:
    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        max_steps: int = 12,
        temperature: float = 0.2,
        enabled_tools: Optional[list[str]] = None,
        show_reasoning: bool = False,
    ):
        self.provider = provider
        self.model = model
        self.max_steps = max_steps
        self.temperature = temperature
        self.enabled_tools = enabled_tools or list(TOOLS.keys())
        self.show_reasoning = show_reasoning

    async def _call_with_tools(
        self, messages: list[dict], tool_defs: list[dict]
    ) -> dict[str, Any]:
        """Call the model with tools; fall back to plain chat() on failure."""
        try:
            return await self.provider.chat_with_tools(
                messages,
                self.model,
                tools=tool_defs,
                temperature=self.temperature,
            )
        except Exception as e:  # noqa: BLE001
            # Provider doesn't support tool calls or the endpoint errored —
            # fall back to a plain chat() and treat its output as the final
            # answer.
            try:
                content = await self.provider.chat(
                    messages, self.model, temperature=self.temperature
                )
                return {
                    "role": "assistant",
                    "content": content or f"(tool-aware chat failed: {e})",
                    "tool_calls": [],
                }
            except Exception as e2:  # noqa: BLE001
                raise RuntimeError(f"chat_with_tools and chat both failed: {e}; {e2}") from e2

    async def run(
        self, task: str, history: Optional[list[dict]] = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Run the agent loop, streaming step/thought/tool_call/observation/final events.

        ``history`` is an optional list of prior conversation messages
        ({"role": ..., "content": ...}) used for context.
        """
        system = _build_system_prompt(self.enabled_tools)
        tool_defs = _build_tool_definitions(self.enabled_tools)

        messages: list[dict] = [{"role": "system", "content": system}]
        if history:
            for m in history:
                role = m.get("role")
                content = m.get("content")
                if role in ("user", "assistant", "system") and content:
                    messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": task})

        for step in range(1, self.max_steps + 1):
            yield {"type": "step", "step": step}

            try:
                msg = await self._call_with_tools(messages, tool_defs)
            except Exception as e:  # noqa: BLE001
                yield {"type": "error", "content": f"model call failed: {e}"}
                yield {"type": "done"}
                return

            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []

            # If the model emitted visible reasoning tokens (some providers
            # stream them inside <think>…</think> in content), surface them.
            stripped = _strip_think(content).strip()
            if content and content != stripped and self.show_reasoning:
                reasoning_text = content.replace(stripped, "").strip()
                # Strip the tags themselves for the UI.
                reasoning_text = _THINK_BLOCK_RE.sub(
                    lambda m: m.group(0)[len(_THINK_OPEN) : -len(_THINK_CLOSE)],
                    reasoning_text,
                )
                if reasoning_text:
                    yield {"type": "thought", "content": reasoning_text}

            # Append the assistant message to the running history.
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                # No tool calls → final answer.
                yield {"type": "final", "content": stripped or content}
                yield {"type": "done"}
                return

            # Execute each tool call in order.
            for tc in tool_calls:
                fn = tc.get("function") or {}
                tool_name = fn.get("name", "")
                raw_args = fn.get("arguments", "")
                tool_call_id = tc.get("id", "")

                try:
                    tool_input = (
                        json.loads(raw_args) if isinstance(raw_args, str) and raw_args else {}
                    )
                except json.JSONDecodeError:
                    tool_input = {"value": raw_args}

                if tool_name not in self.enabled_tools or tool_name not in TOOLS:
                    obs = ToolResult(False, f"tool '{tool_name}' is not available")
                    yield {
                        "type": "tool_call",
                        "tool": tool_name,
                        "input": tool_input,
                        "ok": False,
                    }
                    yield {"type": "observation", "content": obs.as_text(), "ok": False}
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "name": tool_name,
                            "content": obs.as_text(),
                        }
                    )
                    continue

                yield {
                    "type": "tool_call",
                    "tool": tool_name,
                    "input": tool_input,
                    "ok": True,
                }
                result = await call_tool(tool_name, tool_input)
                yield {
                    "type": "observation",
                    "content": result.as_text(),
                    "ok": result.ok,
                }
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                        "content": result.as_text(),
                    }
                )

        yield {
            "type": "final",
            "content": (
                "I reached the maximum number of reasoning steps without "
                "producing a final answer."
            ),
        }
        yield {"type": "done"}
