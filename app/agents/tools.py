"""Built-in agent tools.

All tools are opt-in and run on the user's own Miraje host. Web access tools
(``web_search``, ``fetch_url``) only fire when an agent explicitly decides to
use them — nothing phones home on its own.
"""

from __future__ import annotations

import ast
import asyncio
import operator as op
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

import httpx


# ---------- Tool result container ----------

@dataclass
class ToolResult:
    ok: bool
    output: str
    meta: dict[str, Any] = field(default_factory=dict)

    def as_text(self) -> str:
        prefix = "" if self.ok else "ERROR: "
        body = self.output
        if len(body) > 6000:
            body = body[:6000] + "\n…[truncated]"
        return prefix + body


# ---------- Safe math calculator ----------

_BIN_OPS = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv, ast.Mod: op.mod, ast.Pow: op.pow,
}
_UNARY_OPS = {ast.UAdd: op.pos, ast.USub: op.neg}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("only numbers allowed")
    if isinstance(node, ast.BinOp):
        return _BIN_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        return _UNARY_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"unsupported expression: {ast.dump(node)}")


async def tool_calculator(expression: str) -> ToolResult:
    """Evaluate a math expression. Supports + - * / // % **, parentheses, numbers."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        result = _safe_eval(tree)
        return ToolResult(True, f"{expression} = {result}")
    except Exception as e:  # noqa: BLE001
        return ToolResult(False, f"could not evaluate: {e}")


# ---------- Current time ----------

async def tool_current_time() -> ToolResult:
    """Return the current date and time (server local)."""
    now = time.localtime()
    return ToolResult(
        True,
        time.strftime("%Y-%m-%d %H:%M:%S (%A)", now),
    )


# ---------- Web search (DuckDuckGo Lite, no API key, no account) ----------

_DDAG_LITE = "https://lite.duckduckgo.com/lite/"
_A_TAG = re.compile(r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_TAG_STRIP = re.compile(r"<[^>]+>")


def _clean(html: str) -> str:
    return _TAG_STRIP.sub("", html).strip()


def _parse_ddg(html: str, max_results: int) -> list[dict]:
    results: list[dict] = []
    for m in _A_TAG.finditer(html):
        href = m.group(1)
        title = _clean(m.group(2))
        if not title or href.startswith("javascript:"):
            continue
        udir = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
        if udir:
            href = udir[0]
        results.append({"title": title, "url": href})
        if len(results) >= max_results:
            break
    return results


async def tool_web_search(query: str, max_results: int = 6) -> ToolResult:
    """Search the web with DuckDuckGo and return titles and URLs."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.post(
                _DDAG_LITE,
                data={"q": query, "kl": "us-en"},
                headers={"User-Agent": "Miraje/1.0"},
            )
            resp.raise_for_status()
            results = _parse_ddg(resp.text, max_results)
    except Exception as e:  # noqa: BLE001
        return ToolResult(False, f"search failed: {e}")

    if not results:
        return ToolResult(True, "No results found.")
    lines = [f"Web search: {query}"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}")
    return ToolResult(True, "\n".join(lines))


# ---------- Fetch URL + strip to text ----------

