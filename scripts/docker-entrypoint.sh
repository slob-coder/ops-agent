#!/usr/bin/env bash
# Docker 入口脚本 — 区分 demo/init/check/run 模式
set -euo pipefail

NOTEBOOK="/data/notebook"

if [[ "${1:-}" == "demo" ]]; then
    shift
    exec /usr/local/bin/demo-entrypoint.sh "$@"
elif [[ "${1:-}" == "init" ]]; then
    shift
    exec ops-agent init --notebook "$NOTEBOOK" "$@"
elif [[ "${1:-}" == "check" ]]; then
    shift
    exec ops-agent check --notebook "$NOTEBOOK" "$@"
else
    exec ops-agent --notebook "$NOTEBOOK" "$@"
fi
