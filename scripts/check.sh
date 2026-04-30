#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VERSION="$(tr -d '[:space:]' < VERSION)"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "VERSION must use semantic versioning like 0.1.0" >&2
  exit 1
fi

PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m py_compile bot.py config.py
python3 -m json.tool data/taboo_words_ru_en_uk.json >/dev/null
