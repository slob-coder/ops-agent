#!/usr/bin/env python3
"""
OpsAgent — 数字运维员工
一个实时在岗、会成长、在人类监督下工作的运维 Agent。

用法:
  # 本地模式（监控本机）
  python main.py --notebook ./notebook

  # SSH 远程模式
  python main.py --notebook ./notebook --target user@192.168.1.100

  # 只读模式（不执行任何修改）
  python main.py --notebook ./notebook --readonly
"""

import os
import signal
import logging
import argparse
from pathlib import Path

from tools import TargetConfig

# 重新导出 OpsAgent，保证 `from main import OpsAgent` 继续工作
from core import OpsAgent  # noqa: F401


def main():
    parser = argparse.ArgumentParser(description="OpsAgent — 数字运维员工")
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
        from targets import load_targets
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
        from chat import _restore_terminal
        _restore_terminal()


if __name__ == "__main__":
    main()
