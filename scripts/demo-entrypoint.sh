#!/usr/bin/env bash
# Demo 模式入口 — 只需 API key 即可体验 ops-agent
# 用法: docker run -it -e OPS_LLM_API_KEY=sk-ant-... slobcoder/ops-agent demo

set -euo pipefail

NOTEBOOK="/data/notebook"
CONFIG_DIR="$NOTEBOOK/config"

echo ""
echo "🎮 OpsAgent Demo Mode"
echo "━━━━━━━━━━━━━━━━━━━━"
echo ""

# 检查 API key
if [[ -z "${OPS_LLM_API_KEY:-}" ]]; then
    echo "❌ 请设置 OPS_LLM_API_KEY 环境变量"
    echo ""
    echo "用法:"
    echo "  docker run -it -e OPS_LLM_API_KEY=sk-ant-... slobcoder/ops-agent demo"
    echo ""
    echo "支持的环境变量:"
    echo "  OPS_LLM_API_KEY      (必填) LLM API Key"
    echo "  OPS_LLM_PROVIDER     (可选) anthropic/openai/zhipu，默认 anthropic"
    echo "  OPS_LLM_MODEL        (可选) 模型名"
    echo "  OPS_LLM_BASE_URL     (可选) 自定义 API 地址"
    exit 1
fi

# 生成 demo 配置
mkdir -p "$CONFIG_DIR"

# targets.yaml — 监控本机（容器内）
if [[ ! -f "$CONFIG_DIR/targets.yaml" ]]; then
    cat > "$CONFIG_DIR/targets.yaml" << 'EOF'
# Demo 模式 — 监控容器自身
targets:
  - name: demo-local
    type: local
    description: "Demo: 监控本容器"
    criticality: low
EOF
    echo "✅ $CONFIG_DIR/targets.yaml"
fi

# limits.yaml — 安全默认值
if [[ ! -f "$CONFIG_DIR/limits.yaml" ]]; then
    cat > "$CONFIG_DIR/limits.yaml" << 'EOF'
enabled: true
max_actions_per_hour: 20
max_actions_per_day: 100
max_restarts_per_service_per_hour: 3
max_restarts_per_service_per_day: 5
max_concurrent_incidents: 2
cooldown_after_failure_seconds: 600
llm_tokens_per_hour: 200000
llm_tokens_per_day: 1000000
max_collab_auto_rounds: 30
max_observations_chars: 8000
max_total_rounds: 15
max_diagnose_rounds: 25
max_fix_attempts: 3
silence_window_seconds: 1800
max_observe_commands: 15
max_verify_steps: 15
max_quick_observe_commands: 20
max_gap_commands: 20
max_generated_gap_commands: 20
max_chat_commands: 20
max_collab_history_rounds: 25
max_recent_incidents: 15
max_patch_attempts: 3
max_source_locations: 15
max_unresolved_frames: 5
EOF
    echo "✅ $CONFIG_DIR/limits.yaml"
fi

# permissions.md
if [[ ! -f "$CONFIG_DIR/permissions.md" ]]; then
    cat > "$CONFIG_DIR/permissions.md" << 'EOF'
# Authorization Rules (Demo)

## Default Policy
- Read-only observation commands (L0): execute directly
- Write Notebook (L1): execute directly
- All L2+ operations: require human approval (demo mode)

## Core Services
- All services require approval in demo mode

## Emergency
Standard emergency procedures apply.
EOF
    echo "✅ $CONFIG_DIR/permissions.md"
fi

# .env 文件
if [[ ! -f "$NOTEBOOK/.env" ]]; then
    cat > "$NOTEBOOK/.env" << EOF
OPS_LLM_PROVIDER=${OPS_LLM_PROVIDER:-anthropic}
OPS_LLM_API_KEY=${OPS_LLM_API_KEY}
EOF
    if [[ -n "${OPS_LLM_MODEL:-}" ]]; then
        echo "OPS_LLM_MODEL=${OPS_LLM_MODEL}" >> "$NOTEBOOK/.env"
    fi
    if [[ -n "${OPS_LLM_BASE_URL:-}" ]]; then
        echo "OPS_LLM_BASE_URL=${OPS_LLM_BASE_URL}" >> "$NOTEBOOK/.env"
    fi
    echo "✅ $NOTEBOOK/.env"
fi

# 初始化 notebook git
cd "$NOTEBOOK"
if [[ ! -d ".git" ]]; then
    git init -q
    git add -A
    git commit -m "demo init" -q
fi

echo ""
echo "━━━ Demo Ready ━━━"
echo ""
echo "  Agent 将监控本容器，你可以："
echo "  - 输入自然语言提问（如: 最近有什么异常?）"
echo "  - 输入 status 查看状态"
echo "  - 输入 quit 退出"
echo ""
echo "  提示: 容器内是干净环境，Agent 主要做巡检演示。"
echo "  要监控真实服务器，请使用 ops-agent init 配置 SSH 目标。"
echo ""

# 启动 Agent
exec ops-agent --notebook "$NOTEBOOK"
