#!/usr/bin/env python3
"""Cross-platform local Web UI agent backed by Ollama (Windows, macOS, Linux)."""

import errno
import html as html_lib
import html.parser
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_FROZEN = bool(getattr(sys, "frozen", False))

SHELL_TOOL_NAME = "run_shell" if IS_WINDOWS else "run_bash"

DEFAULT_SOUL = """You are Gipsy, a concise, direct, slightly witty assistant.

- Answer directly in text by default without calling tools.
- If the user asks what a command is, how something works, or asks about another device, just reply with the answer—do NOT run tools on this PC.
- Only use enabled tools when the user turns them on, attaches a file, or explicitly asks for web lookup or a local action on THIS PC.
- Never invent tool outputs; if you run a tool, report only what it returned.
- Ask before running destructive commands (`rm -rf`, formatting, killing critical processes).
- Keep replies short and readable.
"""


def resolve_data_dir() -> Path:
    env_dir = os.environ.get("AGENT_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    if IS_FROZEN:
        return Path.home() / ".local-agent"
    return Path(__file__).resolve().parent.parent


DATA_DIR = resolve_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def init_stdio():
    """Prevent crashes in windowed/noconsole packaged builds where std handles are None."""
    if sys.stdin is None:
        try:
            sys.stdin = open(os.devnull, "r", encoding="utf-8")
        except OSError:
            pass
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_f = open(DATA_DIR / "agent.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        log_f = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = log_f
    if sys.stderr is None:
        sys.stderr = log_f


init_stdio()

SOUL_FILE = DATA_DIR / "soul.md"
SOULS_FILE = DATA_DIR / "souls.json"
MAX_SOUL_SLOTS = 4
CHATS_DIR = DATA_DIR / "chats"
UPLOADS_DIR = DATA_DIR / "uploads"
CHIPS_FILE = DATA_DIR / "chips.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
DEFAULT_CHIPS = [
    {"label": "Check CPU, RAM & disk", "prompt": "Check my CPU, RAM, GPU, and free disk space on this PC"},
    {"label": "Check Downloads", "prompt": "List the largest files in my Downloads folder"},
    {"label": "Explain a command", "prompt": "How do I compress a folder with tar and gzip?"},
]
MAX_STEPS = 8
MIN_TOOL_STEPS = 2
MAX_TOOL_STEPS_LIMIT = 40
DEFAULT_TTS_MODEL = "lfm3-audio:2b"
DEFAULT_SETTINGS = {
    "default_model": "",
    "sound": "chime",
    "max_tool_steps": MAX_STEPS,
    "tts_model": DEFAULT_TTS_MODEL,
    "tts_voice": "",
    "tts_auto": False,
}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
VALID_PACKS = ("web", "files", "system")


def seed_soul_if_missing():
    if SOUL_FILE.exists():
        return
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        cand = Path(bundled) / "soul.md"
        if cand.exists():
            SOUL_FILE.write_text(cand.read_text(encoding="utf-8"), encoding="utf-8")
            return
    SOUL_FILE.write_text(DEFAULT_SOUL, encoding="utf-8")


seed_soul_if_missing()


OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_URL = f"{OLLAMA_HOST}/api/chat"
OLLAMA_GENERATE_URL = f"{OLLAMA_HOST}/api/generate"
OLLAMA_TAGS_URL = f"{OLLAMA_HOST}/api/tags"
OLLAMA_PULL_URL = f"{OLLAMA_HOST}/api/pull"
OLLAMA_DELETE_URL = f"{OLLAMA_HOST}/api/delete"
OLLAMA_PS_URL = f"{OLLAMA_HOST}/api/ps"
OLLAMA_SPEECH_URL = f"{OLLAMA_HOST}/v1/audio/speech"
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "lfm3:1.7b")
NUM_CTX = int(os.environ.get("AGENT_CTX", "16384"))
WEB_HOST = "127.0.0.1"
WEB_PORT = int(os.environ.get("AGENT_PORT", "8765"))
MAX_HISTORY = 16
MAX_HISTORY_CHARS = 28000

PRESET_MODELS = [
    "lfm3:1.7b",
    "granite4.2:3b",
    "qwen3.5:4b",
    "gemma4:e2b",
]
KNOWN_MODEL_PARAMS = {
    "lfm3": "1.7B",
    "lfm3:1.7b": "1.7B",
    "granite4.2:3b": "3B",
    "gemma4:e2b": "2B eff",
    "gemma4:e4b": "4B eff",
    "qwen3.5:0.8b": "0.8B",
    "qwen3.5:2b": "2B",
    "qwen3.5:4b": "4B",
    "lfm3-audio:2b": "2B",
    "phonon": "31M",
}
OLLAMA_LOCK = threading.Lock()
SERVER_REF = {"server": None}


class CancelledError(Exception):
    pass


def os_label() -> str:
    if IS_WINDOWS:
        return "Windows"
    if IS_MAC:
        return "macOS"
    return "Linux"


# --- UPLOAD & FORMAT HELPERS ---
def ensure_uploads_dir():
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def safe_upload_name(name: str) -> str:
    raw = Path(str(name or "").replace("\\", "/")).name.strip()
    if not raw or raw in (".", ".."):
        return "file.txt"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(raw).stem).strip("._") or "file"
    suffix = re.sub(r"[^A-Za-z0-9.]+", "", Path(raw).suffix.lower())[:16]
    max_stem = max(8, 110 - len(suffix))
    return stem[:max_stem] + suffix


def _is_binary_file(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return b"\x00" in f.read(4096)
    except Exception:
        return False


def inspect_file_outline(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if _is_binary_file(path):
            return "binary file"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if ext in (".wiki", ".mediawiki"):
            heads = [m.group(2).strip() for ln in lines for m in [re.match(r"^(={1,4})\s*(.+?)\s*\1\s*$", ln)] if m][:6]
            return f"{len(lines)} lines" + (f" · sections: {', '.join(heads)}" if heads else "")
        if ext == ".md":
            heads = [m.group(1).strip() for ln in lines for m in [re.match(r"^#{1,3}\s+(.+)$", ln)] if m][:6]
            return f"{len(lines)} lines" + (f" · headings: {', '.join(heads)}" if heads else "")
        return f"{len(lines)} lines"
    except Exception:
        return ""


def resolve_doc_path(file_arg: str, chat_id: str = "", for_write: bool = False) -> tuple:
    raw = (file_arg or "").strip()
    if not raw:
        raise ValueError("Missing file name")
    if chat_id and valid_chat_id(chat_id):
        safe = safe_upload_name(raw)
        up_dir = UPLOADS_DIR / chat_id
        cand = up_dir / safe
        if cand.exists():
            return cand, True
        if for_write and "/" not in raw and "\\" not in raw:
            up_dir.mkdir(parents=True, exist_ok=True)
            return cand, True
    p = Path(raw).expanduser()
    if p.exists() or for_write:
        return p, False
    if chat_id and valid_chat_id(chat_id):
        return (UPLOADS_DIR / chat_id / safe_upload_name(raw)), True
    return p, False


def read_doc_impl(file_arg: str, range_spec: str = "", chat_id: str = "") -> str:
    path, _ = resolve_doc_path(file_arg, chat_id=chat_id, for_write=False)
    if not path.exists():
        return f"File not found: {file_arg}"
    if _is_binary_file(path):
        return f"'{path.name}' is a binary file. Supported text formats: MediaWiki (.wiki, .mediawiki), Markdown (.md), and TXT (.txt)."
    ext = path.suffix.lower()
    spec = (range_spec or "").strip()

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if spec.lower().startswith("search:"):
        term = spec.split(":", 1)[1].strip().lower()
        hits = []
        for idx, ln in enumerate(lines, 1):
            if term in ln.lower():
                lo = max(1, idx - 1)
                hi = min(len(lines), idx + 1)
                block = "\n".join(f"L{k}: {lines[k - 1]}" for k in range(lo, hi + 1))
                hits.append(block)
                if len(hits) >= 20:
                    break
        return "\n---\n".join(hits)[:5500] if hits else f"No matches for '{term}' in {path.name}"

    if spec.lower().startswith("section:") and ext in (".wiki", ".mediawiki", ".md"):
        sec_q = spec.split(":", 1)[1].strip().lower()
        start = None
        end = len(lines)
        start_lvl = 9
        for idx, ln in enumerate(lines):
            mw = re.match(r"^(={1,5})\s*(.+?)\s*\1\s*$", ln)
            md = re.match(r"^(#{1,5})\s+(.+)$", ln)
            m = mw or md
            if not m:
                continue
            lvl = len(m.group(1))
            title = m.group(2).strip().lower()
            if start is None and sec_q in title:
                start = idx
                start_lvl = lvl
            elif start is not None and lvl <= start_lvl:
                end = idx
                break
        if start is None:
            return f"Section '{sec_q}' not found in {path.name}"
        numbered = [f"L{k + 1}: {lines[k]}" for k in range(start, end)]
        return ("\n".join(numbered))[:5500]

    l1, l2 = 1, min(len(lines), 120)
    m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", spec)
    if m:
        l1 = max(1, int(m.group(1)))
        l2 = min(len(lines), int(m.group(2) or (l1 + 119)))
    numbered = [f"L{k}: {lines[k - 1]}" for k in range(l1, l2 + 1)]
    tail = f"\n... ({len(lines)} total lines; pass range=\"{l2 + 1}-{min(len(lines), l2 + 120)}\" for more)" if l2 < len(lines) else ""
    return (f"{path.name} ({len(lines)} lines):\n" + "\n".join(numbered))[:5500] + tail


def edit_doc_impl(args: dict, chat_id: str = "") -> tuple:
    file_arg = str(args.get("file") or "").strip()
    action = str(args.get("action") or "write").strip().lower()
    content = "" if args.get("content") is None else str(args.get("content"))
    old_text = "" if args.get("old_text") is None else str(args.get("old_text"))
    line_range = str(args.get("line_range") or "").strip()
    output_file = str(args.get("output_file") or "").strip()

    src_path, src_is_upload = resolve_doc_path(file_arg, chat_id=chat_id, for_write=True)
    if src_path.exists() and _is_binary_file(src_path):
        return f"'{src_path.name}' is a binary file and cannot be edited as text.", None
    target_arg = output_file or file_arg
    dst_path, dst_is_upload = resolve_doc_path(target_arg, chat_id=chat_id, for_write=True)

    existing = src_path.read_text(encoding="utf-8", errors="replace") if src_path.exists() else ""
    if action == "write":
        updated = content
    elif action == "append":
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        updated = existing + sep + content
    elif action == "replace_text":
        if not old_text or old_text not in existing:
            return f"Text to replace was not found in {src_path.name}", None
        updated = existing.replace(old_text, content, 1)
    elif action == "replace_lines":
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", line_range)
        if not m:
            return "Invalid line_range; expected 'start-end' like '10-15'", None
        lines = existing.splitlines()
        s_idx = max(1, int(m.group(1))) - 1
        e_idx = min(len(lines), int(m.group(2) or m.group(1)))
        new_lines = content.splitlines()
        lines[s_idx:e_idx] = new_lines
        updated = "\n".join(lines) + ("\n" if existing.endswith("\n") or new_lines else "")
    else:
        return f"Unsupported edit action: {action}", None

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_text(updated, encoding="utf-8")
    msg = f"Saved {dst_path.name} ({len(updated)} chars)"

    meta = None
    if (dst_is_upload or src_is_upload) and chat_id and valid_chat_id(chat_id) and dst_path.exists():
        meta = {
            "name": dst_path.name,
            "size": dst_path.stat().st_size,
            "url": f"/api/files/{urllib.parse.quote(chat_id)}/{urllib.parse.quote(dst_path.name)}",
        }
    return msg, meta


# --- KEYLESS WEB SEARCH & FETCH HELPERS ---
class _HtmlTextExtractor(html.parser.HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}
    META_KEYS = ("og:title", "twitter:title", "description", "og:description")

    def __init__(self):
        super().__init__()
        self._skip = 0
        self._in_title = False
        self._title_chunks = []
        self._meta = {}
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        tl = tag.lower()
        if tl == "title":
            self._in_title = True
        elif tl == "meta":
            ad = {k.lower(): (v or "").strip() for k, v in attrs}
            key = (ad.get("property") or ad.get("name") or "").lower()
            val = ad.get("content") or ""
            if key in self.META_KEYS and val and key not in self._meta:
                self._meta[key] = val
        if tl in self.SKIP_TAGS:
            self._skip += 1
        elif tl in ("p", "br", "li", "div", "h1", "h2", "h3", "h4", "tr"):
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        tl = tag.lower()
        if tl == "title":
            self._in_title = False
        if tl in self.SKIP_TAGS and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self._title_chunks.append(data)
        elif not self._skip:
            self.chunks.append(data)

    def get_text(self) -> str:
        hdr = []
        title = re.sub(r"\s+", " ", html_lib.unescape("".join(self._title_chunks))).strip()
        og_title = re.sub(r"\s+", " ", html_lib.unescape(self._meta.get("og:title") or self._meta.get("twitter:title") or "")).strip()
        desc = re.sub(r"\s+", " ", html_lib.unescape(self._meta.get("og:description") or self._meta.get("description") or "")).strip()
        best_title = og_title or title
        if best_title:
            hdr.append(f"Title: {best_title}")
        if desc:
            hdr.append(f"Description: {desc}")

        raw = html_lib.unescape("".join(self.chunks))
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines()]
        body_lines = [ln for ln in lines if ln]
        return "\n".join(hdr + body_lines)


class _DdgParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._in_title = False
        self._in_snip = False
        self._cur = None

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "")
        href = dict(attrs).get("href", "")
        if tag == "a" and "result__a" in cls:
            real_url = href
            if "uddg=" in href:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                real_url = qs.get("uddg", [href])[0]
            self._cur = {"title": "", "url": real_url, "snippet": ""}
            self._in_title = True
        elif "result__snippet" in cls and self._cur is not None:
            self._in_snip = True

    def handle_endtag(self, tag):
        if tag == "a" and self._in_title:
            self._in_title = False
        elif self._in_snip and tag in ("a", "div", "span"):
            self._in_snip = False
            if self._cur and self._cur["title"]:
                self.results.append(self._cur)
                self._cur = None

    def handle_data(self, data):
        if self._in_title and self._cur is not None:
            self._cur["title"] += data
        elif self._in_snip and self._cur is not None:
            self._cur["snippet"] += data


_WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
_YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")


def clean_tool_url(raw_url: str) -> str:
    """Extract a clean URL from Markdown links, enclosing parentheses, quotes, or brackets."""
    u = str(raw_url or "").replace("\u200b", "").replace("\ufeff", "").strip()
    if not u:
        return ""
    md = re.search(r"\[[^\]]*\]\(([^()\s]+(?:\([^()\s]*\)[^()\s]*)*)\)", u)
    if md:
        u = md.group(1).strip()
    else:
        url_in_text = re.search(r"https?://[^\s<>\"'`]+", u)
        if url_in_text:
            u = url_in_text.group(0)
    for _ in range(3):
        prev = u
        u = u.strip()
        if len(u) >= 2 and u[0] + u[-1] in ("<>", '""', "''", "``", "()", "[]"):
            u = u[1:-1].strip()
        u = u.lstrip("<(\"'`[").rstrip(">\"'`,.;!?")
        while u.endswith(")") and u.count(")") > u.count("("):
            u = u[:-1]
        while u.endswith("]") and u.count("]") > u.count("["):
            u = u[:-1]
        if u == prev:
            break
    if u and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u):
        if re.match(r"^(?:www\.|youtube\.com/|youtu\.be/|m\.youtube\.com/|music\.youtube\.com/|[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/|$))", u, re.I):
            u = "https://" + u
    return u


def _is_youtube_host(netloc: str) -> bool:
    host = (netloc or "").split("@")[-1].split(":")[0].lower()
    return host in _YOUTUBE_HOSTS


def _fetch_youtube_oembed(url: str) -> str:
    oembed_url = "https://www.youtube.com/oembed?" + urllib.parse.urlencode({"url": url, "format": "json"})
    req = urllib.request.Request(oembed_url, headers={"User-Agent": _WEB_UA})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = resp.read(64 * 1024).decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return ""
        parts = []
        if data.get("title"):
            parts.append(f"YouTube Video: {str(data['title']).strip()}")
        if data.get("author_name"):
            parts.append(f"Channel: {str(data['author_name']).strip()}")
        if parts:
            parts.append(f"URL: {url}")
            return "\n".join(parts)
    except Exception:
        pass
    return ""


def web_search_impl(query: str) -> str:
    q = (query or "").strip()
    m_wrapped = re.fullmatch(r"\(\s*(https?://[^\s()]+)\s*\)", q)
    if m_wrapped:
        q = m_wrapped.group(1)
    if not q:
        return "Empty search query."
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q})
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _WEB_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw_html = resp.read(512 * 1024).decode("utf-8", errors="replace")
    parser = _DdgParser()
    parser.feed(raw_html)
    results = [r for r in parser.results if r.get("title") and r.get("url")][:5]
    if not results:
        return f"No web results parsed for: {q}"
    lines = []
    for i, r in enumerate(results, 1):
        title = re.sub(r"\s+", " ", r["title"]).strip()
        snip = re.sub(r"\s+", " ", r["snippet"]).strip()
        lines.append(f"{i}. {title}\n   {r['url']}\n   {snip}")
    return "\n".join(lines)[:4000]


def fetch_url_impl(url: str) -> str:
    u = clean_tool_url(url)
    parsed = urllib.parse.urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "Invalid URL: must start with http:// or https://"

    yt_info = _fetch_youtube_oembed(u) if _is_youtube_host(parsed.netloc) else ""

    req = urllib.request.Request(
        u,
        headers={"User-Agent": _WEB_UA, "Accept-Language": "en-US,en;q=0.9"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body = resp.read(512 * 1024).decode("utf-8", errors="replace")
    except Exception:
        if yt_info:
            return yt_info[:4000]
        raise

    if "html" in ctype or "<html" in body[:500].lower():
        ext = _HtmlTextExtractor()
        ext.feed(body)
        page_text = ext.get_text()
    else:
        page_text = body.strip()

    if yt_info:
        combined = f"{yt_info}\n\n{page_text}".strip() if page_text else yt_info
        return combined[:4000]
    return page_text[:4000]


def parse_active_packs(data: dict) -> list:
    if isinstance(data.get("enabled_packs"), list):
        return [p for p in VALID_PACKS if p in data["enabled_packs"]]
    return ["system"] if bool(data.get("tools_enabled", True)) else []


def build_tools_schema(active_packs: list = None) -> list:
    packs = set(active_packs if active_packs is not None else ["system"])
    tools = []

    if "web" in packs:
        tools.extend([
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the live web and return top titles, URLs, and snippets.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string", "description": "Search query"}},
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_url",
                    "description": "Fetch readable text or title/metadata from a webpage or YouTube URL (pass only the plain URL without parentheses or quotes).",
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string", "description": "Plain http/https URL without parentheses"}},
                        "required": ["url"],
                    },
                },
            },
        ])

    if "files" in packs:
        tools.extend([
            {
                "type": "function",
                "function": {
                    "name": "read_doc",
                    "description": (
                        "Read numbered lines, MediaWiki/Markdown sections, or search matches from a MediaWiki (.wiki, .mediawiki), "
                        "Markdown (.md), or plain text (.txt) file."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "description": "Exact attached or local filename (e.g. notes.md, page.wiki, log.txt)"},
                            "range": {
                                "type": "string",
                                "description": "Optional range: '1-120', 'section:Heading', or 'search:term'",
                            },
                        },
                        "required": ["file"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_doc",
                    "description": (
                        "Create or edit MediaWiki (.wiki, .mediawiki), Markdown (.md), or plain text (.txt) files "
                        "and make them downloadable."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "description": "Source or target filename"},
                            "action": {
                                "type": "string",
                                "enum": ["write", "append", "replace_text", "replace_lines"],
                                "description": "Edit operation",
                            },
                            "content": {"type": "string", "description": "New text or replacement text"},
                            "old_text": {"type": "string", "description": "Exact text to replace (for replace_text)"},
                            "line_range": {"type": "string", "description": "1-based line range 'start-end' (for replace_lines)"},
                            "output_file": {"type": "string", "description": "Optional new filename to save without altering source"},
                        },
                        "required": ["file", "action"],
                    },
                },
            },
        ])

    if "system" in packs:
        if IS_WINDOWS:
            tool_name = "run_shell"
            shell_desc = (
                "Run a non-interactive PowerShell command silently in the background on THIS Windows PC and return stdout/stderr. "
                "Quote paths or URLs containing spaces, '&', or parentheses like (x86) in single quotes. "
                "Only use when explicitly asked to inspect or act on this local machine; use fetch_url/web_search for URLs."
            )
            cmd_desc = "The PowerShell command to run"
        else:
            tool_name = "run_bash"
            shell_desc = (
                f"Run a bash command on THIS {os_label()} computer and return stdout/stderr. "
                "Only use when explicitly asked to inspect or act on this local machine."
            )
            cmd_desc = "The bash command to run"

        tools.extend([
            {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": shell_desc,
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string", "description": cmd_desc}},
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read the contents of a local file on THIS computer.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string", "description": "Path to the file"}},
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write or overwrite text to a local file on THIS computer.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Path to the file"},
                            "content": {"type": "string", "description": "Content to write"},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
        ])

    return tools


def _windows_subprocess_kwargs(new_group: bool = False) -> dict:
    """Return Windows subprocess kwargs that suppress console windows cleanly."""
    if not IS_WINDOWS:
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if new_group:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    kw = {"creationflags": flags}
    si_cls = getattr(subprocess, "STARTUPINFO", None)
    if si_cls is not None:
        si = si_cls()
        si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kw["startupinfo"] = si
    return kw


def terminate_process_tree(proc: subprocess.Popen):
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                **_windows_subprocess_kwargs(new_group=False),
            )
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def build_shell_argv(cmd: str) -> list:
    if IS_WINDOWS:
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            return [ps, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", cmd]
        return ["cmd", "/c", cmd]
    return ["bash", "-c", cmd]


def execute_tool_with_meta(name: str, args: dict, chat_id: str = "", cancel_check=None) -> tuple:
    try:
        if name == "web_search":
            return web_search_impl(args.get("query", "")), None
        if name == "fetch_url":
            return fetch_url_impl(args.get("url", "")), None
        if name == "read_doc":
            return read_doc_impl(args.get("file", ""), args.get("range", ""), chat_id=chat_id), None
        if name == "edit_doc":
            return edit_doc_impl(args, chat_id=chat_id)

        if name in ("run_shell", "run_bash"):
            cmd = args.get("command", "")
            popen_kwargs = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "cwd": str(Path.home()),
            }
            if IS_WINDOWS:
                popen_kwargs.update(_windows_subprocess_kwargs(new_group=True))
            else:
                popen_kwargs["start_new_session"] = True

            proc = subprocess.Popen(build_shell_argv(cmd), **popen_kwargs)
            out_buf = []
            reader = threading.Thread(
                target=lambda: out_buf.append(proc.communicate()[0]), daemon=True
            )
            reader.start()
            deadline = time.monotonic() + 30
            while reader.is_alive():
                if cancel_check and cancel_check():
                    terminate_process_tree(proc)
                    raise CancelledError()
                if time.monotonic() > deadline:
                    terminate_process_tree(proc)
                    return "(command timed out after 30s)", None
                reader.join(0.1)

            out = (out_buf[0] if out_buf else "").strip()
            return (out[:4000] if out else f"(exit code {proc.returncode}, no output)"), None

        if name == "read_file":
            p = Path(args["path"]).expanduser()
            return p.read_text(encoding="utf-8", errors="replace")[:6000], None

        if name == "write_file":
            p = Path(args["path"]).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"Wrote {len(args['content'])} chars to {p}", None

        return f"Unknown tool: {name}", None
    except CancelledError:
        raise
    except Exception as e:
        return f"Tool error: {e}", None


def execute_tool(name: str, args: dict, cancel_check=None) -> str:
    txt, _ = execute_tool_with_meta(name, args, cancel_check=cancel_check)
    return txt


# --- OLLAMA HELPERS ---
def _default_original_soul() -> str:
    if SOUL_FILE.exists():
        txt = SOUL_FILE.read_text(encoding="utf-8")
        if txt.strip():
            return txt
    return DEFAULT_SOUL


def load_souls() -> dict:
    if SOULS_FILE.exists():
        try:
            raw = json.loads(SOULS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("slots"), list):
                original = str(raw.get("original") or DEFAULT_SOUL)
                slots = []
                for idx, item in enumerate(raw["slots"][:MAX_SOUL_SLOTS]):
                    if not isinstance(item, dict):
                        continue
                    sid = str(item.get("id") or f"soul-{idx + 1}").strip()[:40]
                    name = (str(item.get("name") or f"Soul {len(slots) + 1}").strip() or f"Soul {len(slots) + 1}")[:24]
                    slots.append({"id": sid, "name": name, "content": str(item.get("content") or "")})
                if not slots:
                    slots = [{"id": "original", "name": "Original", "content": original}]
                active_id = str(raw.get("active_id") or slots[0]["id"])
                if not any(s["id"] == active_id for s in slots):
                    active_id = slots[0]["id"]
                return {"active_id": active_id, "original": original, "slots": slots}
        except Exception:
            pass
    orig = _default_original_soul()
    return {
        "active_id": "original",
        "original": orig,
        "slots": [{"id": "original", "name": "Original", "content": orig}],
    }


def save_souls(data: dict) -> dict:
    cur = load_souls()
    original = cur.get("original") or DEFAULT_SOUL
    if isinstance(data, dict) and isinstance(data.get("slots"), list):
        slots = []
        seen = set()
        for idx, item in enumerate(data["slots"][:MAX_SOUL_SLOTS]):
            if not isinstance(item, dict):
                continue
            sid = re.sub(r"[^a-zA-Z0-9_-]", "", str(item.get("id") or f"soul-{idx + 1}"))[:40] or f"soul-{idx + 1}"
            if sid in seen:
                sid = f"{sid}-{len(slots) + 1}"
            seen.add(sid)
            name = (str(item.get("name") or f"Soul {len(slots) + 1}").strip() or f"Soul {len(slots) + 1}")[:24]
            slots.append({"id": sid, "name": name, "content": str(item.get("content") or "")})
        if not slots:
            slots = [{"id": "original", "name": "Original", "content": original}]
        active_id = str(data.get("active_id") or slots[0]["id"])
        if not any(s["id"] == active_id for s in slots):
            active_id = slots[0]["id"]
        res = {"active_id": active_id, "original": original, "slots": slots}
    elif isinstance(data, dict) and "content" in data:
        slots = cur["slots"]
        active_id = cur["active_id"]
        content = str(data.get("content") or "")
        for s in slots:
            if s["id"] == active_id:
                s["content"] = content
                break
        res = {"active_id": active_id, "original": original, "slots": slots}
    else:
        res = cur
    active = next((s for s in res["slots"] if s["id"] == res["active_id"]), res["slots"][0])
    SOUL_FILE.write_text(active["content"], encoding="utf-8")
    SOULS_FILE.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    return res


def load_soul() -> str:
    if SOUL_FILE.exists():
        return SOUL_FILE.read_text(encoding="utf-8").strip()
    return DEFAULT_SOUL.strip()


class MissingSpeechModelError(RuntimeError):
    def __init__(self, model: str, message: str):
        super().__init__(message)
        self.model = model


