#!/bin/bash
# Installs CREATIVE HQ's Python dependencies when a new Claude Code cloud session starts.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"
pip install --quiet --disable-pip-version-check -r requirements.txt 2>&1 | grep -v -i "warning" || true
