#!/usr/bin/env bash
# Docker 入口脚本 — 区分 demo 和正常模式
set -euo pipefail

NOTEBOOK="/data/notebook"

if [[ "${1:-}" == "demo" ]]; then
    # demo 模式：自动生成 mock 配置，只需 API key
    shift
    exec /usr/local/bin/demo-entrypoint.sh "$@"
elif [[ "${1:-}" == "init" ]]; then
    # init 模式
    exec ops-agent --notebook "$NOTEBOOK" init "$@"
elif [[ "${1:-}" == "check" ]]; then
    # check 模式
    exec ops-agent --notebook "$NOTEBOOK" check "$@"
else
    # 默认：启动 Agent
    exec ops-agent --notebook "$NOTEBOOK" "$@"
fi