def clean_tts_text(raw: str) -> str:
    s = str(raw or "")
    s = re.sub(r"<think>.*?</think>", " ", s, flags=re.S)
    s = re.sub(r"<tool_call>.*?</tool_call>", " ", s, flags=re.S)
    s = re.sub(r"```[\s\S]*?```", " Code block omitted. ", s)
    s = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"https?://\S+", " link ", s)
    s = re.sub(r"`([^`\n]+)`", r"\1", s)
    s = re.sub(r"(?m)^\s*#{1,6}\s*", "", s)
    s = re.sub(r"(?m)^\s*[-*+]\s+", "", s)
    s = re.sub(r"(?m)^\s*>\s*", "", s)
    s = s.replace("**", "").replace("__", "").replace("~~", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def split_tts_chunks(text: str, limit: int = 3500) -> list:
    if len(text) <= limit:
        return [text] if text else []
    chunks = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = -1
        for sep in ("\n\n", ". ", "! ", "? ", "\n", " "):
            pos = rest.rfind(sep, 0, limit)
            if pos >= limit // 3:
                cut = pos + len(sep)
                break
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return [c for c in chunks if c]


def build_wav_bytes(pcm_bytes: bytes, sample_rate: int = 24000, channels: int = 1, bits_per_sample: int = 16) -> bytes:
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    data_size = len(pcm_bytes)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + pcm_bytes


def _raise_ollama_audio_http_error(e: urllib.error.HTTPError, eff_model: str, label: str):
    err_body = e.read().decode("utf-8", errors="replace")
    msg = err_body
    try:
        parsed = json.loads(err_body)
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
            msg = str(parsed["error"].get("message") or msg)
    except Exception:
        pass
    if e.code == 404 or "not found" in msg.lower():
        raise MissingSpeechModelError(eff_model, f'Speech model "{eff_model}" is not installed in Ollama yet.') from e
    raise RuntimeError(f"Ollama {label} HTTP {e.code}: {msg}") from e


def iter_ollama_speech_pcm(raw_text: str, model: str = DEFAULT_TTS_MODEL, voice: str = "", allow_empty: bool = False):
    """Yield 24 kHz 16-bit LE mono PCM chunks streamed directly from Ollama /v1/audio/speech."""
    cleaned = clean_tts_text(raw_text)
    if not cleaned:
        if allow_empty:
            return
        raise ValueError("Nothing readable to speak.")
    eff_model = (model or DEFAULT_TTS_MODEL).strip()
    chunks = split_tts_chunks(cleaned, limit=3500)

    for piece in chunks:
        payload = {"model": eff_model, "input": piece, "response_format": "pcm"}
        if voice:
            payload["voice"] = voice
        req = urllib.request.Request(
            OLLAMA_SPEECH_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=180)
        except urllib.error.HTTPError as e:
            _raise_ollama_audio_http_error(e, eff_model, "TTS")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Cannot reach Ollama at {OLLAMA_HOST}. Start Ollama and try again.") from e

        header_checked = False
        carry = b""
        read_fn = getattr(resp, "read1", None) or resp.read
        with resp:
            while True:
                raw_blk = read_fn(8192)
                if not raw_blk:
                    break
                data = carry + raw_blk
                carry = b""
                if not header_checked:
                    if data[:4] == b"RIFF":
                        dpos = data.find(b"data", 0, 64)
                        if dpos == -1 and len(data) < 64:
                            carry = data
                            continue
                        if dpos != -1 and dpos + 8 <= len(data):
                            data = data[dpos + 8:]
                    header_checked = True
                if len(data) % 2:
                    carry = data[-1:]
                    data = data[:-1]
                if data:
                    yield data


def synthesize_ollama_speech(raw_text: str, model: str = DEFAULT_TTS_MODEL, voice: str = "") -> bytes:
    eff_model = (model or DEFAULT_TTS_MODEL).strip()
    pcm_all = bytearray()
    for chunk in iter_ollama_speech_pcm(raw_text, model=eff_model, voice=voice, allow_empty=False):
        pcm_all.extend(chunk)
    if not pcm_all:
        raise RuntimeError(f'Speech model "{eff_model}" returned empty audio.')
    return build_wav_bytes(bytes(pcm_all))


def warm_ollama_model(model: str = DEFAULT_TTS_MODEL) -> dict:
    eff_model = (model or DEFAULT_TTS_MODEL).strip()
    running, installed, _ = probe_ollama_detailed()
    if not running:
        raise RuntimeError(f"Cannot reach Ollama at {OLLAMA_HOST}. Start Ollama and try again.")

    base = eff_model.split(":", 1)[0]
    has_tag = ":" in eff_model
    model_ok = any(
        m == eff_model or m == f"{eff_model}:latest" or (not has_tag and m.split(":", 1)[0] == base)
        for m in installed
    )
    if not model_ok:
        raise MissingSpeechModelError(eff_model, f'Speech model "{eff_model}" is not installed in Ollama yet.')

    if is_model_loaded(eff_model):
        return {"ok": True, "model": eff_model, "already_loaded": True}

    req = urllib.request.Request(
        OLLAMA_GENERATE_URL,
        data=json.dumps({"model": eff_model, "prompt": "", "keep_alive": "15m"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            resp.read()
        return {"ok": True, "model": eff_model, "loaded": True}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        if "not found" in err_body.lower():
            raise MissingSpeechModelError(eff_model, f'Speech model "{eff_model}" is not installed in Ollama yet.') from e
        if e.code in (400, 404, 405, 501):
            for _ in iter_ollama_speech_pcm(".", model=eff_model, allow_empty=True):
                pass
            return {"ok": True, "model": eff_model, "loaded": True, "via": "speech"}
        raise RuntimeError(f"Ollama TTS warm HTTP {e.code}: {err_body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach Ollama at {OLLAMA_HOST}. Start Ollama and try again.") from e


def unload_ollama_model(model: str = DEFAULT_TTS_MODEL) -> dict:
    eff_model = (model or DEFAULT_TTS_MODEL).strip()
    if not eff_model:
        return {"ok": True, "unloaded": False}
    try:
        if not is_model_loaded(eff_model):
            return {"ok": True, "model": eff_model, "already_unloaded": True}
    except Exception:
        pass
    req = urllib.request.Request(
        OLLAMA_GENERATE_URL,
        data=json.dumps({"model": eff_model, "prompt": "", "keep_alive": 0}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
        return {"ok": True, "model": eff_model, "unloaded": True}
    except Exception:
        return {"ok": True, "model": eff_model, "unloaded": False}


def find_ollama_executable() -> str:
    exe = shutil.which("ollama")
    if exe:
        return exe
    if IS_WINDOWS:
        cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if cand.exists():
            return str(cand)
    elif IS_MAC:
        for cand in (
            "/Applications/Ollama.app/Contents/MacOS/Ollama",
            "/opt/homebrew/bin/ollama",
            "/usr/local/bin/ollama",
        ):
            if Path(cand).exists():
                return cand
    return ""


def normalize_model_name(name: str) -> str:
    n = (name or "").strip()
    return n[:-7] if n.endswith(":latest") else n


def probe_ollama_detailed() -> tuple:
    installed = []
    details_list = []
    seen = set()
    try:
        with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for m in data.get("models", []):
                raw_name = str(m.get("name") or "").strip()
                name = normalize_model_name(raw_name)
                if not name or name in seen:
                    continue
                seen.add(name)
                installed.append(name)
                det = m.get("details") if isinstance(m.get("details"), dict) else {}
                details_list.append({
                    "name": name,
                    "raw_name": raw_name,
                    "size": int(m.get("size") or 0),
                    "modified_at": str(m.get("modified_at") or ""),
                    "parameter_size": str(det.get("parameter_size") or ""),
                    "quantization": str(det.get("quantization_level") or ""),
                })
        return True, installed, details_list
    except Exception:
        return False, [], []


def probe_ollama() -> tuple:
    running, installed, _ = probe_ollama_detailed()
    return running, installed


def try_start_ollama() -> bool:
    running, _ = probe_ollama()
    if running:
        return True
    exe = find_ollama_executable()
    if not exe:
        return False
    ol_env = os.environ.copy()
    ol_env.setdefault("OLLAMA_NUM_PARALLEL", "2")
    ol_env.setdefault("OLLAMA_MAX_LOADED_MODELS", "2")
    try:
        if IS_MAC and Path("/Applications/Ollama.app").exists():
            subprocess.Popen(["open", "-a", "Ollama"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ol_env)
        elif IS_WINDOWS:
            subprocess.Popen(
                [exe, "serve"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=ol_env,
                **_windows_subprocess_kwargs(new_group=False),
            )
        else:
            subprocess.Popen(
                [exe, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=ol_env,
                start_new_session=True,
            )
    except Exception:
        return False

    for _ in range(12):
        time.sleep(0.25)
        ok, _ = probe_ollama()
        if ok:
            return True
    return False


def infer_model_param_label(model_name: str, details_map: dict = None) -> str:
    n = (model_name or "").strip()
    if not n:
        return ""
    if details_map:
        det = details_map.get(n) or details_map.get(normalize_model_name(n))
        if isinstance(det, dict):
            ps = str(det.get("parameter_size") or "").strip()
            if ps:
                return ps.upper()
    low = n.lower()
    if low in KNOWN_MODEL_PARAMS:
        return KNOWN_MODEL_PARAMS[low]
    if ":" in low:
        tag = low.split(":", 1)[1]
        em = re.search(r"(?:^|-)e(\d+(?:\.\d+)?b)(?:$|-)", tag, re.I)
        if em:
            return f"{em.group(1).upper()} eff"
        sm = re.search(r"(?:^|-)(\d+(?:\.\d+)?[bm])(?:$|-)", tag, re.I)
        if sm:
            return sm.group(1).upper()
    base = low.split(":", 1)[0]
    if base in KNOWN_MODEL_PARAMS:
        return KNOWN_MODEL_PARAMS[base]
    return ""


def _parse_param_b(label: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*([bm])", label or "", re.I)
    if not m:
        return 999.0
    val = float(m.group(1))
    return val / 1000.0 if m.group(2).lower() == "m" else val


def _model_family_rank(name: str) -> int:
    low = (name or "").lower()
    if low.startswith("lfm3") and "audio" not in low:
        return 0
    if low.startswith("granite4.2") or low.startswith("granite"):
        return 1
    if low.startswith("qwen3.5") or low.startswith("qwen"):
        return 2
    if low.startswith("gemma4") or low.startswith("gemma"):
        return 3
    if low.startswith("lfm"):
        return 4
    return 5


def get_models_info() -> dict:
    running, installed, installed_details = probe_ollama_detailed()
    settings = load_settings()
    saved_default = settings.get("default_model", "").strip()

    details_by_name = {}
    for d in installed_details:
        if isinstance(d, dict):
            if d.get("name"):
                details_by_name[d["name"]] = d
            if d.get("raw_name"):
                details_by_name[d["raw_name"]] = d

    merged = list(installed)
    for p in PRESET_MODELS:
        if any(m == p or m == f"{p}:latest" or (p == "lfm3:1.7b" and m == "lfm3") for m in merged):
            continue
        merged.append(p)
    if saved_default and not any(m == saved_default or m == f"{saved_default}:latest" for m in merged):
        merged.insert(0, saved_default)

    model_params = {}
    for m in merged + [saved_default, DEFAULT_TTS_MODEL, "phonon", "phonon:en"]:
        if m and m not in model_params:
            lbl = infer_model_param_label(m, details_by_name)
            if lbl:
                model_params[m] = lbl

    installed_set = set(installed)
    merged.sort(key=lambda m: (
        _model_family_rank(m),
        _parse_param_b(model_params.get(m, "")),
        0 if m in installed_set else 1,
        m.lower(),
    ))

    active_default = saved_default or DEFAULT_MODEL
    if not saved_default and "AGENT_MODEL" not in os.environ and installed:
        if not any(m == DEFAULT_MODEL or m == f"{DEFAULT_MODEL}:latest" or (DEFAULT_MODEL == "lfm3:1.7b" and m == "lfm3") for m in installed):
            ordered_installed = sorted(installed, key=lambda m: (_model_family_rank(m), _parse_param_b(model_params.get(m, "")), m.lower()))
            active_default = ordered_installed[0]
        elif DEFAULT_MODEL not in installed and "lfm3" in installed and DEFAULT_MODEL == "lfm3:1.7b":
            active_default = "lfm3"

    return {
        "ollama_running": running,
        "has_ollama_bin": bool(find_ollama_executable()),
        "installed": installed,
        "installed_details": installed_details,
        "models": merged,
        "model_params": model_params,
        "default_model": active_default,
        "os": os_label(),
        "data_dir": str(DATA_DIR),
    }


def parse_xml_tool_fallback(text: str) -> list:
    calls = []
    for block in re.findall(r"<tool_call>(.*?)</tool_call>", text, re.S):
        fn = re.search(r"<function=([^>\s]+)>", block)
        if not fn:
            continue
        args = {}
        for k, v in re.findall(r"<parameter=([^>\s]+)>(.*?)</parameter>", block, re.S):
            args[k.strip()] = v.strip()
        calls.append({"function": {"name": fn.group(1).strip(), "arguments": args}})
    return calls


def split_thinking_and_reply(raw_content: str, stream_thinking: str) -> tuple:
    embedded = re.findall(r"<think>(.*?)</think>", raw_content, flags=re.S)
    thinking = stream_thinking.strip()
    if embedded:
        extra = "\n".join(x.strip() for x in embedded if x.strip())
        thinking = (thinking + "\n" + extra).strip() if thinking else extra
    reply = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.S)
    reply = re.sub(r"<tool_call>.*?</tool_call>", "", reply, flags=re.S).strip()
    return thinking, reply


def trim_history(history: list):
    def total_chars():
        return sum(len(json.dumps(m, ensure_ascii=False)) for m in history)

    while len(history) > 1 and (len(history) > MAX_HISTORY or total_chars() > MAX_HISTORY_CHARS):
        history.pop(0)
        while history and history[0].get("role") != "user":
            history.pop(0)


def is_model_loaded(model: str) -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_PS_URL, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        loaded = [m.get("name", "") for m in data.get("models", [])]
        target = model.split(":")[0]
        return any(
            n == model or n == f"{model}:latest" or (":" not in model and n.split(":")[0] == target)
            for n in loaded
        )
    except Exception:
        return True


class _StreamTagFilter:
    """Incrementally strip <think> and <tool_call> blocks while streaming reply text."""

    _TAGS = ("think", "tool_call")

    def __init__(self, on_content=None):
        self.on_content = on_content
        self.inside_tag = None
        self._buf = ""
        self._started = False
        self.emitted_chars = 0

    def pending_tag(self):
        if self.inside_tag:
            return self.inside_tag
        if self._buf.startswith("<"):
            for tag in self._TAGS:
                if f"<{tag}>".startswith(self._buf):
                    return tag
        return None

    def _emit(self, text: str):
        if not text:
            return
        if not self._started:
            text = text.lstrip()
            if not text:
                return
            self._started = True
        self.emitted_chars += len(text)
        if self.on_content:
            self.on_content(text)

    def feed(self, chunk: str):
        if not chunk:
            return
        self._buf += chunk
        while self._buf:
            if self.inside_tag:
                close_tag = f"</{self.inside_tag}>"
                idx = self._buf.find(close_tag)
                if idx != -1:
                    self._buf = self._buf[idx + len(close_tag):]
                    self.inside_tag = None
                    continue
                keep = ""
                for k in range(len(close_tag) - 1, 0, -1):
                    if self._buf.endswith(close_tag[:k]):
                        keep = self._buf[-k:]
                        break
                self._buf = keep
                break

            lt = self._buf.find("<")
            if lt == -1:
                self._emit(self._buf)
                self._buf = ""
                break
            if lt > 0:
                self._emit(self._buf[:lt])
                self._buf = self._buf[lt:]

            matched = None
            for tag in self._TAGS:
                if self._buf.startswith(f"<{tag}>"):
                    matched = tag
                    break
            if matched:
                self._buf = self._buf[len(matched) + 2:]
                self.inside_tag = matched
                continue

            if any(f"<{tag}>".startswith(self._buf) for tag in self._TAGS):
                break

            nxt = self._buf.find("<", 1)
            if nxt == -1:
                self._emit(self._buf)
                self._buf = ""
                break
            self._emit(self._buf[:nxt])
            self._buf = self._buf[nxt:]

    def flush(self):
        if not self.inside_tag and self._buf:
            self._emit(self._buf)
        self._buf = ""


def stream_ollama(
    messages: list,
    model: str,
    tools_enabled: bool = True,
    tools: list = None,
    on_thinking=None,
    on_phase=None,
    on_content=None,
    cancel_check=None,
) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "keep_alive": "15m",
        "options": {
            "temperature": 0.1,
            "top_k": 50,
            "repeat_penalty": 1.1,
            "num_ctx": NUM_CTX,
        },
    }
    eff_tools = tools if tools is not None else (build_tools_schema(["system"]) if tools_enabled else [])
    if eff_tools:
        payload["tools"] = eff_tools

    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    full_thinking = []
    full_content = []
    tool_calls = []
    prompt_tokens = 0
    eval_tokens = 0
    current_phase = None

    def set_phase(p: str):
        nonlocal current_phase
        if p != current_phase:
            current_phase = p
            if on_phase:
                on_phase(p)

    def emit_clean(txt: str):
        set_phase("working")
        if on_content:
            on_content(txt)

    tag_filter = _StreamTagFilter(on_content=emit_clean)

    with OLLAMA_LOCK:
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                for raw_line in resp:
                    if cancel_check and cancel_check():
                        raise CancelledError()
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise RuntimeError(chunk["error"])
                    m = chunk.get("message") or {}

                    t_delta = m.get("thinking") or ""
                    if t_delta:
                        set_phase("thinking")
                        full_thinking.append(t_delta)
                        if on_thinking:
                            on_thinking(t_delta)

                    c_delta = m.get("content") or ""
                    if c_delta:
                        full_content.append(c_delta)
                        tag_filter.feed(c_delta)
                        pend = tag_filter.pending_tag()
                        if pend == "think":
                            set_phase("thinking")
                        elif pend == "tool_call" or current_phase is None:
                            set_phase("working")

                    if m.get("tool_calls"):
                        set_phase("working")
                        tool_calls.extend(m["tool_calls"])

                    if chunk.get("done"):
                        tag_filter.flush()
                        prompt_tokens = int(chunk.get("prompt_eval_count") or 0)
                        eval_tokens = int(chunk.get("eval_count") or 0)
                        break
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama HTTP {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cannot reach Ollama at {OLLAMA_HOST}. Start Ollama and try again."
            ) from e

    used = prompt_tokens + eval_tokens
    if used <= 0:
        est_in = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 4
        est_out = (len("".join(full_thinking)) + len("".join(full_content))) // 4
        used = est_in + est_out

    return {
        "thinking": "".join(full_thinking),
        "content": "".join(full_content),
        "tool_calls": tool_calls,
        "tokens": {
            "used": used,
            "prompt": prompt_tokens,
            "output": eval_tokens,
            "max": NUM_CTX,
        },
    }


def stream_pull_model(model_name: str, emit, cancel_check=None):
    req = urllib.request.Request(
        OLLAMA_PULL_URL,
        data=json.dumps({"name": model_name, "stream": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=1800) as resp:
        for raw_line in resp:
            if cancel_check and cancel_check():
                return
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            ev = json.loads(line)
            emit(ev)
            if ev.get("status") == "success":
                break


def delete_ollama_model(model_name: str):
    req = urllib.request.Request(
        OLLAMA_DELETE_URL,
        data=json.dumps({"name": model_name}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore").strip()
        except Exception:
            pass
        raise RuntimeError(body or f"Ollama HTTP {e.code}")

    settings = load_settings()
    if normalize_model_name(settings.get("default_model", "")) == normalize_model_name(model_name):
        settings["default_model"] = ""
        save_settings(settings)


# --- CORE AGENT LOOP ---
def _build_attachment_header(chat_id: str, attached_files: list) -> str:
    if not attached_files or not chat_id or not valid_chat_id(chat_id):
        return ""
    lines = []
    for fname in attached_files:
        safe = safe_upload_name(fname)
        p = UPLOADS_DIR / chat_id / safe
        if p.exists() and p.is_file():
            outline = inspect_file_outline(p)
            info = f"{p.stat().st_size} B" + (f", {outline}" if outline else "")
            lines.append(f"- {p.name} ({info})")
    if not lines:
        return ""
    return "[Attached files (stored in upload area; always pass exact filename to read_doc / edit_doc, NOT bash or read_file):\n" + "\n".join(lines) + "]\n\n"


def run_agent_stream(
    user_text: str,
    history: list,
    model: str = DEFAULT_MODEL,
    tools_enabled: bool = True,
    active_packs: list = None,
    chat_id: str = "",
    attached_files: list = None,
    emit=None,
    cancel_check=None,
) -> dict:
    packs = list(active_packs) if active_packs is not None else (["system"] if tools_enabled else [])
    if attached_files and "files" not in packs:
        packs.append("files")
    tools = build_tools_schema(packs)
    has_tools = bool(tools)

    prompt_text = _build_attachment_header(chat_id, attached_files or []) + user_text
    history.append({"role": "user", "content": prompt_text})
    trim_history(history)

    sys_prompt = load_soul()
    if "web" in packs:
        sys_prompt += (
            "\n- Web tools enabled: use web_search or fetch_url when external or recent information or a webpage/YouTube link is given "
            "(pass plain URLs without parentheses or quotes). Do not use shell commands for web lookups."
        )
    if "files" in packs:
        sys_prompt += (
            "\n- Document tools enabled: pass the exact attached filename to read_doc(file, range) first, "
            "and edit MediaWiki (.wiki), Markdown (.md), or text (.txt) files with edit_doc(file, action, ...)."
        )
    if "system" in packs and IS_WINDOWS:
        sys_prompt += (
            "\n- Windows shell runs non-interactive PowerShell: always wrap paths or URLs containing spaces, '&', or parentheses like (x86) in single quotes, "
            "and do not keep retrying failed shell commands."
        )

    settings = load_settings()
    step_limit = normalize_tool_steps(settings.get("max_tool_steps", MAX_STEPS))
    all_thinking = []
    all_tools = []
    all_outputs = []
    seen_outputs = set()
    seen_call_counts = {}
    tool_counter = 0
    last_tokens = {"used": 0, "prompt": 0, "output": 0, "max": NUM_CTX}

    try:
        for _ in range(step_limit):
            if cancel_check and cancel_check():
                raise CancelledError()

            messages = [{"role": "system", "content": sys_prompt}] + history

            def on_think(delta):
                if emit:
                    emit({"type": "thinking_delta", "text": delta})

            def on_phase(p):
                if emit:
                    emit({"type": "phase", "phase": p})

            def on_content(delta):
                if emit:
                    emit({"type": "reply_delta", "text": delta})

            msg = stream_ollama(
                messages,
                model=model,
                tools_enabled=has_tools,
                tools=tools,
                on_thinking=on_think,
                on_phase=on_phase,
                on_content=on_content,
                cancel_check=cancel_check,
            )

            last_tokens = msg["tokens"]
            if emit:
                emit({"type": "tokens", "tokens": last_tokens})

            raw_content = msg["content"]
            step_thinking, clean_text = split_thinking_and_reply(raw_content, msg["thinking"])
            if step_thinking:
                all_thinking.append(step_thinking)
                if not msg["thinking"] and emit:
                    emit({"type": "thinking_delta", "text": step_thinking + "\n"})

            tool_calls = msg["tool_calls"]
            if has_tools and not tool_calls and "<tool_call>" in raw_content:
                tool_calls = parse_xml_tool_fallback(raw_content)

            if not tool_calls or not has_tools:
                reply = clean_text or "(done)"
                history.append({"role": "assistant", "content": reply})
                return {
                    "reply": reply,
                    "thinking": "\n\n".join(all_thinking).strip(),
                    "tools": all_tools,
                    "outputs": all_outputs,
                    "tokens": last_tokens,
                    "cancelled": False,
                }

            history.append({
                "role": "assistant",
                "content": raw_content,
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                if cancel_check and cancel_check():
                    raise CancelledError()

                fn = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}

                tool_counter += 1
                tid = tool_counter
                if emit:
                    emit({"type": "tool_start", "id": tid, "name": name, "args": args})

                sig = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                seen_call_counts[sig] = seen_call_counts.get(sig, 0) + 1
                if seen_call_counts[sig] > 2:
                    result = "Repeated identical tool call skipped; summarize the prior result or explain the issue without retrying the same command."
                    file_meta = None
                else:
                    result, file_meta = execute_tool_with_meta(name, args, chat_id=chat_id, cancel_check=cancel_check)

                tool_record = {"id": tid, "name": name, "args": args, "result": result}
                all_tools.append(tool_record)
                if emit:
                    emit({"type": "tool_end", **tool_record})
                if file_meta:
                    if file_meta["name"] not in seen_outputs:
                        seen_outputs.add(file_meta["name"])
                        all_outputs.append(file_meta)
                    else:
                        all_outputs = [file_meta if o["name"] == file_meta["name"] else o for o in all_outputs]
                    if emit:
                        emit({"type": "file_ready", **file_meta})

                history.append({"role": "tool", "content": result})

        return {
            "reply": f"Stopped: reached max tool steps ({step_limit}). You can raise this limit in Settings.",
            "thinking": "\n\n".join(all_thinking).strip(),
            "tools": all_tools,
            "outputs": all_outputs,
            "tokens": last_tokens,
            "cancelled": False,
        }

    except CancelledError:
        if history and history[-1].get("role") != "assistant":
            history.append({"role": "assistant", "content": "(stopped by user)"})
        return {
            "reply": "*(stopped by user)*",
            "thinking": "\n\n".join(all_thinking).strip(),
            "tools": all_tools,
            "outputs": all_outputs,
            "tokens": last_tokens,
            "cancelled": True,
        }


def clean_chat_title(raw: str, fallback: str) -> str:
    txt = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    txt = re.sub(r"<think>.*$", "", txt, flags=re.S).strip()
    line = next((ln.strip() for ln in txt.splitlines() if ln.strip()), "")
    line = line.strip(" \"'`*#-.")
    line = re.sub(r"^(title\s*:\s*)", "", line, flags=re.I).strip(" \"'`*#-.")
    if not line:
        return fallback
    if len(line) > 52:
        line = line[:49].rstrip() + "..."
    return line


def generate_chat_title(user_msg: str, model: str, cancel_check=None) -> str:
    fallback = user_msg[:42] + ("..." if len(user_msg) > 42 else "")
    snippet = user_msg.strip().replace("\n", " ")[:300]
    messages = [
        {
            "role": "system",
            "content": "Write a concise 2 to 5 word chat title for this message. Output ONLY the title, no quotes.",
        },
        {"role": "user", "content": snippet},
    ]

    def _call(disable_think: bool) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.2,
                "num_predict": 24 if disable_think else 96,
                "num_ctx": NUM_CTX,
            },
        }
        if disable_think:
            payload["think"] = False
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("message") or {}).get("content") or ""

    try:
        if cancel_check and cancel_check():
            return fallback
        with OLLAMA_LOCK:
            try:
                raw = _call(disable_think=True)
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    raw = _call(disable_think=False)
                else:
                    return fallback
        return clean_chat_title(raw, fallback)
    except Exception:
        return fallback


def load_chips() -> list:
    if CHIPS_FILE.exists():
        try:
            raw = json.loads(CHIPS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                cleaned = [
                    {
                        "label": str(item.get("label", "")).strip()[:40],
                        "prompt": str(item.get("prompt", "")).strip()[:500],
                    }
                    for item in raw
                    if isinstance(item, dict)
                    and str(item.get("label", "")).strip()
                    and str(item.get("prompt", "")).strip()
                ]
                return cleaned
        except Exception:
            pass
    return [dict(c) for c in DEFAULT_CHIPS]


def save_chips(items: list) -> list:
    cleaned = [
        {
            "label": str(item.get("label", "")).strip()[:40],
            "prompt": str(item.get("prompt", "")).strip()[:500],
        }
        for item in (items or [])
        if isinstance(item, dict)
        and str(item.get("label", "")).strip()
        and str(item.get("prompt", "")).strip()
    ]
    CHIPS_FILE.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False), encoding="utf-8")
    return cleaned


VALID_SOUNDS = ("chime", "ping", "double", "chord", "off")


def normalize_tool_steps(val, default: int = MAX_STEPS) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        n = default
    return max(MIN_TOOL_STEPS, min(MAX_TOOL_STEPS_LIMIT, n))


def load_settings() -> dict:
    out = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                dm = str(raw.get("default_model", "")).strip()[:120]
                snd = str(raw.get("sound", "chime")).strip().lower()
                tm = str(raw.get("tts_model", DEFAULT_TTS_MODEL) or DEFAULT_TTS_MODEL).strip()[:120]
                tv = str(raw.get("tts_voice", "") or "").strip()[:60]
                out["default_model"] = dm
                out["sound"] = snd if snd in VALID_SOUNDS else "chime"
                out["max_tool_steps"] = normalize_tool_steps(raw.get("max_tool_steps", MAX_STEPS))
                out["tts_model"] = tm or DEFAULT_TTS_MODEL
                out["tts_voice"] = tv
                out["tts_auto"] = bool(raw.get("tts_auto", False))
        except Exception:
            pass
    return out


def save_settings(data: dict) -> dict:
    cur = load_settings()
    if isinstance(data, dict):
        if "default_model" in data:
            cur["default_model"] = str(data.get("default_model") or "").strip()[:120]
        if "sound" in data:
            snd = str(data.get("sound") or "chime").strip().lower()
            cur["sound"] = snd if snd in VALID_SOUNDS else "chime"
        if "max_tool_steps" in data:
            cur["max_tool_steps"] = normalize_tool_steps(data.get("max_tool_steps"), cur.get("max_tool_steps", MAX_STEPS))
        if "tts_model" in data:
            tm = str(data.get("tts_model") or "").strip()[:120]
            cur["tts_model"] = tm or DEFAULT_TTS_MODEL
        if "tts_voice" in data:
            cur["tts_voice"] = str(data.get("tts_voice") or "").strip()[:60]
        if "tts_auto" in data:
            cur["tts_auto"] = bool(data.get("tts_auto"))
    SETTINGS_FILE.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
    return cur


# --- CHAT STORAGE ---
def ensure_chats_dir():
    CHATS_DIR.mkdir(parents=True, exist_ok=True)


def valid_chat_id(chat_id: str) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z0-9_-]{4,64}", chat_id or ""))


def new_chat_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def chat_path(chat_id: str) -> Path:
    return CHATS_DIR / f"{chat_id}.json"


def load_chat(chat_id: str) -> dict:
    ensure_chats_dir()
    p = chat_path(chat_id)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {
        "id": chat_id,
        "title": "New chat",
        "model": DEFAULT_MODEL,
        "tokens": {"used": 0, "prompt": 0, "output": 0, "max": NUM_CTX},
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "history": [],
        "ui_messages": [],
    }


def save_chat(chat: dict):
    ensure_chats_dir()
    chat["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    chat_path(chat["id"]).write_text(
        json.dumps(chat, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def list_chats() -> list:
    ensure_chats_dir()
    items = []
    for p in sorted(CHATS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            items.append({
                "id": data.get("id", p.stem),
                "title": data.get("title", p.stem),
                "model": data.get("model", DEFAULT_MODEL),
                "updated_at": data.get("updated_at", ""),
            })
        except Exception:
            continue
    return items


def save_uploaded_file(chat_id: str, filename: str, raw_bytes: bytes) -> dict:
    if not valid_chat_id(chat_id):
        raise ValueError("Invalid chat ID")
    ensure_uploads_dir()
    cdir = UPLOADS_DIR / chat_id
    cdir.mkdir(parents=True, exist_ok=True)
    safe = safe_upload_name(filename)
    target = (cdir / safe).resolve()
    if target.parent != cdir.resolve():
        raise ValueError("Invalid filename")
    target.write_bytes(raw_bytes)
    return {
        "chat_id": chat_id,
        "name": safe,
        "size": len(raw_bytes),
        "outline": inspect_file_outline(target),
        "url": f"/api/files/{urllib.parse.quote(chat_id)}/{urllib.parse.quote(safe)}",
    }


def list_uploaded_files(chat_id: str = "") -> dict:
    ensure_uploads_dir()
    titles = {c["id"]: c["title"] for c in list_chats()}
    files = []
    total_bytes = 0
    dirs = []
    if chat_id:
        if valid_chat_id(chat_id) and (UPLOADS_DIR / chat_id).is_dir():
            dirs = [UPLOADS_DIR / chat_id]
    else:
        dirs = sorted((d for d in UPLOADS_DIR.iterdir() if d.is_dir()), reverse=True)

    for cdir in dirs:
        cid = cdir.name
        for fp in sorted(cdir.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
            if not fp.is_file():
                continue
            st = fp.stat()
            total_bytes += st.st_size
            files.append({
                "chat_id": cid,
                "chat_title": titles.get(cid, "Draft / deleted chat"),
                "name": fp.name,
                "size": st.st_size,
                "ext": fp.suffix.lower(),
                "modified_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                "outline": inspect_file_outline(fp),
                "url": f"/api/files/{urllib.parse.quote(cid)}/{urllib.parse.quote(fp.name)}",
            })
    return {"files": files, "total_bytes": total_bytes}


def delete_uploaded_file(chat_id: str, filename: str) -> bool:
    if not valid_chat_id(chat_id):
        raise ValueError("Invalid chat ID")
    cdir = UPLOADS_DIR / chat_id
    if not cdir.is_dir():
        return False
    target = (cdir / safe_upload_name(filename)).resolve()
    if target.parent != cdir.resolve():
        raise ValueError("Invalid filename")
    deleted = False
    if target.is_file():
        target.unlink()
        deleted = True
    try:
        if not any(cdir.iterdir()):
            cdir.rmdir()
    except OSError:
        pass
    return deleted


def delete_chat_uploads(chat_id: str = "") -> int:
    ensure_uploads_dir()
    if chat_id:
        if not valid_chat_id(chat_id):
            return 0
        cdir = UPLOADS_DIR / chat_id
        if cdir.is_dir():
            cnt = sum(1 for p in cdir.rglob("*") if p.is_file())
            shutil.rmtree(cdir, ignore_errors=True)
            return cnt
        return 0
    cnt = sum(1 for p in UPLOADS_DIR.rglob("*") if p.is_file())
    for sub in list(UPLOADS_DIR.iterdir()):
        if sub.is_dir():
            shutil.rmtree(sub, ignore_errors=True)
    return cnt


# --- CROSS-PLATFORM WEB UI ---
WEB_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>local agent</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #111113;
    --sidebar: #0c0c0e;
    --surface: #18181b;
    --surface-2: #222226;
    --border: #2a2a30;
    --text: #f4f4f6;
    --muted: #9898a3;
    --white: #ffffff;
    --code-bg: #09090b;

    --m-1: #ede9fe;
    --m-2: #c4b5fd;
    --m-3: #a78bfa;
    --m-4: #8b5cf6;
    --m-5: #7c3aed;
    --m-glow: rgba(167, 139, 250, 0.14);

    --font-sans: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
    --font-mono: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace;
  }
  * {
    box-sizing: border-box;
    scrollbar-width: thin;
    scrollbar-color: #3a3a42 transparent;
  }
  *::-webkit-scrollbar { width: 8px; height: 8px; }
  *::-webkit-scrollbar-track { background: transparent; }
  *::-webkit-scrollbar-thumb {
    background: #3a3a42; border-radius: 999px;
    border: 2px solid transparent; background-clip: padding-box;
  }
  *::-webkit-scrollbar-thumb:hover { background: #52525b; border: 2px solid transparent; background-clip: padding-box; }
  *::-webkit-scrollbar-thumb:active { background: #71717a; border: 2px solid transparent; background-clip: padding-box; }
  html, body { height: 100%; margin: 0; background: var(--bg); color: var(--text); font-family: var(--font-sans); font-size: 15px; }
  body { display: flex; overflow: hidden; }
  #main-pane {
    opacity: 0;
    transform: translateY(8px) scale(0.994);
    transition:
      opacity 0.42s cubic-bezier(0.22, 1, 0.36, 1),
      transform 0.46s cubic-bezier(0.22, 1, 0.36, 1);
  }
  body.app-ready #main-pane { opacity: 1; transform: translateY(0) scale(1); }
  svg { display: block; flex-shrink: 0; }

  .m-mark {
    display: inline-grid; grid-template-rows: repeat(5, 1fr); gap: 2px;
    width: 18px; height: 18px; flex-shrink: 0;
  }
  .m-mark span { display: block; border-radius: 1px; background: var(--white); }
  .m-mark span:nth-child(1) { opacity: 0.95; width: 65%; }
  .m-mark span:nth-child(2) { opacity: 0.85; width: 85%; }
  .m-mark span:nth-child(3) { opacity: 1.00; width: 100%; }
  .m-mark span:nth-child(4) { opacity: 0.75; width: 80%; }
  .m-mark span:nth-child(5) { opacity: 0.60; width: 55%; }

  /* Sidebar */
  aside {
    width: 260px; background: var(--sidebar); border-right: 1px solid var(--border);
    display: flex; flex-direction: column; flex-shrink: 0; transition: width 0.2s ease, opacity 0.2s ease;
  }
  aside.collapsed { width: 0; border-right: none; overflow: hidden; opacity: 0; pointer-events: none; visibility: hidden; }
  .side-head {
    height: 52px; padding: 0 14px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;
  }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 14px; color: var(--white); }
  .side-actions { padding: 10px 10px 6px; display: flex; gap: 6px; }

  button {
    background: var(--surface); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 7px 11px; font: inherit; font-size: 13px;
    cursor: pointer; transition: border-color 0.15s, background 0.15s, color 0.15s;
  }
  button:hover { background: var(--surface-2); border-color: rgba(255,255,255,0.35); }
  button:disabled { opacity: 0.55; cursor: not-allowed; }
  .btn-new { flex: 1; font-weight: 500; }

  .side-label {
    padding: 10px 14px 4px; font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--muted);
  }
  #chat-list { flex: 1; overflow-y: auto; padding: 4px 8px 12px; }
  .chat-item {
    display: flex; align-items: center; justify-content: space-between; gap: 6px;
    padding: 8px 10px; border-radius: 8px; cursor: pointer; color: var(--muted);
    margin-bottom: 2px; font-size: 13px;
  }
  .chat-item:hover { background: var(--surface); color: var(--text); }
  .chat-item.active { background: var(--surface-2); color: var(--white); }
  .chat-title-wrap { flex: 1; min-width: 0; overflow: hidden; }
  .chat-title-track { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chat-title-dup { display: none; }
  .chat-item.has-overflow:hover .chat-title-wrap {
    -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 8px, #000 calc(100% - 14px), transparent 100%);
    mask-image: linear-gradient(90deg, transparent 0, #000 8px, #000 calc(100% - 14px), transparent 100%);
  }
  .chat-item.has-overflow:hover .chat-title-track {
    display: inline-flex; width: max-content; overflow: visible; text-overflow: clip;
    animation: titleMarquee var(--scroll-dur, 5s) linear infinite;
  }
  .chat-item.has-overflow:hover .chat-title-main,
  .chat-item.has-overflow:hover .chat-title-dup {
    display: inline-block; padding-right: 34px;
  }
  @keyframes titleMarquee {
    0%   { transform: translateX(0); }
    100% { transform: translateX(-50%); }
  }
  .del-btn {
    opacity: 0; border: none; background: transparent; color: var(--muted);
    padding: 4px; border-radius: 5px; display: grid; place-items: center; flex-shrink: 0;
  }
  .chat-item:hover .del-btn { opacity: 0.7; }
  .del-btn:hover { opacity: 1 !important; color: var(--white); background: rgba(255,255,255,0.1); }
  .side-foot {
    padding: 8px 10px 10px; border-top: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    font-size: 11px; color: var(--muted);
  }
  .quit-btn {
    background: transparent; border: 1px solid var(--border); color: var(--muted);
    padding: 4px 8px; border-radius: 6px; font-size: 11px;
  }
  .quit-btn:hover { color: var(--white); border-color: rgba(255,255,255,0.4); }

  /* Main & Header */
  main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  header {
    height: 52px; padding: 0 18px; display: flex; align-items: center; justify-content: space-between;
    border-bottom: 1px solid var(--border); flex-shrink: 0;
    position: relative; z-index: 10; background: var(--bg);
  }
  .head-left { display: flex; align-items: center; gap: 10px; }
  .icon-btn {
    background: transparent; border: 1px solid transparent; padding: 6px;
    color: var(--muted); border-radius: 7px; display: grid; place-items: center;
  }
  .icon-btn:hover { background: var(--surface); color: var(--white); border-color: var(--border); }

  /* Model Dropdown & Symmetrical Header Badges */
  .model-wrap { position: relative; }
  .model-pill {
    display: inline-flex; align-items: center; gap: 7px;
    height: 30px; padding: 0 11px; border-radius: 7px;
    font-size: 13px; line-height: 1;
    background: var(--surface); border: 1px solid var(--border);
    color: var(--white);
  }
  .model-pill:hover { border-color: rgba(255,255,255,0.4); }
  .dot-live {
    width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
    background: var(--m-3); box-shadow: 0 0 3px var(--m-glow);
    opacity: 0.9; transition: background 0.2s, box-shadow 0.2s;
  }
  .caret-svg { color: var(--muted); transition: transform 0.15s; }
  .model-menu {
    position: absolute; top: calc(100% + 6px); left: 0; min-width: 280px;
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 5px; display: flex; flex-direction: column; gap: 2px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.55); z-index: 30;
    opacity: 0; visibility: hidden; pointer-events: none;
    transform: translateY(-6px) scale(0.97); transform-origin: top left;
    transition:
      opacity 0.16s ease,
      transform 0.2s cubic-bezier(0.22, 1, 0.36, 1),
      visibility 0s linear 0.18s;
  }
  .model-menu.open {
    opacity: 1; visibility: visible; pointer-events: auto;
    transform: translateY(0) scale(1);
    transition:
      opacity 0.16s ease,
      transform 0.2s cubic-bezier(0.22, 1, 0.36, 1),
      visibility 0s;
  }
  .model-opt {
    display: flex; align-items: center; gap: 10px;
    padding: 7px 10px; border-radius: 7px; border: none; background: transparent;
    color: var(--text); font-size: 13px; text-align: left; width: 100%;
  }
  .model-opt:hover { background: var(--surface-2); }
  .model-opt-left { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; }
  .opt-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .opt-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
  .opt-param {
    font-family: var(--font-mono); font-size: 11px; line-height: 1;
    color: var(--muted); flex-shrink: 0;
    width: 52px; text-align: right; white-space: nowrap;
  }
  .opt-status {
    display: inline-flex; align-items: center; justify-content: flex-end;
    width: 28px; flex-shrink: 0;
    color: var(--muted); font-size: 11px; line-height: 1; white-space: nowrap;
  }
  .model-pill .opt-param { width: auto; text-align: left; opacity: 0.78; padding-left: 1px; }
  .model-sep { height: 1px; background: var(--border); margin: 4px 6px; }
  .model-add { color: var(--muted); }
  .model-add:hover { color: var(--white); }

  .head-right { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 8px; }
  .ctx-badge {
    font-family: var(--font-mono); font-size: 11px; color: var(--muted);
    background: var(--surface); border: 1px solid var(--border);
    padding: 4px 9px; border-radius: 7px;
  }
  .ctx-badge.warn { color: #fde68a; border-color: rgba(253, 230, 138, 0.45); }
  .ctx-badge.danger { color: #ff4d4d; border-color: rgba(255, 77, 77, 0.6); }

  /* Setup / Ollama Banner */
  #setup-banner {
    display: none; align-items: center; justify-content: space-between; gap: 12px;
    padding: 9px 18px; background: var(--surface); border-bottom: 1px solid var(--border);
    font-size: 13px; color: var(--text); flex-shrink: 0;
  }
  #setup-banner.show { display: flex; flex-wrap: wrap; }
  .setup-msg { display: flex; align-items: center; gap: 8px; color: #e4e4e7; }
  .setup-actions { display: flex; align-items: center; gap: 8px; }
  .setup-link {
    display: inline-block; padding: 5px 10px; border-radius: 7px; font-size: 12px;
    background: var(--white); color: #111; font-weight: 600; text-decoration: none;
  }
  .setup-link:hover { background: #e4e4e7; }
  .pull-bar {
    width: 140px; height: 6px; background: var(--surface-2); border-radius: 999px;
    overflow: hidden; border: 1px solid var(--border); display: none;
  }
  .pull-bar.show { display: block; }
  .pull-fill { height: 100%; width: 0%; background: var(--white); transition: width 0.2s; }

  /* Viewport & Empty-State Centering */
  #stage { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
  #scroll-area {
    flex: 1 1 0px; min-height: 0; overflow-y: auto; padding: 24px 20px 12px;
  }
  #chat-col { max-width: 760px; margin: 0 auto; display: flex; flex-direction: column; gap: 22px; }

  #welcome {
    position: absolute; left: 20px; right: 20px; bottom: calc(100% + 10px);
    max-width: 680px; margin: 0 auto; text-align: center;
    display: flex; flex-direction: column; align-items: center; gap: 8px;
    opacity: 1; transform: translateY(0);
    transition: opacity 0.22s ease, transform 0.28s cubic-bezier(0.22, 1, 0.36, 1), visibility 0.28s;
  }
  main:not(.is-empty) #welcome {
    opacity: 0; transform: translateY(-14px); visibility: hidden; pointer-events: none;
  }
  #welcome-mascot {
    width: clamp(72px, 11vh, 104px); height: clamp(72px, 11vh, 104px);
    position: relative; flex-shrink: 0;
  }
  .q-rotator { min-height: 36px; display: flex; align-items: center; justify-content: center; overflow: hidden; }
  #rotating-q {
    margin: 0; font-size: 26px; font-weight: 600; letter-spacing: -0.4px; color: var(--white);
    transition: opacity 0.28s cubic-bezier(0.22, 1, 0.36, 1),
                transform 0.28s cubic-bezier(0.22, 1, 0.36, 1),
                filter 0.28s ease;
  }
  #rotating-q.q-exit { opacity: 0; transform: translateY(-10px); filter: blur(3px); }
  #rotating-q.q-enter { opacity: 0; transform: translateY(10px); filter: blur(3px); transition: none; }
  #welcome p { margin: 0; color: var(--muted); font-size: 13px; }

  .empty-spacer {
    flex: 1 1 0px; min-height: 0; overflow: hidden;
    display: flex; flex-direction: column; align-items: center; padding: 0 20px;
    transition: flex-grow 0.5s cubic-bezier(0.22, 1, 0.36, 1);
  }
  main:not(.is-empty) .empty-spacer { flex-grow: 0; }
  .chips {
    display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; margin-top: 2px;
    max-width: 760px; opacity: 1; transform: translateY(0);
    transition: opacity 0.18s ease, transform 0.22s ease;
  }
  main:not(.is-empty) .chips { opacity: 0; transform: translateY(8px); pointer-events: none; }
  .chip {
    background: var(--surface); border: 1px solid var(--border); color: var(--muted);
    border-radius: 7px; padding: 7px 13px; font-size: 13px;
  }
  .chip:hover { color: var(--white); border-color: rgba(255,255,255,0.45); }
  .chip-edit { border-style: dashed; padding: 7px 11px; font-size: 12px; opacity: 0.75; }
  .chip-edit:hover { opacity: 1; }
  @media (prefers-reduced-motion: reduce) {
    .empty-spacer, #welcome, #rotating-q, .chips { transition: none !important; }
  }

  /* Messages */
  .msg-row { display: flex; flex-direction: column; animation: fadeIn 0.16s ease-out; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
  .msg-row.user { align-items: flex-end; }
  .user-bubble {
    max-width: 78%; background: var(--surface-2); border: 1px solid var(--border);
    padding: 10px 15px; border-radius: 16px 16px 4px 16px;
    line-height: 1.55; white-space: pre-wrap; word-break: break-word; color: var(--white);
  }
  .msg-row.assistant { align-items: flex-start; gap: 8px; }
  .asst-head { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); }
  .asst-head .m-mark { width: 14px; height: 14px; gap: 1px; }
  .asst-name { color: var(--white); font-weight: 600; }

  .steps-wrap { display: flex; flex-direction: column; gap: 6px; width: 100%; padding-left: 22px; }
  details.think-box {
    background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border);
    border-radius: 9px; padding: 7px 11px; font-size: 12px; color: var(--muted);
  }
  details.think-box summary {
    cursor: pointer; list-style: none; display: flex; align-items: center; gap: 8px;
    font-weight: 500; user-select: none; color: #d4d4d8;
  }
  details.think-box summary::-webkit-details-marker { display: none; }
  .tri-svg { transition: transform 0.15s ease, opacity 0.2s ease; color: var(--m-3); flex-shrink: 0; }
  .tri-svg.running { animation: pulseTri 0.9s ease-in-out infinite; }
  @keyframes pulseTri { 0%, 100% { opacity: 0.35; } 50% { opacity: 1; } }
  details[open] > summary .tri-svg { transform: rotate(90deg); }
  .think-body {
    margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border);
    font-family: var(--font-mono); font-size: 12px; line-height: 1.5;
    white-space: pre-wrap; color: #d4d4d8; max-height: 280px; overflow-y: auto;
  }

  /* Expandable Tool Cards (clean 1px border) */
  details.tool-pill {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 9px; padding: 7px 11px; font-size: 12px; color: var(--muted); width: 100%;
  }
  details.tool-pill summary {
    cursor: pointer; list-style: none; display: flex; align-items: center; gap: 8px;
    font-family: var(--font-mono); user-select: none;
  }
  details.tool-pill summary::-webkit-details-marker { display: none; }
  .tool-fn { font-weight: 600; flex-shrink: 0; color: var(--m-2); }
  .tool-preview { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; color: #c4c4cc; }
  .tool-detail {
    margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 6px;
  }
  .tool-sublabel { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
  .tool-pre {
    margin: 0; padding: 8px 10px; background: var(--code-bg); border: 1px solid var(--border);
    border-radius: 6px; font-family: var(--font-mono); font-size: 12px;
    overflow-x: auto; max-height: 220px; color: var(--text); white-space: pre-wrap; word-break: break-word;
  }

  /* Markdown Prose */
  .prose { padding-left: 22px; width: 100%; line-height: 1.65; word-break: break-word; }
  .prose > *:first-child { margin-top: 0; }
  .prose > *:last-child { margin-bottom: 0; }
  .prose p { margin: 0 0 10px; }
  .prose h1, .prose h2, .prose h3, .prose h4 { margin: 16px 0 8px; font-weight: 600; letter-spacing: -0.2px; color: var(--white); }
  .prose h1 { font-size: 1.35em; }
  .prose h2 { font-size: 1.18em; }
  .prose h3 { font-size: 1.05em; }
  .prose ul, .prose ol { margin: 6px 0 12px; padding-left: 22px; }
  .prose li { margin: 4px 0; }
  .prose li::marker { color: var(--msg-accent, var(--m-2)); font-weight: 600; }
  .prose strong { color: var(--white); font-weight: 600; }
  .prose a { color: var(--white); text-decoration: none; border-bottom: 1px solid rgba(255,255,255,0.4); }
  .prose a:hover { border-bottom-color: var(--white); }
  .prose blockquote {
    margin: 10px 0; padding: 6px 14px; border-left: 3px solid rgba(255,255,255,0.55);
    background: rgba(255, 255, 255, 0.04); color: #e4e4e7; border-radius: 0 6px 6px 0;
  }
  .prose hr { border: none; border-top: 1px solid var(--border); margin: 14px 0; }
  .prose code.inline {
    font-family: var(--font-mono); font-size: 0.88em; background: var(--surface-2);
    border: 1px solid var(--border); padding: 1px 6px; border-radius: 5px;
    color: var(--white); cursor: pointer; transition: border-color 0.15s;
  }
  .prose code.inline:hover { border-color: rgba(255,255,255,0.6); }
  .prose table { border-collapse: collapse; margin: 10px 0; width: 100%; font-size: 13px; }
  .prose th, .prose td { border: 1px solid var(--border); padding: 6px 10px; text-align: left; }
  .prose th { background: var(--surface); color: var(--white); }

  .code-block {
    margin: 10px 0; background: var(--code-bg); border: 1px solid var(--border);
    border-radius: 10px; overflow: hidden;
  }
  .code-bar {
    display: flex; justify-content: space-between; align-items: center;
    padding: 6px 12px; background: var(--surface); border-bottom: 1px solid var(--border);
    font-family: var(--font-mono); font-size: 11px; color: var(--muted);
  }
  .copy-btn, .speak-btn {
    background: transparent; border: 1px solid var(--border); color: var(--muted);
    padding: 2px 8px; border-radius: 5px; font-size: 11px; flex-shrink: 0;
    display: inline-flex; align-items: center; justify-content: center;
  }
  .speak-btn { padding: 3px 6px; min-width: 27px; min-height: 22px; }
  .copy-btn:hover, .speak-btn:hover { color: var(--white); border-color: rgba(255,255,255,0.55); }
  .speak-btn.speaking {
    color: var(--m-1); border-color: var(--m-3); background: var(--m-glow);
    animation: pulseSpeak 1.3s ease-in-out infinite;
  }
  @keyframes pulseSpeak {
    0%, 100% { box-shadow: 0 0 0 0 var(--m-glow); }
    50%      { box-shadow: 0 0 0 4px var(--m-glow); }
  }
  .tts-spin {
    width: 11px; height: 11px; border-radius: 50%;
    border: 1.5px solid rgba(255,255,255,0.2); border-top-color: var(--m-2);
    animation: spinRing 0.7s linear infinite; display: inline-block;
  }
  .code-block pre {
    margin: 0; padding: 12px 14px; overflow-x: auto;
    font-family: var(--font-mono); font-size: 13px; line-height: 1.5; color: var(--text);
  }

  .msg-actions {
    padding-left: 22px; display: flex; align-items: center; gap: 7px;
    opacity: 0.55; transition: opacity 0.15s; font-size: 11px; color: var(--muted);
  }
  .msg-row.assistant:hover .msg-actions, .msg-actions.tts-active { opacity: 1; }
  .stream-cursor {
    display: inline-block; width: 6px; height: 1.05em; vertical-align: text-bottom;
    margin-left: 3px; border-radius: 2px;
    background: var(--msg-accent, var(--m-2));
    animation: blinkCur 0.85s ease-in-out infinite;
  }
  @keyframes blinkCur { 0%, 100% { opacity: 0.9; } 50% { opacity: 0.18; } }

  /* Wave Loader + Circular Model Load Spinner */
  .css-loader { display: inline-flex; align-items: center; gap: 11px; padding: 4px 0 4px 22px; }
  .bars { display: inline-flex; align-items: flex-end; gap: 3px; height: 18px; }
  .bars span { width: 4px; height: 6px; border-radius: 2px; animation: modelWave 1.05s ease-in-out infinite; }
  .bars span:nth-child(1) { background: var(--m-1); animation-delay: 0s; }
  .bars span:nth-child(2) { background: var(--m-2); animation-delay: 0.12s; }
  .bars span:nth-child(3) { background: var(--m-3); animation-delay: 0.24s; }
  .bars span:nth-child(4) { background: var(--m-4); animation-delay: 0.36s; }
  .bars span:nth-child(5) { background: var(--m-5); animation-delay: 0.48s; }
  @keyframes modelWave { 0%, 100% { height: 5px; opacity: 0.45; } 50% { height: 18px; opacity: 1; } }
  .ring-spin {
    display: none; width: 16px; height: 16px; border-radius: 50%;
    border: 2px solid rgba(255, 255, 255, 0.14);
    border-top-color: var(--m-2); border-right-color: var(--m-3);
    animation: spinRing 0.75s linear infinite; flex-shrink: 0;
  }
  @keyframes spinRing { to { transform: rotate(360deg); } }
  .css-loader.is-loading .bars { display: none; }
  .css-loader.is-loading .ring-spin { display: inline-block; }
  .loader-sec {
    font-size: 13px; font-family: var(--font-mono); color: var(--muted);
    background: linear-gradient(110deg, var(--muted) 35%, var(--white) 48%, var(--m-1) 54%, var(--muted) 65%);
    background-size: 220% 100%;
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    animation: textShimmer 1.9s linear infinite;
  }
  @keyframes textShimmer {
    0%   { background-position: 150% 0; }
    100% { background-position: -50% 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    .loader-sec { background: none; -webkit-text-fill-color: currentColor; animation: none; }
    #main-pane { opacity: 1 !important; transform: none !important; transition: none !important; }
    #composer-bar-wrap, #plus-menu, .model-menu, #plus-btn svg, .plus-toggle, .plus-toggle::after,
    .chip-enter, .chip-leave, #soul-box, #chips-box, #settings-box, #pull-box, #models-box, #files-box,
    #drop-overlay, .drop-box, #drop-overlay.show .drop-box, #drop-overlay.show .drop-arrow {
      transition: none !important; animation: none !important;
    }
  }

  /* Composer */
  .composer-wrap { position: relative; z-index: 2; padding: 10px 20px 14px; flex-shrink: 0; }
  form.composer {
    position: relative;
    max-width: 760px; margin: 0 auto; background: var(--surface);
    border: 1px solid var(--border); border-radius: 14px;
    padding: 8px 10px; display: flex; flex-direction: column; gap: 0;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  form.composer:focus-within {
    border-color: rgba(255, 255, 255, 0.45);
    box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.06);
  }
  #composer-bar-wrap {
    display: grid; grid-template-rows: 0fr;
    opacity: 0; transform: translateY(4px);
    transition:
      grid-template-rows 0.2s cubic-bezier(0.22, 1, 0.36, 1) 0.05s,
      opacity 0.16s ease 0.04s,
      transform 0.2s cubic-bezier(0.22, 1, 0.36, 1) 0.04s;
  }
  #composer-bar-wrap.show {
    grid-template-rows: 1fr; opacity: 1; transform: translateY(0);
    transition:
      grid-template-rows 0.22s cubic-bezier(0.22, 1, 0.36, 1),
      opacity 0.18s ease,
      transform 0.22s cubic-bezier(0.22, 1, 0.36, 1);
  }
  .composer-bar-inner { overflow: hidden; min-height: 0; }
  #composer-bar {
    display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
    padding: 0 2px 8px; margin-bottom: 6px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  }
  .composer-row { display: flex; align-items: flex-end; gap: 8px; }
  #plus-btn {
    width: 34px; height: 34px; border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.14);
    background: transparent; color: var(--muted);
    padding: 0; display: grid; place-items: center; flex-shrink: 0;
    transition: color 0.15s, border-color 0.15s, background 0.15s;
  }
  #plus-btn svg { transition: transform 0.22s cubic-bezier(0.22, 1, 0.36, 1); }
  #plus-btn.open svg { transform: rotate(45deg); }
  #plus-btn:hover, #plus-btn.open {
    color: var(--white); border-color: rgba(255, 255, 255, 0.34); background: rgba(255, 255, 255, 0.06);
  }
  #plus-menu {
    position: absolute; left: 0; bottom: calc(100% + 8px);
    width: 290px; max-width: calc(100vw - 32px);
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 6px;
    box-shadow: 0 14px 36px rgba(0, 0, 0, 0.65);
    display: flex; flex-direction: column; gap: 2px; z-index: 30;
    opacity: 0; visibility: hidden; pointer-events: none;
    transform: translateY(8px) scale(0.97); transform-origin: bottom left;
    transition:
      opacity 0.18s ease,
      transform 0.22s cubic-bezier(0.22, 1, 0.36, 1),
      visibility 0s linear 0.2s;
  }
  #plus-menu.open {
    opacity: 1; visibility: visible; pointer-events: auto;
    transform: translateY(0) scale(1);
    transition:
      opacity 0.18s ease,
      transform 0.22s cubic-bezier(0.22, 1, 0.36, 1),
      visibility 0s;
  }
  .plus-sec {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--muted); padding: 6px 10px 3px;
  }
  .plus-item {
    width: 100%; display: flex; align-items: center; justify-content: space-between;
    gap: 10px; padding: 7px 10px; border: none; border-radius: 8px;
    background: transparent; color: var(--text); font-size: 12.5px; text-align: left;
    transition: background 0.14s ease, color 0.14s ease;
  }
  .plus-item:hover { background: var(--surface-2); color: var(--white); }
  .plus-item-left { display: flex; align-items: center; gap: 9px; min-width: 0; }
  .plus-item-meta { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
  .plus-item-sub { font-size: 10.5px; color: var(--muted); }
  .plus-toggle {
    width: 28px; height: 16px; border-radius: 5px;
    background: var(--surface-2); border: 1px solid var(--border);
    position: relative; flex-shrink: 0; transition: background 0.18s ease, border-color 0.18s ease;
  }
  .plus-toggle::after {
    content: ""; position: absolute; left: 2px; top: 2px;
    width: 10px; height: 10px; border-radius: 3px;
    background: var(--muted); transition: transform 0.2s cubic-bezier(0.22, 1, 0.36, 1), background 0.18s ease;
  }
  .plus-item.on .plus-toggle { background: var(--m-glow); border-color: var(--m-3); }
  .plus-item.on .plus-toggle::after { transform: translateX(12px); background: var(--m-1); }
  .plus-sep { height: 1px; background: var(--border); margin: 4px 6px; }
  .tool-chip, .att-chip {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 8px; border-radius: 6px; font-size: 11.5px;
    border: 1px solid var(--border); background: var(--surface-2);
    color: var(--text); max-width: 240px; white-space: nowrap; overflow: hidden;
    transition: background 0.15s ease, border-color 0.15s ease;
  }
  @keyframes chipIn {
    from { opacity: 0; transform: translateY(3px) scale(0.94); max-width: 0; padding-left: 0; padding-right: 0; margin-right: -6px; border-width: 0; }
    50%  { max-width: 240px; padding-left: 8px; padding-right: 8px; margin-right: 0; border-width: 1px; }
    to   { opacity: 1; transform: translateY(0) scale(1); max-width: 240px; }
  }
  @keyframes chipOut {
    from { opacity: 1; transform: translateY(0) scale(1); max-width: 240px; padding-left: 8px; padding-right: 8px; border-width: 1px; }
    60%  { opacity: 0; transform: translateY(3px) scale(0.92); }
    to   { opacity: 0; transform: translateY(3px) scale(0.90); max-width: 0; padding-left: 0; padding-right: 0; margin-right: -6px; border-width: 0; }
  }
  .chip-enter { animation: chipIn 0.2s cubic-bezier(0.22, 1, 0.36, 1); }
  .chip-leave { animation: chipOut 0.14s cubic-bezier(0.4, 0, 1, 1) forwards; pointer-events: none; }
  .tool-chip { border-color: var(--m-3); background: var(--m-glow); color: var(--m-1); }
  .att-chip-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .att-chip-size { font-size: 10px; color: var(--muted); flex-shrink: 0; }
  .att-chip.uploading { opacity: 0.72; }
  .chip-x {
    border: none; background: transparent; color: var(--muted);
    padding: 0 2px; font-size: 13px; line-height: 1; display: inline-flex; align-items: center;
  }
  .chip-x:hover { color: var(--white); }
  .user-att-row { display: flex; flex-wrap: wrap; gap: 5px; justify-content: flex-end; margin-bottom: 5px; max-width: 78%; }
  .user-att-pill {
    font-size: 11px; color: var(--muted); background: var(--surface-2);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 2px 8px; display: inline-flex; align-items: center; gap: 5px;
  }
  .file-dl-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
  .file-dl-pill {
    display: inline-flex; align-items: center; gap: 7px; padding: 6px 11px;
    border-radius: 8px; border: 1px solid var(--m-3); background: var(--surface-2);
    color: var(--m-1); font-size: 12px; text-decoration: none;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
  }
  .file-dl-pill:hover { background: var(--m-glow); border-color: var(--m-2); color: var(--white); }
  textarea#input {
    flex: 1; background: transparent; border: none; color: var(--white);
    caret-color: var(--m-2);
    font: inherit; font-size: 15px; line-height: 22px;
    height: 22px; min-height: 22px; max-height: 180px;
    padding: 0 2px; margin: 6px 0; resize: none; display: block;
  }
  textarea#input::selection { background: var(--m-glow); color: var(--white); }
  textarea#input:focus { outline: none; }
  textarea#input::placeholder { color: var(--muted); }
  #send-btn {
    width: 34px; height: 34px; border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.24);
    background: transparent; color: var(--white);
    padding: 0; display: grid; place-items: center; flex-shrink: 0;
  }
  #send-btn:hover { background: rgba(255, 255, 255, 0.08); border-color: rgba(255, 255, 255, 0.48); }
  #send-btn.stopping { border-color: rgba(255, 255, 255, 0.45); background: rgba(255, 255, 255, 0.08); }
  .hint { max-width: 760px; margin: 6px auto 0; text-align: center; font-size: 11px; color: var(--muted); }

  /* Drag-and-drop overlay */
  #drop-overlay {
    position: fixed; inset: 0; z-index: 60;
    background:
      radial-gradient(420px circle at var(--drop-x, 50%) var(--drop-y, 50%), var(--m-glow), transparent 68%),
      rgba(10, 10, 14, 0.84);
    backdrop-filter: blur(4px);
    display: flex; align-items: center; justify-content: center; padding: 24px;
    pointer-events: none;
    opacity: 0; visibility: hidden;
    transition: opacity 0.18s ease, visibility 0s linear 0.18s;
  }
  #drop-overlay.show {
    opacity: 1; visibility: visible;
    transition: opacity 0.18s ease, visibility 0s;
  }
  .drop-box {
    position: relative; min-width: 310px;
    border: 2px dashed var(--m-2); background: rgba(20, 20, 26, 0.94);
    border-radius: 18px; padding: 36px 46px; text-align: center;
    display: flex; flex-direction: column; align-items: center; gap: 10px;
    box-shadow: 0 0 0 6px var(--m-glow);
    transform: scale(0.93) translateY(8px);
    transition: transform 0.22s cubic-bezier(0.22, 1, 0.36, 1), border-color 0.18s ease, box-shadow 0.18s ease;
  }
  #drop-overlay.show .drop-box {
    transform: scale(1) translateY(0);
    animation: dropPulse 1.8s ease-in-out infinite;
  }
  #drop-overlay.on-target .drop-box {
    border-style: solid; border-color: var(--m-1);
    transform: scale(1.04) translateY(0);
    box-shadow: 0 0 0 10px var(--m-glow), 0 18px 44px rgba(0, 0, 0, 0.55);
  }
  .drop-icon-wrap {
    width: 52px; height: 52px; border-radius: 14px;
    display: grid; place-items: center;
    background: var(--m-glow); border: 1px solid var(--m-3);
    transition: transform 0.18s ease, background 0.18s ease;
  }
  #drop-overlay.show .drop-arrow {
    animation: dropBob 1.1s ease-in-out infinite;
  }
  #drop-overlay.on-target .drop-icon-wrap { transform: scale(1.08); }
  #drop-overlay.on-target .drop-arrow { animation-duration: 0.65s; }
  @keyframes dropBob {
    0%, 100% { transform: translateY(-2px); }
    50%      { transform: translateY(3px); }
  }
  @keyframes dropPulse {
    0%, 100% { box-shadow: 0 0 0 5px var(--m-glow); }
    50%      { box-shadow: 0 0 0 11px var(--m-glow); }
  }
  .drop-box b { color: var(--white); font-size: 16px; font-weight: 600; }
  .drop-box span { color: var(--muted); font-size: 12.5px; }

  /* Modals (Soul, Quick Buttons, Settings, Pull Model, Manage Models, Manage Files) */
  #soul-modal, #chips-modal, #settings-modal, #pull-modal, #models-modal, #files-modal {
    position: fixed; inset: 0; background: rgba(0,0,0,0.72); display: none;
    align-items: center; justify-content: center; padding: 20px; z-index: 40;
  }
  #soul-box, #chips-box, #settings-box, #pull-box, #models-box, #files-box {
    width: 100%; max-width: 640px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 18px; display: flex; flex-direction: column; gap: 14px;
    animation: modalIn 0.16s cubic-bezier(0.22, 1, 0.36, 1);
  }
  @keyframes modalIn {
    from { opacity: 0; transform: translateY(6px) scale(0.985); }
    to   { opacity: 1; transform: translateY(0) scale(1); }
  }
  #settings-box { max-width: 480px; }
  #pull-box { max-width: 440px; }
  #models-box { max-width: 560px; }
  #files-box { max-width: 600px; }
  .soul-tabs { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
  .soul-tab {
    padding: 5px 10px; border-radius: 7px; font-size: 12px;
    border: 1px solid var(--border); background: var(--bg); color: var(--muted);
    transition: border-color 0.15s, background 0.15s, color 0.15s;
  }
  .soul-tab:hover { color: var(--white); border-color: rgba(255, 255, 255, 0.35); }
  .soul-tab.active {
    background: var(--m-glow); border-color: var(--m-3); color: var(--m-1); font-weight: 500;
  }
  .soul-tab-add { border-style: dashed; }
  .soul-meta-row { display: flex; align-items: center; gap: 8px; }
  #soul-name {
    flex: 1; min-width: 0; background: var(--bg); color: var(--white);
    border: 1px solid var(--border); border-radius: 7px; padding: 6px 10px;
    font: inherit; font-size: 13px; transition: border-color 0.15s, box-shadow 0.15s;
  }
  #soul-text {
    width: 100%; height: 240px; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 12px;
    font-family: var(--font-mono); font-size: 13px; line-height: 1.5; resize: vertical;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  #soul-text:focus, #soul-name:focus {
    outline: none;
    border-color: rgba(255, 255, 255, 0.55);
    box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.08);
  }
  #chips-rows, #models-rows, #files-rows { display: flex; flex-direction: column; gap: 8px; max-height: 300px; overflow-y: auto; }
  .model-mgr-row {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 10px 12px; background: var(--bg); border: 1px solid var(--border); border-radius: 9px;
  }
  .model-mgr-info { min-width: 0; display: flex; flex-direction: column; gap: 3px; flex: 1; }
  .model-mgr-title { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--white); font-weight: 500; min-width: 0; }
  .model-mgr-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .model-mgr-tag {
    font-size: 10px; padding: 1px 6px; border-radius: 5px;
    background: var(--surface-2); border: 1px solid var(--border); color: var(--muted); flex-shrink: 0;
  }
  .model-mgr-meta { font-size: 11px; color: var(--muted); padding-left: 15px; }
  .model-mgr-actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  .model-mgr-actions button { padding: 5px 10px; font-size: 12px; }
  .btn-danger-arm { border-color: rgba(255, 77, 77, 0.7) !important; color: #ff8080 !important; background: rgba(255, 77, 77, 0.12) !important; }
  .models-status-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-height: 18px; font-size: 12px; color: var(--muted); }
  .chip-edit-row { display: grid; grid-template-columns: 160px 1fr auto; gap: 8px; align-items: center; }
  .chip-edit-row input, #pull-input, .setting-ctrl select, .setting-ctrl input[type="text"] {
    background: var(--bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 7px; padding: 7px 10px; font: inherit; font-size: 13px; min-width: 0;
  }
  .chip-edit-row input:focus, #pull-input:focus, .setting-ctrl select:focus, .setting-ctrl input[type="text"]:focus {
    outline: none; border-color: rgba(255,255,255,0.45);
  }
  .pull-presets { display: flex; flex-wrap: wrap; gap: 6px; }
  .pull-preset-btn {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 4px 8px; border-radius: 6px; font-size: 11.5px;
    background: var(--bg); color: var(--text); border: 1px solid var(--border);
    transition: border-color 0.15s, background 0.15s, color 0.15s;
  }
  .pull-preset-btn:hover { border-color: var(--m-3); background: var(--m-glow); color: var(--white); }
  .pull-preset-btn span { font-family: var(--font-mono); font-size: 10px; color: var(--muted); }
  .setting-row {
    display: grid; grid-template-columns: minmax(0, 1fr) 215px;
    align-items: center; gap: 14px;
  }
  .setting-meta { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .setting-meta b { font-size: 13px; color: var(--white); font-weight: 500; }
  .setting-meta span { font-size: 11px; color: var(--muted); }
  .setting-ctrl { display: flex; align-items: center; justify-content: flex-end; gap: 6px; min-width: 0; }
  .setting-ctrl select, .setting-ctrl input[type="text"] { flex: 1; width: 100%; max-width: 215px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .setting-range-wrap { gap: 10px; }
  .setting-ctrl input[type="range"] {
    flex: 1; min-width: 0; height: 4px; cursor: pointer;
    accent-color: var(--m-2); background: var(--bg);
  }
  .setting-val {
    min-width: 26px; text-align: right; font-family: var(--font-mono);
    font-size: 12.5px; color: var(--white); font-weight: 500;
  }
  .chip-del { padding: 6px 9px; color: var(--muted); }
  .modal-row { display: flex; justify-content: flex-end; gap: 8px; }
  .modal-split { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
  .btn-white { background: var(--white); color: #111; border: none; font-weight: 600; }
  .btn-white:hover { background: #e4e4e7; }

  @media (max-width: 720px) {
    aside { position: fixed; top: 0; bottom: 0; left: 0; z-index: 25; }
    .ctx-badge { display: none; }
    .chip-edit-row, .setting-row { grid-template-columns: 1fr; }
    .setting-ctrl { justify-content: flex-start; }
    .setting-ctrl select { max-width: none; }
    .model-mgr-row { flex-direction: column; align-items: stretch; }
    .model-mgr-actions { justify-content: flex-end; }
    #plus-menu { width: calc(100vw - 44px); }
  }
</style>
</head>
<body>
  <aside id="sidebar" class="collapsed">
    <div class="side-head">
      <div class="brand">
        <div class="m-mark" id="brand-mark"><span></span><span></span><span></span><span></span><span></span></div>
        <span>local agent</span>
      </div>
    </div>
    <div class="side-actions">
      <button class="btn-new" onclick="newChat()">+ New chat</button>
      <button onclick="openSoul()" title="Edit soul.md">Soul</button>
    </div>
    <div class="side-label">Saved chats</div>
    <div id="chat-list"></div>
    <div class="side-foot">
      <span>__OS_NAME__</span>
      <button type="button" class="quit-btn" onclick="quitServer()" title="Stop local server">Quit app</button>
    </div>
  </aside>

  <main id="main-pane" class="is-empty">
    <header>
      <div class="head-left">
        <button class="icon-btn" onclick="toggleSidebar()" title="Toggle sidebar">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/></svg>
        </button>
        <div class="model-wrap">
          <button type="button" class="model-pill" id="model-btn" onclick="toggleModelMenu(event)">
            <span class="dot-live"></span>
            <span id="model-label">__MODEL__</span>
            <span id="model-param" class="opt-param" style="display:none;"></span>
            <svg class="caret-svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
          </button>
          <div class="model-menu" id="model-menu"></div>
        </div>
      </div>
      <div class="head-right">
        <span id="ctx-badge" class="ctx-badge" title="Context tokens used in this chat">ctx 0 / __CTX_K__</span>
        <button type="button" class="icon-btn" onclick="openSettings()" title="Settings">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        </button>
      </div>
    </header>

    <div id="setup-banner">
      <div class="setup-msg">
        <span id="setup-text">Checking Ollama...</span>
        <div class="pull-bar" id="pull-bar"><div class="pull-fill" id="pull-fill"></div></div>
      </div>
      <div class="setup-actions" id="setup-actions"></div>
    </div>

    <div id="stage">
      <div id="scroll-area">
        <div id="chat-col"></div>
      </div>

      <div class="composer-wrap">
        <div id="welcome">
          <div id="welcome-mascot" title="LFM3 mascot (click to poke)"></div>
          <div class="q-rotator" aria-live="polite">
            <h1 id="rotating-q">What are we working on?</h1>
          </div>
          <p>Running locally via Ollama <b id="welcome-model">__MODEL__</b></p>
        </div>

        <form class="composer" onsubmit="event.preventDefault(); handleSendOrStop();">
          <div id="composer-bar-wrap" aria-hidden="true">
            <div class="composer-bar-inner">
              <div id="composer-bar"></div>
            </div>
          </div>
          <div class="composer-row">
            <button type="button" id="plus-btn" onclick="togglePlusMenu(event)" title="Attach files or toggle tools (+)">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
            </button>
            <div id="plus-menu" role="menu" aria-label="Tools and attachments"></div>
            <input type="file" id="file-picker" multiple hidden />
            <textarea id="input" rows="1" spellcheck="false" placeholder="Ask anything or drop files..."></textarea>
            <button type="button" id="send-btn" onclick="handleSendOrStop()" title="Send"></button>
          </div>
        </form>
        <div class="hint">Enter to send · Shift+Enter for newline · Drop files or click + · Chats saved in __CHATS_PATH__</div>
      </div>

      <div class="empty-spacer" id="empty-spacer">
        <div class="chips" id="chips-row"></div>
      </div>
    </div>
  </main>

  <div id="soul-modal">
    <div id="soul-box">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <b>Souls (up to 4 presets, applies live)</b>
        <span style="color:var(--muted);font-size:12px;">__SOUL_PATH__</span>
      </div>
      <div id="soul-tabs" class="soul-tabs"></div>
      <div class="soul-meta-row">
        <input id="soul-name" type="text" maxlength="24" spellcheck="false" placeholder="Soul name..." oninput="updateActiveSoulName(this.value)" />
        <button type="button" onclick="restoreOriginalSoul()" title="Restore default original soul">Restore original</button>
        <button type="button" onclick="deleteCurrentSoul()" title="Delete selected soul">Delete</button>
      </div>
      <textarea id="soul-text" spellcheck="false" placeholder="Write instructions for this soul..."></textarea>
      <div class="modal-row">
        <button type="button" onclick="closeSoul()">Cancel</button>
        <button type="button" class="btn-white" onclick="saveSoul()">Save &amp; use</button>
      </div>
    </div>
  </div>

  <div id="chips-modal">
    <div id="chips-box">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <b>Edit quick buttons</b>
        <span style="color:var(--muted);font-size:12px;">Shown under new chat bar</span>
      </div>
      <div id="chips-rows"></div>
      <div class="modal-split">
        <div style="display:flex;gap:8px;">
          <button type="button" onclick="addChipRow()">+ Add button</button>
          <button type="button" onclick="resetChipRows()">Reset defaults</button>
        </div>
        <div class="modal-row">
          <button type="button" onclick="closeChipsModal()">Cancel</button>
          <button type="button" class="btn-white" onclick="saveChipsModal()">Save</button>
        </div>
      </div>
    </div>
  </div>

  <div id="settings-modal">
    <div id="settings-box">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <b>Settings</b>
        <span style="color:var(--muted);font-size:12px;">Saved locally</span>
      </div>
      <div class="setting-row">
        <div class="setting-meta">
          <b>Default model</b>
          <span>Used when opening a new session</span>
        </div>
        <div class="setting-ctrl">
          <select id="set-default-model"></select>
        </div>
      </div>
      <div class="setting-row">
        <div class="setting-meta">
          <b>Response sound</b>
          <span>Plays when window is out of focus</span>
        </div>
        <div class="setting-ctrl">
          <select id="set-sound">
            <option value="chime">Chime</option>
            <option value="ping">Ping</option>
            <option value="double">Double tap</option>
            <option value="chord">Warm chord</option>
            <option value="off">Off</option>
          </select>
          <button type="button" onclick="testSound()">Test</button>
        </div>
      </div>
      <div class="setting-row">
        <div class="setting-meta">
          <b>Max tool steps</b>
          <span>Tool calls allowed per reply</span>
        </div>
        <div class="setting-ctrl setting-range-wrap">
          <input type="range" id="set-max-steps" min="2" max="40" step="1" value="8" oninput="updateStepsLabel(this.value)" />
          <span class="setting-val" id="set-max-steps-val">8</span>
        </div>
      </div>
      <div class="setting-row">
        <div class="setting-meta">
          <b>Speech model</b>
          <span>Local Ollama TTS model</span>
        </div>
        <div class="setting-ctrl">
          <select id="set-tts-model"></select>
        </div>
      </div>
      <div class="setting-row">
        <div class="setting-meta">
          <b>Speech voice</b>
          <span>Optional voice name (default if empty)</span>
        </div>
        <div class="setting-ctrl">
          <input type="text" id="set-tts-voice" spellcheck="false" placeholder="default (or Zoey, Freya...)" />
        </div>
      </div>
      <div class="modal-split">
        <button type="button" onclick="closeSettings(); openModelsModal();">Manage models...</button>
        <div class="modal-row">
          <button type="button" onclick="closeSettings()">Cancel</button>
          <button type="button" class="btn-white" onclick="saveSettings()">Save</button>
        </div>
      </div>
    </div>
  </div>

  <div id="pull-modal">
    <div id="pull-box">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <b>Download Ollama model</b>
        <span style="color:var(--muted);font-size:12px;">Pick light 2B–4B or type larger tag</span>
      </div>
      <input id="pull-input" type="text" placeholder="e.g. lfm3:1.7b, granite4.2:3b, qwen3.5:4b, gemma4:e2b (or :9b, :12b)" onkeydown="if(event.key==='Enter'){event.preventDefault();submitPullModal();}" />
      <div class="pull-presets">
        <button type="button" class="pull-preset-btn" onclick="pickPullModel('lfm3:1.7b')">lfm3:1.7b <span>1.7B</span></button>
        <button type="button" class="pull-preset-btn" onclick="pickPullModel('granite4.2:3b')">granite4.2:3b <span>3B</span></button>
        <button type="button" class="pull-preset-btn" onclick="pickPullModel('qwen3.5:4b')">qwen3.5:4b <span>4B</span></button>
        <button type="button" class="pull-preset-btn" onclick="pickPullModel('gemma4:e2b')">gemma4:e2b <span>2B eff</span></button>
        <button type="button" class="pull-preset-btn" onclick="pickPullModel('lfm3-audio:2b')">lfm3-audio:2b <span>2B TTS</span></button>
        <button type="button" class="pull-preset-btn" onclick="pickPullModel('phonon')">phonon <span>31M TTS</span></button>
      </div>
      <div class="modal-row">
        <button type="button" onclick="closePullModal()">Cancel</button>
        <button type="button" class="btn-white" onclick="submitPullModal()">Download</button>
      </div>
    </div>
  </div>

  <div id="models-modal">
    <div id="models-box">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">
        <b>Manage installed models</b>
        <span id="models-summary" style="color:var(--muted);font-size:12px;"></span>
      </div>
      <div class="models-status-row" id="models-status-row" style="display:none;">
        <span id="models-status-text"></span>
        <div class="pull-bar" id="models-pull-bar" style="width:160px;"><div class="pull-fill" id="models-pull-fill"></div></div>
      </div>
      <div id="models-rows"></div>
      <div class="modal-split">
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button type="button" id="models-update-all-btn" onclick="updateAllModels()">Update all</button>
          <button type="button" onclick="closeModelsModal(); openPullModal();">+ Download model</button>
        </div>
        <div class="modal-row">
          <button type="button" class="btn-white" onclick="closeModelsModal()">Done</button>
        </div>
      </div>
    </div>
  </div>

  <div id="files-modal">
    <div id="files-box">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">
        <b>Manage attached & generated files</b>
        <span id="files-summary" style="color:var(--muted);font-size:12px;"></span>
      </div>
      <div id="files-rows"></div>
      <div class="modal-split">
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button type="button" id="files-delete-all-btn" onclick="deleteAllFilesFromModal()">Delete all</button>
          <button type="button" onclick="closeFilesModal(); triggerFilePicker();">+ Attach files</button>
        </div>
        <div class="modal-row">
          <button type="button" class="btn-white" onclick="closeFilesModal()">Done</button>
        </div>
      </div>
    </div>
  </div>

  <div id="drop-overlay" aria-hidden="true">
    <div class="drop-box">
      <div class="drop-icon-wrap">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="var(--m-2)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <g class="drop-arrow"><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></g>
        </svg>
      </div>
      <b class="drop-title">Drop files anywhere to attach</b>
      <span>Markdown (.md), TXT, MediaWiki (.wiki) · max 25 MB each</span>
    </div>
  </div>

<script src="/lfm3-mascot.js"></script>
<script>
const MAX_CTX = __NUM_CTX__;
let currentChatId = null;
let draftChatId = null;
let currentModel = "__MODEL__";
let availableModels = ["__MODEL__"];
let installedModels = [];
let installedModelDetails = [];
let modelParams = {};
let ollamaRunning = true;
let pullingModel = false;
let toolsEnabled = localStorage.getItem("local_agent_tools") === "1";
let activePacks = { web: false, files: false, system: toolsEnabled };
let pendingFiles = [];
let busy = false;
let currentAbort = null;

const PACK_META = [
  { id: "web", label: "Web Search", sub: "DuckDuckGo search & read URL" },
  { id: "files", label: "Docs & Files", sub: "Read & edit .md, .txt, .wiki files" },
  { id: "system", label: "Local System", sub: "Shell command & workspace files" },
];

const SVG_ARROW_UP = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>`;
const SVG_STOP     = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>`;
const SVG_TRIANGLE = `<svg class="tri-svg" width="11" height="11" viewBox="0 0 16 16" fill="currentColor"><path d="M5.5 3.5l6 4.5-6 4.5z"/></svg>`;
const SVG_CLOSE    = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6l-12 12"/></svg>`;
const SVG_CHECK    = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>`;
const SVG_SPEAKER  = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>`;

const MODEL_PALETTES = {
  "lfm":     ["#ede9fe", "#c4b5fd", "#a78bfa", "#8b5cf6", "#7c3aed"],
  "granite": ["#dbeafe", "#93c5fd", "#60a5fa", "#3b82f6", "#2563eb"],
  "gemma":   ["#e0f2fe", "#7dd3fc", "#38bdf8", "#0ea5e9", "#0284c7"],
  "qwen":    ["#fae8ff", "#f0abfc", "#e879f9", "#d946ef", "#c026d3"],
};
const DEFAULT_PALETTE = ["#fafafa", "#e4e4e7", "#d4d4d8", "#a1a1aa", "#71717a"];

function getModelPalette(name) {
  const n = (name || "").toLowerCase();
  if (n.includes("lfm")) return MODEL_PALETTES["lfm"];
  if (n.includes("granite") || n.includes("ganite")) return MODEL_PALETTES["granite"];
  if (n.includes("gemma")) return MODEL_PALETTES["gemma"];
  if (n.includes("qwen")) return MODEL_PALETTES["qwen"];
  return DEFAULT_PALETTE;
}

const CLIENT_KNOWN_PARAMS = {
  "lfm3": "1.7B",
  "lfm3:1.7b": "1.7B",
  "granite4.2:3b": "3B",
  "gemma4:e2b": "2B eff",
  "gemma4:e4b": "4B eff",
  "qwen3.5:0.8b": "0.8B",
  "qwen3.5:2b": "2B",
  "qwen3.5:4b": "4B",
  "lfm3-audio:2b": "2B",
  "phonon": "31M",
};

function getModelParamLabel(name) {
  const n = String(name || "").trim();
  if (!n) return "";
  if (typeof modelParams !== "undefined" && modelParams && modelParams[n]) return modelParams[n];
  if (typeof installedModelDetails !== "undefined" && Array.isArray(installedModelDetails)) {
    const det = installedModelDetails.find(d => d && (d.name === n || d.raw_name === n));
    if (det && det.parameter_size) return det.parameter_size;
  }
  const low = n.toLowerCase();
  if (CLIENT_KNOWN_PARAMS[low]) return CLIENT_KNOWN_PARAMS[low];
  const colonIdx = low.indexOf(":");
  if (colonIdx >= 0) {
    const tag = low.slice(colonIdx + 1);
    const edgeM = tag.match(/(?:^|-)e(\d+(?:\.\d+)?b)(?:$|-)/i);
    if (edgeM) return edgeM[1].toUpperCase() + " eff";
    const sizeM = tag.match(/(?:^|-)(\d+(?:\.\d+)?[bm])(?:$|-)/i);
    if (sizeM) return sizeM[1].toUpperCase();
  }
  const base = colonIdx >= 0 ? low.slice(0, colonIdx) : low;
  if (CLIENT_KNOWN_PARAMS[base]) return CLIENT_KNOWN_PARAMS[base];
  return "";
}

let welcomeMascot = null;

function applyModelTheme(modelName) {
  const pal = getModelPalette(modelName);
  const root = document.documentElement.style;
  pal.forEach((col, idx) => root.setProperty(`--m-${idx + 1}`, col));
  root.setProperty("--m-glow", pal[2] + "24");
  document.getElementById("model-label").textContent = modelName;
  const mpEl = document.getElementById("model-param");
  if (mpEl) {
    const pLbl = getModelParamLabel(modelName);
    mpEl.textContent = pLbl;
    mpEl.style.display = pLbl ? "" : "none";
  }
  const wm = document.getElementById("welcome-model");
  if (wm) wm.textContent = modelName;
  if (welcomeMascot && typeof welcomeMascot.setRingColors === "function") {
    welcomeMascot.setRingColors(pal[3], pal[4]);
  }
}

function isModelInstalled(name) {
  if (!name) return false;
  if (name.includes(":")) {
    return installedModels.some(m => m === name || m === name + ":latest" || (name === "lfm3:1.7b" && m === "lfm3"));
  }
  return installedModels.some(m => m === name || m.startsWith(name + ":"));
}

function updateSetupBanner() {
  const banner = document.getElementById("setup-banner");
  const textEl = document.getElementById("setup-text");
  const actEl = document.getElementById("setup-actions");
  const bar = document.getElementById("pull-bar");
  if (pullingModel) return;

  bar.classList.remove("show");
  actEl.innerHTML = "";

  if (!ollamaRunning) {
    banner.classList.add("show");
    textEl.textContent = "Ollama is not running yet. Install or launch Ollama to run models locally.";
    const dl = document.createElement("a");
    dl.className = "setup-link";
    dl.href = "https://ollama.com/download";
    dl.target = "_blank";
    dl.rel = "noopener";
    dl.textContent = "Get Ollama";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "Start / Retry";
    retry.onclick = async () => {
      retry.disabled = true;
      retry.textContent = "Checking...";
      await fetch("/api/ollama/start", { method: "POST" }).catch(() => {});
      await loadModels();
      retry.disabled = false;
    };
    actEl.appendChild(dl);
    actEl.appendChild(retry);
    return;
  }

  if (!isModelInstalled(currentModel)) {
    banner.classList.add("show");
    textEl.textContent = `Model "${currentModel}" is not downloaded on this PC yet.`;
    const pullBtn = document.createElement("button");
    pullBtn.type = "button";
    pullBtn.className = "btn-white";
    pullBtn.textContent = `Pull ${currentModel}`;
    pullBtn.onclick = () => pullModel(currentModel);
    actEl.appendChild(pullBtn);
    return;
  }

  banner.classList.remove("show");
}

async function streamPullRequest(modelName, onProgress) {
  const res = await fetch("/api/pull", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: modelName }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const ln of lines) {
      if (!ln.trim()) continue;
      const ev = JSON.parse(ln);
      if (ev.error) throw new Error(ev.error);
      if (onProgress) onProgress(ev);
    }
  }
}

async function pullModel(modelName, switchChatModel = true) {
  if (pullingModel) return;
  pullingModel = true;
  const banner = document.getElementById("setup-banner");
  const textEl = document.getElementById("setup-text");
  const actEl = document.getElementById("setup-actions");
  const bar = document.getElementById("pull-bar");
  const fill = document.getElementById("pull-fill");

  banner.classList.add("show");
  actEl.innerHTML = "";
  bar.classList.add("show");
  fill.style.width = "0%";
  textEl.textContent = `Downloading ${modelName}...`;

  try {
    await streamPullRequest(modelName, (ev) => {
      if (ev.total && ev.completed) {
        const pct = Math.min(100, Math.round((ev.completed / ev.total) * 100));
        fill.style.width = pct + "%";
        textEl.textContent = `Downloading ${modelName}: ${pct}%`;
      } else if (ev.status) {
        textEl.textContent = `${modelName}: ${ev.status}`;
      }
    });
  } catch (err) {
    textEl.textContent = `Failed to pull ${modelName}: ${err.message || err}`;
    pullingModel = false;
    return;
  }

  pullingModel = false;
  if (switchChatModel) {
    currentModel = modelName;
    modelChosenByUser = true;
  }
  await loadModels(false);
}

let uploadNotice = "";
let uploadNoticeTimer = null;

function setUploadNotice(msg) {
  uploadNotice = msg || "";
  if (uploadNoticeTimer) { clearTimeout(uploadNoticeTimer); uploadNoticeTimer = null; }
  renderComposerBar();
  if (uploadNotice) {
    uploadNoticeTimer = setTimeout(() => { uploadNotice = ""; renderComposerBar(); }, 5000);
  }
}

function getEnabledPacksList() {
  return PACK_META.map(p => p.id).filter(id => activePacks[id]);
}

function syncPlusMenuState() {
  const menu = document.getElementById("plus-menu");
  if (!menu) return;
  const items = menu.querySelectorAll(".plus-item[data-pack-id]");
  if (!items.length) {
    renderPlusMenu();
    return;
  }
  for (const item of items) {
    item.classList.toggle("on", !!activePacks[item.dataset.packId]);
  }
  const voiceItem = menu.querySelector(".plus-item[data-tts-auto]");
  if (voiceItem) {
    const ttsOn = typeof appSettings !== "undefined" && !!appSettings.tts_auto;
    const ttsModel = (typeof appSettings !== "undefined" && appSettings.tts_model) || "lfm3-audio:2b";
    voiceItem.classList.toggle("on", ttsOn);
    const sub = voiceItem.querySelector(".plus-item-sub");
    if (sub) sub.textContent = `Speak via ${ttsModel}`;
  }
}

function togglePack(pack) {
  if (!(pack in activePacks)) return;
  activePacks[pack] = !activePacks[pack];
  if (pack === "system") {
    toolsEnabled = activePacks.system;
    localStorage.setItem("local_agent_tools", toolsEnabled ? "1" : "0");
  }
  syncPlusMenuState();
  renderComposerBar();
}

function closePlusMenu() {
  const menu = document.getElementById("plus-menu");
  const btn = document.getElementById("plus-btn");
  if (menu) menu.classList.remove("open");
  if (btn) btn.classList.remove("open");
}

function togglePlusMenu(e) {
  if (e) e.stopPropagation();
  const menu = document.getElementById("plus-menu");
  const btn = document.getElementById("plus-btn");
  const modelMenu = document.getElementById("model-menu");
  if (modelMenu) modelMenu.classList.remove("open");
  if (!menu || !btn) return;
  const opening = !menu.classList.contains("open");
  if (opening) {
    if (!menu.querySelector(".plus-item[data-pack-id]")) renderPlusMenu();
    else syncPlusMenuState();
    menu.classList.add("open");
    btn.classList.add("open");
  } else {
    closePlusMenu();
  }
}

function triggerFilePicker() {
  closePlusMenu();
  const picker = document.getElementById("file-picker");
  if (picker) picker.click();
}

function renderPlusMenu() {
  const menu = document.getElementById("plus-menu");
  if (!menu) return;
  menu.innerHTML = "";

  const secAttach = document.createElement("div");
  secAttach.className = "plus-sec";
  secAttach.textContent = "Files";
  menu.appendChild(secAttach);

  const attachBtn = document.createElement("button");
  attachBtn.type = "button";
  attachBtn.className = "plus-item";
  attachBtn.innerHTML = `
    <span class="plus-item-left">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
      <span class="plus-item-meta">
        <span>Attach files...</span>
        <span class="plus-item-sub">MediaWiki, Markdown, TXT</span>
      </span>
    </span>
  `;
  attachBtn.onclick = (e) => { e.stopPropagation(); triggerFilePicker(); };
  menu.appendChild(attachBtn);

  const mgrBtn = document.createElement("button");
  mgrBtn.type = "button";
  mgrBtn.className = "plus-item";
  mgrBtn.innerHTML = `
    <span class="plus-item-left">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
      <span class="plus-item-meta">
        <span>Manage saved files...</span>
        <span class="plus-item-sub">Inspect or delete stored files</span>
      </span>
    </span>
  `;
  mgrBtn.onclick = (e) => { e.stopPropagation(); closePlusMenu(); openFilesModal(); };
  menu.appendChild(mgrBtn);

  const sep = document.createElement("div");
  sep.className = "plus-sep";
  menu.appendChild(sep);

  const secTools = document.createElement("div");
  secTools.className = "plus-sec";
  secTools.textContent = "Tools (0 tokens when off)";
  menu.appendChild(secTools);

  for (const p of PACK_META) {
    const item = document.createElement("button");
    item.type = "button";
    item.dataset.packId = p.id;
    item.className = "plus-item" + (activePacks[p.id] ? " on" : "");
    item.innerHTML = `
      <span class="plus-item-left">
        <span class="plus-item-meta">
          <span class="plus-item-title"></span>
          <span class="plus-item-sub"></span>
        </span>
      </span>
      <span class="plus-toggle"></span>
    `;
    item.querySelector(".plus-item-title").textContent = p.label;
    item.querySelector(".plus-item-sub").textContent = p.sub;
    item.onclick = (e) => { e.stopPropagation(); togglePack(p.id); };
    menu.appendChild(item);
  }

  const sepVoice = document.createElement("div");
  sepVoice.className = "plus-sep";
  menu.appendChild(sepVoice);

  const secVoice = document.createElement("div");
  secVoice.className = "plus-sec";
  secVoice.textContent = "Voice (0 tokens to chat)";
  menu.appendChild(secVoice);

  const ttsAutoOn = typeof appSettings !== "undefined" && !!appSettings.tts_auto;
  const ttsModelName = (typeof appSettings !== "undefined" && appSettings.tts_model) || "lfm3-audio:2b";
  const voiceItem = document.createElement("button");
  voiceItem.type = "button";
  voiceItem.dataset.ttsAuto = "1";
  voiceItem.className = "plus-item" + (ttsAutoOn ? " on" : "");
  voiceItem.innerHTML = `
    <span class="plus-item-left">
      <span class="plus-item-meta">
        <span class="plus-item-title">Auto-read replies</span>
        <span class="plus-item-sub"></span>
      </span>
    </span>
    <span class="plus-toggle"></span>
  `;
  voiceItem.querySelector(".plus-item-sub").textContent = `Speak via ${ttsModelName}`;
  voiceItem.onclick = (e) => { e.stopPropagation(); toggleTtsAuto(); };
  menu.appendChild(voiceItem);
}

function fmtFileSize(bytes) {
  if (bytes === null || bytes === undefined || bytes < 0) return "";
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1).replace(/\.0$/, "") + " MB";
  if (bytes >= 1024) return Math.max(1, Math.round(bytes / 1024)) + " KB";
  return bytes + " B";
}

function renderComposerBar() {
  const wrap = document.getElementById("composer-bar-wrap");
  const bar = document.getElementById("composer-bar");
  if (!bar) return;

  function markChipLeaving(el, key) {
    if (!el || el.dataset.leaving === "1") return;
    const k = key || el.dataset.chipKey || "";
    if (k) el.dataset.leavingKey = k;
    delete el.dataset.chipKey;
    el.dataset.leaving = "1";
    el.classList.remove("chip-enter");
    el.classList.add("chip-leave");
    setTimeout(() => {
      if (el.isConnected && el.dataset.leaving === "1") el.remove();
    }, 160);
  }

  const enabledIds = getEnabledPacksList();
  const ttsAuto = typeof appSettings !== "undefined" && !!appSettings.tts_auto;
  const hasAny = enabledIds.length > 0 || ttsAuto || pendingFiles.length > 0 || !!uploadNotice;

  if (!hasAny) {
    if (wrap) {
      wrap.classList.remove("show");
      wrap.setAttribute("aria-hidden", "true");
    }
    bar.classList.remove("show");
    for (const el of Array.from(bar.children)) {
      markChipLeaving(el, el.dataset.chipKey);
    }
    return;
  }

  const existing = new Map();
  for (const el of Array.from(bar.children)) {
    if (el.dataset.leaving === "1") continue;
    const k = el.dataset.chipKey;
    if (k) existing.set(k, el);
    else el.remove();
  }

  const desired = [];
  for (const id of enabledIds) {
    const meta = PACK_META.find(p => p.id === id);
    const key = "pack:" + id;
    let chip = existing.get(key);
    if (!chip) {
      chip = document.createElement("span");
      chip.className = "tool-chip chip-enter";
      chip.dataset.chipKey = key;
      const txt = document.createElement("span");
      txt.textContent = meta ? meta.label : id;
      const x = document.createElement("button");
      x.type = "button";
      x.className = "chip-x";
      x.title = "Disable tool";
      x.innerHTML = SVG_CLOSE;
      x.onclick = () => togglePack(id);
      chip.appendChild(txt);
      chip.appendChild(x);
    } else {
      existing.delete(key);
    }
    desired.push(chip);
  }

  if (ttsAuto) {
    const key = "tts:auto";
    let chip = existing.get(key);
    const ttsModel = (appSettings && appSettings.tts_model) || "lfm3-audio:2b";
    if (!chip) {
      chip = document.createElement("span");
      chip.className = "tool-chip chip-enter";
      chip.dataset.chipKey = key;
      const txt = document.createElement("span");
      txt.className = "voice-chip-lbl";
      const x = document.createElement("button");
      x.type = "button";
      x.className = "chip-x";
      x.title = "Turn off auto-read";
      x.innerHTML = SVG_CLOSE;
      x.onclick = () => toggleTtsAuto();
      chip.appendChild(txt);
      chip.appendChild(x);
    } else {
      existing.delete(key);
    }
    const warmState = (typeof ttsWarmState !== "undefined") ? ttsWarmState : "";
    const stateSuffix = warmState === "loading" ? " · loading..." : (warmState === "ready" ? " · ready" : "");
    chip.querySelector(".voice-chip-lbl").textContent = `Voice · ${ttsModel}${stateSuffix}`;
    desired.push(chip);
  }

  pendingFiles.forEach((f) => {
    const key = "file:" + f.name;
    let chip = existing.get(key);
    if (!chip) {
      chip = document.createElement("span");
      chip.className = "att-chip chip-enter";
      chip.dataset.chipKey = key;
      const nm = document.createElement("span");
      nm.className = "att-chip-name";
      const sz = document.createElement("span");
      sz.className = "att-chip-size";
      chip.appendChild(nm);
      chip.appendChild(sz);
    } else {
      existing.delete(key);
    }
    chip.classList.toggle("uploading", !!f.uploading);
    chip.title = f.outline || f.name;
    chip.querySelector(".att-chip-name").textContent = f.name;
    chip.querySelector(".att-chip-size").textContent = f.uploading ? "uploading..." : fmtFileSize(f.size);
    let x = chip.querySelector(".chip-x");
    if (!f.uploading) {
      if (!x) {
        x = document.createElement("button");
        x.type = "button";
        x.className = "chip-x";
        x.title = "Remove & delete uploaded file";
        x.innerHTML = SVG_CLOSE;
        chip.appendChild(x);
      }
      x.onclick = () => removePendingFile(pendingFiles.indexOf(f));
    } else if (x) {
      x.remove();
    }
    desired.push(chip);
  });

  if (uploadNotice) {
    const key = "notice";
    let errChip = existing.get(key);
    if (!errChip) {
      errChip = document.createElement("span");
      errChip.className = "att-chip chip-enter";
      errChip.dataset.chipKey = key;
      errChip.style.cssText = "border-color:rgba(255,77,77,0.55);color:#ff9b9b;";
    } else {
      existing.delete(key);
    }
    errChip.textContent = uploadNotice;
    desired.push(errChip);
  }

  const desiredKeys = new Set(desired.map(d => d.dataset.chipKey).filter(Boolean));
  for (const el of Array.from(bar.children)) {
    if (el.dataset.leaving === "1" && desiredKeys.has(el.dataset.leavingKey || "")) {
      el.remove();
    }
  }

  for (const [oldKey, stale] of existing) {
    markChipLeaving(stale, oldKey);
  }

  let cursor = bar.firstChild;
  for (const el of desired) {
    while (cursor && cursor.dataset && cursor.dataset.leaving === "1") {
      cursor = cursor.nextSibling;
    }
    if (cursor === el) {
      cursor = cursor.nextSibling;
    } else {
      bar.insertBefore(el, cursor);
    }
  }

  if (wrap) {
    wrap.classList.add("show");
    wrap.setAttribute("aria-hidden", "false");
  }
  bar.classList.add("show");
}

async function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  let attachedAny = false;
  for (const file of files) {
    const cid = currentChatId || draftChatId || "";
    const placeholder = { name: file.name, size: file.size, outline: "", uploading: true };
    pendingFiles.push(placeholder);
    renderComposerBar();
    try {
      const qs = `chat_id=${encodeURIComponent(cid)}&name=${encodeURIComponent(file.name)}`;
      const res = await fetch("/api/upload?" + qs, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
      });
      const data = await res.json().catch(() => ({}));
      const pos = pendingFiles.indexOf(placeholder);
      if (!res.ok || data.error) {
        if (pos >= 0) pendingFiles.splice(pos, 1);
        setUploadNotice(data.error || `Upload failed (${res.status})`);
        continue;
      }
      if (!currentChatId && data.chat_id) draftChatId = data.chat_id;
      const saved = {
        chat_id: data.chat_id,
        name: data.name,
        size: data.size,
        outline: data.outline || "",
        url: data.url || "",
        uploading: false,
      };
      const dupIdx = pendingFiles.findIndex((x, i) => i !== pos && x.name === saved.name);
      if (pos >= 0) pendingFiles[pos] = saved;
      if (dupIdx >= 0) pendingFiles.splice(dupIdx, 1);
      attachedAny = true;
    } catch (err) {
      const pos = pendingFiles.indexOf(placeholder);
      if (pos >= 0) pendingFiles.splice(pos, 1);
      setUploadNotice("Upload error: " + String(err));
    }
    renderComposerBar();
  }
  if (attachedAny && !activePacks.files) {
    activePacks.files = true;
    syncPlusMenuState();
    renderComposerBar();
  }
  const inp = document.getElementById("input");
  if (inp) inp.focus();
}

