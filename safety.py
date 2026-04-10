"""
Safety — 紧急停止开关

提供三种触发方式让 Agent 立刻进入只读模式:
1. 文件触发: notebook/EMERGENCY_STOP 文件存在
2. 信号触发: SIGUSR1 (kill -USR1 <pid>)
3. 远程触发: 由其他模块调用 trigger() 方法

紧急停止后:
- Agent 仍然在岗、仍然观察、仍然回答人类
- 但所有 L2+ 修改类动作被强制拒绝
- 可以通过删除文件 + 输入 'unfreeze' 解除
"""

import os
import signal
import logging
import threading
from pathlib import Path

logger = logging.getLogger("ops-agent.safety")


class EmergencyStop:
    """紧急停止开关"""

    STOP_FILE = "EMERGENCY_STOP"

    def __init__(self, notebook_path: str):
        self.notebook_path = Path(notebook_path)
        self._frozen = False
        self._reason = ""
        self._from_file = False  # True 表示由文件触发,文件删除后会自动解冻
        self._lock = threading.Lock()

        # 注册信号处理
        try:
            signal.signal(signal.SIGUSR1, self._on_signal)
            logger.info("SIGUSR1 handler installed")
        except (ValueError, AttributeError):
            # Windows 没有 SIGUSR1
            logger.info("SIGUSR1 not available on this platform")

    def _on_signal(self, signum, frame):
        """SIGUSR1 触发"""
        self.trigger("收到 SIGUSR1 信号")

    def check(self) -> tuple[bool, str]:
        """检查是否处于停止状态。返回 (frozen, reason)"""
        stop_file = self.notebook_path / self.STOP_FILE
        if stop_file.exists():
            with self._lock:
                if not self._frozen:
                    self._frozen = True
                    self._from_file = True
                    try:
                        self._reason = stop_file.read_text(encoding="utf-8").strip() or "EMERGENCY_STOP file exists"
                    except Exception:
                        self._reason = "EMERGENCY_STOP file exists"
                    logger.warning(f"Emergency stop triggered by file: {self._reason}")
        else:
            # 文件被删 + 当前是文件触发的 → 自动解冻
            with self._lock:
                if self._frozen and self._from_file:
                    self._frozen = False
                    self._from_file = False
                    self._reason = ""
                    logger.info("Emergency stop cleared (file removed)")

        with self._lock:
            return self._frozen, self._reason

    def trigger(self, reason: str = "Manual trigger"):
        """主动触发紧急停止(非文件源)"""
        with self._lock:
            self._frozen = True
            self._reason = reason
            self._from_file = False  # 代码触发,不会因为文件被删而解冻
        logger.warning(f"Emergency stop triggered: {reason}")
        # 同时写文件作为持久化标记
        try:
            (self.notebook_path / self.STOP_FILE).write_text(reason, encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to write stop file: {e}")

    def clear(self):
        """解除紧急停止"""
        with self._lock:
            self._frozen = False
            self._reason = ""
            self._from_file = False
        try:
            stop_file = self.notebook_path / self.STOP_FILE
            if stop_file.exists():
                stop_file.unlink()
        except Exception as e:
            logger.error(f"Failed to remove stop file: {e}")
        logger.info("Emergency stop cleared")

    @property
    def frozen(self) -> bool:
        return self._frozen