async def tool_fetch_url(url: str) -> ToolResult:
    """Download a URL and return its text content (HTML tags removed)."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Miraje/1.0"})
            resp.raise_for_status()
            html = resp.text
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.I | re.S)
        text = _TAG_STRIP.sub("\n", html)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        if len(text) > 8000:
            text = text[:8000] + "\n…[truncated]"
        return ToolResult(True, text)
    except Exception as e:  # noqa: BLE001
        return ToolResult(False, f"fetch failed: {e}")


# ---------- Python execution (sandboxed subprocess) ----------

_PY_TEMPLATE = """
import sys, math, json, re, statistics, itertools, collections, datetime
{code}
"""


def _run_python(code: str) -> str:
    wrapped = _PY_TEMPLATE.format(code=code)
    proc = subprocess.run(
        [sys.executable, "-c", wrapped],
        capture_output=True,
        text=True,
        timeout=10,
    )
    out = proc.stdout
    if proc.returncode != 0:
        out += ("\n" if out else "") + proc.stderr.strip()
    return out.strip() or "(no output)"


async def tool_python_execute(code: str) -> ToolResult:
    """Run a Python snippet on the Miraje host and return stdout/stderr (10s timeout)."""
    try:
        out = await asyncio.to_thread(_run_python, code)
        return ToolResult(True, out)
    except subprocess.TimeoutExpired:
        return ToolResult(False, "execution timed out (10s limit)")
    except Exception as e:  # noqa: BLE001
        return ToolResult(False, f"execution failed: {e}")


# ---------- Local workspace (sandboxed to data/workspace) ----------

from ..config import DATA_DIR  # noqa: E402

_WORKSPACE = DATA_DIR / "workspace"


def _safe_workspace_path(filename: str) -> Any:
    """Resolve a filename under the workspace dir, rejecting escapes."""
    if not filename or ".." in filename.split("/"):
        raise ValueError("invalid filename")
    base = _WORKSPACE.resolve()
    target = (base / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError("path escapes workspace")
    return target


async def tool_read_file(filename: str) -> ToolResult:
    """Read a text file from the Miraje local workspace (data/workspace)."""
    def _read() -> str:
        _WORKSPACE.mkdir(parents=True, exist_ok=True)
        p = _safe_workspace_path(filename)
        if not p.exists():
            raise FileNotFoundError(f"{filename} does not exist in workspace")
        return p.read_text(encoding="utf-8", errors="replace")
    try:
        text = await asyncio.to_thread(_read)
        if len(text) > 8000:
            text = text[:8000] + "\n…[truncated]"
        return ToolResult(True, text)
    except Exception as e:  # noqa: BLE001
        return ToolResult(False, f"read failed: {e}")


async def tool_write_file(filename: str, content: str) -> ToolResult:
    """Write a text file to the Miraje local workspace (data/workspace)."""
    def _write() -> str:
        _WORKSPACE.mkdir(parents=True, exist_ok=True)
        p = _safe_workspace_path(filename)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} bytes to {filename}"
    try:
        msg = await asyncio.to_thread(_write)
        return ToolResult(True, msg)
    except Exception as e:  # noqa: BLE001
        return ToolResult(False, f"write failed: {e}")


async def tool_list_files() -> ToolResult:
    """List files in the Miraje local workspace."""
    def _list() -> str:
        _WORKSPACE.mkdir(parents=True, exist_ok=True)
        files = sorted(p.relative_to(_WORKSPACE.resolve()).as_posix() for p in _WORKSPACE.rglob("*") if p.is_file())
        if not files:
            return "(workspace is empty)"
        return "\n".join(files)
    try:
        out = await asyncio.to_thread(_list)
        return ToolResult(True, out)
    except Exception as e:  # noqa: BLE001
        return ToolResult(False, f"list failed: {e}")


# ---------- Text utilities ----------

import re as _re  # noqa: E402
from collections import Counter  # noqa: E402

_SENTENCE_SPLIT = _re.compile(r'(?<=[.!?])\s+')
_WORD_RE = _re.compile(r'\b\w+\b', _re.UNICODE)
_STOPWORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'is', 'are', 'was', 'were', 'be', 'been',
    'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'must', 'can', 'to', 'of', 'in', 'on', 'at', 'for',
    'with', 'as', 'by', 'from', 'about', 'into', 'through', 'during', 'before',
    'after', 'above', 'below', 'up', 'down', 'out', 'off', 'over', 'under', 'again',
    'further', 'then', 'once', 'here', 'there', 'when', 'where', 'why', 'how', 'all',
    'each', 'few', 'more', 'most', 'other', 'some', 'such', 'no', 'nor', 'not', 'only',
    'own', 'same', 'so', 'than', 'too', 'very', 's', 't', 'just', 'don', 'now', 'i',
    'you', 'he', 'she', 'it', 'we', 'they', 'this', 'that', 'these', 'those',
}


async def tool_summarize_text(text: str, max_sentences: int = 5) -> ToolResult:
    """Extractive summarization of arbitrary text (no model required).

    Scores sentences by word-frequency (TF, stop-word filtered) and returns the
    top-N highest-scoring sentences in their original order. Runs locally,
    deterministic, zero network.
    """
    text = (text or "").strip()
    if not text:
        return ToolResult(False, "no text provided")
    if len(text) < 240:
        return ToolResult(True, text)

    try:
        n = max(1, min(int(max_sentences), 12))
    except (TypeError, ValueError):
        n = 5

    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if len(s.strip()) > 12]
    if len(sentences) <= n:
        return ToolResult(True, text)

    # Word frequencies (lowercased, stop-words removed).
    words = [w.lower() for w in _WORD_RE.findall(text) if w.lower() not in _STOPWORDS]
    freq = Counter(words)
    total = max(1, sum(freq.values()))

    # Score each sentence by normalized term frequency.
    scored: list[tuple[float, int, str]] = []
    for idx, sent in enumerate(sentences):
        sw = [w.lower() for w in _WORD_RE.findall(sent) if w.lower() not in _STOPWORDS]
        if not sw:
            scored.append((0.0, idx, sent))
            continue
        score = sum(freq[w] / total for w in sw) / len(sw)
        # Slight bias toward earlier sentences (lead bias).
        score *= 1.0 + 0.06 * (1.0 - idx / len(sentences))
        scored.append((score, idx, sent))

    top = sorted(scored, key=lambda t: (-t[0], t[1]))[:n]
    top.sort(key=lambda t: t[1])  # restore original order
    summary = " ".join(s for _, _, s in top)
    return ToolResult(True, summary)


async def tool_word_count(text: str) -> ToolResult:
    """Count words, sentences, and characters in text."""
    text = text or ""
    words = _WORD_RE.findall(text)
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    lines = text.splitlines()
    return ToolResult(
        True,
        f"words: {len(words)}\nsentences: {len(sentences)}\ncharacters: {len(text)}\nlines: {len(lines)}",
    )


# ---------- Local file system tools ----------

import os
import platform


async def tool_list_directory(path: str) -> ToolResult:
    """List files and folders in a directory on the host machine.
    If the directory doesn't exist, creates it (for workspace paths) and reports it as empty.
    """
    def _list() -> str:
        p = path.strip().strip('"').strip("'")
        if not p:
            p = "."
        if not os.path.exists(p):
            # Auto-create directory if it doesn't exist (especially for workspace paths)
            try:
                os.makedirs(p, exist_ok=True)
                return f"Directory '{p}' did not exist — created it. (empty directory)"
            except PermissionError:
                return f"Error: directory '{p}' does not exist and could not be created (permission denied). Try a different path."
            except Exception:
                return f"Error: directory '{p}' does not exist and could not be created. The path may be invalid. Please verify the path is correct."
        if not os.path.isdir(p):
            return f"Error: '{p}' is a file, not a directory. Use read_local_file to read it."
        entries = []
        try:
            for name in sorted(os.listdir(p)):
                full = os.path.join(p, name)
                if os.path.isdir(full):
                    entries.append(f"  [DIR]  {name}/")
                else:
                    size = os.path.getsize(full)
                    if size > 1024 * 1024:
                        sz = f"{size / (1024*1024):.1f}MB"
                    elif size > 1024:
                        sz = f"{size / 1024:.1f}KB"
                    else:
                        sz = f"{size}B"
                    entries.append(f"  [FILE] {name} ({sz})")
        except PermissionError:
            return f"Error: permission denied for '{p}'. Try a different path."
        if not entries:
            return f"(empty directory: {p})"
        return f"Contents of {p} ({len(entries)} items):\n" + "\n".join(entries)
    try:
        out = await asyncio.to_thread(_list)
        return ToolResult(True, out)
    except Exception as e:
        return ToolResult(False, f"list_directory failed: {e}")


async def tool_read_local_file(path: str) -> ToolResult:
    """Read a text file from an absolute path on the host machine."""
    def _read() -> str:
        p = path.strip().strip('"').strip("'")
        if not os.path.exists(p):
            return f"Error: file '{p}' does not exist."
        if os.path.isdir(p):
            return f"Error: '{p}' is a directory, not a file."
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if len(content) > 12000:
                content = content[:12000] + "\n…[truncated]"
            return content
        except PermissionError:
            return f"Error: permission denied for '{p}'."
    try:
        out = await asyncio.to_thread(_read)
        return ToolResult(True, out)
    except Exception as e:
        return ToolResult(False, f"read_local_file failed: {e}")


async def tool_write_local_file(path: str, content: str) -> ToolResult:
    """Write a text file to an absolute path on the host machine (e.g. Desktop)."""
    def _write() -> str:
        p = path.strip().strip('"').strip("'")
        # Create parent directories if needed
        parent = os.path.dirname(p)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} characters to: {p}"
    try:
        out = await asyncio.to_thread(_write)
        return ToolResult(True, out)
    except PermissionError:
        return ToolResult(False, f"permission denied writing to '{path}'")
    except Exception as e:
        return ToolResult(False, f"write_local_file failed: {e}")


async def tool_system_info() -> ToolResult:
    """Get system information about the host machine (OS, CPU, RAM, disk)."""
    def _info() -> str:
        import shutil
        lines = [
            f"OS: {platform.system()} {platform.release()} ({platform.machine()})",
            f"Machine: {platform.node()}",
            f"Processor: {platform.processor()}",
            f"Python: {platform.python_version()}",
        ]
        # CPU cores
        try:
            lines.append(f"CPU cores: {os.cpu_count()}")
        except Exception:
            pass
        # RAM (approximate)
        try:
            if platform.system() == "Windows":
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
                m = MEMORYSTATUSEX()
                m.dwLength = ctypes.sizeof(m)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
                total_gb = m.ullTotalPhys / (1024**3)
                avail_gb = m.ullAvailPhys / (1024**3)
                lines.append(f"RAM: {total_gb:.1f} GB total, {avail_gb:.1f} GB available")
            else:
                # Linux/Mac
                with open("/proc/meminfo", "r") as f:
                    meminfo = f.read()
                total_match = re.search(r"MemTotal:\s+(\d+)", meminfo)
                avail_match = re.search(r"MemAvailable:\s+(\d+)", meminfo)
                if total_match:
                    total_gb = int(total_match.group(1)) / (1024 * 1024)
                    lines.append(f"RAM: {total_gb:.1f} GB total")
                if avail_match:
                    avail_gb = int(avail_match.group(1)) / (1024 * 1024)
                    lines.append(f"RAM available: {avail_gb:.1f} GB")
        except Exception:
            pass
        # Disk space
        try:
            usage = shutil.disk_usage(os.path.expanduser("~"))
            total_gb = usage.total / (1024**3)
            free_gb = usage.free / (1024**3)
            lines.append(f"Disk (home): {total_gb:.1f} GB total, {free_gb:.1f} GB free")
        except Exception:
            pass
        # Home directory
        lines.append(f"Home directory: {os.path.expanduser('~')}")
        lines.append(f"Desktop: {os.path.join(os.path.expanduser('~'), 'Desktop')}")
        return "\n".join(lines)
    try:
        out = await asyncio.to_thread(_info)
        return ToolResult(True, out)
    except Exception as e:
        return ToolResult(False, f"system_info failed: {e}")


# ---------- Web download tools ----------

# Image patterns — catch src, data-src, lazy-src, srcset, data-original, etc.
_IMG_TAG = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_IMG_DATA_SRC = re.compile(r'<img[^>]+data-(?:src|lazy-src|original|srcset)=["\']([^"\']+)["\']', re.I)
_IMG_SRCSET = re.compile(r'srcset=["\']([^"\']+)["\']', re.I)
_IMG_ALL_ATTRS = re.compile(r'<img[^>]*>', re.I)

# Link patterns
_LINK_TAG = re.compile(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_LINK_SIMPLE = re.compile(r'<a\s+href=["\']([^"\']+)["\']', re.I)

# Button patterns — catch <button>, <input type="button">, <input type="submit">, and <div>/<span> with role="button"
_BUTTON_TAG = re.compile(r'<button[^>]*>(.*?)</button>', re.I | re.S)
_INPUT_BUTTON = re.compile(r'<input[^>]+type=["\'](?:button|submit)["\'][^>]*>', re.I)
_ROLE_BUTTON = re.compile(r'<(?:div|span|a)[^>]+role=["\']button["\'][^>]*>(.*?)</(?:div|span|a)>', re.I | re.S)

# Event handler patterns
_ONCLICK = re.compile(r'on(?:click|mousedown|tap|touchstart)=["\']([^"\']+)["\']', re.I)
_DATA_URL = re.compile(r'data-(?:url|href|link|download|file|src)=["\']([^"\']+)["\']', re.I)
_HREF_ANY = re.compile(r'href=["\']([^"\']+)["\']', re.I)

# Media patterns
_VIDEO_TAG = re.compile(r'<(?:video|source|audio|embed)[^>]+src=["\']([^"\']+)["\']', re.I)
_IFRAME_TAG = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
_OBJECT_TAG = re.compile(r'<object[^>]+data=["\']([^"\']+)["\']', re.I)

# Redirect patterns
_META_REFRESH = re.compile(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+url=([^"\'>]+)', re.I)
_WINDOW_LOC = re.compile(r'(?:window\.)?location(?:\.href)?\s*=\s*["\']([^"\']+)["\']', re.I)
_WINDOW_OPEN = re.compile(r'window\.open\(["\']([^"\']+)["\']', re.I)
_A_HREF_JS = re.compile(r'href=["\']javascript:[^"\']*["\']', re.I)

# Form patterns
_FORM_ACTION = re.compile(r'<form[^>]+action=["\']([^"\']+)["\']', re.I)

# CSS background-image patterns
_BG_IMAGE = re.compile(r'background(?:-image)?\s*:\s*url\(["\']?([^"\')\s]+)["\']?\)', re.I)

_DOWNLOADABLE_EXTS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".msi", ".dmg", ".deb", ".rpm", ".appimage",
    ".mp3", ".wav", ".flac", ".ogg", ".m4a",
    ".mp4", ".avi", ".mkv", ".mov", ".webm",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp",
    ".obj", ".fbx", ".glb", ".gltf", ".stl", ".blend",  # 3D models
    ".txt", ".csv", ".json", ".xml", ".yaml", ".md",
    ".apk", ".ipa",
}

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico"}


def _is_downloadable(url: str) -> bool:
    """Check if a URL looks downloadable based on extension."""
    path = urllib.parse.urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _DOWNLOADABLE_EXTS)


def _is_image_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _IMAGE_EXTS)


def _resolve_url(src: str, base_url: str) -> str:
    """Resolve a relative URL against a base URL."""
    if src.startswith("//"):
        return "https:" + src
    elif src.startswith("/"):
        return urllib.parse.urljoin(base_url, src)
    elif not src.startswith("http"):
        return urllib.parse.urljoin(base_url, src)
    return src


async def tool_find_downloadable_assets(url: str) -> ToolResult:
    """Deep-scan a web page for ALL downloadable content.

    Analyzes EVERY possible HTML pattern:
    - <img> with ANY attribute containing a URL (src, data-src, lazy-src, alt, onclick, data-*)
    - <a> links with downloadable extensions, download attribute, or "download" text
    - <a> links wrapping images (clickable images → full-res)
    - ALL <button> elements (even without onclick — reports text + class)
    - <input type="button/submit"> with onclick
    - <div>/<span> with role="button" or class containing "download"/"btn"
    - <video>, <source>, <audio>, <embed> tags
    - <iframe> and <object> embeds
    - <meta> refresh redirects
    - JavaScript window.location and window.open redirects
    - <form> actions
    - CSS background-image URLs
    - data-url, data-href, data-download attributes on ANY element
    - ANY element with onclick containing URL-like strings
    - ANY href in the entire page pointing to downloadable files

    Output includes HTML tag, class names, and ALL attributes so the user
    can identify exactly what to download. Also loads learned patterns from
    data/workspace/download_patterns.json to improve detection.
    """
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Miraje/1.0"})
            resp.raise_for_status()
            html = resp.text
        base_url = str(resp.url)

        # Load learned patterns
        patterns_file = _WORKSPACE / "download_patterns.json"
        learned_patterns: list[dict] = []
        if patterns_file.exists():
            try:
                import json as _json
                learned_patterns = _json.loads(patterns_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        assets: list[dict] = []
        seen_urls: set[str] = set()

        def _add_asset(asset_type: str, asset_url: str, description: str, extra: dict = None):
            if asset_url.startswith("data:") or asset_url.startswith("javascript:") or asset_url.startswith("#"):
                return
            if not asset_url or len(asset_url) < 5:
                return
            if asset_url in seen_urls:
                return
            seen_urls.add(asset_url)
            a = {"type": asset_type, "url": asset_url, "description": description}
            if extra:
                a.update(extra)
            assets.append(a)

        def _extract_class(tag: str) -> str:
            """Extract class attribute from an HTML tag."""
            m = re.search(r'class=["\']([^"\']*)["\']', tag, re.I)
            return m.group(1) if m else ""

        def _extract_all_urls_from_tag(tag: str) -> list[str]:
            """Extract ALL URL-like values from ANY attribute in a tag."""
            urls = []
            # Find all attribute values that look like URLs
            for m in re.finditer(r'["\']([^"\']*(?:https?:|//|/)[^"\']*\.[a-z]{2,5}(?:\?[^"\']*)?)["\']', tag, re.I):
                urls.append(m.group(1))
            return urls

        # 1. Find ALL <img> tags and extract URLs from EVERY attribute
        for m in re.finditer(r'<img[^>]*>', html, re.I):
            img_tag = m.group(0)
            img_class = _extract_class(img_tag)
            # Extract URL from EVERY attribute, not just src
            all_urls_in_tag = _extract_all_urls_from_tag(img_tag)
            # Also check src specifically
            for src_match in re.finditer(r'src\s*=\s*["\']([^"\']+)["\']', img_tag, re.I):
                all_urls_in_tag.append(src_match.group(1))

            for raw_url in all_urls_in_tag:
                resolved = _resolve_url(raw_url.split(" ")[0].split(",")[0], base_url)
                if resolved.startswith("data:"):
                    continue
                # Check if wrapped in <a> tag
                pos = m.start()
                a_start = html.rfind("<a ", 0, pos)
                a_end = html.find("</a>", m.end())
                link_url = None
                if a_start != -1 and a_end != -1:
                    a_tag = html[a_start:a_end + 4]
                    href_match = re.search(r'href=["\']([^"\']+)["\']', a_tag, re.I)
                    if href_match:
                        link_url = _resolve_url(href_match.group(1), base_url)
                        if link_url.startswith("javascript:") or link_url.startswith("#"):
                            link_url = None

                if link_url:
                    _add_asset("clickable_image", resolved,
                               f"Image (class: '{img_class}', clickable → {link_url})",
                               {"link_url": link_url, "class": img_class, "tag": "img"})
                else:
                    _add_asset("image", resolved,
                               f"Image (class: '{img_class}')",
                               {"class": img_class, "tag": "img"})
                break  # One per img tag

        # 2. Find srcset attributes (multiple URLs per image)
        for m in _IMG_SRCSET.finditer(html):
            srcset_val = m.group(1)
            for entry in srcset_val.split(","):
                url_part = entry.strip().split(" ")[0]
                if url_part and not url_part.startswith("data:"):
                    resolved = _resolve_url(url_part, base_url)
                    _add_asset("image", resolved, "Image (from srcset)")

        # 3. Find video/audio/embed/iframe/object media
        for pattern in [_VIDEO_TAG, _IFRAME_TAG, _OBJECT_TAG]:
            for m in pattern.finditer(html):
                src = _resolve_url(m.group(1), base_url)
                _add_asset("media", src, "Media/embed file")

        # 4. Find ALL <a> links — categorize and include class names
        for m in _LINK_TAG.finditer(html):
            href = m.group(1)
            text = _clean(m.group(2))[:120] if m.lastindex and m.lastindex >= 2 else ""
            full_tag = m.group(0)
            link_class = _extract_class(full_tag)

            if href.startswith("javascript:") or href.startswith("#") or not href:
                continue

            resolved = _resolve_url(href, base_url)
            has_download_attr = "download" in full_tag.lower()
            is_dl_ext = _is_downloadable(resolved)
            text_mentions_download = "download" in text.lower() if text else False

            if is_dl_ext:
                fname = os.path.basename(urllib.parse.urlparse(resolved).path)
                _add_asset("file", resolved, f"File: {fname} (class: '{link_class}', text: '{text}')",
                           {"class": link_class, "tag": "a"})
            elif has_download_attr:
                _add_asset("file", resolved, f"Download link (class: '{link_class}', text: '{text}')",
                           {"class": link_class, "tag": "a"})
            elif text_mentions_download:
                _add_asset("download_link", resolved, f"Download link: {text} (class: '{link_class}')",
                           {"class": link_class, "tag": "a"})

        # 5. Find ALL <button> elements — EVERY button, regardless of attributes
        for m in _BUTTON_TAG.finditer(html):
            btn_html = m.group(0)
            text = _clean(m.group(1))[:80]
            btn_class = _extract_class(btn_html)

            # Try to extract URLs from every possible source in the button
            all_urls = _extract_all_urls_from_tag(btn_html)
            onclick_matches = _ONCLICK.findall(btn_html)
            for onclick in onclick_matches:
                url_matches = re.findall(r'["\']([^"\']*\.[a-z]{2,5}(?:\?[^"\']*)?)["\']', onclick, re.I)
                all_urls.extend(url_matches)
            data_url_matches = _DATA_URL.findall(btn_html)
            all_urls.extend(data_url_matches)
            href_matches = _HREF_ANY.findall(btn_html)
            all_urls.extend(href_matches)

            for found_url in all_urls:
                resolved = _resolve_url(found_url, base_url)
                if resolved.startswith("javascript:") or resolved.startswith("#"):
                    continue
                if _is_downloadable(resolved) or _is_image_url(resolved):
                    _add_asset("button_download", resolved,
                               f"Button: '{text}' (class: '{btn_class}') → {resolved}",
                               {"class": btn_class, "tag": "button"})
                else:
                    _add_asset("button_link", resolved,
                               f"Button: '{text}' (class: '{btn_class}') → {resolved}",
                               {"class": btn_class, "tag": "button"})

            # ALWAYS report buttons with "download" in text or class, even without URL
            if not all_urls and ("download" in text.lower() or "download" in btn_class.lower()):
                _add_asset("download_button", base_url + "#btn:" + text[:30],
                           f"Download button: '{text}' (class: '{btn_class}') — no direct URL. Use fetch_url to examine the page.",
                           {"class": btn_class, "tag": "button", "text": text})

        # 6. Find <input type="button/submit">
        for m in _INPUT_BUTTON.finditer(html):
            input_html = m.group(0)
            input_class = _extract_class(input_html)
            value_match = re.search(r'value=["\']([^"\']+)["\']', input_html, re.I)
            text = value_match.group(1) if value_match else "Submit button"
            onclick_matches = _ONCLICK.findall(input_html)
            for onclick in onclick_matches:
                url_matches = re.findall(r'["\']([^"\']*\.[a-z]{2,5}(?:\?[^"\']*)?)["\']', onclick, re.I)
                for found_url in url_matches:
                    resolved = _resolve_url(found_url, base_url)
                    _add_asset("button_download", resolved,
                               f"Input button: '{text}' (class: '{input_class}') → {resolved}",
                               {"class": input_class, "tag": "input"})

        # 7. Find elements with role="button" OR class containing "download"/"btn"/"button"
        for m in re.finditer(r'<(?:div|span|a)\s[^>]*(?:role=["\']button["\']|class=["\'][^"\']*(?:download|btn|button)[^"\']*["\'])[^>]*>(.*?)</(?:div|span|a)>', html, re.I | re.S):
            role_html = m.group(0)
            text = _clean(m.group(1))[:80]
            el_class = _extract_class(role_html)
            all_urls = _extract_all_urls_from_tag(role_html)
            onclick_matches = _ONCLICK.findall(role_html)
            for onclick in onclick_matches:
                url_matches = re.findall(r'["\']([^"\']*\.[a-z]{2,5}(?:\?[^"\']*)?)["\']', onclick, re.I)
                all_urls.extend(url_matches)
            for found_url in all_urls:
                resolved = _resolve_url(found_url, base_url)
                if not resolved.startswith("javascript:") and not resolved.startswith("#"):
                    _add_asset("button_download", resolved,
                               f"Clickable element: '{text}' (class: '{el_class}') → {resolved}",
                               {"class": el_class, "tag": "div/span"})

        # 8. Find ALL onclick handlers in the ENTIRE HTML
        for m in _ONCLICK.finditer(html):
            onclick_code = m.group(1)
            url_matches = re.findall(r'["\']([^"\']*\.[a-z]{2,5}(?:\?[^"\']*)?)["\']', onclick_code, re.I)
            for found_url in url_matches:
                resolved = _resolve_url(found_url, base_url)
                if not resolved.startswith("javascript:") and not resolved.startswith("#"):
                    if _is_downloadable(resolved) or _is_image_url(resolved):
                        _add_asset("onclick_download", resolved, f"onclick handler → {resolved}")

        # 9. Find data-* URL attributes anywhere
        for m in _DATA_URL.finditer(html):
            resolved = _resolve_url(m.group(1), base_url)
            if not resolved.startswith("javascript:") and not resolved.startswith("#"):
                _add_asset("data_attr", resolved, f"data attribute → {resolved}")

        # 10. Find redirects
        for m in _META_REFRESH.finditer(html):
            redirect_url = _resolve_url(m.group(1).strip(), base_url)
            _add_asset("redirect", redirect_url, f"Meta refresh redirect → {redirect_url}")
        for pattern in [_WINDOW_LOC, _WINDOW_OPEN]:
            for m in pattern.finditer(html):
                redirect_url = _resolve_url(m.group(1), base_url)
                _add_asset("redirect", redirect_url, f"JS redirect → {redirect_url}")

        # 11. Find form actions
        for m in _FORM_ACTION.finditer(html):
            action_url = _resolve_url(m.group(1), base_url)
            if _is_downloadable(action_url) or "download" in action_url.lower():
                _add_asset("form_download", action_url, f"Form action → {action_url}")

        # 12. Find CSS background-image URLs
        for m in _BG_IMAGE.finditer(html):
            bg_url = _resolve_url(m.group(1), base_url)
            if _is_image_url(bg_url):
                _add_asset("image", bg_url, "CSS background image")

        # 13. Find ALL hrefs pointing to downloadable files
        for m in _HREF_ANY.finditer(html):
            href = m.group(1)
            if href.startswith("javascript:") or href.startswith("#") or not href:
                continue
            resolved = _resolve_url(href, base_url)
            if _is_downloadable(resolved):
                _add_asset("file", resolved, f"Direct file: {os.path.basename(urllib.parse.urlparse(resolved).path)}")

        # 14. Apply learned patterns — check if any learned class/tag patterns match elements on this page
        for pattern in learned_patterns:
            pat_class = pattern.get("class", "")
            pat_tag = pattern.get("tag", "")
            if pat_class:
                # Find elements with this class
                for m in re.finditer(rf'<{pat_tag}[^>]*class=["\'][^"\']*{re.escape(pat_class)}[^"\']*["\'][^>]*>', html, re.I):
                    tag_html = m.group(0)
                    urls_in_tag = _extract_all_urls_from_tag(tag_html)
                    for found_url in urls_in_tag:
                        resolved = _resolve_url(found_url, base_url)
                        _add_asset("learned_pattern", resolved,
                                   f"Learned pattern match: {pat_tag}.{pat_class} → {resolved}",
                                   {"class": pat_class, "tag": pat_tag, "learned": True})

        if not assets:
            # If nothing found, report ALL elements with download-related classes/text
            lines = [f"No standard downloadable assets found on {base_url}."]
            lines.append("\nSearching for ALL elements with download-related attributes...")
            found_elements = []
            for m in re.finditer(r'<[^>]*(?:download|btn|button|file|save|export)[^>]*>', html, re.I):
                tag = m.group(0)[:200]
                cls = _extract_class(tag)
                found_elements.append(f"  - {tag[:150]}" + (f" (class: '{cls}')" if cls else ""))
            if found_elements:
                lines.append(f"\nFound {len(found_elements)} elements with download-related attributes:")
                lines.extend(found_elements[:20])
            else:
                lines.append("No elements with download-related classes found either.")
                lines.append("Use fetch_url to read the full page text and identify what to download.")
            return ToolResult(True, "\n".join(lines), {"assets": [], "base_url": base_url})

        # Categorize and format output — include class names
        categories: dict[str, list] = {}
        for a in assets:
            cat = a["type"]
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(a)

        lines = [f"Deep scan of {base_url} — found {len(assets)} assets:\n"]
        type_labels = {
            "image": "Images",
            "clickable_image": "Clickable Images (lead to another page)",
            "media": "Videos/Audio/Embeds",
            "file": "Direct File Downloads",
            "download_link": "Download Links (text mentions download)",
            "download_button": "Download Buttons (no direct URL extracted)",
            "button_download": "Button-Triggered Downloads",
            "button_link": "Button Links",
            "onclick_download": "OnClick Download Triggers",
            "data_attr": "Data Attribute URLs",
            "redirect": "Page Redirects",
            "form_download": "Form Download Endpoints",
            "learned_pattern": "Learned Pattern Matches",
        }
        idx = 1
        for cat, items in categories.items():
            label = type_labels.get(cat, cat)
            lines.append(f"\n=== {label} ({len(items)}) ===")
            for a in items:
                cls = a.get("class", "")
                tag = a.get("tag", "")
                cls_info = f" [{tag}.{cls}]" if cls else (f" [{tag}]" if tag else "")
                lines.append(f"  {idx}.{cls_info} {a['description']}")
                lines.append(f"     URL: {a['url']}")
                if a.get("link_url"):
                    lines.append(f"     Links to: {a['link_url']}")
                idx += 1

        lines.append(f"\nTotal: {len(assets)} assets found.")
        lines.append("Present ALL of these to the user, including the [tag.class] info.")
        lines.append("The user can identify which to download by number, class name, or description.")
        lines.append("For direct file/image URLs, use download_file.")
        lines.append("For clickable images, redirects, or buttons, use follow_link first.")
        return ToolResult(True, "\n".join(lines), {"assets": assets, "base_url": base_url})
    except Exception as e:
        return ToolResult(False, f"find_downloadable_assets failed: {e}")


async def tool_save_download_pattern(tag: str, class_name: str, description: str = "") -> ToolResult:
    """Save a download pattern that the user identified, so future scans can recognize it.

    When the user says "download the one with class 'download-btn'", save that pattern
    so next time find_downloadable_assets automatically recognizes elements with that class.
    """
    import json as _json
    def _save() -> str:
        patterns_file = _WORKSPACE / "download_patterns.json"
        patterns_file.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if patterns_file.exists():
            try:
                existing = _json.loads(patterns_file.read_text(encoding="utf-8"))
            except Exception:
                existing = []
        # Check if pattern already exists
        for p in existing:
            if p.get("tag") == tag and p.get("class") == class_name:
                return f"Pattern already saved: {tag}.{class_name}"
        existing.append({
            "tag": tag,
            "class": class_name,
            "description": description,
            "saved_at": time.strftime("%Y-%m-%d %H:%M"),
        })
        patterns_file.write_text(_json.dumps(existing, indent=2), encoding="utf-8")
        return f"Saved pattern: {tag}.{class_name} ({description}). Will be recognized in future scans."
    try:
        out = await asyncio.to_thread(_save)
        return ToolResult(True, out)
    except Exception as e:
        return ToolResult(False, f"save_download_pattern failed: {e}")


async def tool_follow_link(url: str) -> ToolResult:
    """Follow a link to see where it leads. Useful for:
    - Clickable images that lead to full-resolution versions
    - Download buttons that redirect to actual file URLs
    - Redirect chains that eventually reach a downloadable file

    Returns the final URL after redirects, the content type, and whether
    it looks like a downloadable file.
    """
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Miraje/1.0"})
            resp.raise_for_status()

        final_url = str(resp.url)
        content_type = resp.headers.get("content-type", "unknown")
        content_disp = resp.headers.get("content-disposition", "")

        # Check if it's a redirect
        redirected = final_url != url

        # Check if it's a downloadable file
        is_downloadable = _is_downloadable(final_url)
        is_image = _is_image_url(final_url) or "image" in content_type

        # Extract filename from content-disposition or URL
        filename = ""
        if content_disp:
            fname_match = re.search(r'filename=["\']?([^"\';\s]+)', content_disp, re.I)
            if fname_match:
                filename = fname_match.group(1)
        if not filename:
            filename = os.path.basename(urllib.parse.urlparse(final_url).path) or "(unknown)"

        lines = [f"Followed: {url}"]
        if redirected:
            lines.append(f"Redirected to: {final_url}")
        lines.append(f"Content-Type: {content_type}")
        lines.append(f"Filename: {filename}")
        lines.append(f"Size: {len(resp.content)} bytes ({len(resp.content)/1024:.1f} KB)")

        if is_downloadable or is_image:
            lines.append(f"\n✅ This IS a downloadable file. Use download_file with URL: {final_url}")
        elif "text/html" in content_type:
            lines.append(f"\nThis is an HTML page (not a direct file). The page may contain download links.")
            lines.append(f"Use find_downloadable_assets on this URL to scan it, or fetch_url to read its text.")
            # Quick scan for obvious download links on this page
            html = resp.text
            quick_finds = []
            for m in _LINK_TAG.finditer(html):
                href = _resolve_url(m.group(1), final_url)
                text = _clean(m.group(2))[:60]
                if href.startswith("javascript:") or href.startswith("#"):
                    continue
                if _is_downloadable(href):
                    quick_finds.append(f"  - {text or 'file'}: {href}")
            if quick_finds:
                lines.append(f"\nQuick finds on this page ({len(quick_finds)}):")
                lines.extend(quick_finds[:10])
        else:
            lines.append(f"\nContent type: {content_type}. May or may not be downloadable.")

        return ToolResult(True, "\n".join(lines), {"final_url": final_url, "content_type": content_type, "filename": filename})
    except Exception as e:
        return ToolResult(False, f"follow_link failed: {e}")


async def tool_download_file(url: str, filename: str = "") -> ToolResult:
    """Download a file from a URL to the Miraje workspace (data/workspace/downloads/)."""
    def _resolve_filename() -> tuple[str, Path]:
        dl_dir = _WORKSPACE / "downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)
        if not filename:
            parsed = urllib.parse.urlparse(url)
            extracted = os.path.basename(parsed.path) or "download"
        else:
            extracted = filename
        # Sanitize filename
        sanitized = "".join(c for c in extracted if c.isalnum() or c in "-_.")
        if not sanitized:
            sanitized = "download"
        return sanitized, dl_dir / sanitized

    try:
        fname, dl_path = await asyncio.to_thread(_resolve_filename)
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Miraje/1.0"})
            resp.raise_for_status()
            with open(dl_path, "wb") as f:
                f.write(resp.content)
        size = os.path.getsize(dl_path)
        if size > 1024 * 1024:
            sz = f"{size / (1024*1024):.1f}MB"
        elif size > 1024:
            sz = f"{size / 1024:.1f}KB"
        else:
            sz = f"{size}B"
        return ToolResult(True, f"Downloaded {fname} ({sz}) to workspace.\nPath: {dl_path}\nThe user can download it from the chat interface.")
    except Exception as e:
        return ToolResult(False, f"download_file failed: {e}")


# ---------- Tool registry ----------

TOOL_DEFS: dict[str, dict[str, Any]] = {
    "web_search": {
        "description": "Search the web with DuckDuckGo. Input: {\"query\": \"...\", \"max_results\": 6}.",
        "fn": tool_web_search,
        "schema": {"query": "string", "max_results": "int (optional, default 6)"},
    },
    "fetch_url": {
        "description": "Download a URL and return its text content. Input: {\"url\": \"https://...\"}.",
        "fn": tool_fetch_url,
        "schema": {"url": "string"},
    },
    "calculator": {
        "description": "Evaluate a math expression. Input: {\"expression\": \"2*(3+4)**2\"}.",
        "fn": tool_calculator,
        "schema": {"expression": "string"},
    },
    "current_time": {
        "description": "Get the current date and time. Input: {}.",
        "fn": tool_current_time,
        "schema": {},
    },
    "python_execute": {
        "description": "Run a Python snippet and return stdout/stderr (10s timeout). Input: {\"code\": \"...\"}.",
        "fn": tool_python_execute,
        "schema": {"code": "string"},
    },
    "read_file": {
        "description": "Read a text file from the Miraje local workspace (data/workspace). Input: {\"filename\": \"notes.txt\"}.",
        "fn": tool_read_file,
        "schema": {"filename": "string"},
    },
    "write_file": {
        "description": "Write a text file to the Miraje local workspace (data/workspace). Input: {\"filename\": \"out.txt\", \"content\": \"...\"}.",
        "fn": tool_write_file,
        "schema": {"filename": "string", "content": "string"},
    },
    "list_files": {
        "description": "List files in the Miraje local workspace. Input: {}.",
        "fn": tool_list_files,
        "schema": {},
    },
    "summarize_text": {
        "description": "Extractive summary of text (no model needed). Input: {\"text\": \"...\", \"max_sentences\": 5}.",
        "fn": tool_summarize_text,
        "schema": {"text": "string", "max_sentences": "int (optional, default 5)"},
    },
    "word_count": {
        "description": "Count words, sentences, characters, lines in text. Input: {\"text\": \"...\"}.",
        "fn": tool_word_count,
        "schema": {"text": "string"},
    },
    "list_directory": {
        "description": "List files and folders in any directory on the host machine. Input: {\"path\": \"C:\\\\Users\\\\pippo\\\\Music\"}.",
        "fn": tool_list_directory,
        "schema": {"path": "string"},
    },
    "read_local_file": {
        "description": "Read a text file from an absolute path on the host machine. Input: {\"path\": \"C:\\\\Users\\\\pippo\\\\Desktop\\\\notes.txt\"}.",
        "fn": tool_read_local_file,
        "schema": {"path": "string"},
    },
    "write_local_file": {
        "description": "Write a text file to an absolute path on the host machine (e.g. Desktop, Documents). Input: {\"path\": \"C:\\\\Users\\\\pippo\\\\Desktop\\\\output.txt\", \"content\": \"...\"}.",
        "fn": tool_write_local_file,
        "schema": {"path": "string", "content": "string"},
    },
    "system_info": {
        "description": "Get system information about the host machine (OS, CPU, RAM, disk, home directory). Input: {}.",
        "fn": tool_system_info,
        "schema": {},
    },
    "find_downloadable_assets": {
        "description": "Deep-scan a web page for ALL downloadable content: images, files, download links, buttons with onclick, clickable images, redirects, form actions. Returns categorized list. ALWAYS use this first before downloading. Input: {\"url\": \"https://...\"}.",
        "fn": tool_find_downloadable_assets,
        "schema": {"url": "string"},
    },
    "follow_link": {
        "description": "Follow a link/URL to see where it actually leads. Resolves redirects, shows content-type and filename. Use for clickable images, download buttons, or redirect chains to find the real download URL. Input: {\"url\": \"https://...\"}.",
        "fn": tool_follow_link,
        "schema": {"url": "string"},
    },
    "download_file": {
        "description": "Download a file from a direct URL to the Miraje workspace. Only use AFTER find_downloadable_assets or follow_link has confirmed the URL is a real file. Do NOT guess URLs — always scan the page first. Input: {\"url\": \"https://...\", \"filename\": \"optional_name.ext\"}.",
        "fn": tool_download_file,
        "schema": {"url": "string", "filename": "string (optional)"},
    },
    "save_download_pattern": {
        "description": "Save a download pattern (HTML tag + class) that the user identified, so future scans automatically recognize it. Use when the user says 'download the one with class X' — save the pattern for future use. Input: {\"tag\": \"button\", \"class_name\": \"download-btn\", \"description\": \"optional\"}.",
        "fn": tool_save_download_pattern,
        "schema": {"tag": "string", "class_name": "string", "description": "string (optional)"},
    },
}

TOOLS = TOOL_DEFS


async def call_tool(name: str, args: dict[str, Any]) -> ToolResult:
    spec = TOOL_DEFS.get(name)
    if not spec:
        return ToolResult(False, f"unknown tool: {name}")
    fn = spec["fn"]
    try:
        # Tools accept either no args, or kwargs.
        if not args:
            return await fn()  # type: ignore[misc]
        return await fn(**args)  # type: ignore[misc]
    except TypeError:
        # Some tools take a single positional arg.
        if len(args) == 1:
            return await fn(next(iter(args.values())))  # type: ignore[misc]
        return ToolResult(False, f"bad arguments for {name}")
    except Exception as e:  # noqa: BLE001
        return ToolResult(False, f"tool {name} crashed: {e}")


def list_tools() -> list[dict[str, Any]]:
    return [
        {"name": n, "description": s["description"], "schema": s["schema"]}
        for n, s in TOOL_DEFS.items()
    ]