async function removePendingFile(idx) {
  const f = pendingFiles[idx];
  if (!f || f.uploading) return;
  pendingFiles.splice(idx, 1);
  renderComposerBar();
  const cid = f.chat_id || currentChatId || draftChatId;
  if (cid && f.name) {
    try {
      await fetch(`/api/files/${encodeURIComponent(cid)}/${encodeURIComponent(f.name)}`, { method: "DELETE" });
    } catch (_) {}
  }
}

let filesCache = [];
let filesTotalBytes = 0;
let armedFileKey = "";
let armedFileTimer = null;
let armedDeleteAllFiles = false;
let armedDeleteAllFilesTimer = null;

function clearArmedFileDeletes() {
  armedFileKey = "";
  if (armedFileTimer) { clearTimeout(armedFileTimer); armedFileTimer = null; }
  armedDeleteAllFiles = false;
  if (armedDeleteAllFilesTimer) { clearTimeout(armedDeleteAllFilesTimer); armedDeleteAllFilesTimer = null; }
}

function closeFilesModal() {
  clearArmedFileDeletes();
  document.getElementById("files-modal").style.display = "none";
}

async function openFilesModal() {
  clearArmedFileDeletes();
  document.getElementById("files-modal").style.display = "flex";
  await refreshFilesModal();
}

