#!/usr/bin/env python3
"""
OpsAgent — 数字运维员工
一个实时在岗、会成长、在人类监督下工作的运维 Agent。

用法:
  # 安装命令行工具
  pip install -e .

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

  # 未安装时用 python main.py 替代 ops-agent
"""

import os
import signal
import logging
import argparse
from pathlib import Path

from src.infra.tools import TargetConfig

# 重新导出 OpsAgent，保证 `from main import OpsAgent` 继续工作
from src.core import OpsAgent  # noqa: F401


def _load_dotenv():
    """自动加载 notebook/.env 文件到环境变量（不覆盖已有值）"""
    import sys
    notebook = "./notebook"
    # 从命令行参数获取 notebook 路径
    for i, arg in enumerate(sys.argv):
        if arg == "--notebook" and i + 1 < len(sys.argv):
            notebook = sys.argv[i + 1]
            break

    env_path = Path(notebook) / ".env"
    if not env_path.exists():
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
    init_parser.add_argument("--notebook", default="./notebook", help="Notebook 目录路径")
    init_parser.add_argument("--from-env", action="store_true",
                             help="Read all config from env vars, fail on missing")

    # ── 运行模式参数（无子命令时）──
    parser.add_argument("--notebook", default="./notebook", help="Notebook 目录路径")
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
    # 手动解析以支持子命令和主参数共存
    # 扫描 argv 查找子命令（支持子命令不在第一个位置的情况）
    import sys
    subcmd = None
    for arg in sys.argv[1:]:
        if arg in ("init", "check"):
            subcmd = arg
            break
        elif arg.startswith("-"):
            # 跳过 flag 和它的值
            if arg in ("--notebook", "--targets", "--target", "--key", "--port",
                       "--targets-file", "--model"):
                continue  # next arg is the value, skip it in the next iteration
        else:
            break  # unknown positional, stop

    if subcmd == "init":
        parser = argparse.ArgumentParser(description="ops-agent init")
        parser.add_argument("command", nargs="?", default="init")
        parser.add_argument("--notebook", default="./notebook", help="Notebook 目录路径")
        parser.add_argument("--from-env", action="store_true",
                            help="Read all config from env vars, fail on missing")
        args = parser.parse_args()
        from src.init import run_init
        run_init(notebook_path=args.notebook, from_env=args.from_env)
        return

    if subcmd == "check":
        parser = argparse.ArgumentParser(description="ops-agent check")
        parser.add_argument("command", nargs="?", default="check")
        parser.add_argument("--notebook", default="./notebook", help="Notebook 目录路径")
        parser.add_argument("--test-llm", action="store_true", help="Test LLM connectivity")
        args = parser.parse_args()
        from src.check import run_check
        run_check(notebook_path=args.notebook, test_llm=args.test_llm)
        return

    # 自动加载 .env 文件
    _load_dotenv()

    parser = _build_parser()
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── 加载目标 ──
    targets = []
    fallback = None

    # 优先尝试 --targets yaml 文件
    targets_file = args.targets
    if not targets_file:
        # 尝试默认路径 notebook/config/targets.yaml
        default_path = Path(args.notebook) / "config" / "targets.yaml"
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
        print("ℹ️  未指定目标,使用本机模式。如需多目标请创建 notebook/config/targets.yaml")

    # 启动 Agent
    agent = OpsAgent(
        notebook_path=args.notebook,
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
        # 确保终端状态恢复（防止 echo 丢失）
        from src.infra.chat import _restore_terminal
        _restore_terminal()


if __name__ == "__main__":
    main()
