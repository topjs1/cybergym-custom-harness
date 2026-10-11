#!/usr/bin/env bash
# A1a DeepSeek pilot convenience wrapper.
#
#   read -s DEEPSEEK_API_KEY && export DEEPSEEK_API_KEY
#   bash tools/run_pilot.sh
#
# Env overrides: MODEL, TOKEN_BUDGET, MAX_ATTEMPTS, STUB, HARNESS_MAX_COST_USD
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  echo "DEEPSEEK_API_KEY is not set." >&2
  echo "Run this in the tab first:  read -s DEEPSEEK_API_KEY && export DEEPSEEK_API_KEY" >&2
  exit 1
fi

: "${HARNESS_MAX_COST_USD:=1}"
export HARNESS_MAX_COST_USD

exec env UV_CACHE_DIR="$PWD/.uv-cache" uv run python tools/deepseek_pilot.py \
  --tcp \
  --stub "${STUB:-nth=3}" \
  --model "${MODEL:-deepseek-flash}" \
  --token-budget "${TOKEN_BUDGET:-100000}" \
  --max-attempts "${MAX_ATTEMPTS:-30}" \
  --cost-cap-usd "$HARNESS_MAX_COST_USD" \
  "$@"
