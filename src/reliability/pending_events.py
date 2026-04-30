"""
pending_events — 待处理事件队列(轻量文件版)

Agent 在处理一个 Incident 时,如果发现新的异常,把它放进队列里。
当前 Incident 处理完之后,从队列里取下一个继续处理。

设计要点:
- 文件格式:JSONL,append-only,人类可读
- 单消费者假设(Agent 进程是唯一消费者),不需要复杂锁
- 已消费的条目用"删除整行 → 重写文件"实现(队列通常很小,O(N) 没问题)
- 大小硬上限:超过 1000 条直接拒绝新增,防止失控
"""

from __future__ import annotations

import os
import json
import time
import logging
from dataclasses import dataclass, asdict, field

logger = logging.getLogger("ops-agent.pending_events")

MAX_PENDING_EVENTS = 1000


@dataclass
class PendingEvent:
    id: str                        # 唯一 id,通常是时间戳
    target_name: str
    summary: str
    detected_at: float
    severity: int = 5
    raw: str = ""                  # 原始观察文本(截断到 4KB)
    metadata: dict = field(default_factory=dict)


class PendingEventQueue:
    """文件队列。

    用法:
        q = PendingEventQueue("notebook/pending.jsonl")
        q.push(PendingEvent(id="1", target_name="web", summary="..."))
        ev = q.pop()
        if ev: handle(ev)
    """

    def __init__(self, path: str):
        self.path = path
        self._ensure_file()

    def _ensure_file(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            if not os.path.exists(self.path):
                with open(self.path, "w") as f:
                    pass
        except OSError as e:
            logger.warning(f"pending queue init failed: {e}")

    # ──────────── 公共 API ────────────

    def push(self, event: PendingEvent) -> bool:
        """加入队列。重复 id 自动忽略;超过上限直接拒绝。"""
        items = self._read_all()
        if len(items) >= MAX_PENDING_EVENTS:
            logger.warning(f"pending queue full ({MAX_PENDING_EVENTS}), dropping event")
            return False
        if any(it.get("id") == event.id for it in items):
            return False  # 去重
        d = asdict(event)
        # 截断 raw
        if d.get("raw"):
            d["raw"] = d["raw"][:4096]
        try:
            with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
            return True
        except OSError as e:
            logger.warning(f"pending push failed: {e}")
            return False

    def pop(self) -> PendingEvent | None:
        """取出最早的事件并从文件中删除。空队列返回 None。"""
        items = self._read_all()
        if not items:
            return None
        first = items[0]
        rest = items[1:]
        try:
            self._write_all(rest)
        except OSError as e:
            logger.warning(f"pending pop write failed: {e}")
            return None
        try:
            return PendingEvent(**{
                k: v for k, v in first.items()
                if k in PendingEvent.__dataclass_fields__
            })
        except TypeError as e:
            logger.warning(f"pending pop bad event: {e}")
            return None

    def peek_all(self) -> list[PendingEvent]:
        """只读地查看队列内容。"""
        items = self._read_all()
        out = []
        for d in items:
            try:
                out.append(PendingEvent(**{
                    k: v for k, v in d.items()
                    if k in PendingEvent.__dataclass_fields__
                }))
            except TypeError:
                continue
        return out

    def size(self) -> int:
        return len(self._read_all())

    def clear(self):
        try:
            self._write_all([])
        except OSError:
            pass

    # ──────────── 内部 ────────────

    def _read_all(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        items = []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            logger.warning(f"pending read failed: {e}")
        return items

    def _write_all(self, items: list[dict]):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", errors="replace") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