async function refreshFilesModal() {
  try {
    const res = await fetch("/api/files");
    const data = await res.json();
    filesCache = Array.isArray(data.files) ? data.files : [];
    filesTotalBytes = Number(data.total_bytes || 0);
  } catch (_) {
    filesCache = [];
    filesTotalBytes = 0;
  }
  renderFilesManager();
}

function renderFilesManager() {
  const sumEl = document.getElementById("files-summary");
  const rowsEl = document.getElementById("files-rows");
  const delAllBtn = document.getElementById("files-delete-all-btn");
  if (!rowsEl) return;

  sumEl.textContent = filesCache.length
    ? `${filesCache.length} file${filesCache.length === 1 ? "" : "s"} · ${fmtFileSize(filesTotalBytes)}`
    : "0 files stored";

  if (delAllBtn) {
    delAllBtn.disabled = filesCache.length === 0;
    delAllBtn.classList.toggle("btn-danger-arm", armedDeleteAllFiles);
    delAllBtn.textContent = armedDeleteAllFiles ? "Confirm delete all?" : "Delete all";
  }

  rowsEl.innerHTML = "";
  if (!filesCache.length) {
    const empty = document.createElement("div");
    empty.style.cssText = "color:var(--muted);font-size:13px;padding:12px 2px;";
    empty.textContent = "No uploaded or generated files on disk yet.";
    rowsEl.appendChild(empty);
    return;
  }

  for (const f of filesCache) {
    const row = document.createElement("div");
    row.className = "model-mgr-row";

    const info = document.createElement("div");
    info.className = "model-mgr-info";

    const title = document.createElement("div");
    title.className = "model-mgr-title";
    const nameEl = document.createElement("span");
    nameEl.className = "model-mgr-name";
    nameEl.textContent = f.name;
    title.appendChild(nameEl);

    const sizeTag = document.createElement("span");
    sizeTag.className = "model-mgr-tag";
    sizeTag.textContent = fmtFileSize(f.size);
    title.appendChild(sizeTag);

    const meta = document.createElement("div");
    meta.className = "model-mgr-meta";
    meta.style.paddingLeft = "0";
    const parts = [f.chat_title || f.chat_id];
    if (f.outline) parts.push(f.outline);
    meta.textContent = parts.filter(Boolean).join(" · ");

    info.appendChild(title);
    info.appendChild(meta);

    const acts = document.createElement("div");
    acts.className = "model-mgr-actions";

    const dlLink = document.createElement("a");
    dlLink.className = "setup-link";
    dlLink.style.padding = "5px 10px";
    dlLink.style.fontSize = "12px";
    dlLink.href = f.url;
    dlLink.download = f.name;
    dlLink.textContent = "Download";
    acts.appendChild(dlLink);

    const key = `${f.chat_id}/${f.name}`;
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    const armed = armedFileKey === key;
    delBtn.className = armed ? "btn-danger-arm" : "";
    delBtn.textContent = armed ? "Confirm?" : "Delete";
    delBtn.onclick = async () => {
      if (armedFileKey !== key) {
        clearArmedFileDeletes();
        armedFileKey = key;
        renderFilesManager();
        armedFileTimer = setTimeout(() => { armedFileKey = ""; renderFilesManager(); }, 3500);
        return;
      }
      clearArmedFileDeletes();
      await fetch(`/api/files/${encodeURIComponent(f.chat_id)}/${encodeURIComponent(f.name)}`, { method: "DELETE" });
      pendingFiles = pendingFiles.filter(pf => !(pf.name === f.name && (pf.chat_id || currentChatId || draftChatId) === f.chat_id));
      renderComposerBar();
      await refreshFilesModal();
    };
    acts.appendChild(delBtn);

    row.appendChild(info);
    row.appendChild(acts);
    rowsEl.appendChild(row);
  }
}

