#!/usr/bin/env python3
import errno
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import readline  # Arrow keys & history in Ubuntu terminal
except ImportError:
    pass

# --- CONFIG ---
BASE_DIR = Path(__file__).resolve().parent
SOUL_FILE = BASE_DIR / "soul.md"
CHATS_DIR = BASE_DIR / "chats"
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_URL = f"{OLLAMA_HOST}/api/chat"
OLLAMA_TAGS_URL = f"{OLLAMA_HOST}/api/tags"
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "lfm3")
NUM_CTX = int(os.environ.get("AGENT_CTX", "16384"))
WEB_HOST = "127.0.0.1"
WEB_PORT = int(os.environ.get("AGENT_PORT", "8765"))
MAX_HISTORY = 16
MAX_HISTORY_CHARS = 28000
MAX_STEPS = 8

PRESET_MODELS = ["lfm3", "granite4.2", "gemma4", "qwen3.5", "lfm3-uncensored"]

OLLAMA_LOCK = threading.Lock()
USE_COLOR = sys.stdout.isatty() and "--web" not in sys.argv


class CancelledError(Exception):
    pass


class C:
    RESET   = "\033[0m" if USE_COLOR else ""
    BOLD    = "\033[1m" if USE_COLOR else ""
    DIM     = "\033[2m" if USE_COLOR else ""
    WHITE   = "\033[97m" if USE_COLOR else ""
    RED     = "\033[91m" if USE_COLOR else ""
    GREEN   = "\033[92m" if USE_COLOR else ""
    YELLOW  = "\033[93m" if USE_COLOR else ""
    BLUE    = "\033[94m" if USE_COLOR else ""
    MAGENTA = "\033[95m" if USE_COLOR else ""
    CYAN    = "\033[96m" if USE_COLOR else ""
    GRAY    = "\033[90m" if USE_COLOR else ""


# --- TERMINAL ASCII LINE SPINNER ---
class Spinner:
    FRAMES = [
        "[=       ]", "[==      ]", "[===     ]", "[ ===    ]",
        "[  ===   ]", "[   ===  ]", "[    === ]", "[     ===]",
        "[      ==]", "[       =]", "[      ==]", "[     ===]",
        "[    === ]", "[   ===  ]", "[  ===   ]", "[ ===    ]",
        "[===     ]", "[==      ]",
    ]
    RAINBOW = [C.CYAN, C.BLUE, C.MAGENTA, C.GREEN, C.YELLOW]

    def __init__(self, label: str = "thinking", enabled: bool = True):
        self.label = label
        self.enabled = enabled and sys.stdout.isatty()
        self._stop = threading.Event()
        self._thread = None

    def _animate(self):
        start = time.monotonic()
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            col = self.RAINBOW[(i // 3) % len(self.RAINBOW)]
            elapsed = time.monotonic() - start
            sys.stdout.write(
                f"\r\033[K  {col}{frame}{C.RESET} "
                f"{C.DIM}{self.label}... {elapsed:.1f}s{C.RESET}"
            )
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.08)

    def __enter__(self):
        if self.enabled:
            sys.stdout.write("\033[?25l")
            sys.stdout.flush()
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.enabled:
            self._stop.set()
            if self._thread:
                self._thread.join()
            sys.stdout.write("\r\033[K\033[?25h")
            sys.stdout.flush()


# --- TOOLS ---
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a bash command on THIS Ubuntu PC and return stdout/stderr. Only use when explicitly asked to inspect or act on this local machine.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to run"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a local file on THIS PC. Only use when explicitly asked to inspect a local file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or overwrite text to a local file on THIS PC. Only use when explicitly asked to create or modify a local file.",
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
]


def execute_tool(name: str, args: dict, cancel_check=None) -> str:
    try:
        if name == "run_bash":
            cmd = args.get("command", "")
            proc = subprocess.Popen(
                ["bash", "-c", cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(Path.home()),
                start_new_session=True,
            )
            out_buf = []
            reader = threading.Thread(
                target=lambda: out_buf.append(proc.communicate()[0]), daemon=True
            )
            reader.start()
            deadline = time.monotonic() + 30
            while reader.is_alive():
                if cancel_check and cancel_check():
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    raise CancelledError()
                if time.monotonic() > deadline:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return "(command timed out after 30s)"
                reader.join(0.1)

            out = (out_buf[0] if out_buf else "").strip()
            return out[:4000] if out else f"(exit code {proc.returncode}, no output)"

        elif name == "read_file":
            p = Path(args["path"]).expanduser()
            return p.read_text(encoding="utf-8", errors="replace")[:6000]

        elif name == "write_file":
            p = Path(args["path"]).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"Wrote {len(args['content'])} chars to {p}"

        return f"Unknown tool: {name}"
    except CancelledError:
        raise
    except Exception as e:
        return f"Tool error: {e}"


# --- OLLAMA HELPERS ---
def load_soul() -> str:
    if SOUL_FILE.exists():
        return SOUL_FILE.read_text(encoding="utf-8").strip()
    return (
        "You are a helpful, concise assistant. "
        "Answer directly in text by default, and only use run_bash, read_file, or write_file when explicitly asked to check or change something on this PC."
    )


def get_installed_models() -> list:
    installed = []
    try:
        with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for m in data.get("models", []):
                name = m.get("name", "")
                if name.endswith(":latest"):
                    name = name[:-7]
                if name and name not in installed:
                    installed.append(name)
    except Exception:
        pass

    merged = list(installed)
    for p in PRESET_MODELS:
        if not any(m == p or m.startswith(p + ":") for m in merged):
            merged.append(p)
    return merged


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


def stream_ollama(messages: list, model: str, on_thinking=None, cancel_check=None) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS_SCHEMA,
        "stream": True,
        "options": {
            "temperature": 0.1,
            "top_k": 50,
            "repeat_penalty": 1.1,
            "num_ctx": NUM_CTX,
        },
    }
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

    with OLLAMA_LOCK:
        with urllib.request.urlopen(req, timeout=180) as resp:
            for raw_line in resp:
                if cancel_check and cancel_check():
                    raise CancelledError()
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                chunk = json.loads(line)
                m = chunk.get("message") or {}

                t_delta = m.get("thinking") or ""
                if t_delta:
                    full_thinking.append(t_delta)
                    if on_thinking:
                        on_thinking(t_delta)

                c_delta = m.get("content") or ""
                if c_delta:
                    full_content.append(c_delta)

                if m.get("tool_calls"):
                    tool_calls.extend(m["tool_calls"])

                if chunk.get("done"):
                    prompt_tokens = int(chunk.get("prompt_eval_count") or 0)
                    eval_tokens = int(chunk.get("eval_count") or 0)
                    break

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


