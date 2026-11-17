#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${HOME}/.local/bin"

chmod +x "${SCRIPT_DIR}/ai"
mkdir -p "${TARGET_DIR}"
ln -sf "${SCRIPT_DIR}/ai" "${TARGET_DIR}/ai"

echo "Installed: ${TARGET_DIR}/ai -> ${SCRIPT_DIR}/ai"
if [[ ":${PATH}:" != *":${TARGET_DIR}:"* ]]; then
  echo "Note: add ~/.local/bin to your PATH, e.g.:"
  echo '  echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.bashrc && source ~/.bashrc'
fi