async function deleteAllFilesFromModal() {
  if (!filesCache.length) return;
  if (!armedDeleteAllFiles) {
    clearArmedFileDeletes();
    armedDeleteAllFiles = true;
    renderFilesManager();
    armedDeleteAllFilesTimer = setTimeout(() => { armedDeleteAllFiles = false; renderFilesManager(); }, 3500);
    return;
  }
  clearArmedFileDeletes();
  await fetch("/api/files/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ all: true }),
  });
  pendingFiles = [];
  draftChatId = null;
  renderComposerBar();
  await refreshFilesModal();
}

function fmtTok(n) {
  if (!n) return "0";
  return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(n);
}

function updateCtxBadge(used = 0, maxCtx = MAX_CTX) {
  const badge = document.getElementById("ctx-badge");
  const pct = maxCtx ? Math.min(100, Math.round((used / maxCtx) * 100)) : 0;
  badge.textContent = used > 0
    ? `ctx ${fmtTok(used)} / ${fmtTok(maxCtx)} (${pct}%)`
    : `ctx 0 / ${fmtTok(maxCtx)}`;
  badge.classList.remove("warn", "danger");
  if (pct >= 88) badge.classList.add("danger");
  else if (pct >= 70) badge.classList.add("warn");
}

function setSendBtnState(isBusy) {
  const btn = document.getElementById("send-btn");
  if (isBusy) {
    btn.innerHTML = SVG_STOP;
    btn.title = "Stop model";
    btn.classList.add("stopping");
  } else {
    btn.innerHTML = SVG_ARROW_UP;
    btn.title = "Send";
    btn.classList.remove("stopping");
  }
}

function handleSendOrStop() {
  if (busy) {
    if (currentAbort) currentAbort.abort();
  } else {
    sendMsg();
  }
}

let modelChosenByUser = false;

function renderModelMenu() {
  const menu = document.getElementById("model-menu");
  menu.innerHTML = "";
  for (const m of availableModels) {
    const pal = getModelPalette(m);
    const installed = isModelInstalled(m);
    const paramLabel = getModelParamLabel(m);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "model-opt";
    btn.innerHTML = `
      <span class="model-opt-left">
        <span class="opt-dot" style="background:${pal[2]};opacity:${installed ? 0.95 : 0.35};"></span>
        <span class="opt-name"></span>
      </span>
      <span class="opt-param"></span>
      <span class="opt-status">${m === currentModel ? SVG_CHECK : (!installed && ollamaRunning ? "pull" : "")}</span>
    `;
    btn.querySelector(".opt-name").textContent = m;
    const pEl = btn.querySelector(".opt-param");
    if (pEl) pEl.textContent = paramLabel || "";
    btn.onclick = () => selectModel(m);
    menu.appendChild(btn);
  }
  const sep = document.createElement("div");
  sep.className = "model-sep";
  menu.appendChild(sep);

  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "model-opt model-add";
  addBtn.innerHTML = `<span class="model-opt-left"><span>+</span><span>Download new model...</span></span>`;
  addBtn.onclick = (e) => {
    e.stopPropagation();
    menu.classList.remove("open");
    openPullModal();
  };
  menu.appendChild(addBtn);

  const mgrBtn = document.createElement("button");
  mgrBtn.type = "button";
  mgrBtn.className = "model-opt model-add";
  mgrBtn.innerHTML = `<span class="model-opt-left"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 0 1-9 9m9-9a9 9 0 0 0-9-9m9 9H3m0 0a9 9 0 0 1 9-9m0 18c-2.5 0-4.5-4-4.5-9S9.5 3 12 3m0 18c2.5 0 4.5-4 4.5-9S14.5 3 12 3"/></svg><span>Manage / update models...</span></span>`;
  mgrBtn.onclick = (e) => {
    e.stopPropagation();
    menu.classList.remove("open");
    openModelsModal();
  };
  menu.appendChild(mgrBtn);
}

function fmtModelSize(bytes) {
  if (!bytes || bytes <= 0) return "";
  if (bytes >= 1e9) return (bytes / 1e9).toFixed(1).replace(/\.0$/, "") + " GB";
  return Math.max(1, Math.round(bytes / 1e6)) + " MB";
}

function fmtModelAge(iso) {
  const ts = Date.parse(iso || "");
  if (Number.isNaN(ts)) return "";
  const days = Math.floor(Math.max(0, Date.now() - ts) / 86400000);
  if (days <= 0) return "updated today";
  if (days === 1) return "1d old";
  if (days < 30) return `${days}d old`;
  if (days < 365) return `${Math.floor(days / 30)}mo old`;
  return `${Math.floor(days / 365)}y old`;
}

function setModelsStatus(text, pct = null) {
  const row = document.getElementById("models-status-row");
  const txt = document.getElementById("models-status-text");
  const bar = document.getElementById("models-pull-bar");
  const fill = document.getElementById("models-pull-fill");
  if (!text) {
    row.style.display = "none";
    bar.classList.remove("show");
    return;
  }
  row.style.display = "flex";
  txt.textContent = text;
  if (pct === null) {
    bar.classList.remove("show");
  } else {
    bar.classList.add("show");
    fill.style.width = Math.min(100, Math.max(0, pct)) + "%";
  }
}

let armedDeleteModel = "";
let armedDeleteTimer = null;

function clearArmedDelete() {
  armedDeleteModel = "";
  if (armedDeleteTimer) {
    clearTimeout(armedDeleteTimer);
    armedDeleteTimer = null;
  }
}

function openModelsModal() {
  clearArmedDelete();
  if (!pullingModel) setModelsStatus("");
  document.getElementById("models-modal").style.display = "flex";
  renderModelsManager();
  loadModels(false);
}

function closeModelsModal() {
  clearArmedDelete();
  document.getElementById("models-modal").style.display = "none";
}

function renderModelsManager() {
  const rowsEl = document.getElementById("models-rows");
  const summaryEl = document.getElementById("models-summary");
  const updateAllBtn = document.getElementById("models-update-all-btn");
  rowsEl.innerHTML = "";

  if (!ollamaRunning) {
    summaryEl.textContent = "Ollama offline";
    updateAllBtn.disabled = true;
    const msg = document.createElement("div");
    msg.style.cssText = "color:var(--muted);font-size:13px;padding:12px 4px;display:flex;align-items:center;justify-content:space-between;gap:10px;";
    msg.innerHTML = `<span>Ollama is not running yet. Start it to update or remove local models.</span>`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Start / Retry";
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = "Checking...";
      await fetch("/api/ollama/start", { method: "POST" }).catch(() => {});
      await loadModels(false);
      renderModelsManager();
    };
    msg.appendChild(btn);
    rowsEl.appendChild(msg);
    return;
  }

  const items = installedModelDetails.slice().sort((a, b) => {
    const ta = Date.parse(a.modified_at || "") || 0;
    const tb = Date.parse(b.modified_at || "") || 0;
    return ta - tb;
  });

  const totalBytes = items.reduce((acc, m) => acc + (m.size || 0), 0);
  const totalTxt = fmtModelSize(totalBytes);
  summaryEl.textContent = items.length
    ? `${items.length} installed${totalTxt ? " · " + totalTxt : ""} · oldest first`
    : "0 installed";
  updateAllBtn.disabled = pullingModel || items.length === 0;

  if (!items.length) {
    const empty = document.createElement("div");
    empty.style.cssText = "color:var(--muted);font-size:13px;padding:12px 4px;";
    empty.textContent = "No models downloaded on this PC yet.";
    rowsEl.appendChild(empty);
    return;
  }

  for (const m of items) {
    const row = document.createElement("div");
    row.className = "model-mgr-row";

    const info = document.createElement("div");
    info.className = "model-mgr-info";

    const title = document.createElement("div");
    title.className = "model-mgr-title";
    const pal = getModelPalette(m.name);
    const dot = document.createElement("span");
    dot.className = "opt-dot";
    dot.style.background = pal[2];
    title.appendChild(dot);

    const nm = document.createElement("span");
    nm.className = "model-mgr-name";
    nm.textContent = m.name;
    title.appendChild(nm);

    if (m.name === currentModel || m.raw_name === currentModel) {
      const tag = document.createElement("span");
      tag.className = "model-mgr-tag";
      tag.textContent = "active";
      title.appendChild(tag);
    }
    if (typeof appSettings !== "undefined" && appSettings.default_model && (appSettings.default_model === m.name || appSettings.default_model === m.raw_name)) {
      const tag = document.createElement("span");
      tag.className = "model-mgr-tag";
      tag.textContent = "default";
      title.appendChild(tag);
    }
    info.appendChild(title);

    const meta = document.createElement("div");
    meta.className = "model-mgr-meta";
    meta.textContent = [fmtModelSize(m.size), m.parameter_size, m.quantization, fmtModelAge(m.modified_at)].filter(Boolean).join(" · ") || "Installed locally";
    info.appendChild(meta);

    const acts = document.createElement("div");
    acts.className = "model-mgr-actions";

    const updBtn = document.createElement("button");
    updBtn.type = "button";
    updBtn.textContent = "Update";
    updBtn.disabled = pullingModel;
    updBtn.onclick = () => updateInstalledModel(m.name);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    const isArmed = armedDeleteModel === m.name;
    delBtn.textContent = isArmed ? "Confirm?" : "Delete";
    if (isArmed) delBtn.className = "btn-danger-arm";
    delBtn.disabled = pullingModel;
    delBtn.onclick = () => {
      if (armedDeleteModel !== m.name) {
        clearArmedDelete();
        armedDeleteModel = m.name;
        armedDeleteTimer = setTimeout(() => {
          armedDeleteModel = "";
          renderModelsManager();
        }, 3500);
        renderModelsManager();
        return;
      }
      clearArmedDelete();
      deleteInstalledModel(m.raw_name || m.name, m.name);
    };

    acts.appendChild(updBtn);
    acts.appendChild(delBtn);
    row.appendChild(info);
    row.appendChild(acts);
    rowsEl.appendChild(row);
  }
}

async function runModelUpdates(targets) {
  if (pullingModel || !targets || !targets.length) return;
  pullingModel = true;
  clearArmedDelete();
  renderModelsManager();
  let updated = 0;
  try {
    for (let i = 0; i < targets.length; i++) {
      const name = targets[i];
      const prefix = targets.length > 1 ? `(${i + 1}/${targets.length}) ` : "";
      setModelsStatus(`${prefix}Checking updates for ${name}...`, 0);
      await streamPullRequest(name, (ev) => {
        if (ev.total && ev.completed) {
          const pct = Math.min(100, Math.round((ev.completed / ev.total) * 100));
          setModelsStatus(`${prefix}Updating ${name}: ${pct}%`, pct);
        } else if (ev.status) {
          setModelsStatus(`${prefix}${name}: ${ev.status}`, ev.status === "success" ? 100 : null);
        }
      });
      updated++;
    }
    setModelsStatus(
      targets.length > 1 ? `Checked ${updated} model(s). All up to date.` : `${targets[0]} is up to date.`,
      null
    );
  } catch (err) {
    const label = targets.length > 1 ? `after ${updated}/${targets.length}` : `for ${targets[0]}`;
    setModelsStatus(`Update failed ${label}: ${err.message || err}`, null);
  }
  pullingModel = false;
  await loadModels(false);
}

function updateInstalledModel(modelName) {
  return runModelUpdates([modelName]);
}

function updateAllModels() {
  return runModelUpdates(installedModels.slice());
}

