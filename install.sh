#!/usr/bin/env bash
# One-shot setup + start. macOS only.
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Swarm runs on a Mac." >&2
  exit 1
fi

need_tmux=0
need_py=0
command -v tmux >/dev/null || need_tmux=1
if ! command -v python3.11 >/dev/null && ! command -v python3 >/dev/null; then
  need_py=1
fi

if [[ $need_tmux -eq 1 || $need_py -eq 1 ]]; then
  if ! command -v brew >/dev/null; then
    echo "Install Homebrew (https://brew.sh), then run ./install.sh again." >&2
    exit 1
  fi
  [[ $need_tmux -eq 1 ]] && brew install tmux
  [[ $need_py -eq 1 ]] && brew install python@3.11
fi

PY="$(command -v python3.11 || command -v python3)"
if [[ ! -x .venv/bin/python ]]; then
  echo "→ Python venv"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

if [[ "${1:-}" == "--browser" ]]; then
  pip install -q playwright
  playwright install chromium
fi

ok=0
for bin in grok claude codex; do
  if command -v "$bin" >/dev/null; then
    echo "→ found $bin"
    ok=1
  fi
done
if [[ $ok -eq 0 ]]; then
  echo ""
  echo "No AI CLI in PATH yet. Install one, then run ./install.sh again:"
  echo "  Grok     https://grok.com"
  echo "  Claude   https://claude.ai/code"
  echo "  ChatGPT  https://github.com/openai/codex"
  exit 1
fi

echo ""
echo "Starting Swarm. Open the Wi-Fi URL on your iPhone, then tap + Bot."
echo ""
exec ./bin/imac-phone
