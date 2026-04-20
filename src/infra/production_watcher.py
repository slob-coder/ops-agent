"""
production_watcher — 部署后观察一段时间,检测异常是否复发

复发检测靠 stack_parser.ParsedTrace.signature():
  - 把原始 incident 的异常文本解析成 signature(语言:类型:文件:行号)
  - 观察期内每隔 interval 秒调用 observe_fn 拿一段新日志
  - 如果新日志里出现相同 signature → FAILED_RECURRENCE

完全不依赖具体执行环境;observe_fn / sleep_fn / now_fn 都可注入。
"""

from __future__ import annotations

import time
import logging
from src.context_limits import get_context_limits
from dataclasses import dataclass
from enum import Enum

from src.repair.stack_parser import StackTraceParser

logger = logging.getLogger("ops-agent.production_watcher")


class WatchOutcome(str, Enum):
    OK = "ok"                          # 观察期满,无复发
    FAILED_RECURRENCE = "recurrence"   # 检测到原异常复发
    OBSERVE_ERROR = "observe_error"    # observe_fn 反复抛错
    NO_BASELINE = "no_baseline"        # 没有可用 signature,无法监听


@dataclass
class WatchResult:
    outcome: WatchOutcome
    elapsed: float = 0.0
    checks: int = 0
    detail: str = ""
    last_observation: str = ""

    @property
    def success(self) -> bool:
        return self.outcome == WatchOutcome.OK


class ProductionWatcher:
    """生产观察期监听。"""

    def __init__(self, sleep_fn=None, now_fn=None):
        self._sleep = sleep_fn or time.sleep
        self._now = now_fn or time.monotonic
        self._parser = StackTraceParser()

    def signature_from_text(self, text: str) -> str:
        """从一段异常文本提取 signature。空文本或解析失败返回空串。"""
        if not text:
            return ""
        parsed = self._parser.extract_and_parse(text)
        if not parsed.frames:
            return ""
        return parsed.signature()

    def watch(self, original_error_text: str, observe_fn,
              duration: int = 300, interval: int = 30) -> WatchResult:
        """观察 duration 秒,每 interval 秒调用 observe_fn 拿一段日志。

        observe_fn() -> str   返回当前应该被检查的日志/观察文本
        """
        baseline_sig = self.signature_from_text(original_error_text)
        if not baseline_sig:
            return WatchResult(
                outcome=WatchOutcome.NO_BASELINE,
                detail="无法从原始异常提取 signature,无法做复发检测",
            )

        start = self._now()
        deadline = start + duration
        checks = 0
        consecutive_observe_errors = 0
        last_obs = ""

        while self._now() < deadline:
            try:
                observation = observe_fn() or ""
                consecutive_observe_errors = 0
            except Exception as e:
                consecutive_observe_errors += 1
                logger.debug(f"observe_fn raised: {e}")
                if consecutive_observe_errors >= 3:
                    return WatchResult(
                        outcome=WatchOutcome.OBSERVE_ERROR,
                        elapsed=self._now() - start,
                        checks=checks,
                        detail=f"观察函数连续 3 次失败: {e}",
                    )
                self._sleep(interval)
                continue

            checks += 1
            last_obs = observation
            cur_sig = self.signature_from_text(observation)
            if cur_sig and cur_sig == baseline_sig:
                return WatchResult(
                    outcome=WatchOutcome.FAILED_RECURRENCE,
                    elapsed=self._now() - start,
                    checks=checks,
                    detail=f"检测到原异常复发: {cur_sig}",
                    last_observation=observation[:get_context_limits().observe_output_chars],
                )

            self._sleep(interval)

        return WatchResult(
            outcome=WatchOutcome.OK,
            elapsed=self._now() - start,
            checks=checks,
            detail=f"观察 {duration}s,{checks} 次检查,无复发",
            last_observation=last_obs[:get_context_limits().observe_output_chars],
        )