async function deleteInstalledModel(targetName, displayName) {
  if (pullingModel) return;
  setModelsStatus(`Removing ${displayName || targetName}...`, null);
  try {
    const res = await fetch("/api/models/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: targetName }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    await loadSettings();
    await loadModels(false);
    if (!isModelInstalled(currentModel)) {
      currentModel = installedModels[0] || availableModels[0] || "__MODEL__";
      applyModelTheme(currentModel);
      renderModelMenu();
      updateSetupBanner();
    }
    setModelsStatus(`Deleted ${displayName || targetName}.`, null);
  } catch (err) {
    setModelsStatus(`Delete failed: ${err.message || err}`, null);
  }
}

function pickPullModel(tag) {
  const inp = document.getElementById("pull-input");
  if (!inp) return;
  inp.value = tag || "";
  inp.focus();
}

function openPullModal() {
  const inp = document.getElementById("pull-input");
  inp.value = "";
  document.getElementById("pull-modal").style.display = "flex";
  inp.focus();
}

function closePullModal() {
  document.getElementById("pull-modal").style.display = "none";
}

function submitPullModal() {
  const name = document.getElementById("pull-input").value.trim();
  if (!name) return;
  closePullModal();
  if (!availableModels.includes(name)) availableModels.unshift(name);
  currentModel = name;
  modelChosenByUser = true;
  applyModelTheme(name);
  renderModelMenu();
  pullModel(name);
}

function selectModel(m) {
  currentModel = m;
  modelChosenByUser = true;
  applyModelTheme(m);
  renderModelMenu();
  updateSetupBanner();
  document.getElementById("model-menu").classList.remove("open");
}

function toggleModelMenu(e) {
  e.stopPropagation();
  document.getElementById("model-menu").classList.toggle("open");
}

document.addEventListener("click", () => {
  document.getElementById("model-menu").classList.remove("open");
});

async function loadModels(retryOnBoot = true) {
  let hasBin = false;
  try {
    const res = await fetch("/api/models");
    const info = await res.json();
    ollamaRunning = Boolean(info.ollama_running);
    hasBin = Boolean(info.has_ollama_bin);
    installedModels = Array.isArray(info.installed) ? info.installed : [];
    installedModelDetails = Array.isArray(info.installed_details) ? info.installed_details : [];
    modelParams = (info && info.model_params && typeof info.model_params === "object") ? info.model_params : {};
    if (Array.isArray(info.models) && info.models.length) availableModels = info.models;
    if (!currentChatId && !modelChosenByUser && info.default_model) {
      if ((typeof appSettings !== "undefined" && appSettings.default_model) || !isModelInstalled(currentModel)) {
        currentModel = info.default_model;
      }
    }
  } catch (_) {}
  applyModelTheme(currentModel);
  renderModelMenu();
  updateSetupBanner();
  if (ollamaRunning && typeof appSettings !== "undefined" && appSettings.tts_auto && !_bootTtsWarmed) {
    warmTtsModel(false);
  }
  if (document.getElementById("models-modal").style.display === "flex") {
    renderModelsManager();
  }
  if (!ollamaRunning && hasBin && retryOnBoot) {
    setTimeout(() => loadModels(false), 1500);
  }
}

function toggleSidebar() {
  document.getElementById("sidebar").classList.toggle("collapsed");
  setTimeout(refreshTitleOverflow, 220);
}
window.addEventListener("resize", refreshTitleOverflow);

function usePrompt(txt) {
  const inp = document.getElementById("input");
  inp.value = txt;
  autoResize(inp);
  sendMsg();
}

function autoResize(el) {
  el.style.height = "22px";
  el.style.height = Math.min(el.scrollHeight, 180) + "px";
}

function escHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function renderInline(s) {
  s = s.replace(/\*\*\*([\s\S]+?)\*\*\*/g, "<strong><em>$1</em></strong>");
  s = s.replace(/\*\*([\s\S]+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\*([^*\n]+?)\*/g, "<em>$1</em>");
  s = s.replace(/~~([^~\n]+?)~~/g, "<del>$1</del>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s;
}

function renderMarkdown(raw) {
  const codeBlocks = [];
  let text = (raw || "").replace(/\r\n/g, "\n");

  text = text.replace(/```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const id = codeBlocks.length;
    const cleanCode = code.replace(/\n$/, "");
    codeBlocks.push(
      `<div class="code-block">` +
        `<div class="code-bar"><span>${escHtml(lang || "code")}</span>` +
        `<button type="button" class="copy-btn" onclick="copyCode(this)">Copy</button></div>` +
        `<pre><code>${escHtml(cleanCode)}</code></pre>` +
      `</div>`
    );
    return `\n@@CODEBLOCK_${id}@@\n`;
  });

  text = escHtml(text);

  const inlineCodes = [];
  text = text.replace(/`([^`\n]+)`/g, (_, code) => {
    const id = inlineCodes.length;
    inlineCodes.push(`<code class="inline" title="Click to copy" onclick="copyInline(this)">${code}</code>`);
    return `@@INLINE_${id}@@`;
  });

  const lines = text.split("\n");
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const cbMatch = line.trim().match(/^@@CODEBLOCK_(\d+)@@$/);
    if (cbMatch) { out.push(codeBlocks[Number(cbMatch[1])]); i++; continue; }
    if (!line.trim()) { i++; continue; }

    const hMatch = line.match(/^(#{1,4})\s+(.*)$/);
    if (hMatch) {
      const lvl = hMatch[1].length;
      out.push(`<h${lvl}>${renderInline(hMatch[2])}</h${lvl}>`);
      i++; continue;
    }

    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push("<hr>"); i++; continue; }

    if (/^&gt;\s?/.test(line)) {
      const bq = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i])) {
        bq.push(renderInline(lines[i].replace(/^&gt;\s?/, "")));
        i++;
      }
      out.push(`<blockquote>${bq.join("<br>")}</blockquote>`);
      continue;
    }

    if (line.trim().startsWith("|") && i + 1 < lines.length && /^\|?[\s:-]+\|/.test(lines[i + 1])) {
      const parseRow = (r) => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => renderInline(c.trim()));
      const headers = parseRow(line);
      i += 2;
      let tbl = "<table><thead><tr>" + headers.map(h => `<th>${h}</th>`).join("") + "</tr></thead><tbody>";
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        tbl += "<tr>" + parseRow(lines[i]).map(c => `<td>${c}</td>`).join("") + "</tr>";
        i++;
      }
      out.push(tbl + "</tbody></table>");
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      let ul = "<ul>";
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        ul += `<li>${renderInline(lines[i].replace(/^\s*[-*+]\s+/, ""))}</li>`;
        i++;
      }
      out.push(ul + "</ul>");
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      let ol = "<ol>";
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        ol += `<li>${renderInline(lines[i].replace(/^\s*\d+\.\s+/, ""))}</li>`;
        i++;
      }
      out.push(ol + "</ol>");
      continue;
    }

    const para = [];
    while (
      i < lines.length && lines[i].trim() &&
      !/^(#{1,4})\s+/.test(lines[i]) &&
      !/^\s*[-*+]\s+/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i]) &&
      !/^&gt;\s?/.test(lines[i]) &&
      !/^@@CODEBLOCK_\d+@@$/.test(lines[i].trim())
    ) {
      para.push(renderInline(lines[i]));
      i++;
    }
    out.push(`<p>${para.join("<br>")}</p>`);
  }

  return out.join("\n").replace(/@@INLINE_(\d+)@@/g, (_, id) => inlineCodes[Number(id)]);
}

function snapStreamSliceEnd(str, pos) {
  if (pos <= 0 || pos >= str.length) return Math.max(0, Math.min(pos, str.length));
  const code = str.charCodeAt(pos - 1);
  if (code >= 0xD800 && code <= 0xDBFF) return pos + 1;
  return pos;
}

function renderStreamingMarkdown(raw) {
  const s = raw || "";
  const fences = (s.match(/```/g) || []).length;
  return renderMarkdown(fences % 2 === 1 ? s + "\n```" : s);
}

function appendStreamCursor(proseEl) {
  if (!proseEl) return;
  const cur = document.createElement("span");
  cur.className = "stream-cursor";
  cur.setAttribute("aria-hidden", "true");
  let target = proseEl.lastElementChild;
  while (target && (target.tagName === "UL" || target.tagName === "OL")) {
    target = target.lastElementChild;
  }
  if (target && /^(P|LI|H[1-4]|BLOCKQUOTE)$/.test(target.tagName)) {
    target.appendChild(cur);
  } else if (target && target.classList && target.classList.contains("code-block")) {
    const codeEl = target.querySelector("code");
    if (codeEl) codeEl.appendChild(cur);
    else proseEl.appendChild(cur);
  } else {
    proseEl.appendChild(cur);
  }
}

function copyCode(btn) {
  const code = btn.closest(".code-block").querySelector("code").textContent;
  navigator.clipboard.writeText(code);
  btn.textContent = "Copied!";
  setTimeout(() => btn.textContent = "Copy", 1400);
}

function copyInline(el) {
  navigator.clipboard.writeText(el.textContent);
  const old = el.style.borderColor;
  el.style.borderColor = "#ffffff";
  setTimeout(() => el.style.borderColor = old, 900);
}

function copyText(btn, rawText) {
  navigator.clipboard.writeText(rawText);
  btn.textContent = "Copied!";
  setTimeout(() => btn.textContent = "Copy", 1400);
}

let activeTtsAudio = null;
let activeTtsBtn = null;
let activeLiveSpeech = null;

class SpeechSentenceBuffer {
  constructor() {
    this.raw = "";
    this.cursor = 0;
    this.inCodeFence = false;
    this.chunksSent = 0;
  }
  reset() {
    this.raw = "";
    this.cursor = 0;
    this.inCodeFence = false;
    this.chunksSent = 0;
  }
  pushDelta(delta) {
    if (!delta) return [];
    this.raw += delta;
    return this.flush(false);
  }
  finish(finalText = "") {
    if (finalText && this.chunksSent === 0 && !this.raw) {
      this.raw = finalText;
    }
    return this.flush(true);
  }
  flush(final = false) {
    const out = [];
    while (this.cursor < this.raw.length) {
      if (this.inCodeFence) {
        const closeIdx = this.raw.indexOf("```", this.cursor);
        if (closeIdx < 0) {
          if (final) {
            this.cursor = this.raw.length;
            this.inCodeFence = false;
          }
          break;
        }
        this.cursor = closeIdx + 3;
        this.inCodeFence = false;
        continue;
      }
      const fenceIdx = this.raw.indexOf("```", this.cursor);
      const limit = fenceIdx >= 0 ? fenceIdx : this.raw.length;
      const segment = this.raw.slice(this.cursor, limit);

      let cutRel = -1;
      const re = /([.!?…](?=\s)|\n+|[。！？])/g;
      const m = re.exec(segment);
      if (m) {
        cutRel = m.index + m[0].length;
        if (m[1] === "." && /^\s*\d+\.$/.test(segment.slice(0, m.index + 1))) {
          const m2 = re.exec(segment);
          cutRel = m2 ? m2.index + m2[0].length : -1;
        }
      } else if (fenceIdx >= 0) {
        cutRel = segment.length;
      } else if (final) {
        cutRel = segment.length;
      } else if (this.chunksSent === 0 && segment.length >= 48) {
        const clauseRe = /(?:,\s|;\s|:\s)/g;
        let cm;
        while ((cm = clauseRe.exec(segment)) !== null) {
          if (cm.index >= 24) {
            cutRel = cm.index + cm[0].length;
            break;
          }
        }
        if (cutRel < 0 && segment.length >= 80) {
          const sp = segment.lastIndexOf(" ", 80);
          if (sp >= 42) cutRel = sp + 1;
        }
      }

      if (cutRel >= 0) {
        const piece = segment.slice(0, cutRel).trim();
        this.cursor += cutRel;
        if (piece && /[A-Za-z0-9\u00C0-\uFFFF]/.test(piece)) {
          out.push(piece);
          this.chunksSent += 1;
        }
        if (fenceIdx >= 0 && this.cursor === fenceIdx) {
          this.cursor += 3;
          this.inCodeFence = true;
        }
        continue;
      }
      break;
    }
    return out;
  }
}

function ttsCacheKey(model, voice) {
  return `${String(model || "").trim()}|${String(voice || "").trim()}`;
}

function buildWavHeader(dataBytes, sampleRate = 24000, channels = 1, bitsPerSample = 16) {
  const hdr = new ArrayBuffer(44);
  const v = new DataView(hdr);
  const writeTag = (off, s) => {
    for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i));
  };
  const byteRate = sampleRate * channels * (bitsPerSample >> 3);
  const blockAlign = channels * (bitsPerSample >> 3);
  writeTag(0, "RIFF");
  v.setUint32(4, 36 + dataBytes, true);
  writeTag(8, "WAVE");
  writeTag(12, "fmt ");
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);
  v.setUint16(22, channels, true);
  v.setUint32(24, sampleRate, true);
  v.setUint32(28, byteRate, true);
  v.setUint16(32, blockAlign, true);
  v.setUint16(34, bitsPerSample, true);
  writeTag(36, "data");
  v.setUint32(40, dataBytes, true);
  return new Uint8Array(hdr);
}

function normalizePcmChunks(pcmChunks) {
  const aligned = [];
  let totalBytes = 0;
  for (const raw of (pcmChunks || [])) {
    if (!raw || raw.length < 2) continue;
    const evenLen = raw.length & ~1;
    const chunk = evenLen === raw.length ? raw : raw.subarray(0, evenLen);
    aligned.push(chunk);
    totalBytes += evenLen;
  }
  return { aligned, totalBytes };
}

function encodePcmChunksToWavBytes(pcmChunks, sampleRate = 24000) {
  const { aligned, totalBytes } = normalizePcmChunks(pcmChunks);
  const out = new Uint8Array(44 + totalBytes);
  out.set(buildWavHeader(totalBytes, sampleRate), 0);
  let off = 44;
  for (const c of aligned) {
    out.set(c, off);
    off += c.length;
  }
  return out;
}

function encodePcmChunksToWavBlob(pcmChunks, sampleRate = 24000) {
  const { aligned, totalBytes } = normalizePcmChunks(pcmChunks);
  if (!totalBytes) return null;
  return new Blob([buildWavHeader(totalBytes, sampleRate), ...aligned], { type: "audio/wav" });
}

function cacheWavOnSpeakBtn(btn, blob, cacheKey) {
  if (!btn || !blob || !blob.size) return;
  if (btn._ttsUrl && btn._ttsKey === cacheKey) return;
  if (btn._ttsUrl) {
    try { URL.revokeObjectURL(btn._ttsUrl); } catch (_) {}
  }
  btn._ttsUrl = URL.createObjectURL(blob);
  btn._ttsKey = cacheKey;
}

function resetSpeakBtn(btn) {
  if (!btn) return;
  btn.classList.remove("speaking", "loading");
  btn.title = "Read aloud";
  btn.innerHTML = SVG_SPEAKER;
  const row = btn.closest(".msg-actions");
  if (row) row.classList.remove("tts-active");
}

function stopTtsPlayback() {
  if (activeLiveSpeech) {
    const ls = activeLiveSpeech;
    activeLiveSpeech = null;
    ls.stop();
  }
  if (activeTtsAudio) {
    try { activeTtsAudio.pause(); activeTtsAudio.currentTime = 0; } catch (_) {}
    activeTtsAudio = null;
  }
  if (activeTtsBtn) {
    resetSpeakBtn(activeTtsBtn);
    activeTtsBtn = null;
  }
}

const TTS_SAMPLE_RATE = 24000;
const TTS_INITIAL_PREBUF_BYTES = 24000; // 0.50s cushion at 24kHz 16-bit mono
const TTS_REBUF_BYTES = 36864;          // 0.77s recovery cushion after any underrun
const TTS_COALESCE_BYTES = 20480;       // 0.43s coalesced AudioBuffer blocks
const TTS_LOW_RUNWAY_BYTES = 8192;
const TTS_FADE_SAMPLES = 144;           // 6ms linear edge fade at non-contiguous starts / EOF
const TTS_FIRST_BATCH_MIN_CHARS = 100;  // Target initial runway before starting live TTS
const TTS_FIRST_BATCH_WAIT_MS = 280;    // Brief settle for tiny openers to merge clauses
const TTS_COALESCE_MIN_CHARS = 240;     // Coalesce subsequent short sentences while audio plays
const TTS_COALESCE_RUNWAY_SEC = 1.5;    // Dispatch next batch once remaining runway dips below 1.5s

function pcmInt16ToSoftFloats(u8, sampleCount, applyFadeIn, applyFadeOut) {
  const dv = new DataView(u8.buffer, u8.byteOffset, sampleCount * 2);
  const floats = new Float32Array(sampleCount);
  for (let i = 0; i < sampleCount; i++) {
    let x = dv.getInt16(i * 2, true) / 32768.0;
    const ax = x < 0 ? -x : x;
    if (ax > 0.85) {
      const shaped = 0.85 + 0.09 * Math.tanh((ax - 0.85) / 0.15);
      x = x < 0 ? -shaped : shaped;
    }
    floats[i] = x;
  }
  const maxRamp = Math.floor(sampleCount / 2);
  if (applyFadeIn && maxRamp > 0) {
    const n = Math.min(TTS_FADE_SAMPLES, maxRamp);
    for (let i = 0; i < n; i++) floats[i] *= i / n;
  }
  if (applyFadeOut && maxRamp > 0) {
    const n = Math.min(TTS_FADE_SAMPLES, maxRamp);
    for (let i = 0; i < n; i++) floats[sampleCount - 1 - i] *= i / n;
  }
  return floats;
}

function createLiveSpeechSession(model, voice, headEl, existingBtn = null) {
  stopTtsPlayback();
  const effModel = (model || appSettings.tts_model || "lfm3-audio:2b").trim();
  const effVoice = (voice || appSettings.tts_voice || "").trim();
  const cacheKey = ttsCacheKey(effModel, effVoice);

  let liveHeaderBtn = null;
  if (!existingBtn && headEl) {
    liveHeaderBtn = document.createElement("button");
    liveHeaderBtn.type = "button";
    liveHeaderBtn.className = "speak-btn live-spk";
    liveHeaderBtn.title = `Speaking live (${effModel}) — click to stop`;
    liveHeaderBtn.setAttribute("aria-label", "Stop live speech");
    liveHeaderBtn.innerHTML = '<span class="tts-spin"></span>';
    liveHeaderBtn.onclick = () => stopTtsPlayback();
    headEl.appendChild(liveHeaderBtn);
  }

  const startBtn = existingBtn || liveHeaderBtn;
  const session = {
    model: effModel,
    voice: effVoice,
    cacheKey,
    queue: [],
    batchesRequested: 0,
    inputEnded: false,
    aborted: false,
    workerRunning: false,
    hasAudio: false,
    abortCtrl: null,
    drainTimer: null,
    firstEnqueueAt: 0,
    nextTime: 0,
    sources: new Set(),
    pendingAudio: [],
    pendingBytes: 0,
    pcmChunks: [],
    wavBlob: null,
    wavCommitted: false,
    btn: startBtn,
    finalBtn: existingBtn || null,
    liveHeaderBtn,
    _queuedChars() {
      let total = 0;
      for (const q of session.queue) total += q.length;
      return total;
    },
    _audioRunway() {
      const ctx = audioCtx;
      return (ctx && session.nextTime > ctx.currentTime) ? (session.nextTime - ctx.currentTime) : 0;
    },
    _clearDrainTimer() {
      if (session.drainTimer) {
        clearTimeout(session.drainTimer);
        session.drainTimer = null;
      }
    },
    _scheduleDrain() {
      if (session.aborted || session.workerRunning || !session.queue.length) return;
      session._clearDrainTimer();
      if (session.inputEnded) {
        session._drain();
        return;
      }
      const qChars = session._queuedChars();
      if (session.batchesRequested === 0) {
        if (qChars >= TTS_FIRST_BATCH_MIN_CHARS) {
          session._drain();
          return;
        }
        const elapsed = session.firstEnqueueAt ? (Date.now() - session.firstEnqueueAt) : 0;
        const waitMs = Math.max(0, TTS_FIRST_BATCH_WAIT_MS - elapsed);
        if (waitMs === 0) {
          session._drain();
        } else {
          session.drainTimer = setTimeout(() => {
            session.drainTimer = null;
            if (!session.aborted && !session.workerRunning && session.queue.length > 0) session._drain();
          }, waitMs);
        }
        return;
      }
      const runway = session._audioRunway();
      if (runway > TTS_COALESCE_RUNWAY_SEC && qChars < TTS_COALESCE_MIN_CHARS) {
        const checkMs = Math.min(250, Math.max(80, Math.round((runway - 1.4) * 500)));
        session.drainTimer = setTimeout(() => {
          session.drainTimer = null;
          session._scheduleDrain();
        }, checkMs);
        return;
      }
      session._drain();
    },
    _commitWavCache() {
      if (session.aborted) return;
      if (session.finalBtn && session.wavBlob && !session.wavCommitted) {
        cacheWavOnSpeakBtn(session.finalBtn, session.wavBlob, session.cacheKey);
        session.wavCommitted = true;
        session.wavBlob = null;
      }
    },
    _buildCacheIfComplete() {
      if (
        !session.aborted &&
        session.inputEnded &&
        !session.workerRunning &&
        session.queue.length === 0 &&
        !session.wavBlob &&
        !session.wavCommitted &&
        session.pcmChunks.length > 0
      ) {
        session.wavBlob = encodePcmChunksToWavBlob(session.pcmChunks, TTS_SAMPLE_RATE);
        session.pcmChunks = [];
        session._commitWavCache();
      }
    },
    _doneCheck() {
      if (session.aborted) return;
      session._buildCacheIfComplete();
      if (session.inputEnded && !session.workerRunning && session.queue.length === 0 && session.sources.size === 0) {
        if (activeLiveSpeech === session) activeLiveSpeech = null;
        if (session.liveHeaderBtn && session.liveHeaderBtn.isConnected) {
          session.liveHeaderBtn.remove();
          session.liveHeaderBtn = null;
        }
        if (activeTtsBtn === session.btn) {
          resetSpeakBtn(activeTtsBtn);
          activeTtsBtn = null;
        } else if (session.btn) {
          resetSpeakBtn(session.btn);
        }
      }
    },
    _syncBtnState() {
      if (session.aborted || !session.btn) return;
      if (session.hasAudio) {
        session.btn.classList.remove("loading");
        session.btn.classList.add("speaking");
        session.btn.innerHTML = SVG_SPEAKER;
        session.btn.title = "Stop speaking";
      } else {
        session.btn.classList.add("loading");
        session.btn.classList.remove("speaking");
        session.btn.innerHTML = '<span class="tts-spin"></span>';
        session.btn.title = `Synthesizing voice (${session.model})...`;
      }
      const acts = session.btn.closest(".msg-actions");
      if (acts) acts.classList.add("tts-active");
    },
    attachFinalButton(btn) {
      if (!btn) return;
      session.finalBtn = btn;
      session._commitWavCache();
      if (session.liveHeaderBtn && session.liveHeaderBtn.isConnected) {
        session.liveHeaderBtn.remove();
        session.liveHeaderBtn = null;
      }
      if (session.aborted) return;
      const alive = !session.inputEnded || session.workerRunning || session.queue.length > 0 || session.sources.size > 0;
      if (!alive) {
        if (activeLiveSpeech === session) activeLiveSpeech = null;
        if (activeTtsBtn === session.btn) activeTtsBtn = null;
        resetSpeakBtn(btn);
        return;
      }
      session.btn = btn;
      activeTtsBtn = btn;
      session._syncBtnState();
    },
    enqueue(text) {
      if (session.aborted || !text) return;
      if (!session.firstEnqueueAt) session.firstEnqueueAt = Date.now();
      session.queue.push(text);
      session._syncBtnState();
      session._scheduleDrain();
    },
    finish(remainingChunks) {
      if (session.aborted) return;
      if (Array.isArray(remainingChunks)) {
        for (const c of remainingChunks) {
          if (c) session.queue.push(c);
        }
      }
      session.inputEnded = true;
      session._clearDrainTimer();
      session._syncBtnState();
      if (!session.workerRunning) {
        if (session.queue.length > 0) session._drain();
        else session._doneCheck();
      }
    },
    stop() {
      session.aborted = true;
      session._clearDrainTimer();
      session.queue.length = 0;
      session.pendingAudio.length = 0;
      session.pendingBytes = 0;
      session.pcmChunks.length = 0;
      session.wavBlob = null;
      if (session.abortCtrl) {
        try { session.abortCtrl.abort(); } catch (_) {}
        session.abortCtrl = null;
      }
      for (const src of Array.from(session.sources)) {
        try { src.stop(); } catch (_) {}
      }
      session.sources.clear();
      if (session.liveHeaderBtn && session.liveHeaderBtn.isConnected) {
        session.liveHeaderBtn.remove();
        session.liveHeaderBtn = null;
      }
      if (activeTtsBtn === session.btn) {
        resetSpeakBtn(activeTtsBtn);
        activeTtsBtn = null;
      } else if (session.btn) {
        resetSpeakBtn(session.btn);
      }
    },
    _takeBatch() {
      if (!session.queue.length) return "";
      const maxChars = session.batchesRequested === 0 ? 280 : 1000;
      let piece = session.queue.shift();
      while (session.queue.length > 0 && (piece.length + 1 + session.queue[0].length) <= maxChars) {
        piece += " " + session.queue.shift();
      }
      session.batchesRequested += 1;
      return piece;
    },
    async _drain() {
      if (session.workerRunning || session.aborted) return;
      session._clearDrainTimer();
      session.workerRunning = true;
      try {
        while (!session.aborted && session.queue.length > 0) {
          if (!session.inputEnded && session.batchesRequested > 0) {
            if (session._audioRunway() > TTS_COALESCE_RUNWAY_SEC && session._queuedChars() < TTS_COALESCE_MIN_CHARS) {
              if (session.hasAudio && session.pendingBytes >= 2) session._flushPendingAudio(false);
              break;
            }
          }
          const piece = session._takeBatch();
          if (!piece) continue;
          session.abortCtrl = new AbortController();
          const res = await fetch("/api/tts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: piece, model: session.model, voice: session.voice, stream_pcm: true }),
            signal: session.abortCtrl.signal,
          });
          if (session.aborted) break;
          if (res.status === 204) continue;
          if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            const msg = errData.error || `Speech failed (HTTP ${res.status})`;
            showSpeechBanner(msg, errData.missing_model || "");
            session.stop();
            return;
          }
          if (!res.body || !res.body.getReader) {
            const buf = await res.arrayBuffer();
            if (session.aborted) break;
            const raw = new Uint8Array(buf);
            const evenLen = raw.length & ~1;
            if (evenLen >= 2) session._pushPcmRecord(evenLen === raw.length ? raw : raw.subarray(0, evenLen));
            session._maybeFlushAudio(true);
            continue;
          }
          const reader = res.body.getReader();
          let carry = null;
          while (!session.aborted) {
            const { value, done } = await reader.read();
            if (done) break;
            if (!value || !value.length) continue;
            let bytes = value;
            if (carry) {
              const merged = new Uint8Array(carry.length + bytes.length);
              merged.set(carry, 0);
              merged.set(bytes, carry.length);
              bytes = merged;
              carry = null;
            }
            if (bytes.length % 2 === 1) {
              carry = bytes.slice(bytes.length - 1);
              bytes = bytes.subarray(0, bytes.length - 1);
            }
            if (bytes.length >= 2) session._pushPcmRecord(bytes);
          }
          session._maybeFlushAudio(true);
        }
      } catch (err) {
        if (!session.aborted && (!err || err.name !== "AbortError")) {
          showSpeechBanner(String(err));
          session.stop();
          return;
        }
      } finally {
        if (!session.aborted && session.inputEnded && session.queue.length === 0 && session.pendingBytes >= 2) {
          session._flushPendingAudio(true);
        } else if (!session.aborted && session.hasAudio && session.pendingBytes >= 2) {
          session._flushPendingAudio(false);
        }
        session.workerRunning = false;
        session.abortCtrl = null;
        if (!session.aborted && session.queue.length > 0) {
          session._scheduleDrain();
        } else {
          session._doneCheck();
        }
      }
    },
    _pushPcmRecord(u8) {
      if (session.aborted || !u8 || u8.length < 2) return;
      const copy = new Uint8Array(u8);
      session.pcmChunks.push(copy);
      session.pendingAudio.push(copy);
      session.pendingBytes += copy.length;
      session._maybeFlushAudio(false);
    },
    _maybeFlushAudio(batchEnded = false) {
      if (session.aborted || session.pendingBytes < 2) return;
      const ctx = audioCtx;
      const runway = (ctx && session.nextTime > ctx.currentTime) ? (session.nextTime - ctx.currentTime) : 0;
      if (!session.hasAudio) {
        if (session.pendingBytes < TTS_INITIAL_PREBUF_BYTES && !(batchEnded && session.queue.length === 0)) return;
      } else if (runway <= 0.015) {
        if (session.pendingBytes < TTS_REBUF_BYTES && !(batchEnded && session.queue.length === 0)) return;
      } else {
        const lowRunway = runway < 0.25 && session.pendingBytes >= TTS_LOW_RUNWAY_BYTES;
        const streamDone = batchEnded && session.inputEnded && session.queue.length === 0;
        if (session.pendingBytes < TTS_COALESCE_BYTES && !lowRunway && !streamDone) return;
      }
      const isFinalStream = Boolean(batchEnded && session.inputEnded && session.queue.length === 0);
      session._flushPendingAudio(isFinalStream);
    },
    _flushPendingAudio(isFinal = false) {
      if (session.aborted || session.pendingBytes < 2) return;
      unlockAudio();
      const ctx = audioCtx;
      if (!ctx) return;
      const { aligned, totalBytes } = normalizePcmChunks(session.pendingAudio);
      session.pendingAudio = [];
      session.pendingBytes = 0;
      const sampleCount = Math.floor(totalBytes / 2);
      if (sampleCount <= 0) return;

      const joined = new Uint8Array(sampleCount * 2);
      let off = 0;
      for (const c of aligned) {
        const take = Math.min(c.length, joined.length - off);
        if (take > 0) {
          joined.set(take === c.length ? c : c.subarray(0, take), off);
          off += take;
        }
      }

      const contiguous = session.hasAudio && session.nextTime > (ctx.currentTime + 0.015);
      const startAt = contiguous ? session.nextTime : (ctx.currentTime + 0.035);
      const floats = pcmInt16ToSoftFloats(joined, sampleCount, !contiguous, isFinal);

      const audioBuf = ctx.createBuffer(1, sampleCount, TTS_SAMPLE_RATE);
      audioBuf.copyToChannel(floats, 0);
      const src = ctx.createBufferSource();
      src.buffer = audioBuf;
      src.connect(ctx.destination);
      session.nextTime = startAt + audioBuf.duration;
      session.sources.add(src);
      src.onended = () => {
        session.sources.delete(src);
        session._doneCheck();
      };
      if (!session.hasAudio) {
        session.hasAudio = true;
        session._syncBtnState();
      }
      src.start(startAt);
    },
  };

  activeLiveSpeech = session;
  if (startBtn) activeTtsBtn = startBtn;
  if (existingBtn) session._syncBtnState();
  return session;
}

function showSpeechBanner(message, missingModel = "") {
  const banner = document.getElementById("setup-banner");
  const textEl = document.getElementById("setup-text");
  const actEl = document.getElementById("setup-actions");
  const bar = document.getElementById("pull-bar");
  if (!banner || !textEl || !actEl || pullingModel) return;
  if (bar) bar.classList.remove("show");
  actEl.innerHTML = "";
  banner.classList.add("show");
  textEl.textContent = message;
  if (missingModel) {
    const pullBtn = document.createElement("button");
    pullBtn.type = "button";
    pullBtn.className = "btn-white";
    pullBtn.textContent = `Pull ${missingModel}`;
    pullBtn.onclick = () => pullModel(missingModel, false);
    actEl.appendChild(pullBtn);
  }
  const dismissBtn = document.createElement("button");
  dismissBtn.type = "button";
  dismissBtn.textContent = "Dismiss";
  dismissBtn.onclick = () => updateSetupBanner();
  actEl.appendChild(dismissBtn);
}

function playAudioUrlForBtn(btn, url) {
  const audio = new Audio(url);
  activeTtsAudio = audio;
  btn.classList.remove("loading");
  btn.classList.add("speaking");
  btn.innerHTML = SVG_SPEAKER;
  btn.title = "Stop speaking";
  const acts = btn.closest(".msg-actions");
  if (acts) acts.classList.add("tts-active");
  audio.addEventListener("ended", () => {
    if (activeTtsBtn === btn) stopTtsPlayback();
  });
  audio.addEventListener("error", () => {
    if (activeTtsBtn === btn) stopTtsPlayback();
  });
  audio.play().catch(() => {
    if (activeTtsBtn === btn) stopTtsPlayback();
  });
}

function speakReply(btn, rawText) {
  if (!btn) return;
  if (activeTtsBtn === btn) {
    stopTtsPlayback();
    return;
  }
  stopTtsPlayback();
  const model = (appSettings.tts_model || "lfm3-audio:2b").trim();
  const voice = (appSettings.tts_voice || "").trim();
  const cacheKey = ttsCacheKey(model, voice);

  if (btn._ttsUrl && btn._ttsKey === cacheKey) {
    activeTtsBtn = btn;
    playAudioUrlForBtn(btn, btn._ttsUrl);
    return;
  }

  const textStr = String(rawText || "").trim();
  if (!textStr) return;
  const buf = new SpeechSentenceBuffer();
  const pieces = buf.finish(textStr);
  if (!pieces.length) return;

  const session = createLiveSpeechSession(model, voice, null, btn);
  session.finish(pieces);
}

let ttsWarmState = "";
let ttsWarmSeq = 0;
let _bootTtsWarmed = false;

