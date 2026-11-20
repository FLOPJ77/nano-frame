# local agent (`v1.1.0`)

A tiny, zero-dependency local AI framework built for small models (`lfm3`, `granite4.2`, `qwen3.5`, `gemma4`) powered directly by **Hugging Face GGUF** and an auto-managed **`llama.cpp` (`llama-server`)** runtime.

Split into two standalone tools:
- **Web App (`web/`)** — Cross-platform browser UI for Windows, macOS, and Linux (Chat Mode & Projects Mode).
- **Terminal CLI (`cli/`)** — Fast terminal assistant (`ai "how do I ..."`) sharing the same local GGUF models & `llama.cpp` runtime.

---

## Quick Start (Windows, macOS, Linux)

No Ollama install or Python packages required. On first model download (or launch), Local Agent automatically fetches the right `llama.cpp` binary for your OS and hardware and downloads `.gguf` weights directly from Hugging Face Hub with resume support.

### 1. Run Local Agent
- **From GitHub Releases (no Python needed):**
  - **Windows:** Download `LocalAgent-Windows.exe` and double-click it.
  - **macOS:** Download `LocalAgent-macOS.zip`, unzip, and open `LocalAgent.app`.
  - **Linux:** Download `LocalAgent-Linux`, `chmod +x LocalAgent-Linux && ./LocalAgent-Linux`.
- **From Source (Python 3 stdlib only):**
  - **Windows:** Double-click `web/Start-Windows.bat` (or run `python web/local_agent.py`)
  - **macOS:** Double-click `web/Start-macOS.command` (or run `python3 web/local_agent.py`)
  - **Linux:** Run `./web/start-linux.sh` (or `python3 web/local_agent.py`)

Your default browser opens automatically at `http://127.0.0.1:8765`. Use **Download from Hugging Face** to pick a preset model, search Hugging Face Hub live, or paste any `owner/repo` / `.gguf` URL. Any `.gguf` files already placed in `models/` are discovered automatically.

> **First-launch OS warning (unsigned GitHub apps):**
> - **Windows SmartScreen:** Click *More info* → *Run anyway*.
> - **macOS Gatekeeper:** Right-click `LocalAgent.app` (or `Start-macOS.command`) → *Open*, or run `xattr -dr com.apple.quarantine LocalAgent.app`.

### Hardware & GPU Acceleration
Hardware is detected automatically at startup across PC builds:
- **Windows:** CUDA / Vulkan (NVIDIA, AMD, Intel) via `ggml-org/llama.cpp`, with automatic CPU fallback.
- **macOS:** Apple Silicon Metal GPU acceleration out of the box (plus Intel x64 support).
- **Linux:** CUDA, Vulkan, ROCm/HIP, or SYCL GPU acceleration with automatic CPU fallback (and headless software-rasterizer filtering).

### Features (`v1.1.0`)
- **Direct Hugging Face GGUF Engine:** Live Hub search, preset light models, resumable `.part` downloads, zero external runtime dependencies.
- **Chat Mode & Projects Mode:** Chat with the **Boo** mascot gliding beside active replies, or switch to **Projects Mode** for multi-file workspaces with the **Cube** orchestrator and configurable parallel worker lanes (`1` low-memory single lane or `2–4` parallel wave lanes).
- **Tools Toggle & Custom Step Budget:** Toggle `Tools: On / Off` per chat, customize max tool steps (`2–40` steps or `Uncapped`), and inspect/edit `soul.md` and `cube_soul.md`.
- **Instant Memory Control:** Click **Clear RAM - VRAM** in the model menu (or **Manage models**) at any time to terminate the inference process and free 100% of GPU VRAM and system RAM. **Quit app** cleanly stops both the model process and local server.
- **Live Voice Mode:** Zero-VRAM OS TTS on Windows, macOS, and Linux, plus browser voice input and optional local audio output.

---

## Terminal CLI (`cli/`)

Install the `ai` command into `~/.local/bin` (macOS / Linux):

```bash
./cli/install.sh
```

Or run directly with `python3 cli/ai` / `python cli/ai` on any OS:

```bash
# Download a preset or Hugging Face GGUF repo
ai --pull lfm3

# One-shot command lookup or question
ai "how do I forward port 8080 over ssh"

# Pure text mode (skips tool schemas for maximum speed)
ai -n "command to list open ports"

# Pick another model
ai -m gemma4:e2b "explain systemd user timers"

# Interactive terminal chat (/model, /pull, /tools, /soul, /save, /clear)
ai
```

---

## Building Standalone Releases on GitHub

Push a version tag (e.g. `git tag -a v1.1.0 -m "v1.1.0" && git push origin v1.1.0`) or run the **Build & Release** workflow manually in GitHub Actions to generate `LocalAgent-Windows.exe`, `LocalAgent-macOS.zip`, and `LocalAgent-Linux`.
