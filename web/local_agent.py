#!/usr/bin/env python3
"""Cross-platform local Web UI agent backed by Ollama (Windows, macOS, Linux)."""

import errno
import html as html_lib
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
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

DEFAULT_SOUL = f"""You are Gipsy, a concise, direct, slightly witty assistant.

- Answer directly in text by default without calling tools.
- If the user asks what a command is, how something works, or asks about another device, just reply with the answer—do NOT run tools on this PC.
- Only use `{SHELL_TOOL_NAME}`, `read_file`, or `write_file` when explicitly asked to check, read, or change something on THIS PC.
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
    """Prevent crashes in windowed/noconsole packaged builds where stdout/stderr are None."""
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
CHATS_DIR = DATA_DIR / "chats"
CHIPS_FILE = DATA_DIR / "chips.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
DEFAULT_CHIPS = [
    {"label": "Check CPU, RAM & disk", "prompt": "Check my CPU, RAM, GPU, and free disk space on this PC"},
    {"label": "Check Downloads", "prompt": "List the largest files in my Downloads folder"},
    {"label": "Explain a command", "prompt": "How do I compress a folder with tar and gzip?"},
]
DEFAULT_SETTINGS = {"default_model": "", "sound": "chime"}


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
OLLAMA_TAGS_URL = f"{OLLAMA_HOST}/api/tags"
OLLAMA_PULL_URL = f"{OLLAMA_HOST}/api/pull"
OLLAMA_DELETE_URL = f"{OLLAMA_HOST}/api/delete"
OLLAMA_PS_URL = f"{OLLAMA_HOST}/api/ps"
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "lfm3")
NUM_CTX = int(os.environ.get("AGENT_CTX", "16384"))
WEB_HOST = "127.0.0.1"
WEB_PORT = int(os.environ.get("AGENT_PORT", "8765"))
MAX_HISTORY = 16
MAX_HISTORY_CHARS = 28000
MAX_STEPS = 8

PRESET_MODELS = ["lfm3", "granite4.2", "gemma4", "qwen3.5"]
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


def build_tools_schema() -> list:
    if IS_WINDOWS:
        tool_name = "run_shell"
        shell_desc = (
            "Run a PowerShell command on THIS Windows PC and return stdout/stderr. "
            "Only use when explicitly asked to inspect or act on this local machine."
        )
        cmd_desc = "The PowerShell command to run"
    else:
        tool_name = "run_bash"
        shell_desc = (
            f"Run a bash command on THIS {os_label()} computer and return stdout/stderr. "
            "Only use when explicitly asked to inspect or act on this local machine."
        )
        cmd_desc = "The bash command to run"

    return [
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
                "description": (
                    "Read the contents of a local file on THIS computer. "
                    "Only use when explicitly asked to inspect a local file."
                ),
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
                "description": (
                    "Write or overwrite text to a local file on THIS computer. "
                    "Only use when explicitly asked to create or modify a local file."
                ),
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


TOOLS_SCHEMA = build_tools_schema()


def terminate_process_tree(proc: subprocess.Popen):
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
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
            return [ps, "-NoProfile", "-NonInteractive", "-Command", cmd]
        return ["cmd", "/c", cmd]
    return ["bash", "-c", cmd]


def execute_tool(name: str, args: dict, cancel_check=None) -> str:
    try:
        if name in ("run_shell", "run_bash"):
            cmd = args.get("command", "")
            popen_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "cwd": str(Path.home()),
            }
            if IS_WINDOWS:
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
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
                    return "(command timed out after 30s)"
                reader.join(0.1)

            out = (out_buf[0] if out_buf else "").strip()
            return out[:4000] if out else f"(exit code {proc.returncode}, no output)"

        if name == "read_file":
            p = Path(args["path"]).expanduser()
            return p.read_text(encoding="utf-8", errors="replace")[:6000]

        if name == "write_file":
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
    return DEFAULT_SOUL.strip()


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
    try:
        if IS_MAC and Path("/Applications/Ollama.app").exists():
            subprocess.Popen(["open", "-a", "Ollama"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif IS_WINDOWS:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                [exe, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        else:
            subprocess.Popen(
                [exe, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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


def get_models_info() -> dict:
    running, installed, installed_details = probe_ollama_detailed()
    settings = load_settings()
    saved_default = settings.get("default_model", "").strip()

    merged = list(installed)
    for p in PRESET_MODELS:
        if not any(m == p or m.startswith(p + ":") for m in merged):
            merged.append(p)
    if saved_default and not any(m == saved_default or m.startswith(saved_default + ":") for m in merged):
        merged.insert(0, saved_default)

    active_default = saved_default or DEFAULT_MODEL
    if not saved_default and "AGENT_MODEL" not in os.environ and installed:
        if not any(m == DEFAULT_MODEL or m.startswith(DEFAULT_MODEL + ":") for m in installed):
            active_default = installed[0]

    return {
        "ollama_running": running,
        "has_ollama_bin": bool(find_ollama_executable()),
        "installed": installed,
        "installed_details": installed_details,
        "models": merged,
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


def stream_ollama(
    messages: list,
    model: str,
    tools_enabled: bool = True,
    on_thinking=None,
    on_phase=None,
    cancel_check=None,
) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": 0.1,
            "top_k": 50,
            "repeat_penalty": 1.1,
            "num_ctx": NUM_CTX,
        },
    }
    if tools_enabled:
        payload["tools"] = TOOLS_SCHEMA

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
                        joined = "".join(full_content).lstrip()
                        if (len(joined) < 8 and "<think>".startswith(joined)) or (
                            joined.startswith("<think>") and "</think>" not in joined
                        ):
                            set_phase("thinking")
                        else:
                            set_phase("working")

                    if m.get("tool_calls"):
                        set_phase("working")
                        tool_calls.extend(m["tool_calls"])

                    if chunk.get("done"):
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
def run_agent_stream(
    user_text: str,
    history: list,
    model: str = DEFAULT_MODEL,
    tools_enabled: bool = True,
    emit=None,
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

            def on_phase(p):
                if emit:
                    emit({"type": "phase", "phase": p})

            msg = stream_ollama(
                messages,
                model=model,
                tools_enabled=tools_enabled,
                on_thinking=on_think,
                on_phase=on_phase,
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
            if tools_enabled and not tool_calls and "<tool_call>" in raw_content:
                tool_calls = parse_xml_tool_fallback(raw_content)

            if not tool_calls or not tools_enabled:
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
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                tool_counter += 1
                tid = tool_counter
                if emit:
                    emit({"type": "tool_start", "id": tid, "name": name, "args": args})

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
        if history and history[-1].get("role") != "assistant":
            history.append({"role": "assistant", "content": "(stopped by user)"})
        return {
            "reply": "*(stopped by user)*",
            "thinking": "\n\n".join(all_thinking).strip(),
            "tools": all_tools,
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


def load_settings() -> dict:
    out = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                dm = str(raw.get("default_model", "")).strip()[:120]
                snd = str(raw.get("sound", "chime")).strip().lower()
                out["default_model"] = dm
                out["sound"] = snd if snd in VALID_SOUNDS else "chime"
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


# --- CROSS-PLATFORM WEB UI ---
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
  aside.collapsed { width: 0; border-right: none; overflow: hidden; opacity: 0; }
  .side-head {
    height: 52px; padding: 0 14px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;
  }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 14px; color: var(--white); }
  .brand-slot { width: 18px; height: 18px; display: grid; place-items: center; flex-shrink: 0; }
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

  /* Model Dropdown & Symmetrical Header Pills */
  .model-wrap { position: relative; }
  .model-pill, .tools-pill {
    display: inline-flex; align-items: center; gap: 7px;
    height: 30px; padding: 0 11px; border-radius: 999px;
    font-size: 13px; line-height: 1;
    background: var(--surface); border: 1px solid var(--border);
  }
  .model-pill { color: var(--white); }
  .model-pill:hover { border-color: rgba(255,255,255,0.4); }
  .dot-live, .tools-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
  .dot-live {
    background: var(--m-3); box-shadow: 0 0 3px var(--m-glow);
    opacity: 0.9; transition: background 0.2s, box-shadow 0.2s;
  }
  .caret-svg { color: var(--muted); transition: transform 0.15s; }
  .model-menu {
    position: absolute; top: calc(100% + 6px); left: 0; min-width: 235px;
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
  .opt-right { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 11px; }
  .model-sep { height: 1px; background: var(--border); margin: 4px 6px; }
  .model-add { color: var(--muted); }
  .model-add:hover { color: var(--white); }

  .head-right { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 8px; }
  .tools-pill { color: var(--muted); }
  .tools-pill.on { color: var(--white); border-color: rgba(255,255,255,0.28); }
  .tools-dot { background: var(--muted); transition: background 0.15s; }
  .tools-pill.on .tools-dot { background: #4ade80; }
  .ctx-badge {
    font-family: var(--font-mono); font-size: 11px; color: var(--muted);
    background: var(--surface); border: 1px solid var(--border);
    padding: 3px 8px; border-radius: 999px;
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
  .pull-fill { height: 100%; width: 0%; background: var(--m-3); transition: width 0.2s; }

  /* Viewport & Empty-State Centering */
  #stage { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
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
    border-radius: 999px; padding: 7px 14px; font-size: 13px;
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
  }

  /* Composer */
  .composer-wrap { position: relative; z-index: 2; padding: 10px 20px 14px; flex-shrink: 0; }
  form.composer {
    max-width: 760px; margin: 0 auto; background: var(--surface);
    border: 1px solid var(--border); border-radius: 14px;
    padding: 8px 10px 8px 16px; display: flex; align-items: center; gap: 10px;
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

  /* Modals (Soul, Quick Buttons, Settings, Pull Model, Manage Models) */
  #soul-modal, #chips-modal, #settings-modal, #pull-modal, #models-modal {
    position: fixed; inset: 0; background: rgba(0,0,0,0.72); display: none;
    align-items: center; justify-content: center; padding: 20px; z-index: 40;
  }
  #soul-box, #chips-box, #settings-box, #pull-box, #models-box {
    width: 100%; max-width: 640px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 18px; display: flex; flex-direction: column; gap: 14px;
  }
  #settings-box { max-width: 480px; }
  #pull-box { max-width: 440px; }
  #models-box { max-width: 560px; }
  #soul-text {
    width: 100%; height: 280px; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 12px;
    font-family: var(--font-mono); font-size: 13px; line-height: 1.5; resize: vertical;
  }
  #chips-rows, #models-rows { display: flex; flex-direction: column; gap: 8px; max-height: 300px; overflow-y: auto; }
  .model-mgr-row {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 10px 12px; background: var(--bg); border: 1px solid var(--border); border-radius: 9px;
  }
  .model-mgr-info { min-width: 0; display: flex; flex-direction: column; gap: 3px; flex: 1; }
  .model-mgr-title { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--white); font-weight: 500; min-width: 0; }
  .model-mgr-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .model-mgr-tag {
    font-size: 10px; padding: 1px 6px; border-radius: 999px;
    background: var(--surface-2); border: 1px solid var(--border); color: var(--muted); flex-shrink: 0;
  }
  .model-mgr-meta { font-size: 11px; color: var(--muted); padding-left: 15px; }
  .model-mgr-actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  .model-mgr-actions button { padding: 5px 10px; font-size: 12px; }
  .btn-danger-arm { border-color: rgba(255, 77, 77, 0.7) !important; color: #ff8080 !important; background: rgba(255, 77, 77, 0.12) !important; }
  .models-status-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-height: 18px; font-size: 12px; color: var(--muted); }
  .chip-edit-row { display: grid; grid-template-columns: 160px 1fr auto; gap: 8px; align-items: center; }
  .chip-edit-row input, #pull-input, .setting-ctrl select {
    background: var(--bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 7px; padding: 7px 10px; font: inherit; font-size: 13px; min-width: 0;
  }
  .chip-edit-row input:focus, #pull-input:focus, .setting-ctrl select:focus {
    outline: none; border-color: rgba(255,255,255,0.45);
  }
  .setting-row {
    display: grid; grid-template-columns: minmax(0, 1fr) 215px;
    align-items: center; gap: 14px;
  }
  .setting-meta { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .setting-meta b { font-size: 13px; color: var(--white); font-weight: 500; }
  .setting-meta span { font-size: 11px; color: var(--muted); }
  .setting-ctrl { display: flex; align-items: center; justify-content: flex-end; gap: 6px; min-width: 0; }
  .setting-ctrl select { flex: 1; width: 100%; max-width: 215px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chip-del { padding: 6px 9px; color: var(--muted); }
  .modal-row { display: flex; justify-content: flex-end; gap: 8px; }
  .modal-split { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
  .btn-white { background: var(--white); color: #111; border: none; font-weight: 600; }
  .btn-white:hover { background: #e4e4e7; }

  /* Startup Splash Overlay */
  #splash-overlay {
    position: fixed; inset: 0; z-index: 100; background: #050506;
    display: grid; place-items: center;
    transition: background 0.8s ease, opacity 0.45s ease;
  }
  #splash-overlay.fading { background: transparent; pointer-events: none; }
  .splash-stage { position: relative; width: 84px; height: 84px; display: grid; place-items: center; }
  .splash-glide {
    width: 45px; height: 45px; display: grid; place-items: center;
    transform-origin: center center;
  }
  #splash-overlay #brand-mark {
    width: 100%; height: 100%; gap: 5px;
    transform-origin: center center;
    animation: spinLinesMark 2.2s ease-in-out forwards;
  }
  #splash-overlay #brand-mark span {
    border-radius: 2.5px;
    transform-origin: left center;
    animation: extendLine 1.75s cubic-bezier(0.22, 1, 0.36, 1) both;
  }
  #splash-overlay #brand-mark span:nth-child(1) { animation-delay: 0.08s; }
  #splash-overlay #brand-mark span:nth-child(2) { animation-delay: 0.16s; }
  #splash-overlay #brand-mark span:nth-child(3) { animation-delay: 0.24s; }
  #splash-overlay #brand-mark span:nth-child(4) { animation-delay: 0.32s; }
  #splash-overlay #brand-mark span:nth-child(5) { animation-delay: 0.40s; }
  @keyframes spinLinesMark {
    0%   { opacity: 0; transform: rotate(-90deg) scale(1.08); }
    18%  { opacity: 1; transform: rotate(-90deg) scale(1.08); animation-timing-function: cubic-bezier(0.22, 1, 0.36, 1); }
    82%  { opacity: 1; transform: rotate(0deg)   scale(1.02); animation-timing-function: cubic-bezier(0.22, 1, 0.36, 1); }
    100% { opacity: 1; transform: rotate(0deg)   scale(1.00); }
  }
  @keyframes extendLine {
    0%   { transform: scaleX(0.35); }
    62%  { transform: scaleX(1.05); }
    100% { transform: scaleX(1.00); }
  }
  @media (prefers-reduced-motion: reduce) {
    #splash-overlay, #splash-overlay * { animation: none !important; transition: none !important; }
  }

  @media (max-width: 720px) {
    aside { position: fixed; top: 0; bottom: 0; left: 0; z-index: 25; }
    .ctx-badge { display: none; }
    .chip-edit-row, .setting-row { grid-template-columns: 1fr; }
    .setting-ctrl { justify-content: flex-start; }
    .setting-ctrl select { max-width: none; }
    .model-mgr-row { flex-direction: column; align-items: stretch; }
    .model-mgr-actions { justify-content: flex-end; }
  }
</style>
</head>
<body>
  <div id="splash-overlay" aria-hidden="true">
    <div class="splash-stage">
      <div class="splash-glide" id="splash-glide">
        <div class="m-mark" id="brand-mark"><span></span><span></span><span></span><span></span><span></span></div>
      </div>
    </div>
  </div>

  <aside id="sidebar">
    <div class="side-head">
      <div class="brand">
        <div class="brand-slot" id="brand-slot"></div>
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
            <svg class="caret-svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
          </button>
          <div class="model-menu" id="model-menu"></div>
        </div>
        <button type="button" id="tools-btn" class="tools-pill on" onclick="toggleTools()" title="Allow model to run local shell/file tools when asked">
          <span class="tools-dot"></span>
          <span id="tools-label">Tools: On</span>
        </button>
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
        <div class="hint">Enter to send · Shift+Enter for newline · Click inline code to copy · Chats saved in __CHATS_PATH__</div>
      </div>

      <div class="empty-spacer" id="empty-spacer">
        <div class="chips" id="chips-row"></div>
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
        <span style="color:var(--muted);font-size:12px;">LFM · Granite · Gemma · Qwen</span>
      </div>
      <input id="pull-input" type="text" placeholder="Model tag, e.g. gemma4, qwen3.5:9b, granite4.2..." onkeydown="if(event.key==='Enter'){event.preventDefault();submitPullModal();}" />
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

<script>
const MAX_CTX = __NUM_CTX__;
let currentChatId = null;
let currentModel = "__MODEL__";
let availableModels = ["__MODEL__"];
let installedModels = [];
let installedModelDetails = [];
let ollamaRunning = true;
let pullingModel = false;
let toolsEnabled = localStorage.getItem("local_agent_tools") !== "0";
let busy = false;
let currentAbort = null;

const SVG_ARROW_UP = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>`;
const SVG_STOP     = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>`;
const SVG_TRIANGLE = `<svg class="tri-svg" width="11" height="11" viewBox="0 0 16 16" fill="currentColor"><path d="M5.5 3.5l6 4.5-6 4.5z"/></svg>`;
const SVG_CLOSE    = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6l-12 12"/></svg>`;
const SVG_CHECK    = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>`;

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

function applyModelTheme(modelName) {
  const pal = getModelPalette(modelName);
  const root = document.documentElement.style;
  pal.forEach((col, idx) => root.setProperty(`--m-${idx + 1}`, col));
  root.setProperty("--m-glow", pal[2] + "24");
  document.getElementById("model-label").textContent = modelName;
  const wm = document.getElementById("welcome-model");
  if (wm) wm.textContent = modelName;
}

function isModelInstalled(name) {
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

async function pullModel(modelName) {
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
  currentModel = modelName;
  modelChosenByUser = true;
  await loadModels(false);
}

function renderToolsToggle() {
  const btn = document.getElementById("tools-btn");
  const lbl = document.getElementById("tools-label");
  btn.classList.toggle("on", toolsEnabled);
  lbl.textContent = toolsEnabled ? "Tools: On" : "Tools: Off";
}

function toggleTools() {
  toolsEnabled = !toolsEnabled;
  localStorage.setItem("local_agent_tools", toolsEnabled ? "1" : "0");
  renderToolsToggle();
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
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "model-opt";
    btn.innerHTML = `
      <span class="model-opt-left">
        <span class="opt-dot" style="background:${pal[2]};opacity:${installed ? 0.95 : 0.35};"></span>
        <span class="opt-name"></span>
      </span>
      <span class="opt-right">${m === currentModel ? SVG_CHECK : (!installed && ollamaRunning ? "pull" : "")}</span>
    `;
    btn.querySelector(".opt-name").textContent = m;
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
    if (m.thinking) steps.appendChild(buildThinkingBox(m.thinking, msgModel));
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

let audioCtx = null;
function unlockAudio() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!audioCtx) audioCtx = new AC();
    if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  } catch (_) {}
}

let appSettings = { default_model: "", sound: "chime" };

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
  const input = document.getElementById("input");
  const text = input.value.trim();
  if (!text) return;

  unlockAudio();
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

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: currentChatId,
        model: activeModel,
        message: text,
        tools_enabled: toolsEnabled,
      }),
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
          scrollBottom();
        } else if (ev.type === "tool_start") {
          setLoaderPhase("working", 400);
          const card = buildToolCard(ev, true, activeModel);
          toolCards[ev.id] = card;
          stepsWrap.appendChild(card);
          scrollBottom();
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
          scrollBottom();
        } else if (ev.type === "tokens" && ev.tokens) {
          updateCtxBadge(ev.tokens.used, ev.tokens.max);
        } else if (ev.type === "done") {
          clearInterval(secTimer);
          clearTimeout(pendingPhaseTimer);
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
          playDoneChime();
        }
      }
    }
  } catch (err) {
    clearInterval(secTimer);
    clearTimeout(pendingPhaseTimer);
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
    clearTimeout(pendingPhaseTimer);
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
    }
  } catch (_) {}
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
    opt.textContent = m;
    selModel.appendChild(opt);
  }
  selModel.value = appSettings.default_model || "";
  document.getElementById("set-sound").value = appSettings.sound || "chime";
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
  const default_model = document.getElementById("set-default-model").value;
  const sound = document.getElementById("set-sound").value;
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ default_model, sound }),
  });
  const data = await res.json();
  if (data && data.settings) appSettings = data.settings;
  if (default_model && !currentChatId) {
    selectModel(default_model);
  }
  closeSettings();
}

async function quitServer() {
  try {
    await fetch("/api/quit", { method: "POST" });
  } catch (_) {}
  document.body.innerHTML = '<div style="margin:auto;text-align:center;color:#9898a3;font-family:sans-serif;"><h2 style="color:#fff;">Local agent stopped</h2><p>You can close this tab now.</p></div>';
}

function finishSplashHandoff() {
  const overlay = document.getElementById("splash-overlay");
  const mark = document.getElementById("brand-mark");
  const slot = document.getElementById("brand-slot");
  if (slot && mark && mark.parentElement !== slot) {
    slot.appendChild(mark);
  }
  if (overlay && overlay.isConnected) overlay.remove();
}

function waitForSplashIntro() {
  return new Promise((resolve) => {
    const mark = document.getElementById("brand-mark");
    if (!mark) { resolve(); return; }
    let settled = false;
    const done = () => { if (!settled) { settled = true; resolve(); } };
    mark.addEventListener("animationend", (e) => {
      if (e.target === mark && e.animationName === "spinLinesMark") done();
    });
    setTimeout(done, 2500);
  });
}

function runSplashOutro() {
  const overlay = document.getElementById("splash-overlay");
  const glide = document.getElementById("splash-glide");
  const mark = document.getElementById("brand-mark");
  const slot = document.getElementById("brand-slot");
  if (!overlay || !glide || !mark || !slot) {
    finishSplashHandoff();
    return;
  }
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    finishSplashHandoff();
    return;
  }
  requestAnimationFrame(() => {
    const tr = slot.getBoundingClientRect();
    const mr = mark.getBoundingClientRect();
    if (!tr || tr.width <= 0 || !mr || mr.width <= 0) {
      finishSplashHandoff();
      return;
    }
    const scale = tr.width / mr.width;
    const dx = (tr.left + tr.width / 2) - (mr.left + mr.width / 2);
    const dy = (tr.top + tr.height / 2) - (mr.top + mr.height / 2);
    overlay.classList.add("fading");
    if (typeof glide.animate === "function") {
      const anim = glide.animate(
        [
          { transform: "translate(0px, 0px) scale(1)" },
          { transform: `translate(${dx}px, ${dy}px) scale(${scale})` },
        ],
        { duration: 900, easing: "cubic-bezier(0.22, 1, 0.36, 1)", fill: "forwards" }
      );
      let done = false;
      const complete = () => {
        if (done) return;
        done = true;
        finishSplashHandoff();
        try { anim.cancel(); } catch (_) {}
      };
      anim.finished.then(complete, complete);
      setTimeout(complete, 1500);
    } else {
      finishSplashHandoff();
    }
  });
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

renderToolsToggle();
setSendBtnState(false);
updateCtxBadge(0, MAX_CTX);
startQuestionRotation();
loadChips();
loadChatList();

const bootReady = loadSettings().then(() => loadModels());
if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
  finishSplashHandoff();
} else {
  const splashMinWait = waitForSplashIntro();
  const splashMaxWait = new Promise(r => setTimeout(r, 4000));
  Promise.race([Promise.all([bootReady, splashMinWait]), splashMaxWait]).then(runSplashOutro);
}
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
        if self.path == "/":
            body = render_web_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/models":
            self.send_json(200, get_models_info())
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

        if self.path == "/api/chips":
            self.send_json(200, {"chips": load_chips(), "defaults": DEFAULT_CHIPS})
            return

        if self.path == "/api/settings":
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
        if self.path.startswith("/api/models/"):
            name = urllib.parse.unquote(self.path[len("/api/models/"):])
            self._handle_delete_model(name)
            return
        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/api/models/delete":
            data = self.read_json()
            self._handle_delete_model(data.get("model", ""))
            return

        if self.path == "/api/soul":
            data = self.read_json()
            SOUL_FILE.write_text(data.get("content", ""), encoding="utf-8")
            self.send_json(200, {"ok": True})
            return

        if self.path == "/api/chips":
            data = self.read_json()
            saved = save_chips(data.get("chips", []))
            self.send_json(200, {"ok": True, "chips": saved})
            return

        if self.path == "/api/settings":
            data = self.read_json()
            saved = save_settings(data)
            self.send_json(200, {"ok": True, "settings": saved})
            return

        if self.path == "/api/ollama/start":
            ok = try_start_ollama()
            self.send_json(200, {"ollama_running": ok})
            return

        if self.path == "/api/quit":
            self.send_json(200, {"ok": True})
            srv = SERVER_REF.get("server")
            if srv is not None:
                threading.Timer(0.15, srv.shutdown).start()
            return

        if self.path == "/api/pull":
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

        if self.path == "/api/chat":
            data = self.read_json()
            msg = (data.get("message") or "").strip()
            model = (data.get("model") or DEFAULT_MODEL).strip()
            tools_enabled = bool(data.get("tools_enabled", True))
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
            if is_new_chat:
                chat["title"] = generate_chat_title(msg, model, cancel_check=is_cancelled)
                chat["ui_messages"].append({"role": "user", "text": msg})
                save_chat(chat)
                emit({"type": "title", "chat_id": cid, "title": chat["title"]})
                emit({"type": "phase", "phase": "thinking"})
            else:
                chat["ui_messages"].append({"role": "user", "text": msg})

            try:
                res = run_agent_stream(
                    msg,
                    chat["history"],
                    model=model,
                    tools_enabled=tools_enabled,
                    emit=emit,
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