async function warmTtsModel(showMissingBanner = true) {
  if (typeof appSettings === "undefined" || !appSettings.tts_auto || !ollamaRunning) return;
  const seq = ++ttsWarmSeq;
  _bootTtsWarmed = true;
  ttsWarmState = "loading";
  renderComposerBar();
  const model = (appSettings.tts_model || "lfm3-audio:2b").trim();
  try {
    const res = await fetch("/api/tts/warm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model }),
    });
    const data = await res.json().catch(() => ({}));
    if (seq !== ttsWarmSeq || !appSettings.tts_auto) return;
    if (!res.ok) {
      ttsWarmState = "";
      renderComposerBar();
      if (res.status === 404 && data.missing_model && showMissingBanner) {
        showSpeechBanner(data.error || `Speech model "${data.missing_model}" is not installed in Ollama yet.`, data.missing_model);
      }
      return;
    }
    ttsWarmState = "ready";
    renderComposerBar();
    setTimeout(() => {
      if (seq === ttsWarmSeq && ttsWarmState === "ready") {
        ttsWarmState = "";
        renderComposerBar();
      }
    }, 3800);
  } catch (_) {
    if (seq === ttsWarmSeq) {
      ttsWarmState = "";
      renderComposerBar();
    }
  }
}

async function unloadTtsModel(model) {
  if (!ollamaRunning) return;
  const eff = (model || (typeof appSettings !== "undefined" && appSettings.tts_model) || "lfm3-audio:2b").trim();
  if (!eff) return;
  try {
    await fetch("/api/tts/unload", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: eff }),
    });
  } catch (_) {}
}

async function toggleTtsAuto() {
  appSettings.tts_auto = !appSettings.tts_auto;
  if (!appSettings.tts_auto) {
    ttsWarmSeq++;
    ttsWarmState = "";
    stopTtsPlayback();
  }
  syncPlusMenuState();
  renderComposerBar();
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tts_auto: !!appSettings.tts_auto }),
    });
  } catch (_) {}
  if (appSettings.tts_auto) {
    warmTtsModel(true);
  } else {
    unloadTtsModel();
  }
}

function buildMsgActions(rawText) {
  const acts = document.createElement("div");
  acts.className = "msg-actions";
  const spk = document.createElement("button");
  spk.type = "button";
  spk.className = "speak-btn";
  spk.title = "Read aloud";
  spk.setAttribute("aria-label", "Read aloud");
  spk.innerHTML = SVG_SPEAKER;
  spk.onclick = () => speakReply(spk, rawText || "");
  const cpy = document.createElement("button");
  cpy.type = "button";
  cpy.className = "copy-btn";
  cpy.textContent = "Copy reply";
  cpy.onclick = () => copyText(cpy, rawText || "");
  acts.appendChild(spk);
  acts.appendChild(cpy);
  return acts;
}

function buildThinkingBox(initialText = "", modelName = currentModel) {
  const pal = getModelPalette(modelName);
  const det = document.createElement("details");
  det.className = "think-box";
  det.innerHTML = `<summary>${SVG_TRIANGLE}<span class="think-label">Thinking</span></summary><div class="think-body"></div>`;
  det.querySelector(".tri-svg").style.color = pal[2];
  det.querySelector(".think-body").textContent = initialText;
  return det;
}

function buildToolCard(t, isRunning = false, modelName = currentModel) {
  const pal = getModelPalette(modelName);
  const det = document.createElement("details");
  det.className = "tool-pill";

  const preview = Object.values(t.args || {}).map(v => String(v)).join(" ").slice(0, 70);
  const cmdCopyText = (t.args && t.args.command) ? t.args.command : JSON.stringify(t.args || {}, null, 2);

  const sum = document.createElement("summary");
  sum.innerHTML = `
    ${SVG_TRIANGLE}
    <span class="tool-fn"></span>
    <span class="tool-preview"></span>
    <button type="button" class="copy-btn">Copy</button>
  `;
  const tri = sum.querySelector(".tri-svg");
  tri.style.color = isRunning ? pal[1] : pal[2];
  if (isRunning) tri.classList.add("running");

  const fnEl = sum.querySelector(".tool-fn");
  fnEl.textContent = t.name;
  fnEl.style.color = pal[1];

  sum.querySelector(".tool-preview").textContent = preview;
  const copyBtn = sum.querySelector(".copy-btn");
  copyBtn.onclick = (e) => {
    e.preventDefault();
    e.stopPropagation();
    copyText(copyBtn, cmdCopyText);
  };

  const detail = document.createElement("div");
  detail.className = "tool-detail";
  detail.innerHTML = `
    <div class="tool-sublabel">Input</div>
    <pre class="tool-pre tool-in"></pre>
    <div class="tool-sublabel">Output</div>
    <pre class="tool-pre tool-out"></pre>
  `;
  detail.querySelector(".tool-in").textContent = JSON.stringify(t.args || {}, null, 2);
  detail.querySelector(".tool-out").textContent = t.result || (isRunning ? "(running...)" : "(no output)");

  det.appendChild(sum);
  det.appendChild(detail);
  return det;
}

function refreshTitleOverflow() {
  for (const div of document.querySelectorAll("#chat-list .chat-item")) {
    const wrap = div.querySelector(".chat-title-wrap");
    const track = div.querySelector(".chat-title-track");
    if (!wrap || !track) continue;
    div.classList.remove("has-overflow");
    if (wrap.clientWidth > 20 && track.scrollWidth > wrap.clientWidth + 2) {
      div.classList.add("has-overflow");
      const dur = Math.max(4.5, (track.scrollWidth + 34) / 34).toFixed(2);
      div.style.setProperty("--scroll-dur", `${dur}s`);
    }
  }
}

async function loadChatList() {
  const res = await fetch("/api/chats");
  const chats = await res.json();
  const list = document.getElementById("chat-list");
  list.innerHTML = "";
  for (const c of chats) {
    const label = c.title || c.id;
    const div = document.createElement("div");
    div.className = "chat-item" + (c.id === currentChatId ? " active" : "");
    div.onclick = () => openChat(c.id);

    const wrap = document.createElement("div");
    wrap.className = "chat-title-wrap";
    const track = document.createElement("div");
    track.className = "chat-title-track";

    const mainSpan = document.createElement("span");
    mainSpan.className = "chat-title-main";
    mainSpan.textContent = label;

    const dupSpan = document.createElement("span");
    dupSpan.className = "chat-title-dup";
    dupSpan.setAttribute("aria-hidden", "true");
    dupSpan.textContent = label;

    track.appendChild(mainSpan);
    track.appendChild(dupSpan);
    wrap.appendChild(track);

    const del = document.createElement("button");
    del.className = "del-btn";
    del.innerHTML = SVG_CLOSE;
    del.title = "Delete chat";
    del.onclick = async (e) => {
      e.stopPropagation();
      await fetch("/api/chats/" + encodeURIComponent(c.id), { method: "DELETE" });
      if (currentChatId === c.id) newChat();
      else loadChatList();
    };
    div.appendChild(wrap);
    div.appendChild(del);
    list.appendChild(div);
  }
  refreshTitleOverflow();
}

const WELCOME_QUESTIONS = [
  "What are we working on?",
  "Need a command you forgot?",
  "Debugging something today?",
  "Working on another device?",
  "Want to inspect a file or folder?",
  "Got a quick tech question?",
];
let qIndex = 0;
let qTimer = null;
let qSwapTimer = null;

function rotateWelcomeQuestion() {
  const main = document.getElementById("main-pane");
  const el = document.getElementById("rotating-q");
  if (!main || !el || !main.classList.contains("is-empty") || document.hidden) return;
  el.classList.add("q-exit");
  clearTimeout(qSwapTimer);
  qSwapTimer = setTimeout(() => {
    if (!main.classList.contains("is-empty")) {
      el.classList.remove("q-exit");
      return;
    }
    qIndex = (qIndex + 1) % WELCOME_QUESTIONS.length;
    el.classList.remove("q-exit");
    el.classList.add("q-enter");
    el.textContent = WELCOME_QUESTIONS[qIndex];
    void el.offsetWidth;
    el.classList.remove("q-enter");
  }, 260);
}

function startQuestionRotation() {
  stopQuestionRotation();
  qTimer = setInterval(rotateWelcomeQuestion, 3500);
}

function stopQuestionRotation() {
  clearInterval(qTimer);
  clearTimeout(qSwapTimer);
  qTimer = null;
  qSwapTimer = null;
  const el = document.getElementById("rotating-q");
  if (el) el.classList.remove("q-exit", "q-enter");
}

let welcomeMascotPauseTimer = null;

function initWelcomeMascot() {
  const box = document.getElementById("welcome-mascot");
  if (!box || welcomeMascot || typeof BooMascot === "undefined") return;
  const pal = getModelPalette(currentModel);
  welcomeMascot = BooMascot.mount(box, {
    variant: "with-purple",
    face: "soft",
    theme: "dark",
    mood: "auto",
    mouth: "happy",
    ringTop: pal[3],
    ringBottom: pal[4],
  });
  const main = document.getElementById("main-pane");
  if (main && !main.classList.contains("is-empty") && typeof welcomeMascot.setPaused === "function") {
    welcomeMascot.setPaused(true);
  }
}

function setEmptyState(isEmpty) {
  const main = document.getElementById("main-pane");
  if (!main) return;
  const wasEmpty = main.classList.contains("is-empty");
  main.classList.toggle("is-empty", isEmpty);
  clearTimeout(welcomeMascotPauseTimer);
  if (isEmpty && !wasEmpty) {
    qIndex = 0;
    const el = document.getElementById("rotating-q");
    if (el) el.textContent = WELCOME_QUESTIONS[0];
    startQuestionRotation();
    if (welcomeMascot && typeof welcomeMascot.setPaused === "function") welcomeMascot.setPaused(false);
  } else if (isEmpty) {
    if (!qTimer) startQuestionRotation();
    if (welcomeMascot && typeof welcomeMascot.setPaused === "function") welcomeMascot.setPaused(false);
  } else {
    stopQuestionRotation();
    if (welcomeMascot && typeof welcomeMascot.setPaused === "function") {
      welcomeMascotPauseTimer = setTimeout(() => {
        if (welcomeMascot) welcomeMascot.setPaused(true);
      }, 320);
    }
  }
}

let userScrolledAway = false;
let lastAutoScrollAt = 0;
let lastObservedScrollTop = 0;
let activeStreamRow = null;
let activeStreamFlush = null;

function scrollBottom() {
  const area = document.getElementById("scroll-area");
  if (!area) return;
  lastAutoScrollAt = performance.now();
  area.scrollTop = area.scrollHeight;
  lastObservedScrollTop = area.scrollTop;
}

function scrollFollow(anchorRow) {
  if (userScrolledAway) return;
  const area = document.getElementById("scroll-area");
  if (!area || area.scrollHeight <= area.clientHeight) return;
  const bottomTarget = Math.max(0, area.scrollHeight - area.clientHeight);
  let target = bottomTarget;
  if (anchorRow && anchorRow.isConnected) {
    const topOffset = anchorRow.getBoundingClientRect().top - area.getBoundingClientRect().top;
    const anchorTop = Math.max(0, area.scrollTop + topOffset - 16);
    target = Math.min(bottomTarget, Math.max(area.scrollTop, anchorTop));
  }
  if (target > area.scrollTop + 1) {
    lastAutoScrollAt = performance.now();
    area.scrollTop = target;
    lastObservedScrollTop = area.scrollTop;
  }
}

function buildOutputsRow(outputs) {
  const wrap = document.createElement("div");
  wrap.className = "file-dl-row";
  for (const out of outputs) {
    if (!out || !out.url || !out.name) continue;
    const a = document.createElement("a");
    a.className = "file-dl-pill";
    a.href = out.url;
    a.download = out.name;
    a.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg><span class="file-dl-name"></span>`;
    const lbl = out.size ? `${out.name} (${fmtFileSize(out.size)})` : out.name;
    a.querySelector(".file-dl-name").textContent = lbl;
    wrap.appendChild(a);
  }
  return wrap;
}

function renderMessage(m) {
  setEmptyState(false);
  const col = document.getElementById("chat-col");
  const row = document.createElement("div");
  row.className = "msg-row " + m.role;

  if (m.role === "user") {
    if (Array.isArray(m.attachments) && m.attachments.length) {
      const attRow = document.createElement("div");
      attRow.className = "user-att-row";
      for (const a of m.attachments) {
        const name = typeof a === "string" ? a : (a && a.name) || "";
        if (!name) continue;
        const pill = document.createElement("span");
        pill.className = "user-att-pill";
        pill.textContent = name;
        attRow.appendChild(pill);
      }
      if (attRow.children.length) row.appendChild(attRow);
    }
    const bubble = document.createElement("div");
    bubble.className = "user-bubble";
    bubble.textContent = m.text;
    row.appendChild(bubble);
    col.appendChild(row);
    scrollBottom();
    return row;
  }

  const msgModel = m.model || currentModel;
  row.style.setProperty("--msg-accent", getModelPalette(msgModel)[2]);
  const head = document.createElement("div");
  head.className = "asst-head";
  head.innerHTML = `<div class="m-mark"><span></span><span></span><span></span><span></span><span></span></div><span class="asst-name"></span>`;
  head.querySelector(".asst-name").textContent = msgModel;
  if (m.elapsed) {
    const tm = document.createElement("span");
    tm.textContent = `· ${m.elapsed.toFixed(1)}s`;
    head.appendChild(tm);
  }
  row.appendChild(head);

  if (m.thinking || (m.tools && m.tools.length)) {
    const steps = document.createElement("div");
    steps.className = "steps-wrap";
    if (m.thinking) steps.appendChild(buildThinkingBox(m.thinking, msgModel));
    for (const t of (m.tools || [])) steps.appendChild(buildToolCard(t, false, msgModel));
    row.appendChild(steps);
  }

  const prose = document.createElement("div");
  prose.className = "prose";
  prose.innerHTML = renderMarkdown(m.text);
  row.appendChild(prose);

  if (Array.isArray(m.outputs) && m.outputs.length) {
    row.appendChild(buildOutputsRow(m.outputs));
  }

  row.appendChild(buildMsgActions(m.text));

  col.appendChild(row);
  scrollBottom();
  return row;
}

async function openChat(id) {
  if (busy && currentAbort) currentAbort.abort();
  if (activeStreamFlush) activeStreamFlush();
  stopTtsPlayback();
  userScrolledAway = false;
  activeStreamRow = null;
  closePlusMenu();
  draftChatId = null;
  pendingFiles = [];
  renderComposerBar();
  const res = await fetch("/api/chats/" + encodeURIComponent(id));
  const data = await res.json();
  currentChatId = data.id;
  if (data.model) selectModel(data.model);
  const tok = data.tokens || {};
  updateCtxBadge(tok.used || 0, tok.max || MAX_CTX);
  document.getElementById("chat-col").innerHTML = "";
  const msgs = data.ui_messages || [];
  setEmptyState(msgs.length === 0);
  for (const m of msgs) renderMessage(m);
  loadChatList();
}

function newChat() {
  if (busy && currentAbort) currentAbort.abort();
  if (activeStreamFlush) activeStreamFlush();
  stopTtsPlayback();
  userScrolledAway = false;
  activeStreamRow = null;
  closePlusMenu();
  currentChatId = null;
  draftChatId = null;
  pendingFiles = [];
  renderComposerBar();
  updateCtxBadge(0, MAX_CTX);
  document.getElementById("chat-col").innerHTML = "";
  setEmptyState(true);
  loadChatList();
  document.getElementById("input").focus();
}

let audioCtx = null;
function unlockAudio() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!audioCtx) audioCtx = new AC();
    if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  } catch (_) {}
}

let appSettings = {
  default_model: "",
  sound: "chime",
  max_tool_steps: 8,
  tts_model: "lfm3-audio:2b",
  tts_voice: "",
  tts_auto: false,
};

function playDoneChime(force = false, soundName = appSettings.sound) {
  try {
    if (!soundName || soundName === "off") return;
    if (!force && !document.hidden && document.hasFocus()) return;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!audioCtx) audioCtx = new AC();
    if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
    const now = audioCtx.currentTime;
    const patterns = {
      chime:  [[587.33, 0, 0.24, 0.06], [880, 0.09, 0.26, 0.06]],
      ping:   [[784, 0, 0.28, 0.06]],
      double: [[659.25, 0, 0.14, 0.055], [659.25, 0.12, 0.18, 0.055]],
      chord:  [[523.25, 0, 0.34, 0.04], [659.25, 0.02, 0.34, 0.04], [783.99, 0.04, 0.34, 0.04]],
    };
    const notes = patterns[soundName] || patterns.chime;
    for (const [freq, delay, dur, peak] of notes) {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      const t0 = now + delay;
      osc.type = "sine";
      osc.frequency.setValueAtTime(freq, t0);
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.linearRampToValueAtTime(peak, t0 + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start(t0);
      osc.stop(t0 + dur + 0.02);
    }
  } catch (_) {}
}

async function sendMsg() {
  if (busy) return;
  if (pendingFiles.some(f => f.uploading)) {
    setUploadNotice("Please wait for file upload to finish.");
    return;
  }
  const input = document.getElementById("input");
  const text = input.value.trim();
  const attachedNames = pendingFiles.map(f => f.name);
  if (!text && !attachedNames.length) return;
  if (activeStreamFlush) activeStreamFlush();
  userScrolledAway = false;
  const requestChatId = currentChatId || draftChatId;
  const enabledPacksList = getEnabledPacksList();

  unlockAudio();
  closePlusMenu();
  input.value = "";
  autoResize(input);
  pendingFiles = [];
  draftChatId = null;
  renderComposerBar();
  busy = true;
  currentAbort = new AbortController();
  setSendBtnState(true);

  renderMessage({
    role: "user",
    text: text || "(attached files)",
    attachments: attachedNames,
  });

  const activeModel = currentModel;
  const pal = getModelPalette(activeModel);
  const col = document.getElementById("chat-col");
  const row = document.createElement("div");
  row.className = "msg-row assistant";
  row.style.setProperty("--msg-accent", pal[2]);
  row.innerHTML = `
    <div class="asst-head">
      <div class="m-mark"><span></span><span></span><span></span><span></span><span></span></div>
      <span class="asst-name"></span>
      <span class="asst-time"></span>
    </div>
    <div class="steps-wrap"></div>
    <div class="css-loader">
      <span class="ring-spin"></span>
      <div class="bars"><span></span><span></span><span></span><span></span><span></span></div>
      <span class="loader-sec">Thinking... 0.0s</span>
    </div>
  `;
  row.querySelector(".asst-name").textContent = activeModel;
  col.appendChild(row);
  scrollBottom();

  const stepsWrap = row.querySelector(".steps-wrap");
  const loaderEl = row.querySelector(".css-loader");
  const loaderSec = row.querySelector(".loader-sec");

  const tStart = performance.now();
  let loaderPhase = "Thinking";
  let workingHoldUntil = 0;
  let pendingPhaseTimer = null;
  function applyLoaderPhase(phase) {
    if (phase === "loading") {
      loaderEl.classList.add("is-loading");
      loaderPhase = "Loading model";
    } else if (phase === "working") {
      loaderEl.classList.remove("is-loading");
      loaderPhase = "Working";
    } else {
      loaderEl.classList.remove("is-loading");
      loaderPhase = "Thinking";
    }
    const s = ((performance.now() - tStart) / 1000).toFixed(1);
    loaderSec.textContent = `${loaderPhase}... ${s}s`;
  }
  function setLoaderPhase(phase, minHoldMs = 0) {
    clearTimeout(pendingPhaseTimer);
    pendingPhaseTimer = null;
    if (phase === "working" && minHoldMs > 0) {
      workingHoldUntil = Math.max(workingHoldUntil, performance.now() + minHoldMs);
    }
    if (phase === "thinking" && performance.now() < workingHoldUntil) {
      applyLoaderPhase("working");
      pendingPhaseTimer = setTimeout(() => {
        pendingPhaseTimer = null;
        if (loaderEl.isConnected) applyLoaderPhase("thinking");
      }, Math.max(0, workingHoldUntil - performance.now()));
      return;
    }
    applyLoaderPhase(phase);
  }
  const secTimer = setInterval(() => {
    const s = ((performance.now() - tStart) / 1000).toFixed(1);
    loaderSec.textContent = `${loaderPhase}... ${s}s`;
  }, 100);

  let thinkBox = null;
  const toolCards = {};
  const turnOutputs = [];
  const autoSpeakActive = Boolean(appSettings.tts_auto);
  const speechModel = appSettings.tts_model || "lfm3-audio:2b";
  const speechBuf = autoSpeakActive ? new SpeechSentenceBuffer() : null;
  let liveSpeech = null;
  let speechDoneFlushed = false;
  function flushSpeechChunks(pieces) {
    if (!autoSpeakActive || !pieces || !pieces.length) return;
    if (!liveSpeech) {
      liveSpeech = createLiveSpeechSession(speechModel, appSettings.tts_voice || "", row.querySelector(".asst-head"));
    }
    if (liveSpeech && !liveSpeech.aborted) {
      for (const p of pieces) liveSpeech.enqueue(p);
    }
  }
  function finalizeLiveSpeech(replyText) {
    if (!autoSpeakActive || !speechBuf || speechDoneFlushed) return;
    speechDoneFlushed = true;
    const cleanReply = String(replyText || "");
    if (!cleanReply || cleanReply.startsWith("**Error:**")) return;
    const tail = speechBuf.finish(cleanReply);
    if (!liveSpeech && tail.length) {
      liveSpeech = createLiveSpeechSession(speechModel, appSettings.tts_voice || "", row.querySelector(".asst-head"));
    }
    if (liveSpeech && !liveSpeech.aborted) liveSpeech.finish(tail);
  }

  let streamTarget = "";
  let streamShown = 0;
  let streamFinalReply = null;
  let streamOutputs = [];
  let streamServerDone = false;
  let streamFinalized = false;
  let streamTimer = null;
  let liveProse = null;

  activeStreamRow = row;

  function ensureLiveProse() {
    if (liveProse && liveProse.isConnected) return liveProse;
    liveProse = document.createElement("div");
    liveProse.className = "prose";
    if (loaderEl.isConnected && loaderEl.parentElement === row) {
      row.insertBefore(liveProse, loaderEl);
    } else {
      row.appendChild(liveProse);
    }
    return liveProse;
  }

  function resetLiveDraft() {
    clearInterval(streamTimer);
    streamTimer = null;
    streamTarget = "";
    streamShown = 0;
    if (speechBuf) speechBuf.reset();
    if (liveProse && liveProse.isConnected) liveProse.remove();
    liveProse = null;
    if (loaderEl.isConnected) loaderEl.style.display = "";
  }

  function finishReply() {
    if (streamFinalized) return;
    streamFinalized = true;
    clearInterval(streamTimer);
    streamTimer = null;
    if (activeStreamFlush === flushImmediate) activeStreamFlush = null;
    if (activeStreamRow === row) activeStreamRow = null;
    if (!row.isConnected) return;

    const finalText = streamFinalReply !== null ? streamFinalReply : (streamTarget || "(empty)");
    const prose = ensureLiveProse();
    prose.innerHTML = renderMarkdown(finalText);
    if (loaderEl.isConnected) loaderEl.remove();
    if (!stepsWrap.children.length) stepsWrap.remove();

    if (streamOutputs.length) {
      row.appendChild(buildOutputsRow(streamOutputs));
    }

    const acts = buildMsgActions(finalText);
    row.appendChild(acts);
    loadChatList();
    playDoneChime();
    const spk = acts.querySelector(".speak-btn");
    if (autoSpeakActive && !finalText.startsWith("**Error:**")) {
      finalizeLiveSpeech(finalText);
      if (spk && liveSpeech) {
        liveSpeech.attachFinalButton(spk);
      }
    } else if (liveSpeech && liveSpeech.liveHeaderBtn && liveSpeech.liveHeaderBtn.isConnected) {
      liveSpeech.stop();
    }
  }

  function flushImmediate() {
    clearInterval(streamTimer);
    streamTimer = null;
    if (activeStreamFlush === flushImmediate) activeStreamFlush = null;
    if (streamServerDone && !streamFinalized) {
      streamShown = streamTarget.length;
      finishReply();
    }
  }
  activeStreamFlush = flushImmediate;

  function renderRevealTick() {
    if (!row.isConnected) {
      clearInterval(streamTimer);
      streamTimer = null;
      if (activeStreamFlush === flushImmediate) activeStreamFlush = null;
      return;
    }
    const backlog = streamTarget.length - streamShown;
    if (backlog > 0) {
      const step = streamServerDone
        ? Math.max(2, Math.min(28, Math.ceil(backlog / 9)))
        : Math.max(1, Math.min(8, Math.ceil(backlog / 14)));
      streamShown = snapStreamSliceEnd(streamTarget, Math.min(streamTarget.length, streamShown + step));
      if (streamServerDone && streamShown >= streamTarget.length) {
        finishReply();
        return;
      }
      const prose = ensureLiveProse();
      if (loaderEl.isConnected) loaderEl.style.display = "none";
      prose.innerHTML = renderStreamingMarkdown(streamTarget.slice(0, streamShown));
      appendStreamCursor(prose);
      scrollFollow(row);
    } else if (streamServerDone) {
      finishReply();
    }
  }

  function ensureRevealLoop() {
    if (streamTimer || streamFinalized) return;
    streamTimer = setInterval(renderRevealTick, 32);
  }

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: requestChatId,
        model: activeModel,
        message: text,
        tools_enabled: activePacks.system,
        enabled_packs: enabledPacksList,
        attached_files: attachedNames,
      }),
      signal: currentAbort.signal,
    });

    if (!res.ok) {
      const errJson = await res.json().catch(() => ({}));
      throw new Error(errJson.error || `HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();

      for (const ln of lines) {
        if (!ln.trim()) continue;
        const ev = JSON.parse(ln);

        if (ev.type === "phase") {
          setLoaderPhase(ev.phase);
        } else if (ev.type === "title") {
          currentChatId = ev.chat_id;
          loadChatList();
        } else if (ev.type === "thinking_delta") {
          if (!thinkBox) {
            thinkBox = buildThinkingBox("", activeModel);
            stepsWrap.prepend(thinkBox);
          }
          const tb = thinkBox.querySelector(".think-body");
          tb.textContent += ev.text;
          tb.scrollTop = tb.scrollHeight;
          thinkBox.querySelector(".think-label").textContent = `Thinking (${tb.textContent.length} chars)`;
          scrollFollow(row);
        } else if (ev.type === "tool_start") {
          resetLiveDraft();
          setLoaderPhase("working", 400);
          const card = buildToolCard(ev, true, activeModel);
          toolCards[ev.id] = card;
          stepsWrap.appendChild(card);
          scrollFollow(row);
        } else if (ev.type === "tool_end") {
          const card = toolCards[ev.id];
          if (card) {
            const tri = card.querySelector(".tri-svg");
            if (tri) {
              tri.classList.remove("running");
              tri.style.color = pal[2];
            }
            card.querySelector(".tool-out").textContent = ev.result || "(no output)";
          }
          setLoaderPhase("working", 250);
          scrollFollow(row);
        } else if (ev.type === "reply_delta" && ev.text) {
          streamTarget += ev.text;
          ensureRevealLoop();
          if (speechBuf) flushSpeechChunks(speechBuf.pushDelta(ev.text));
        } else if (ev.type === "file_ready" && ev.name && ev.url) {
          const item = { name: ev.name, url: ev.url, size: ev.size };
          const idx = turnOutputs.findIndex(o => o.name === ev.name);
          if (idx >= 0) turnOutputs[idx] = item;
          else turnOutputs.push(item);
        } else if (ev.type === "tokens" && ev.tokens) {
          updateCtxBadge(ev.tokens.used, ev.tokens.max);
        } else if (ev.type === "done") {
          clearInterval(secTimer);
          clearTimeout(pendingPhaseTimer);
          if (loaderEl.isConnected) loaderEl.remove();
          currentChatId = ev.chat_id;
          if (ev.tokens) updateCtxBadge(ev.tokens.used, ev.tokens.max);
          if (ev.elapsed) row.querySelector(".asst-time").textContent = `· ${ev.elapsed.toFixed(1)}s`;
          if (!stepsWrap.children.length) stepsWrap.remove();

          streamFinalReply = ev.reply || "(empty)";
          streamTarget = streamFinalReply;
          streamShown = Math.min(streamShown, streamTarget.length);
          streamOutputs = (Array.isArray(ev.outputs) && ev.outputs.length) ? ev.outputs : turnOutputs.slice();
          streamServerDone = true;
          finalizeLiveSpeech(streamFinalReply);
          if (!streamTarget.length || streamShown >= streamTarget.length) {
            finishReply();
          } else {
            ensureRevealLoop();
          }
        }
      }
    }
  } catch (err) {
    stopTtsPlayback();
    clearInterval(secTimer);
    clearInterval(streamTimer);
    streamTimer = null;
    clearTimeout(pendingPhaseTimer);
    if (activeStreamFlush === flushImmediate) activeStreamFlush = null;
    if (activeStreamRow === row) activeStreamRow = null;
    if (loaderEl.isConnected) loaderEl.remove();
    const prose = ensureLiveProse();
    if (err && err.name === "AbortError") {
      const elapsed = ((performance.now() - tStart) / 1000).toFixed(1);
      row.querySelector(".asst-time").textContent = `· stopped (${elapsed}s)`;
      const partial = streamShown > 0 ? streamTarget.slice(0, streamShown) + "\n\n*(stopped by user)*" : "*(stopped by user)*";
      prose.innerHTML = renderMarkdown(partial);
      setTimeout(loadChatList, 300);
    } else {
      prose.innerHTML = renderMarkdown("**Error:** `" + String(err) + "`");
    }
    if (!stepsWrap.children.length) stepsWrap.remove();
  } finally {
    clearInterval(secTimer);
    clearTimeout(pendingPhaseTimer);
    busy = false;
    currentAbort = null;
    setSendBtnState(false);
    input.focus();
  }
}

let soulState = { active_id: "original", original: "", slots: [] };

function getActiveSoulSlot() {
  return soulState.slots.find(s => s.id === soulState.active_id) || soulState.slots[0] || null;
}

function syncCurrentSoulInputs() {
  const cur = getActiveSoulSlot();
  if (!cur) return;
  const nm = document.getElementById("soul-name");
  const tx = document.getElementById("soul-text");
  if (nm) cur.name = (nm.value.trim() || "Soul").slice(0, 24);
  if (tx) cur.content = tx.value;
}

