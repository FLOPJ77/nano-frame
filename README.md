# local agent

A tiny, zero-dependency local AI framework built for small models (`lfm3`, `gemma4`, `qwen3.5`, `granite4.2`) via **Ollama**.

Split into two standalone tools:
- **Web App (`web/`)** — Cross-platform browser UI for Windows, macOS, and Linux.
- **Terminal CLI (`cli/`)** — Ultra-fast terminal command helper (`ai "how do I ..."`).

---

## Quick Start (Friends / Desktop — Windows, macOS, Linux)

### 1. Install Ollama (handles GPU automatically)
Download and install Ollama from **[ollama.com/download](https://ollama.com/download)**.
- **Windows (RTX / NVIDIA):** CUDA GPU acceleration works automatically.
- **macOS (Apple Silicon):** Metal GPU acceleration works automatically.

### 2. Run Local Agent
- **From GitHub Releases (no Python needed):**
  - **Windows:** Download `LocalAgent-Windows.exe` and double-click it.
  - **macOS:** Download `LocalAgent-macOS.zip`, unzip, and open `LocalAgent.app`.
  - **Linux:** Download `LocalAgent-Linux`, `chmod +x LocalAgent-Linux && ./LocalAgent-Linux`.
- **From Source (Python 3):**
  - **Windows:** Double-click `web/Start-Windows.bat`
  - **macOS:** Double-click `web/Start-macOS.command`
  - **Linux:** Run `./web/start-linux.sh` or `python3 web/local_agent.py`

Your default browser opens automatically at `http://127.0.0.1:8765`. If you haven't pulled a model yet, click **Pull `<model>`** right inside the web UI banner.

> **First-launch OS warning (unsigned GitHub apps):**
> - **Windows SmartScreen:** Click *More info* → *Run anyway*.
> - **macOS Gatekeeper:** Right-click `LocalAgent.app` (or `Start-macOS.command`) → *Open*, or run `xattr -dr com.apple.quarantine LocalAgent.app`.

### Features
- Works in any browser (Chrome, Edge, Firefox, Safari)
- **Tools toggle (`Tools: On / Off`)** in the top bar for safe chat-only mode vs. local file & shell tools
- Live editable `soul.md` system prompt (stored in `~/.local-agent/` when packaged)
- Auto-saved local chat history

---

## Quick Start (Personal Terminal CLI — Linux)

Install the `ai` command into `~/.local/bin`:

```bash
./cli/install.sh
```

Usage:

```bash
# One-shot command lookup or question
ai "how do I forward port 8080 over ssh"

# Pure text mode (skips tool schemas for maximum speed)
ai -n "command to list open ports"

# Pick another model
ai -m gemma4 "explain systemd user timers"

# Interactive terminal chat (/model, /tools, /soul, /save, /clear)
ai
```

---

## Building Standalone Releases on GitHub

Push a version tag (e.g. `git tag v1.0.0 && git push origin v1.0.0`) or run the **Build & Release** workflow manually in GitHub Actions to generate `LocalAgent-Windows.exe`, `LocalAgent-macOS.zip`, and `LocalAgent-Linux`.
