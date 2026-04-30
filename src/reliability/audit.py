"""
audit — append-only 审计日志(JSONL)

每条记录包含 timestamp / type / 任意上下文字段。
按日期滚动到 audit/YYYY-MM-DD.jsonl。

设计要点:
- 文件 append-only,绝不删除/重写
- 写入失败不抛异常(只记 logger.warning,不影响主流程)
- 可选:批量缓冲(默认关闭,Sprint 6 简单实现先不加)
- 按日期自动分文件,目录由调用方传入
"""

from __future__ import annotations

import os
import json
import time
import logging
from datetime import datetime, timezone

logger = logging.getLogger("ops-agent.audit")


class AuditLog:
    """append-only 审计日志。

    用法:
        log = AuditLog("notebook/audit")
        log.record("action_executed", target="web", action="restart")
    """

    def __init__(self, dir_path: str):
        self.dir_path = dir_path
        try:
            os.makedirs(self.dir_path, exist_ok=True)
        except OSError as e:
            logger.warning(f"audit dir create failed: {e}")

    def record(self, event_type: str, **kwargs) -> bool:
        """写一条事件。失败静默,返回是否成功。"""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
        }
        # 过滤掉非 JSON-safe 的值
        for k, v in kwargs.items():
            try:
                json.dumps(v, ensure_ascii=False)
                entry[k] = v
            except (TypeError, ValueError):
                entry[k] = str(v)

        path = self._today_file()
        try:
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except OSError as e:
            logger.warning(f"audit write failed: {e}")
            return False

    def read_day(self, date_str: str = "") -> list[dict]:
        """读取某天的所有事件。date_str 为空时读今天。"""
        date_str = date_str or self._today_str()
        path = os.path.join(self.dir_path, f"{date_str}.jsonl")
        if not os.path.exists(path):
            return []
        out = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            logger.warning(f"audit read failed: {e}")
        return out

    def list_dates(self) -> list[str]:
        """列出已有日期文件(按字典序)"""
        try:
            files = os.listdir(self.dir_path)
        except OSError:
            return []
        dates = []
        for fn in files:
            if fn.endswith(".jsonl"):
                dates.append(fn[:-len(".jsonl")])
        return sorted(dates)

    def count_by_type(self, date_str: str = "") -> dict[str, int]:
        """按事件类型统计某天的事件数"""
        events = self.read_day(date_str)
        counts: dict[str, int] = {}
        for e in events:
            t = e.get("type", "unknown")
            counts[t] = counts.get(t, 0) + 1
        return counts

    def _today_file(self) -> str:
        return os.path.join(self.dir_path, f"{self._today_str()}.jsonl")

    @staticmethod
    def _today_str() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