function renderSoulModal() {
  const tabs = document.getElementById("soul-tabs");
  const cur = getActiveSoulSlot();
  if (tabs) {
    tabs.innerHTML = "";
    for (const slot of soulState.slots) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.dataset.soulId = slot.id;
      btn.className = "soul-tab" + (cur && slot.id === cur.id ? " active" : "");
      btn.textContent = slot.name || "Soul";
      btn.onclick = () => selectSoulSlot(slot.id);
      tabs.appendChild(btn);
    }
    if (soulState.slots.length < 4) {
      const addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "soul-tab soul-tab-add";
      addBtn.textContent = "+ New soul";
      addBtn.onclick = addSoulSlot;
      tabs.appendChild(addBtn);
    }
  }
  const nm = document.getElementById("soul-name");
  const tx = document.getElementById("soul-text");
  if (nm && cur) nm.value = cur.name || "";
  if (tx && cur) tx.value = cur.content || "";
}

function selectSoulSlot(id) {
  syncCurrentSoulInputs();
  soulState.active_id = id;
  renderSoulModal();
}

function updateActiveSoulName(val) {
  const cur = getActiveSoulSlot();
  if (!cur) return;
  cur.name = (val.trim() || "Soul").slice(0, 24);
  const btn = document.querySelector(`#soul-tabs .soul-tab[data-soul-id="${cur.id}"]`);
  if (btn) btn.textContent = cur.name;
}

function addSoulSlot() {
  syncCurrentSoulInputs();
  if (soulState.slots.length >= 4) return;
  const num = soulState.slots.length + 1;
  const id = "soul-" + Date.now().toString(36);
  soulState.slots.push({ id, name: `Soul ${num}`, content: "" });
  soulState.active_id = id;
  renderSoulModal();
  const tx = document.getElementById("soul-text");
  if (tx) tx.focus();
}

function deleteCurrentSoul() {
  const cur = getActiveSoulSlot();
  if (!cur) return;
  soulState.slots = soulState.slots.filter(s => s.id !== cur.id);
  if (!soulState.slots.length) {
    soulState.slots.push({ id: "original", name: "Original", content: soulState.original || "" });
  }
  soulState.active_id = soulState.slots[0].id;
  renderSoulModal();
}

function restoreOriginalSoul() {
  syncCurrentSoulInputs();
  const origText = soulState.original || "";
  const existingOrig = soulState.slots.find(s => s.id === "original");
  if (existingOrig) {
    existingOrig.name = "Original";
    existingOrig.content = origText;
    soulState.active_id = "original";
  } else if (soulState.slots.length < 4) {
    soulState.slots.unshift({ id: "original", name: "Original", content: origText });
    soulState.active_id = "original";
  } else {
    const cur = getActiveSoulSlot();
    if (cur) {
      cur.name = "Original";
      cur.content = origText;
    }
  }
  renderSoulModal();
}

async function openSoul() {
  const res = await fetch("/api/soul");
  const data = await res.json();
  if (data && Array.isArray(data.slots) && data.slots.length) {
    soulState = {
      active_id: data.active_id || data.slots[0].id,
      original: data.original || data.content || "",
      slots: data.slots.slice(0, 4).map(s => ({ id: s.id, name: s.name, content: s.content || "" })),
    };
  } else {
    const txt = (data && data.content) || "";
    soulState = {
      active_id: "original",
      original: txt,
      slots: [{ id: "original", name: "Original", content: txt }],
    };
  }
  renderSoulModal();
  document.getElementById("soul-modal").style.display = "flex";
}
function closeSoul() { document.getElementById("soul-modal").style.display = "none"; }
async function saveSoul() {
  syncCurrentSoulInputs();
  await fetch("/api/soul", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ active_id: soulState.active_id, slots: soulState.slots }),
  });
  closeSoul();
}

let defaultChips = [
  { label: "Check CPU, RAM & disk", prompt: "Check my CPU, RAM, GPU, and free disk space on this PC" },
  { label: "Check Downloads", prompt: "List the largest files in my Downloads folder" },
  { label: "Explain a command", prompt: "How do I compress a folder with tar and gzip?" },
];
let userChips = defaultChips.slice();

function renderChips() {
  const row = document.getElementById("chips-row");
  if (!row) return;
  row.innerHTML = "";
  for (const c of userChips) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip";
    btn.textContent = c.label;
    btn.title = c.prompt;
    btn.onclick = () => usePrompt(c.prompt);
    row.appendChild(btn);
  }
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "chip chip-edit";
  edit.textContent = "Edit";
  edit.title = "Customize quick buttons";
  edit.onclick = openChipsModal;
  row.appendChild(edit);
}

async function loadChips() {
  renderChips();
  try {
    const res = await fetch("/api/chips");
    const data = await res.json();
    if (Array.isArray(data.defaults)) defaultChips = data.defaults;
    if (Array.isArray(data.chips)) userChips = data.chips;
    renderChips();
  } catch (_) {}
}

function addChipRow(label = "", prompt = "") {
  const box = document.getElementById("chips-rows");
  const row = document.createElement("div");
  row.className = "chip-edit-row";

  const lblIn = document.createElement("input");
  lblIn.type = "text";
  lblIn.placeholder = "Button label";
  lblIn.maxLength = 40;
  lblIn.value = label;

  const prmIn = document.createElement("input");
  prmIn.type = "text";
  prmIn.placeholder = "Prompt or task to run...";
  prmIn.maxLength = 500;
  prmIn.value = prompt;

  const del = document.createElement("button");
  del.type = "button";
  del.className = "chip-del";
  del.innerHTML = SVG_CLOSE;
  del.title = "Remove button";
  del.onclick = () => row.remove();

  row.appendChild(lblIn);
  row.appendChild(prmIn);
  row.appendChild(del);
  box.appendChild(row);
}

function openChipsModal() {
  const box = document.getElementById("chips-rows");
  box.innerHTML = "";
  const list = userChips.length ? userChips : [{ label: "", prompt: "" }];
  for (const c of list) addChipRow(c.label, c.prompt);
  document.getElementById("chips-modal").style.display = "flex";
}

function closeChipsModal() {
  document.getElementById("chips-modal").style.display = "none";
}

function resetChipRows() {
  const box = document.getElementById("chips-rows");
  box.innerHTML = "";
  for (const c of defaultChips) addChipRow(c.label, c.prompt);
}

async function saveChipsModal() {
  const rows = Array.from(document.querySelectorAll("#chips-rows .chip-edit-row"));
  const items = [];
  for (const r of rows) {
    const inputs = r.querySelectorAll("input");
    const label = inputs[0].value.trim();
    const prompt = inputs[1].value.trim();
    if (label && prompt) items.push({ label, prompt });
  }
  const res = await fetch("/api/chips", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chips: items }),
  });
  const data = await res.json();
  if (Array.isArray(data.chips)) userChips = data.chips;
  renderChips();
  closeChipsModal();
}

async function loadSettings() {
  try {
    const res = await fetch("/api/settings");
    const data = await res.json();
    if (data && typeof data === "object") {
      appSettings.default_model = data.default_model || "";
      appSettings.sound = data.sound || "chime";
      appSettings.max_tool_steps = Number(data.max_tool_steps) || 8;
      appSettings.tts_model = data.tts_model || "lfm3-audio:2b";
      appSettings.tts_voice = data.tts_voice || "";
      appSettings.tts_auto = !!data.tts_auto;
      syncPlusMenuState();
      renderComposerBar();
    }
  } catch (_) {}
}

function updateStepsLabel(v) {
  const el = document.getElementById("set-max-steps-val");
  if (el) el.textContent = String(v);
}

function openSettings() {
  const selModel = document.getElementById("set-default-model");
  selModel.innerHTML = "";
  const autoOpt = document.createElement("option");
  autoOpt.value = "";
  autoOpt.textContent = "Auto (first installed)";
  selModel.appendChild(autoOpt);

  const list = availableModels.slice();
  if (appSettings.default_model && !list.includes(appSettings.default_model)) {
    list.unshift(appSettings.default_model);
  }
  for (const m of list) {
    const opt = document.createElement("option");
    opt.value = m;
    const p = getModelParamLabel(m);
    opt.textContent = p ? `${m} · ${p}` : m;
    selModel.appendChild(opt);
  }
  selModel.value = appSettings.default_model || "";
  document.getElementById("set-sound").value = appSettings.sound || "chime";
  const steps = Number(appSettings.max_tool_steps) || 8;
  const stepsEl = document.getElementById("set-max-steps");
  if (stepsEl) stepsEl.value = String(steps);
  updateStepsLabel(steps);

  const selTts = document.getElementById("set-tts-model");
  if (selTts) {
    selTts.innerHTML = "";
    const curTts = appSettings.tts_model || "lfm3-audio:2b";
    const ttsOpts = Array.from(new Set(["lfm3-audio:2b", "phonon", "phonon:en", curTts, ...installedModels].filter(Boolean)));
    for (const m of ttsOpts) {
      const opt = document.createElement("option");
      opt.value = m;
      const p = getModelParamLabel(m);
      opt.textContent = p ? `${m} · ${p}` : m;
      selTts.appendChild(opt);
    }
    selTts.value = curTts;
  }
  const voiceEl = document.getElementById("set-tts-voice");
  if (voiceEl) voiceEl.value = appSettings.tts_voice || "";

  document.getElementById("settings-modal").style.display = "flex";
}

function closeSettings() {
  document.getElementById("settings-modal").style.display = "none";
}

function testSound() {
  unlockAudio();
  const snd = document.getElementById("set-sound").value;
  playDoneChime(true, snd);
}

async function saveSettings() {
  const prevTts = (appSettings.tts_model || "lfm3-audio:2b").trim();
  const default_model = document.getElementById("set-default-model").value;
  const sound = document.getElementById("set-sound").value;
  const stepsEl = document.getElementById("set-max-steps");
  const max_tool_steps = stepsEl ? (Number(stepsEl.value) || 8) : 8;
  const ttsEl = document.getElementById("set-tts-model");
  const tts_model = ttsEl ? (ttsEl.value || "lfm3-audio:2b") : "lfm3-audio:2b";
  const voiceEl = document.getElementById("set-tts-voice");
  const tts_voice = voiceEl ? voiceEl.value.trim() : "";
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      default_model,
      sound,
      max_tool_steps,
      tts_model,
      tts_voice,
      tts_auto: !!appSettings.tts_auto,
    }),
  });
  const data = await res.json();
  if (data && data.settings) appSettings = data.settings;
  if (default_model && !currentChatId) {
    selectModel(default_model);
  }
  renderPlusMenu();
  renderComposerBar();
  closeSettings();
  if (tts_model.trim() !== prevTts) {
    unloadTtsModel(prevTts);
    if (appSettings.tts_auto) {
      warmTtsModel(true);
    }
  }
}

async function quitServer() {
  try {
    await fetch("/api/quit", { method: "POST" });
  } catch (_) {}
  document.body.innerHTML = '<div style="margin:auto;text-align:center;color:#9898a3;font-family:sans-serif;"><h2 style="color:#fff;">Local agent stopped</h2><p>You can close this tab now.</p></div>';
}

const inpEl = document.getElementById("input");
inpEl.addEventListener("input", () => autoResize(inpEl));
inpEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (!busy) sendMsg();
  }
});
inpEl.addEventListener("paste", (e) => {
  const fl = e.clipboardData && e.clipboardData.files;
  if (fl && fl.length) {
    e.preventDefault();
    uploadFiles(fl);
  }
});

const filePickerEl = document.getElementById("file-picker");
if (filePickerEl) {
  filePickerEl.addEventListener("change", (e) => {
    if (e.target.files && e.target.files.length) uploadFiles(e.target.files);
    e.target.value = "";
  });
}

let dragCounter = 0;
const dropOverlayEl = document.getElementById("drop-overlay");
const dropBoxEl = dropOverlayEl ? dropOverlayEl.querySelector(".drop-box") : null;
const dropTitleEl = dropOverlayEl ? dropOverlayEl.querySelector(".drop-title") : null;
function hasDraggedFiles(e) {
  const dt = e && e.dataTransfer;
  if (!dt) return false;
  const types = Array.from(dt.types || []);
  return types.includes("Files");
}
function updateDropCursor(e) {
  if (!dropOverlayEl) return;
  const x = (typeof e.clientX === "number" && e.clientX > 0) ? e.clientX : (window.innerWidth / 2);
  const y = (typeof e.clientY === "number" && e.clientY > 0) ? e.clientY : (window.innerHeight / 2);
  dropOverlayEl.style.setProperty("--drop-x", x + "px");
  dropOverlayEl.style.setProperty("--drop-y", y + "px");
  if (dropBoxEl) {
    const r = dropBoxEl.getBoundingClientRect();
    const over = x >= r.left - 18 && x <= r.right + 18 && y >= r.top - 18 && y <= r.bottom + 18;
    dropOverlayEl.classList.toggle("on-target", over);
    if (dropTitleEl) dropTitleEl.textContent = over ? "Release to attach files" : "Drop files anywhere to attach";
  }
}
function hideDropOverlay() {
  dragCounter = 0;
  if (dropOverlayEl) dropOverlayEl.classList.remove("show", "on-target");
  if (dropTitleEl) dropTitleEl.textContent = "Drop files anywhere to attach";
}
window.addEventListener("dragenter", (e) => {
  if (!hasDraggedFiles(e)) return;
  e.preventDefault();
  dragCounter += 1;
  if (dropOverlayEl) dropOverlayEl.classList.add("show");
  updateDropCursor(e);
});
window.addEventListener("dragover", (e) => {
  if (!hasDraggedFiles(e)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
  if (dropOverlayEl && !dropOverlayEl.classList.contains("show")) dropOverlayEl.classList.add("show");
  updateDropCursor(e);
});
window.addEventListener("dragleave", (e) => {
  if (!hasDraggedFiles(e) && dragCounter <= 0) return;
  dragCounter = Math.max(0, dragCounter - 1);
  if (dragCounter === 0) hideDropOverlay();
});
window.addEventListener("drop", (e) => {
  const files = e.dataTransfer && e.dataTransfer.files;
  const hadFiles = hasDraggedFiles(e) || (files && files.length);
  hideDropOverlay();
  if (!hadFiles) return;
  e.preventDefault();
  if (files && files.length) uploadFiles(files);
});
window.addEventListener("blur", hideDropOverlay);

const MODAL_CLOSERS = [
  ["soul-modal", closeSoul],
  ["chips-modal", closeChipsModal],
  ["settings-modal", closeSettings],
  ["pull-modal", closePullModal],
  ["models-modal", closeModelsModal],
  ["files-modal", closeFilesModal],
];
for (const [id, closeFn] of MODAL_CLOSERS) {
  const el = document.getElementById(id);
  if (!el) continue;
  let startedOutside = false;
  el.addEventListener("mousedown", (e) => { startedOutside = (e.target === el); });
  el.addEventListener("click", (e) => {
    if (startedOutside && e.target === el) closeFn();
    startedOutside = false;
  });
}

document.addEventListener("click", (e) => {
  const menu = document.getElementById("plus-menu");
  const btn = document.getElementById("plus-btn");
  if (menu && menu.classList.contains("open") && !menu.contains(e.target) && (!btn || !btn.contains(e.target))) {
    closePlusMenu();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closePlusMenu();
    for (const [id, closeFn] of MODAL_CLOSERS) {
      const m = document.getElementById(id);
      if (m && m.style.display === "flex") closeFn();
    }
  }
});

(function bindScrollAreaWatchers() {
  const area = document.getElementById("scroll-area");
  if (!area) return;
  area.addEventListener("wheel", (e) => {
    if (e.deltaY < 0) {
      userScrolledAway = true;
    } else if (e.deltaY > 0) {
      requestAnimationFrame(() => {
        if (area.scrollHeight - area.scrollTop - area.clientHeight < 20) userScrolledAway = false;
      });
    }
  }, { passive: true });
  area.addEventListener("touchstart", () => { userScrolledAway = true; }, { passive: true });
  area.addEventListener("scroll", () => {
    const cur = area.scrollTop;
    const isManual = performance.now() - lastAutoScrollAt > 120;
    if (isManual) {
      if (cur < lastObservedScrollTop - 2) {
        userScrolledAway = true;
      } else if (area.scrollHeight - cur - area.clientHeight < 20) {
        userScrolledAway = false;
      }
    }
    lastObservedScrollTop = cur;
  }, { passive: true });
})();

document.getElementById("empty-spacer").addEventListener("transitionend", (e) => {
  if (e.propertyName === "flex-grow" && !document.getElementById("main-pane").classList.contains("is-empty")) {
    if (activeStreamRow) scrollFollow(activeStreamRow);
    else if (!userScrolledAway) scrollBottom();
  }
});

renderPlusMenu();
renderComposerBar();
setSendBtnState(false);
initWelcomeMascot();
updateCtxBadge(0, MAX_CTX);
let _appRevealed = false;
function revealApp() {
  if (_appRevealed) return;
  _appRevealed = true;
  requestAnimationFrame(() => document.body.classList.add("app-ready"));
}

startQuestionRotation();
const _bootMinDelay = new Promise(resolve => setTimeout(resolve, 500));
const _bootFallback = setTimeout(revealApp, 1500);
Promise.allSettled([
  _bootMinDelay,
  loadChips(),
  loadChatList(),
  loadSettings().then(() => loadModels()),
]).then(() => {
  clearTimeout(_bootFallback);
  revealApp();
});
</script>
</body>
</html>
"""


def render_web_html() -> bytes:
    ctx_k = f"{NUM_CTX // 1000}k" if NUM_CTX >= 1000 else str(NUM_CTX)
    page = (
        WEB_HTML.replace("__MODEL__", html_lib.escape(DEFAULT_MODEL))
        .replace("__SOUL_PATH__", html_lib.escape(str(SOUL_FILE)))
        .replace("__CHATS_PATH__", html_lib.escape(str(CHATS_DIR)))
        .replace("__OS_NAME__", html_lib.escape(os_label()))
        .replace("__NUM_CTX__", str(NUM_CTX))
        .replace("__CTX_K__", html_lib.escape(ctx_k))
    )
    return page.encode("utf-8")


def load_mascot_js() -> bytes:
    candidates = []
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidates.extend([Path(bundled) / "web" / "lfm3-mascot.js", Path(bundled) / "lfm3-mascot.js"])
    here = Path(__file__).resolve().parent
    candidates.extend([here / "lfm3-mascot.js", here.parent / "lfm3-mascot.js"])
    for p in candidates:
        if p.is_file():
            return p.read_bytes()
    return b""


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        try:
            print(f"[web] {args[0]}")
        except Exception:
            pass

    def send_json(self, code: int, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_no_content(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send_audio_error(self, err: Exception):
        if isinstance(err, MissingSpeechModelError):
            self.send_json(404, {"error": str(err), "missing_model": err.model})
        elif isinstance(err, ValueError):
            self.send_json(400, {"error": str(err)})
        else:
            self.send_json(502, {"error": str(err)})

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def make_cancel_check(self, cancelled: threading.Event):
        def is_cancelled() -> bool:
            if cancelled.is_set():
                return True
            try:
                readable, _, exceptional = select.select(
                    [self.connection], [], [self.connection], 0
                )
                if exceptional:
                    cancelled.set()
                    return True
                if readable:
                    peek = self.connection.recv(1, socket.MSG_PEEK)
                    if peek == b"":
                        cancelled.set()
                        return True
            except BlockingIOError:
                return False
            except (OSError, ValueError):
                cancelled.set()
                return True
            return False

        return is_cancelled

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            body = render_web_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/lfm3-mascot.js":
            body = load_mascot_js()
            if not body:
                self.send_json(404, {"error": "Mascot script not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/models":
            self.send_json(200, get_models_info())
            return

        if path == "/api/chats":
            self.send_json(200, list_chats())
            return

        if path.startswith("/api/chats/"):
            cid = path[len("/api/chats/"):]
            if not valid_chat_id(cid):
                self.send_json(400, {"error": "Invalid chat ID"})
                return
            self.send_json(200, load_chat(cid))
            return

        if path == "/api/files":
            cid = qs.get("chat_id", [""])[0]
            self.send_json(200, list_uploaded_files(cid))
            return

        if path.startswith("/api/files/"):
            rest = path[len("/api/files/"):]
            parts = rest.split("/", 1)
            if len(parts) != 2:
                self.send_json(400, {"error": "Invalid file path"})
                return
            cid = urllib.parse.unquote(parts[0])
            fname = urllib.parse.unquote(parts[1])
            if not valid_chat_id(cid):
                self.send_json(400, {"error": "Invalid chat ID"})
                return
            cdir = UPLOADS_DIR / cid
            target = (cdir / safe_upload_name(fname)).resolve()
            if not cdir.is_dir() or target.parent != cdir.resolve() or not target.is_file():
                self.send_json(404, {"error": "File not found"})
                return
            size = target.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with target.open("rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return

        if path == "/api/soul":
            souls = load_souls()
            self.send_json(200, {"content": load_soul(), **souls})
            return

        if path == "/api/chips":
            self.send_json(200, {"chips": load_chips(), "defaults": DEFAULT_CHIPS})
            return

        if path == "/api/settings":
            self.send_json(200, load_settings())
            return

        self.send_json(404, {"error": "Not found"})

    def _handle_delete_model(self, model_name: str):
        model_name = (model_name or "").strip()
        if not model_name:
            self.send_json(400, {"error": "Missing model name"})
            return
        try:
            delete_ollama_model(model_name)
        except Exception as e:
            self.send_json(502, {"error": str(e)})
            return
        self.send_json(200, {"deleted": model_name})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/chats/"):
            cid = path[len("/api/chats/"):]
            if not valid_chat_id(cid):
                self.send_json(400, {"error": "Invalid chat ID"})
                return
            p = chat_path(cid)
            if p.exists():
                p.unlink()
            delete_chat_uploads(cid)
            self.send_json(200, {"deleted": cid})
            return

        if path.startswith("/api/files/"):
            rest = path[len("/api/files/"):]
            parts = rest.split("/", 1)
            if len(parts) != 2:
                self.send_json(400, {"error": "Invalid file path"})
                return
            cid = urllib.parse.unquote(parts[0])
            fname = urllib.parse.unquote(parts[1])
            try:
                ok = delete_uploaded_file(cid, fname)
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
                return
            self.send_json(200, {"deleted": ok, "chat_id": cid, "name": safe_upload_name(fname)})
            return

        if path.startswith("/api/models/"):
            name = urllib.parse.unquote(path[len("/api/models/"):])
            self._handle_delete_model(name)
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/upload":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json(400, {"error": "Invalid Content-Length"})
                return
            if length <= 0:
                self.send_json(400, {"error": "Empty file upload"})
                return
            if length > MAX_UPLOAD_BYTES:
                self.send_json(413, {"error": f"File exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"})
                return
            raw_cid = (qs.get("chat_id", [""])[0] or self.headers.get("X-Chat-Id", "")).strip()
            cid = raw_cid if valid_chat_id(raw_cid) else new_chat_id()
            fname = (qs.get("name", [""])[0] or self.headers.get("X-Filename", "") or "file.txt").strip()
            raw_bytes = self.rfile.read(length)
            try:
                info = save_uploaded_file(cid, fname, raw_bytes)
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
                return
            self.send_json(200, {"ok": True, **info})
            return

        if path == "/api/files/delete":
            data = self.read_json()
            if data.get("all"):
                cnt = delete_chat_uploads("")
                self.send_json(200, {"ok": True, "deleted_count": cnt})
                return
            cid = str(data.get("chat_id") or "").strip()
            fname = str(data.get("name") or "").strip()
            if not valid_chat_id(cid):
                self.send_json(400, {"error": "Invalid chat ID"})
                return
            if fname:
                ok = delete_uploaded_file(cid, fname)
                self.send_json(200, {"ok": True, "deleted": ok})
            else:
                cnt = delete_chat_uploads(cid)
                self.send_json(200, {"ok": True, "deleted_count": cnt})
            return

        if path == "/api/models/delete":
            data = self.read_json()
            self._handle_delete_model(data.get("model", ""))
            return

        if path == "/api/soul":
            data = self.read_json()
            saved = save_souls(data)
            self.send_json(200, {"ok": True, **saved})
            return

        if path == "/api/tts/warm":
            data = self.read_json()
            st = load_settings()
            tts_model = str(data.get("model") or st.get("tts_model") or DEFAULT_TTS_MODEL).strip()
            try:
                info = warm_ollama_model(tts_model)
                self.send_json(200, info)
            except Exception as e:
                self._send_audio_error(e)
            return

        if path == "/api/tts/unload":
            data = self.read_json()
            st = load_settings()
            tts_model = str(data.get("model") or st.get("tts_model") or DEFAULT_TTS_MODEL).strip()
            self.send_json(200, unload_ollama_model(tts_model))
            return

        if path == "/api/tts":
            data = self.read_json()
            raw_text = str(data.get("text") or "").strip()
            stream_pcm = bool(data.get("stream_pcm"))
            if not raw_text:
                if stream_pcm:
                    self._send_no_content()
                else:
                    self.send_json(400, {"error": "Missing text to speak"})
                return
            st = load_settings()
            tts_model = str(data.get("model") or st.get("tts_model") or DEFAULT_TTS_MODEL).strip()
            tts_voice = str(data.get("voice") if "voice" in data else st.get("tts_voice", "")).strip()
            if stream_pcm:
                try:
                    pcm_iter = iter_ollama_speech_pcm(raw_text, model=tts_model, voice=tts_voice, allow_empty=True)
                    first_chunk = next(pcm_iter, None)
                except Exception as e:
                    self._send_audio_error(e)
                    return
                if not first_chunk:
                    self._send_no_content()
                    return
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "audio/pcm;rate=24000;channels=1;bits=16")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(first_chunk)
                    self.wfile.flush()
                    for chunk in pcm_iter:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except Exception:
                    pass
                return
            try:
                wav_bytes = synthesize_ollama_speech(raw_text, model=tts_model, voice=tts_voice)
            except Exception as e:
                self._send_audio_error(e)
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(wav_bytes)
            return

        if path == "/api/chips":
            data = self.read_json()
            saved = save_chips(data.get("chips", []))
            self.send_json(200, {"ok": True, "chips": saved})
            return

        if path == "/api/settings":
            data = self.read_json()
            saved = save_settings(data)
            self.send_json(200, {"ok": True, "settings": saved})
            return

        if path == "/api/ollama/start":
            ok = try_start_ollama()
            self.send_json(200, {"ollama_running": ok})
            return

        if path == "/api/quit":
            self.send_json(200, {"ok": True})
            srv = SERVER_REF.get("server")
            if srv is not None:
                threading.Timer(0.15, srv.shutdown).start()
            return

        if path == "/api/pull":
            data = self.read_json()
            model = (data.get("model") or DEFAULT_MODEL).strip()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            cancelled = threading.Event()
            is_cancelled = self.make_cancel_check(cancelled)

            def emit(ev: dict):
                if cancelled.is_set():
                    return
                try:
                    self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    cancelled.set()

            try:
                stream_pull_model(model, emit, cancel_check=is_cancelled)
            except Exception as e:
                emit({"error": str(e)})
            return

        if path == "/api/chat":
            data = self.read_json()
            raw_msg = (data.get("message") or "").strip()
            attached_files = [
                safe_upload_name(x)
                for x in (data.get("attached_files") if isinstance(data.get("attached_files"), list) else [])
                if str(x).strip()
            ]
            if not raw_msg and not attached_files:
                self.send_json(400, {"error": "Empty message"})
                return
            msg = raw_msg or "Inspect and summarize the attached file(s)."
            model = (data.get("model") or DEFAULT_MODEL).strip()
            active_packs = parse_active_packs(data)
            tools_enabled = bool(active_packs or attached_files)
            cid = data.get("chat_id") or new_chat_id()
            if not valid_chat_id(cid):
                cid = new_chat_id()

            chat = load_chat(cid)
            chat["model"] = model
            is_new_chat = not chat["ui_messages"]

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            cancelled = threading.Event()
            is_cancelled = self.make_cancel_check(cancelled)

            def emit(ev: dict):
                if cancelled.is_set():
                    return
                try:
                    self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    cancelled.set()

            t0 = time.monotonic()
            emit({"type": "phase", "phase": "thinking" if is_model_loaded(model) else "loading"})
            user_ui = {"role": "user", "text": raw_msg or "(attached files)"}
            if attached_files:
                user_ui["attachments"] = attached_files
            if is_new_chat:
                title_seed = raw_msg or (attached_files[0] if attached_files else "New chat")
                chat["title"] = generate_chat_title(title_seed, model, cancel_check=is_cancelled)
                chat["ui_messages"].append(user_ui)
                save_chat(chat)
                emit({"type": "title", "chat_id": cid, "title": chat["title"]})
                emit({"type": "phase", "phase": "thinking"})
            else:
                chat["ui_messages"].append(user_ui)

            try:
                res = run_agent_stream(
                    msg,
                    chat["history"],
                    model=model,
                    tools_enabled=tools_enabled,
                    active_packs=active_packs,
                    chat_id=cid,
                    attached_files=attached_files,
                    emit=emit,
                    cancel_check=is_cancelled,
                )
            except Exception as e:
                res = {
                    "reply": f"**Error:** `{e}`",
                    "thinking": "",
                    "tools": [],
                    "outputs": [],
                    "tokens": chat.get("tokens", {"used": 0, "max": NUM_CTX}),
                    "cancelled": False,
                }

            elapsed = time.monotonic() - t0
            chat["tokens"] = res["tokens"]
            chat["ui_messages"].append({
                "role": "assistant",
                "model": model,
                "text": res["reply"],
                "thinking": res["thinking"],
                "tools": res["tools"],
                "outputs": res.get("outputs", []),
                "elapsed": elapsed,
            })
            save_chat(chat)

            if not cancelled.is_set():
                emit({
                    "type": "done",
                    "chat_id": cid,
                    "model": model,
                    "title": chat["title"],
                    "reply": res["reply"],
                    "outputs": res.get("outputs", []),
                    "tokens": res["tokens"],
                    "elapsed": elapsed,
                })
            return

        self.send_json(404, {"error": "Not found"})


def is_port_in_use_error(err: OSError) -> bool:
    in_use = {errno.EADDRINUSE}
    if hasattr(errno, "WSAEADDRINUSE"):
        in_use.add(errno.WSAEADDRINUSE)
    return err.errno in in_use or getattr(err, "winerror", None) == 10048


def run_web(open_browser: bool = True):
    ensure_chats_dir()
    url = f"http://{WEB_HOST}:{WEB_PORT}"
    try:
        server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), WebHandler)
    except OSError as e:
        if is_port_in_use_error(e):
            print(f"Local Agent already running at {url}")
            if open_browser:
                webbrowser.open(url)
            return
        raise

    SERVER_REF["server"] = server
    threading.Thread(target=try_start_ollama, daemon=True).start()

    print(f"Local Agent ({os_label()}) running at {url}")
    print(f"Data directory: {DATA_DIR}")
    print("Close this window or press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    if "--check" in sys.argv:
        print("OK", os_label(), len(render_web_html()))
    else:
        run_web(open_browser="--no-open" not in sys.argv)
