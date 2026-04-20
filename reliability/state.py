"""
state — Agent 运行时状态的持久化与崩溃恢复

设计要点:
- 序列化:JSON 文件,人类可读,git 可 diff
- 写入原子化:先写 .tmp 再 os.replace,防止崩溃留下损坏文件
- 版本号:不兼容的旧状态直接丢弃,不做迁移(简单粗暴但可靠)
- 字段尽量保持扁平,所有值必须 JSON-safe
"""

from __future__ import annotations

import os
import json
import time
import logging
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("ops-agent.state")

STATE_VERSION = 1


@dataclass
class AgentState:
    """Agent 运行时状态快照。

    每次状态变化(模式切换 / Incident 开关 / 关键动作前后)都应该 checkpoint。
    """
    version: int = STATE_VERSION
    mode: str = "patrol"
    current_target_name: str = ""
    current_incident: str = ""           # 文件名 e.g. "incident-001.md",空 = 无
    current_issue: str = ""
    paused: bool = False
    readonly: bool = False
    last_checkpoint_time: float = 0.0

    # Sprint 4 留下的两项 in-memory 状态
    last_error_text: str = ""             # 复发检测 baseline
    auto_merge_timestamps: list = field(default_factory=list)

    # 正在进行中的、可能被崩溃打断的动作描述(纯字符串,不可重放,只供人类参考)
    uncompleted_action: str = ""

    # ──────────── 序列化 ────────────

    def save(self, path: str) -> bool:
        """原子写入。失败返回 False。"""
        self.last_checkpoint_time = time.time()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return True
        except OSError as e:
            logger.warning(f"state save failed: {e}")
            return False

    @classmethod
    def load(cls, path: str) -> "AgentState | None":
        """加载状态。版本不匹配 / 文件不存在 / 解析失败 → 返回 None。"""
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"state load failed: {e}")
            return None

        if not isinstance(data, dict):
            return None
        if data.get("version") != STATE_VERSION:
            logger.info(
                f"state version mismatch (file={data.get('version')}, "
                f"expected={STATE_VERSION}), discarding old state"
            )
            return None

        # 过滤未知字段,允许新增字段时旧状态仍可加载(但版本不一致时已丢弃)
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid}
        try:
            return cls(**filtered)
        except TypeError as e:
            logger.warning(f"state instantiation failed: {e}")
            return None

    def has_active_work(self) -> bool:
        """是否有未完成的工作需要恢复"""
        return bool(self.current_incident) or bool(self.uncompleted_action)