# --- CORE AGENT LOOP ---
def run_agent_stream(
    user_text: str,
    history: list,
    model: str = DEFAULT_MODEL,
    emit=None,
    use_spinner: bool = False,
    cancel_check=None,
) -> dict:
    history.append({"role": "user", "content": user_text})
    trim_history(history)

    all_thinking = []
    all_tools = []
    tool_counter = 0
    last_tokens = {"used": 0, "prompt": 0, "output": 0, "max": NUM_CTX}

    try:
        for _ in range(MAX_STEPS):
            if cancel_check and cancel_check():
                raise CancelledError()

            messages = [{"role": "system", "content": load_soul()}] + history

            def on_think(delta):
                if emit:
                    emit({"type": "thinking_delta", "text": delta})

            with Spinner("thinking", enabled=use_spinner):
                msg = stream_ollama(
                    messages,
                    model=model,
                    on_thinking=on_think,
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
            if not tool_calls and "<tool_call>" in raw_content:
                tool_calls = parse_xml_tool_fallback(raw_content)

            if not tool_calls:
                reply = clean_text or "(done)"
                history.append({"role": "assistant", "content": reply})
                return {
                    "reply": reply,
                    "thinking": "\n\n".join(all_thinking).strip(),
                    "tools": all_tools,
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
                    args = json.loads(args)

                tool_counter += 1
                tid = tool_counter
                if emit:
                    emit({"type": "tool_start", "id": tid, "name": name, "args": args})

                with Spinner(f"running {name}", enabled=use_spinner):
                    result = execute_tool(name, args, cancel_check=cancel_check)

                tool_record = {"id": tid, "name": name, "args": args, "result": result}
                all_tools.append(tool_record)
                if emit:
                    emit({"type": "tool_end", **tool_record})

                history.append({"role": "tool", "content": result})

        return {
            "reply": "Stopped: reached max tool steps.",
            "thinking": "\n\n".join(all_thinking).strip(),
            "tools": all_tools,
            "tokens": last_tokens,
            "cancelled": False,
        }

    except CancelledError:
        # Keep conversation turn pair valid
        if history and history[-1].get("role") != "assistant":
            history.append({"role": "assistant", "content": "(stopped by user)"})
        return {
            "reply": "*(stopped by user)*",
            "thinking": "\n\n".join(all_thinking).strip(),
            "tools": all_tools,
            "tokens": last_tokens,
            "cancelled": True,
        }


# --- CHAT STORAGE (~/my-agent/chats/) ---
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


# --- MINIMAL WHITE + PER-MODEL COLOR WEB UI ---
WEB_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>local agent</title>
<style>
  :root {
    --bg: #111113;
    --sidebar: #0c0c0e;
    --surface: #18181b;
    --surface-2: #222226;
    --border: #2a2a30;
    --text: #f4f4f6;
    --muted: #9898a3;
    --white: #ffffff;
    --code-bg: #09090b;

    /* Active model accent shades (defaults to lfm3 violet; updated dynamically) */
    --m-1: #ede9fe;
    --m-2: #c4b5fd;
    --m-3: #a78bfa;
    --m-4: #8b5cf6;
    --m-5: #7c3aed;
    --m-glow: rgba(167, 139, 250, 0.14);

    --font-sans: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
    --font-mono: ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; background: var(--bg); color: var(--text); font-family: var(--font-sans); font-size: 15px; }
  body { display: flex; overflow: hidden; }
  svg { display: block; flex-shrink: 0; }

  /* Minimal White Block Logo */
  .m-mark {
    display: inline-grid; grid-template-rows: repeat(5, 1fr); gap: 1.5px;
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
  aside.collapsed { width: 0; border-right: none; overflow: hidden; opacity: 0; }
  .side-head {
    height: 52px; padding: 0 14px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; flex-shrink: 0;
  }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 14px; color: var(--white); }
  .side-actions { padding: 10px 10px 6px; display: flex; gap: 6px; }

  button {
    background: var(--surface); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 7px 11px; font: inherit; font-size: 13px;
    cursor: pointer; transition: border-color 0.15s, background 0.15s;
  }
  button:hover { background: var(--surface-2); border-color: rgba(255,255,255,0.35); }
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
  .chat-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
  .del-btn {
    opacity: 0; border: none; background: transparent; color: var(--muted);
    padding: 4px; border-radius: 5px; display: grid; place-items: center;
  }
  .chat-item:hover .del-btn { opacity: 0.7; }
  .del-btn:hover { opacity: 1 !important; color: var(--white); background: rgba(255,255,255,0.1); }

  /* Main */
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

  /* Top-Left Model Dropdown */
  .model-wrap { position: relative; }
  .model-pill {
    display: inline-flex; align-items: center; gap: 8px; font-size: 13px;
    background: var(--surface); border: 1px solid var(--border);
    padding: 5px 10px 5px 11px; border-radius: 999px; color: var(--white);
  }
  .model-pill:hover { border-color: rgba(255,255,255,0.4); }
  .dot-live {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--m-3); box-shadow: 0 0 3px var(--m-glow);
    opacity: 0.9;
    transition: background 0.2s, box-shadow 0.2s;
  }
  .caret-svg { color: var(--muted); transition: transform 0.15s; }
  .model-menu {
    position: absolute; top: calc(100% + 6px); left: 0; min-width: 215px;
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 5px; display: none; flex-direction: column; gap: 2px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.55); z-index: 30;
  }
  .model-menu.open { display: flex; }
  .model-opt {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 7px 10px; border-radius: 7px; border: none; background: transparent;
    color: var(--text); font-size: 13px; text-align: left; width: 100%;
  }
  .model-opt:hover { background: var(--surface-2); }
  .model-opt-left { display: flex; align-items: center; gap: 8px; }
  .opt-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
  .opt-check { color: var(--muted); display: flex; align-items: center; }

  .head-right { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 10px; }
  .ctx-badge {
    font-family: var(--font-mono); font-size: 11px; color: var(--muted);
    background: var(--surface); border: 1px solid var(--border);
    padding: 3px 8px; border-radius: 999px;
  }
  .ctx-badge.warn { color: #fde68a; border-color: rgba(253, 230, 138, 0.45); }
  .ctx-badge.danger { color: #ff4d4d; border-color: rgba(255, 77, 77, 0.6); }

  /* Viewport & Empty-State Centering */
  #stage {
    flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: hidden;
  }
  #scroll-area {
    flex: 1 1 0px; min-height: 0; overflow-y: auto; padding: 24px 20px 12px; scroll-behavior: smooth;
  }
  #chat-col { max-width: 760px; margin: 0 auto; display: flex; flex-direction: column; gap: 22px; }

  #welcome {
    position: absolute; left: 20px; right: 20px; bottom: calc(100% + 10px);
    max-width: 680px; margin: 0 auto; text-align: center;
    display: flex; flex-direction: column; align-items: center; gap: 10px;
    opacity: 1; transform: translateY(0);
    transition: opacity 0.22s ease, transform 0.28s cubic-bezier(0.22, 1, 0.36, 1), visibility 0.28s;
  }
  main:not(.is-empty) #welcome {
    opacity: 0; transform: translateY(-14px); visibility: hidden; pointer-events: none;
  }
  #welcome .m-mark { width: 30px; height: 30px; gap: 2px; }
  .q-rotator {
    min-height: 36px; display: flex; align-items: center; justify-content: center; overflow: hidden;
  }
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
    border-radius: 999px; padding: 7px 14px; font-size: 13px;
  }
  .chip:hover { color: var(--white); border-color: rgba(255,255,255,0.45); }
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

  /* Expandable Thinking & Tools with SVG Triangle */
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
  .tri-svg { transition: transform 0.15s ease; color: var(--muted); }
  details[open] > summary .tri-svg { transform: rotate(90deg); }
  .think-body {
    margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border);
    font-family: var(--font-mono); font-size: 12px; line-height: 1.5;
    white-space: pre-wrap; color: #d4d4d8; max-height: 280px; overflow-y: auto;
  }

  /* Expandable Tool Cards */
  details.tool-pill {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 9px; padding: 7px 11px; font-size: 12px; color: var(--muted); width: 100%;
  }
  details.tool-pill summary {
    cursor: pointer; list-style: none; display: flex; align-items: center; gap: 8px;
    font-family: var(--font-mono); user-select: none;
  }
  details.tool-pill summary::-webkit-details-marker { display: none; }
  .tool-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--m-3); flex-shrink: 0; }
  .tool-dot.running { background: var(--m-2); animation: pulseDot 0.9s ease-in-out infinite; }
  .tool-dot.done { background: var(--m-3); }
  @keyframes pulseDot { 0%, 100% { opacity: 0.35; transform: scale(0.9); } 50% { opacity: 1; transform: scale(1.25); } }

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

  /* Markdown Prose (Clean White) */
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

  /* Fenced Code Block */
  .code-block {
    margin: 10px 0; background: var(--code-bg); border: 1px solid var(--border);
    border-radius: 10px; overflow: hidden;
  }
  .code-bar {
    display: flex; justify-content: space-between; align-items: center;
    padding: 6px 12px; background: var(--surface); border-bottom: 1px solid var(--border);
    font-family: var(--font-mono); font-size: 11px; color: var(--muted);
  }
  .copy-btn {
    background: transparent; border: 1px solid var(--border); color: var(--muted);
    padding: 2px 8px; border-radius: 5px; font-size: 11px; flex-shrink: 0;
  }
  .copy-btn:hover { color: var(--white); border-color: rgba(255,255,255,0.55); }
  .code-block pre {
    margin: 0; padding: 12px 14px; overflow-x: auto;
    font-family: var(--font-mono); font-size: 13px; line-height: 1.5; color: var(--text);
  }

  .msg-actions {
    padding-left: 22px; display: flex; align-items: center; gap: 10px;
    opacity: 0; transition: opacity 0.15s; font-size: 11px; color: var(--muted);
  }
  .msg-row.assistant:hover .msg-actions { opacity: 1; }

  /* PURE CSS MODEL-COLORED WAVE LOADER + LIVE SECONDS */
  .css-loader { display: inline-flex; align-items: center; gap: 11px; padding: 4px 0 4px 22px; }
  .bars { display: inline-flex; align-items: flex-end; gap: 3px; height: 18px; }
  .bars span { width: 4px; height: 6px; border-radius: 2px; animation: modelWave 1.05s ease-in-out infinite; }
  .bars span:nth-child(1) { background: var(--m-1); animation-delay: 0s; }
  .bars span:nth-child(2) { background: var(--m-2); animation-delay: 0.12s; }
  .bars span:nth-child(3) { background: var(--m-3); animation-delay: 0.24s; }
  .bars span:nth-child(4) { background: var(--m-4); animation-delay: 0.36s; }
  .bars span:nth-child(5) { background: var(--m-5); animation-delay: 0.48s; }
  @keyframes modelWave {
    0%, 100% { height: 5px; opacity: 0.45; }
    50%      { height: 18px; opacity: 1; }
  }
  .loader-sec { font-size: 13px; font-family: var(--font-mono); color: var(--muted); }

  /* Centered Floating Composer + Minimal White Square Send/Stop Button */
  .composer-wrap { position: relative; z-index: 2; padding: 10px 20px 14px; flex-shrink: 0; }
  form.composer {
    max-width: 760px; margin: 0 auto; background: var(--surface);
    border: 1px solid var(--border); border-radius: 14px;
    padding: 8px 10px 8px 16px;
    display: flex; align-items: center; gap: 10px;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  form.composer:focus-within {
    border-color: rgba(255, 255, 255, 0.45);
    box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.06);
  }
  textarea#input {
    flex: 1; background: transparent; border: none; color: var(--white);
    font: inherit; font-size: 15px; line-height: 22px;
    height: 22px; min-height: 22px; max-height: 180px;
    padding: 0; margin: 0; resize: none; display: block;
  }
  textarea#input:focus { outline: none; }
  textarea#input::placeholder { color: var(--muted); }
  #send-btn {
    width: 34px; height: 34px; border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.85);
    background: transparent; color: var(--white);
    padding: 0; display: grid; place-items: center; flex-shrink: 0;
  }
  #send-btn:hover { background: rgba(255, 255, 255, 0.1); border-color: var(--white); }
  #send-btn.stopping { border-color: var(--white); background: rgba(255, 255, 255, 0.08); }
  .hint { max-width: 760px; margin: 6px auto 0; text-align: center; font-size: 11px; color: var(--muted); }

  /* Soul Modal */
  #soul-modal {
    position: fixed; inset: 0; background: rgba(0,0,0,0.72); display: none;
    align-items: center; justify-content: center; padding: 20px; z-index: 20;
  }
  #soul-box {
    width: 100%; max-width: 640px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 18px; display: flex; flex-direction: column; gap: 12px;
  }
  #soul-text {
    width: 100%; height: 280px; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 12px;
    font-family: var(--font-mono); font-size: 13px; line-height: 1.5; resize: vertical;
  }
  .modal-row { display: flex; justify-content: flex-end; gap: 8px; }
  .btn-white { background: var(--white); color: #111; border: none; font-weight: 600; }
  .btn-white:hover { background: #e4e4e7; }
</style>
</head>
<body>
  <aside id="sidebar">
    <div class="side-head">
      <div class="brand">
        <div class="m-mark"><span></span><span></span><span></span><span></span><span></span></div>
        <span>local agent</span>
      </div>
    </div>
    <div class="side-actions">
      <button class="btn-new" onclick="newChat()">+ New chat</button>
      <button onclick="openSoul()" title="Edit soul.md">Soul</button>
    </div>
    <div class="side-label">Saved chats</div>
    <div id="chat-list"></div>
  </aside>

  <main id="main-pane" class="is-empty">
    <header>
      <div class="head-left">
        <button class="icon-btn" onclick="toggleSidebar()" title="Toggle sidebar">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
        </button>
        <div class="model-wrap">
          <button type="button" class="model-pill" id="model-btn" onclick="toggleModelMenu(event)">
            <span class="dot-live"></span>
            <span id="model-label">__MODEL__</span>
            <svg class="caret-svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
          </button>
          <div class="model-menu" id="model-menu"></div>
        </div>
      </div>
      <div class="head-right">
        <span id="ctx-badge" class="ctx-badge" title="Context tokens used in this chat (resets on New chat)">ctx 0 / __CTX_K__</span>
        <span>·</span>
        <span>soul.md (live)</span>
      </div>
    </header>

    <div id="stage">
      <div id="scroll-area">
        <div id="chat-col"></div>
      </div>

      <div class="composer-wrap">
        <div id="welcome">
          <div class="m-mark"><span></span><span></span><span></span><span></span><span></span></div>
          <div class="q-rotator" aria-live="polite">
            <h1 id="rotating-q">What are we working on?</h1>
          </div>
          <p>Running locally via Ollama <b id="welcome-model">__MODEL__</b></p>
        </div>

        <form class="composer" onsubmit="event.preventDefault(); handleSendOrStop();">
          <textarea id="input" rows="1" placeholder="Ask anything or give a task..."></textarea>
          <button type="button" id="send-btn" onclick="handleSendOrStop()" title="Send"></button>
        </form>
        <div class="hint">Enter to send · Shift+Enter for newline · Click inline code to copy · Chats auto-save to ~/my-agent/chats/</div>
      </div>

      <div class="empty-spacer" id="empty-spacer">
        <div class="chips">
          <button type="button" class="chip" onclick="usePrompt('Check my disk space, RAM usage, and uptime')">Check disk &amp; RAM</button>
          <button type="button" class="chip" onclick="usePrompt('List the largest files in my home directory')">Find big files</button>
          <button type="button" class="chip" onclick="usePrompt('Show active systemd user services')">Running services</button>
        </div>
      </div>
    </div>
  </main>

  <div id="soul-modal">
    <div id="soul-box">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <b>Edit soul.md (applies live)</b>
        <span style="color:var(--muted);font-size:12px;">__SOUL_PATH__</span>
      </div>
      <textarea id="soul-text"></textarea>
      <div class="modal-row">
        <button onclick="closeSoul()">Cancel</button>
        <button class="btn-white" onclick="saveSoul()">Save soul</button>
      </div>
    </div>
  </div>

<script>
const MAX_CTX = __NUM_CTX__;
let currentChatId = null;
let currentModel = "__MODEL__";
let availableModels = ["__MODEL__"];
let busy = false;
let currentAbort = null;

// Crisp Inline SVGs
const SVG_ARROW_UP = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>`;
const SVG_STOP     = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>`;
const SVG_TRIANGLE = `<svg class="tri-svg" width="11" height="11" viewBox="0 0 16 16" fill="currentColor"><path d="M5.5 3.5l6 4.5-6 4.5z"/></svg>`;
const SVG_CLOSE    = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6l-12 12"/></svg>`;
const SVG_CHECK    = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>`;

// Per-model detail palettes (lfm3-uncensored is strong vivid red; others stay light pastel)
const MODEL_PALETTES = {
  "lfm3-uncensored": ["#ff5252", "#ff2a2a", "#ff1414", "#dc0000", "#a80000"], // Strong Vivid Red
  "lfm3":            ["#ede9fe", "#c4b5fd", "#a78bfa", "#8b5cf6", "#7c3aed"], // Violet
  "granite":         ["#dbeafe", "#93c5fd", "#60a5fa", "#3b82f6", "#2563eb"], // Blue
  "gemma":           ["#e0f2fe", "#7dd3fc", "#38bdf8", "#0ea5e9", "#0284c7"], // Azure
  "qwen":            ["#fae8ff", "#f0abfc", "#e879f9", "#d946ef", "#c026d3"], // Magenta-Purple
};
const DEFAULT_PALETTE = ["#fafafa", "#e4e4e7", "#d4d4d8", "#a1a1aa", "#71717a"];

function getModelPalette(name) {
  const n = (name || "").toLowerCase();
  if (n.includes("uncensored")) return MODEL_PALETTES["lfm3-uncensored"];
  if (n.includes("lfm3")) return MODEL_PALETTES["lfm3"];
  if (n.includes("granite") || n.includes("ganite")) return MODEL_PALETTES["granite"];
  if (n.includes("gemma")) return MODEL_PALETTES["gemma"];
  if (n.includes("qwen")) return MODEL_PALETTES["qwen"];
  return DEFAULT_PALETTE;
}

function applyModelTheme(modelName) {
  const pal = getModelPalette(modelName);
  const root = document.documentElement.style;
  pal.forEach((col, idx) => root.setProperty(`--m-${idx + 1}`, col));
  root.setProperty("--m-glow", pal[2] + "24");
  document.getElementById("model-label").textContent = modelName;
  const wm = document.getElementById("welcome-model");
  if (wm) wm.textContent = modelName;
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

function renderModelMenu() {
  const menu = document.getElementById("model-menu");
  menu.innerHTML = "";
  for (const m of availableModels) {
    const pal = getModelPalette(m);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "model-opt";
    btn.innerHTML = `
      <span class="model-opt-left">
        <span class="opt-dot" style="background:${pal[2]};opacity:0.9;"></span>
        <span class="opt-name"></span>
      </span>
      <span class="opt-check">${m === currentModel ? SVG_CHECK : ""}</span>
    `;
    btn.querySelector(".opt-name").textContent = m;
    btn.onclick = () => selectModel(m);
    menu.appendChild(btn);
  }
}

function selectModel(m) {
  currentModel = m;
  applyModelTheme(m);
  renderModelMenu();
  document.getElementById("model-menu").classList.remove("open");
}

function toggleModelMenu(e) {
  e.stopPropagation();
  document.getElementById("model-menu").classList.toggle("open");
}

document.addEventListener("click", () => {
  document.getElementById("model-menu").classList.remove("open");
});

async function loadModels() {
  try {
    const res = await fetch("/api/models");
    const list = await res.json();
    if (Array.isArray(list) && list.length) availableModels = list;
  } catch (_) {}
  applyModelTheme(currentModel);
  renderModelMenu();
}

function toggleSidebar() {
  document.getElementById("sidebar").classList.toggle("collapsed");
}

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

function buildThinkingBox(initialText = "") {
  const det = document.createElement("details");
  det.className = "think-box";
  det.innerHTML = `<summary>${SVG_TRIANGLE}<span class="think-label">Thinking</span></summary><div class="think-body"></div>`;
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
    <span class="tool-dot ${isRunning ? "running" : "done"}"></span>
    <span class="tool-fn"></span>
    <span class="tool-preview"></span>
    <button type="button" class="copy-btn">Copy</button>
  `;
  const dot = sum.querySelector(".tool-dot");
  dot.style.background = isRunning ? pal[1] : pal[2];
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

async function loadChatList() {
  const res = await fetch("/api/chats");
  const chats = await res.json();
  const list = document.getElementById("chat-list");
  list.innerHTML = "";
  for (const c of chats) {
    const div = document.createElement("div");
    div.className = "chat-item" + (c.id === currentChatId ? " active" : "");
    const title = document.createElement("span");
    title.className = "chat-title";
    title.textContent = c.title || c.id;
    title.onclick = () => openChat(c.id);

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
    div.appendChild(title);
    div.appendChild(del);
    list.appendChild(div);
  }
}

const WELCOME_QUESTIONS = [
  "What are we working on?",
  "Need a command you forgot?",
  "Debugging something today?",
  "Working on another device?",
  "Want to inspect a file or service?",
  "Got a quick Linux question?",
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

function setEmptyState(isEmpty) {
  const main = document.getElementById("main-pane");
  if (!main) return;
  const wasEmpty = main.classList.contains("is-empty");
  main.classList.toggle("is-empty", isEmpty);
  if (isEmpty && !wasEmpty) {
    qIndex = 0;
    const el = document.getElementById("rotating-q");
    if (el) el.textContent = WELCOME_QUESTIONS[0];
    startQuestionRotation();
  } else if (isEmpty) {
    if (!qTimer) startQuestionRotation();
  } else {
    stopQuestionRotation();
  }
}

function scrollBottom() {
  const area = document.getElementById("scroll-area");
  area.scrollTop = area.scrollHeight;
}

function renderMessage(m) {
  setEmptyState(false);
  const col = document.getElementById("chat-col");
  const row = document.createElement("div");
  row.className = "msg-row " + m.role;

  if (m.role === "user") {
    const bubble = document.createElement("div");
    bubble.className = "user-bubble";
    bubble.textContent = m.text;
    row.appendChild(bubble);
    col.appendChild(row);
    scrollBottom();
    return row;
  }

  const msgModel = m.model || currentModel;
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
    if (m.thinking) steps.appendChild(buildThinkingBox(m.thinking));
    for (const t of (m.tools || [])) steps.appendChild(buildToolCard(t, false, msgModel));
    row.appendChild(steps);
  }

  const prose = document.createElement("div");
  prose.className = "prose";
  prose.innerHTML = renderMarkdown(m.text);
  row.appendChild(prose);

  const acts = document.createElement("div");
  acts.className = "msg-actions";
  const cpy = document.createElement("button");
  cpy.className = "copy-btn";
  cpy.textContent = "Copy reply";
  cpy.onclick = () => copyText(cpy, m.text);
  acts.appendChild(cpy);
  row.appendChild(acts);

  col.appendChild(row);
  scrollBottom();
  return row;
}

async function openChat(id) {
  if (busy && currentAbort) currentAbort.abort();
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
  currentChatId = null;
  updateCtxBadge(0, MAX_CTX);
  document.getElementById("chat-col").innerHTML = "";
  setEmptyState(true);
  loadChatList();
  document.getElementById("input").focus();
}

async function sendMsg() {
  if (busy) return;
  const input = document.getElementById("input");
  const text = input.value.trim();
  if (!text) return;

  input.value = "";
  autoResize(input);
  busy = true;
  currentAbort = new AbortController();
  setSendBtnState(true);

  renderMessage({ role: "user", text });

  const activeModel = currentModel;
  const pal = getModelPalette(activeModel);
  const col = document.getElementById("chat-col");
  const row = document.createElement("div");
  row.className = "msg-row assistant";
  row.innerHTML = `
    <div class="asst-head">
      <div class="m-mark"><span></span><span></span><span></span><span></span><span></span></div>
      <span class="asst-name"></span>
      <span class="asst-time"></span>
    </div>
    <div class="steps-wrap"></div>
    <div class="css-loader">
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
  const secTimer = setInterval(() => {
    const s = ((performance.now() - tStart) / 1000).toFixed(1);
    loaderSec.textContent = `${loaderPhase}... ${s}s`;
  }, 100);

  let thinkBox = null;
  const toolCards = {};

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: currentChatId, model: activeModel, message: text }),
      signal: currentAbort.signal,
    });

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

        if (ev.type === "thinking_delta") {
          if (!thinkBox) {
            thinkBox = buildThinkingBox("");
            stepsWrap.prepend(thinkBox);
          }
          const tb = thinkBox.querySelector(".think-body");
          tb.textContent += ev.text;
          tb.scrollTop = tb.scrollHeight;
          thinkBox.querySelector(".think-label").textContent = `Thinking (${tb.textContent.length} chars)`;
          scrollBottom();
        } else if (ev.type === "tool_start") {
          loaderPhase = `Running ${ev.name}`;
          const card = buildToolCard(ev, true, activeModel);
          toolCards[ev.id] = card;
          stepsWrap.appendChild(card);
          scrollBottom();
        } else if (ev.type === "tool_end") {
          const card = toolCards[ev.id];
          if (card) {
            const dot = card.querySelector(".tool-dot");
            dot.classList.remove("running");
            dot.classList.add("done");
            dot.style.background = pal[2];
            card.querySelector(".tool-out").textContent = ev.result || "(no output)";
          }
          loaderPhase = "Thinking";
          scrollBottom();
        } else if (ev.type === "tokens" && ev.tokens) {
          updateCtxBadge(ev.tokens.used, ev.tokens.max);
        } else if (ev.type === "done") {
          clearInterval(secTimer);
          loaderEl.remove();
          currentChatId = ev.chat_id;
          if (ev.tokens) updateCtxBadge(ev.tokens.used, ev.tokens.max);
          if (ev.elapsed) row.querySelector(".asst-time").textContent = `· ${ev.elapsed.toFixed(1)}s`;
          if (!stepsWrap.children.length) stepsWrap.remove();

          const prose = document.createElement("div");
          prose.className = "prose";
          prose.innerHTML = renderMarkdown(ev.reply || "(empty)");
          row.appendChild(prose);

          const acts = document.createElement("div");
          acts.className = "msg-actions";
          const cpy = document.createElement("button");
          cpy.className = "copy-btn";
          cpy.textContent = "Copy reply";
          cpy.onclick = () => copyText(cpy, ev.reply || "");
          acts.appendChild(cpy);
          row.appendChild(acts);
          scrollBottom();
          loadChatList();
        }
      }
    }
  } catch (err) {
    clearInterval(secTimer);
    loaderEl.remove();
    const prose = document.createElement("div");
    prose.className = "prose";
    if (err && err.name === "AbortError") {
      const elapsed = ((performance.now() - tStart) / 1000).toFixed(1);
      row.querySelector(".asst-time").textContent = `· stopped (${elapsed}s)`;
      prose.innerHTML = renderMarkdown("*(stopped by user)*");
      setTimeout(loadChatList, 300);
    } else {
      prose.innerHTML = renderMarkdown("**Error:** `" + String(err) + "`");
    }
    if (!stepsWrap.children.length) stepsWrap.remove();
    row.appendChild(prose);
  } finally {
    clearInterval(secTimer);
    busy = false;
    currentAbort = null;
    setSendBtnState(false);
    input.focus();
  }
}

async function openSoul() {
  const res = await fetch("/api/soul");
  const data = await res.json();
  document.getElementById("soul-text").value = data.content || "";
  document.getElementById("soul-modal").style.display = "flex";
}
function closeSoul() { document.getElementById("soul-modal").style.display = "none"; }
async function saveSoul() {
  const content = document.getElementById("soul-text").value;
  await fetch("/api/soul", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  closeSoul();
}

const inpEl = document.getElementById("input");
inpEl.addEventListener("input", () => autoResize(inpEl));
inpEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (!busy) sendMsg();
  }
});

document.getElementById("empty-spacer").addEventListener("transitionend", (e) => {
  if (e.propertyName === "flex-grow" && !document.getElementById("main-pane").classList.contains("is-empty")) {
    scrollBottom();
  }
});

setSendBtnState(false);
updateCtxBadge(0, MAX_CTX);
startQuestionRotation();
loadModels();
loadChatList();
</script>
</body>
</html>
"""


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[web] {args[0]}")

    def send_json(self, code: int, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        if self.path == "/":
            ctx_k = f"{NUM_CTX // 1000}k" if NUM_CTX >= 1000 else str(NUM_CTX)
            html = (
                WEB_HTML.replace("__MODEL__", DEFAULT_MODEL)
                .replace("__SOUL_PATH__", str(SOUL_FILE))
                .replace("__NUM_CTX__", str(NUM_CTX))
                .replace("__CTX_K__", ctx_k)
                .encode("utf-8")
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if self.path == "/api/models":
            self.send_json(200, get_installed_models())
            return

        if self.path == "/api/chats":
            self.send_json(200, list_chats())
            return

        if self.path.startswith("/api/chats/"):
            cid = self.path[len("/api/chats/"):]
            if not valid_chat_id(cid):
                self.send_json(400, {"error": "Invalid chat ID"})
                return
            self.send_json(200, load_chat(cid))
            return

        if self.path == "/api/soul":
            self.send_json(200, {"content": load_soul()})
            return

        self.send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        if self.path.startswith("/api/chats/"):
            cid = self.path[len("/api/chats/"):]
            if not valid_chat_id(cid):
                self.send_json(400, {"error": "Invalid chat ID"})
                return
            p = chat_path(cid)
            if p.exists():
                p.unlink()
            self.send_json(200, {"deleted": cid})
            return
        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/api/soul":
            data = self.read_json()
            SOUL_FILE.write_text(data.get("content", ""), encoding="utf-8")
            self.send_json(200, {"ok": True})
            return

        if self.path == "/api/chat":
            data = self.read_json()
            msg = (data.get("message") or "").strip()
            model = (data.get("model") or DEFAULT_MODEL).strip()
            cid = data.get("chat_id") or new_chat_id()
            if not valid_chat_id(cid):
                cid = new_chat_id()

            chat = load_chat(cid)
            chat["model"] = model
            if not chat["ui_messages"]:
                chat["title"] = msg[:42] + ("..." if len(msg) > 42 else "")

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            cancelled = threading.Event()

            def is_cancelled() -> bool:
                if cancelled.is_set():
                    return True
                try:
                    peek = self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                    if peek == b"":
                        cancelled.set()
                        return True
                except BlockingIOError:
                    return False
                except OSError:
                    cancelled.set()
                    return True
                return False

            def emit(ev: dict):
                if cancelled.is_set():
                    return
                try:
                    self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    cancelled.set()

            t0 = time.monotonic()
            try:
                res = run_agent_stream(
                    msg,
                    chat["history"],
                    model=model,
                    emit=emit,
                    use_spinner=False,
                    cancel_check=is_cancelled,
                )
            except Exception as e:
                res = {
                    "reply": f"**Error:** `{e}`",
                    "thinking": "",
                    "tools": [],
                    "tokens": chat.get("tokens", {"used": 0, "max": NUM_CTX}),
                    "cancelled": False,
                }

            elapsed = time.monotonic() - t0
            chat["tokens"] = res["tokens"]
            chat["ui_messages"].append({"role": "user", "text": msg})
            chat["ui_messages"].append({
                "role": "assistant",
                "model": model,
                "text": res["reply"],
                "thinking": res["thinking"],
                "tools": res["tools"],
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
                    "tokens": res["tokens"],
                    "elapsed": elapsed,
                })
            return

        self.send_json(404, {"error": "Not found"})


def run_web(open_browser: bool = False):
    ensure_chats_dir()
    url = f"http://{WEB_HOST}:{WEB_PORT}"
    try:
        server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), WebHandler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"Web UI already running at {url}")
            if open_browser:
                webbrowser.open(url)
            return
        raise

    print(f"Web UI running at {url}")
    print(f"Chats saved in:   {CHATS_DIR}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


# --- TERMINAL UI ---
def print_banner():
    line1 = f"  * {DEFAULT_MODEL} agent   |   soul: {SOUL_FILE.name} (live)"
    line2 = "  cmds: /soul  /save  /clear  exit"
    width = max(len(line1), len(line2)) + 2
    border = "-" * width
    print(f"\n{C.WHITE}+{border}+{C.RESET}")
    print(f"{C.WHITE}|{C.RESET}{C.BOLD}{line1.ljust(width)}{C.RESET}{C.WHITE}|{C.RESET}")
    print(f"{C.WHITE}|{C.RESET}{C.GRAY}{line2.ljust(width)}{C.RESET}{C.WHITE}|{C.RESET}")
    print(f"{C.WHITE}+{border}+{C.RESET}\n")


def print_tool_call(name: str, args: dict):
    preview = ", ".join(f"{k}={repr(str(v)[:60])}" for k, v in args.items())
    tool_col = {"run_bash": C.CYAN, "read_file": C.GREEN, "write_file": C.MAGENTA}.get(name, C.YELLOW)
    print(
        f"  {tool_col}[tool]{C.RESET} "
        f"{tool_col}{C.BOLD}{name}{C.RESET} "
        f"{C.GRAY}({preview}){C.RESET}"
    )


def run_terminal():
    history = []
    ui_log = []

    def term_emit(ev):
        if ev["type"] == "tool_start":
            print_tool_call(ev["name"], ev["args"])

    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
        try:
            res = run_agent_stream(
                prompt, history, model=DEFAULT_MODEL, emit=term_emit, use_spinner=sys.stdout.isatty()
            )
            print(res["reply"])
        except Exception as e:
            print(f"{C.RED}[error] {e}{C.RESET}")
        return

    print_banner()
    while True:
        try:
            user_input = input(f"{C.WHITE}{C.BOLD}you >{C.RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C.GRAY}bye.{C.RESET}")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print(f"{C.GRAY}bye.{C.RESET}")
            break
        if user_input == "/clear":
            history.clear()
            ui_log.clear()
            print(f"  {C.GRAY}(memory cleared){C.RESET}\n")
            continue
        if user_input == "/soul":
            print(f"\n{C.WHITE}--- {SOUL_FILE.name} ---{C.RESET}")
            print(f"{C.GRAY}{load_soul()}{C.RESET}")
            print(f"{C.WHITE}-----------------{C.RESET}\n")
            continue
        if user_input == "/save":
            if not ui_log:
                print(f"  {C.GRAY}(nothing to save yet){C.RESET}\n")
                continue
            cid = new_chat_id()
            title = ui_log[0]["text"][:42]
            save_chat({"id": cid, "title": title, "model": DEFAULT_MODEL, "history": history, "ui_messages": ui_log})
            print(f"  {C.GRAY}(saved to chats/{cid}.json){C.RESET}\n")
            continue

        t0 = time.monotonic()
        try:
            res = run_agent_stream(
                user_input, history, model=DEFAULT_MODEL, emit=term_emit, use_spinner=True
            )
            dt = time.monotonic() - t0
            tok = res["tokens"]
            ui_log.append({"role": "user", "text": user_input})
            ui_log.append({
                "role": "assistant",
                "model": DEFAULT_MODEL,
                "text": res["reply"],
                "thinking": res["thinking"],
                "tools": res["tools"],
                "elapsed": dt,
            })

            print(
                f"\n{C.WHITE}{C.BOLD}agent{C.RESET} "
                f"{C.GRAY}({dt:.1f}s · {tok['used']}/{tok['max']} tok){C.RESET}"
            )
            print(res["reply"])
            print(f"{C.GRAY}{'-' * 40}{C.RESET}\n")
        except Exception as e:
            print(f"\n{C.RED}[error] {e}{C.RESET}\n")


if __name__ == "__main__":
    if "--web" in sys.argv:
        run_web(open_browser="--open" in sys.argv)
    else:
        run_terminal()
