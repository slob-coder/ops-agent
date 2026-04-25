#!/usr/bin/env bash
# ops-agent 一键安装脚本
# 用法: curl -fsSL https://raw.githubusercontent.com/slob-coder/ops-agent/main/scripts/install-quick.sh | bash
# 或:   wget -qO- https://raw.githubusercontent.com/slob-coder/ops-agent/main/scripts/install-quick.sh | bash

set -euo pipefail

REPO="slob-coder/ops-agent"
INSTALL_DIR="${OPS_AGENT_HOME:-$HOME/.ops-agent}"
VENV_DIR="$INSTALL_DIR/.venv"
BRANCH="${OPS_AGENT_BRANCH:-main}"

# ── 颜色 ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}ℹ️  $*${NC}"; }
ok()    { echo -e "${GREEN}✅ $*${NC}"; }
warn()  { echo -e "${YELLOW}⚠️  $*${NC}"; }
err()   { echo -e "${RED}❌ $*${NC}"; }

# ── 检查依赖 ──
check_deps() {
    local missing=()
    for cmd in python3 git; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        err "缺少必要依赖: ${missing[*]}"
        echo ""
        echo "安装方式:"
        echo "  Ubuntu/Debian: sudo apt install -y python3 python3-venv git"
        echo "  CentOS/RHEL:   sudo yum install -y python3 git"
        echo "  macOS:         brew install python3 git"
        exit 1
    fi

    # Python 版本检查
    PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
    if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 9 ]]; }; then
        err "Python 版本过低: $PY_VER，需要 >= 3.9"
        exit 1
    fi
    info "Python $PY_VER ✓"
}

# ── 安装 ──
install() {
    info "安装目录: $INSTALL_DIR"

    # 克隆（或更新）
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        info "已存在，拉取最新版本..."
        cd "$INSTALL_DIR"
        git fetch origin "$BRANCH" --quiet
        git reset --hard "origin/$BRANCH" --quiet
    else
        info "克隆仓库..."
        git clone --depth 1 --branch "$BRANCH" "https://github.com/$REPO.git" "$INSTALL_DIR" --quiet
        cd "$INSTALL_DIR"
    fi

    # 创建 venv
    if [[ ! -d "$VENV_DIR" ]]; then
        info "创建 Python 虚拟环境..."
        python3 -m venv "$VENV_DIR"
    fi

    # 安装依赖
    info "安装 Python 依赖..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet -r requirements.txt
    "$VENV_DIR/bin/pip" install --quiet -e .

    # 创建 shell wrapper
    create_wrapper

    ok "安装完成！"
}

# ── 创建 shell wrapper ──
create_wrapper() {
    local wrapper="$INSTALL_DIR/ops-agent"
    cat > "$wrapper" << 'WRAPPER'
#!/usr/bin/env bash
# ops-agent wrapper — 自动激活 venv 并运行
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/.venv/bin/activate"
exec python "$SCRIPT_DIR/main.py" "$@"
WRAPPER
    chmod +x "$wrapper"

    # 创建符号链接到用户 PATH
    local link_target=""
    for dir in "$HOME/.local/bin" "$HOME/bin"; do
        if [[ -d "$dir" ]] && echo ":$PATH:" | grep -q ":$dir:"; then
            link_target="$dir/ops-agent"
            break
        fi
    done

    if [[ -n "$link_target" ]]; then
        ln -sf "$wrapper" "$link_target"
        ok "已创建命令链接: $link_target"
    else
        # 创建 ~/.local/bin 并加到 PATH
        mkdir -p "$HOME/.local/bin"
        ln -sf "$wrapper" "$HOME/.local/bin/ops-agent"
        ok "已创建命令链接: ~/.local/bin/ops-agent"

        # 提示加 PATH
        local shell_rc=""
        if [[ -f "$HOME/.bashrc" ]]; then shell_rc="$HOME/.bashrc"
        elif [[ -f "$HOME/.zshrc" ]]; then shell_rc="$HOME/.zshrc"; fi

        if [[ -n "$shell_rc" ]] && ! grep -q '.local/bin' "$shell_rc"; then
            echo '' >> "$shell_rc"
            echo '# ops-agent' >> "$shell_rc"
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$shell_rc"
            warn "已将 ~/.local/bin 加入 $shell_rc"
            warn "请运行: source $shell_rc"
        fi
    fi
}

# ── 主流程 ──
main() {
    echo ""
    echo "🦀 OpsAgent 一键安装"
    echo "━━━━━━━━━━━━━━━━━━━━"
    echo ""

    check_deps
    install

    echo ""
    echo "━━━ Next Steps ━━━"
    echo ""
    echo "  1. 初始化配置:"
    echo "     ops-agent init"
    echo ""
    echo "  2. 或从环境变量初始化:"
    echo "     ops-agent init --from-env"
    echo ""
    echo "  3. 启动:"
    echo "     ops-agent"
    echo ""
    echo "🎉 欢迎使用 OpsAgent！"
}

main "$@"
