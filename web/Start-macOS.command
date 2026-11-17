#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  exec python3 local_agent.py
fi

echo ""
echo "Python 3 was not found on this Mac."
echo "Either download LocalAgent-macOS.zip from GitHub Releases,"
echo "or install Python 3 with: brew install python"
echo ""
read -r -p "Press Enter to close..."
exit 1
