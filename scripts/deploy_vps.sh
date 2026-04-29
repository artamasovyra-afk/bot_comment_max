#!/usr/bin/env bash
set -Eeuo pipefail

: "${DEPLOY_HOST:?DEPLOY_HOST is required}"
: "${DEPLOY_USER:?DEPLOY_USER is required}"
: "${DEPLOY_PATH:?DEPLOY_PATH is required}"
: "${DEPLOY_SERVICE:?DEPLOY_SERVICE is required}"

DEPLOY_PORT="${DEPLOY_PORT:-22}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SSH_OPTS=(
  -p "$DEPLOY_PORT"
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
)

SCP_OPTS=(
  -P "$DEPLOY_PORT"
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
)

APP_FILES=(
  "$ROOT_DIR/bot.py"
  "$ROOT_DIR/config.py"
  "$ROOT_DIR/VERSION"
)

WEBAPP_FILES=(
  "$ROOT_DIR/webapp/index.html"
  "$ROOT_DIR/webapp/app.css"
  "$ROOT_DIR/webapp/app.js"
)

scp "${SCP_OPTS[@]}" "${APP_FILES[@]}" "$DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_PATH/"
scp "${SCP_OPTS[@]}" "${WEBAPP_FILES[@]}" "$DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_PATH/webapp/"

ssh "${SSH_OPTS[@]}" "$DEPLOY_USER@$DEPLOY_HOST" \
  "cd '$DEPLOY_PATH' && python3 -m py_compile bot.py config.py && systemctl restart '$DEPLOY_SERVICE' && systemctl is-active '$DEPLOY_SERVICE'"
