#!/usr/bin/env bash
# ops-agent 一键安装脚本
# 用法: curl -fsSL https://raw.githubusercontent.com/slob-coder/ops-agent/main/scripts/install-quick.sh | bash
# 或:   wget -qO- https://raw.githubusercontent.com/slob-coder/ops-agent/main/scripts/install-quick.sh | bash

set -euo pipefail

REPO="slob-coder/ops-agent"
INSTALL_DIR="${OPS_AGENT_HOME:-$HOME/.ops-agent}"
VENV_DIR="$INSTALL_DIR/.venv"
BRANCH="${OPS_AGENT_BRANCH:-main}"
BIN_DIR="$INSTALL_DIR/bin"

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

    # 创建 bin 目录 + wrapper
    create_bin

    ok "安装完成！"
}

# ── 创建可执行 ──
create_bin() {
    mkdir -p "$BIN_DIR"

    # 主 wrapper
    cat > "$BIN_DIR/ops-agent" << 'WRAPPER'
#!/usr/bin/env bash
# Resolve symlink to get real install directory
SELF="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"
REAL_DIR="$(cd "$(dirname "$SELF")" && pwd)"
VENVSRC="$REAL_DIR/../.venv/bin/activate"
if [[ -f "$VENVSRC" ]]; then
    source "$VENVSRC"
fi
exec python3 "$REAL_DIR/../main.py" "$@"
WRAPPER
    chmod +x "$BIN_DIR/ops-agent"

    # 尝试加入 PATH — 两种方式都做

    # 方式 1: 符号链接到已有 PATH 目录
    local linked=false
    for dir in "$HOME/.local/bin" "$HOME/bin" /usr/local/bin; do
        if echo ":$PATH:" | grep -q ":$dir:" && [[ -w "$dir" || -w "$(dirname "$dir")" ]]; then
            mkdir -p "$dir" 2>/dev/null || true
            if ln -sf "$BIN_DIR/ops-agent" "$dir/ops-agent" 2>/dev/null; then
                ok "命令链接: $dir/ops-agent"
                linked=true
                break
            fi
        fi
    done

    # 方式 2: 写 shell rc 加 PATH
    local shell_rc=""
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
        if [[ -f "$rc" ]]; then
            shell_rc="$rc"
            break
        fi
    done

    if [[ -n "$shell_rc" ]] && ! grep -q 'ops-agent/bin' "$shell_rc" 2>/dev/null; then
        echo '' >> "$shell_rc"
        echo '# ops-agent' >> "$shell_rc"
        echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$shell_rc"
        warn "已将 $BIN_DIR 加入 $shell_rc"
    fi

    # 方式 3: 如果还是没有直接可用的 ops-agent，打印 eval 命令
    if ! command -v ops-agent &>/dev/null; then
        warn "当前 shell 尚未识别 ops-agent 命令"
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

    # 判断 ops-agent 是否直接可用
    if command -v ops-agent &>/dev/null; then
        echo "  直接运行:"
        echo "    ops-agent init"
        echo ""
    else
        echo "  当前终端运行（立即可用）:"
        echo "    export PATH=\"$BIN_DIR:\$PATH\""
        echo "    ops-agent init"
        echo ""
        echo "  新终端自动可用（已写入 shell 配置）"
        echo ""
        echo "  或直接用完整路径:"
        echo "    $BIN_DIR/ops-agent init"
    fi

    echo "  启动:"
    if command -v ops-agent &>/dev/null; then
        echo "    ops-agent"
    else
        echo "    $BIN_DIR/ops-agent"
    fi
    echo ""
    echo "🎉 欢迎使用 OpsAgent！"
}

main "$@"
