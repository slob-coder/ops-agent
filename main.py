#!/usr/bin/env python3
"""
OpsAgent — 数字运维员工
一个实时在岗、会成长、在人类监督下工作的运维 Agent。

用法:
  # 初始化配置
  ops-agent init

  # 从环境变量初始化（Docker / CI）
  ops-agent init --from-env

  # 本地模式（监控本机）
  ops-agent

  # SSH 远程模式
  ops-agent --target user@192.168.1.100

  # 只读模式（不执行任何修改）
  ops-agent --readonly

  # 指定 workspace 目录
  ops-agent --workspace /root/.ops-agent
"""

import os
import signal
import logging
import argparse
from pathlib import Path

from src.infra.tools import TargetConfig

# 重新导出 OpsAgent，保证 `from main import OpsAgent` 继续工作
from src.core import OpsAgent  # noqa: F401

# 默认 workspace 目录
DEFAULT_WORKSPACE = "~/.ops-agent"


def _resolve_workspace(args_workspace: str, args_notebook: str = "") -> str:
    """解析 workspace 路径，--workspace 优先，--notebook 向后兼容"""
    if args_workspace:
        return str(Path(args_workspace).expanduser().resolve())
    # 向后兼容：--notebook 指定时，推导 workspace 为其父目录（如果子目录名是 notebook）
    if args_notebook:
        nb = Path(args_notebook).expanduser().resolve()
        if nb.name == "notebook":
            return str(nb.parent)
        # 否则 notebook_path 就是 workspace 本身（兼容旧用法）
        return str(nb)
    return str(Path(DEFAULT_WORKSPACE).expanduser().resolve())


def _load_dotenv(workspace: str):
    """自动加载 workspace/.env 文件到环境变量（不覆盖已有值）"""
    env_path = Path(workspace) / ".env"
    if not env_path.exists():
        # 向后兼容：尝试 notebook/.env
        legacy_path = Path(workspace) / "notebook" / ".env"
        if legacy_path.exists():
            env_path = legacy_path
        else:
            return

    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpsAgent — 数字运维员工")
    subparsers = parser.add_subparsers(dest="command")

    # ── init 子命令 ──
    init_parser = subparsers.add_parser(
        "init", help="Interactive setup wizard",
        description="Generate config files with an interactive guide",
    )
    init_parser.add_argument("--workspace", default=DEFAULT_WORKSPACE,
                             help="Workspace 根目录 (默认 ~/.ops-agent)")
    init_parser.add_argument("--notebook", default="",
                             help="[deprecated] 使用 --workspace 代替")
    init_parser.add_argument("--from-env", action="store_true",
                             help="Read all config from env vars, fail on missing")

    # ── check 子命令 ──
    check_parser = subparsers.add_parser(
        "check", help="Config validation",
    )
    check_parser.add_argument("--workspace", default=DEFAULT_WORKSPACE,
                              help="Workspace 根目录 (默认 ~/.ops-agent)")
    check_parser.add_argument("--notebook", default="",
                              help="[deprecated] 使用 --workspace 代替")
    check_parser.add_argument("--test-llm", action="store_true",
                              help="Test LLM connectivity")

    # ── 运行模式参数（无子命令时）──
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE,
                        help="Workspace 根目录 (默认 ~/.ops-agent)")
    parser.add_argument("--notebook", default="",
                        help="[deprecated] 使用 --workspace 代替")
    parser.add_argument("--targets", default="",
                        help="targets.yaml 路径(多目标模式,推荐)")
    parser.add_argument("--target", default="",
                        help="单目标模式(SSH: user@host)。--targets 优先")
    parser.add_argument("--port", type=int, default=22, help="SSH 端口")
    parser.add_argument("--key", default="", help="SSH 密钥路径")
    parser.add_argument("--password", action="store_true",
                        help="使用密码认证(将交互式提示输入,需要 sshpass)")
    parser.add_argument("--readonly", action="store_true", help="只读模式")
    parser.add_argument("--debug", action="store_true", help="调试模式")

    return parser


def main():
    import sys

    # 扫描子命令
    subcmd = None
    skip_next = False
    for i, arg in enumerate(sys.argv[1:], 1):
        if skip_next:
            skip_next = False
            continue
        if arg in ("init", "check"):
            subcmd = arg
            break
        elif arg.startswith("-") and not arg.startswith("--from-env") and not arg.startswith("--test-llm"):
            if arg in ("--workspace", "--notebook", "--targets", "--target", "--key", "--port",
                       "--targets-file", "--model"):
                skip_next = True

    if subcmd == "init":
        parser = argparse.ArgumentParser(description="ops-agent init")
        parser.add_argument("command", nargs="?", default="init")
        parser.add_argument("--workspace", default=DEFAULT_WORKSPACE,
                            help="Workspace 根目录 (默认 ~/.ops-agent)")
        parser.add_argument("--notebook", default="",
                            help="[deprecated] 使用 --workspace 代替")
        parser.add_argument("--from-env", action="store_true",
                            help="Read all config from env vars, fail on missing")
        args = parser.parse_args()
        workspace = _resolve_workspace(args.workspace, args.notebook)
        from src.init import run_init
        run_init(workspace_path=workspace, from_env=args.from_env)
        return

    if subcmd == "check":
        parser = argparse.ArgumentParser(description="ops-agent check")
        parser.add_argument("command", nargs="?", default="check")
        parser.add_argument("--workspace", default=DEFAULT_WORKSPACE,
                            help="Workspace 根目录 (默认 ~/.ops-agent)")
        parser.add_argument("--notebook", default="",
                            help="[deprecated] 使用 --workspace 代替")
        parser.add_argument("--test-llm", action="store_true",
                            help="Test LLM connectivity")
        args = parser.parse_args()
        workspace = _resolve_workspace(args.workspace, args.notebook)
        from src.check import run_check
        run_check(workspace_path=workspace, test_llm=args.test_llm)
        return

    # 主运行模式
    parser = _build_parser()
    args = parser.parse_args()

    workspace = _resolve_workspace(args.workspace, args.notebook)

    # 自动加载 .env 文件
    _load_dotenv(workspace)

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    notebook_path = str(Path(workspace) / "notebook")

    # ── 加载目标 ──
    targets = []
    fallback = None

    targets_file = args.targets
    if not targets_file:
        default_path = Path(notebook_path) / "config" / "targets.yaml"
        if default_path.exists():
            targets_file = str(default_path)

    if targets_file:
        from src.infra.targets import load_targets
        loaded = load_targets(targets_file)
        targets = [TargetConfig.from_target(t) for t in loaded]
        if targets:
            print(f"✓ 已从 {targets_file} 加载 {len(targets)} 个目标")

    # 单目标兼容模式
    if not targets and args.target:
        password = ""
        if args.password:
            import getpass
            password = getpass.getpass(f"SSH password for {args.target}: ")
        elif os.getenv("OPS_SSH_PASSWORD"):
            password = os.getenv("OPS_SSH_PASSWORD", "")
        fallback = TargetConfig.ssh(args.target, args.port, args.key, password)

    if not targets and not fallback:
        fallback = TargetConfig.local()
        print(f"ℹ️  未指定目标,使用本机模式。如需多目标请创建 {notebook_path}/config/targets.yaml")

    # 启动 Agent
    agent = OpsAgent(
        workspace_path=workspace,
        targets=targets,
        readonly=args.readonly,
        fallback_target=fallback,
    )

    # 优雅退出
    def handler(sig, frame):
        agent._running = False

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    try:
        agent.run()
    finally:
        from src.infra.chat import _restore_terminal
        _restore_terminal()


if __name__ == "__main__":
    main()
