"""
Limits — 爆炸半径限制

定义 Agent 的硬性数值约束:
- 每小时最多执行多少 L2+ 动作
- 同一服务每天最多重启多少次
- 同时处理的 Incident 数量上限
- LLM token 预算
- 修复失败后的冷却期

任何超限都强制升级给人类。这是最后的物理护栏,LLM 再聪明也突破不了。

配置文件: notebook/config/limits.yaml
"""

import os
import time
import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field

logger = logging.getLogger("ops-agent.limits")


@dataclass
class LimitsConfig:
    """限制配置"""
    # 动作频率
    max_actions_per_hour: int = 20
    max_actions_per_day: int = 100

    # 单服务限制
    max_restarts_per_service_per_day: int = 5
    max_restarts_per_service_per_hour: int = 3

    # 并发限制
    max_concurrent_incidents: int = 2

    # 冷却期
    cooldown_after_failure_seconds: int = 600  # 修复失败后冷静 10 分钟

    # 成本限制
    llm_tokens_per_day: int = 1_000_000
    llm_tokens_per_hour: int = 200_000

    # Sprint 4: 自动合并 PR 的硬上限
    max_auto_merges_per_day: int = 5

    # 协作模式连续自主执行轮次上限
    max_collab_auto_rounds: int = 30

    # 诊断上下文: observations 传入 LLM 的最大字符数
    max_observations_chars: int = 8000

    # ── 流程控制上限 ──
    max_total_rounds: int = 40
    max_diagnose_rounds: int = 4
    max_fix_attempts: int = 2
    silence_window_seconds: int = 1800

    # ── 命令数量限制 ──
    max_observe_commands: int = 20
    max_verify_steps: int = 6
    max_quick_observe_commands: int = 4
    max_gap_commands: int = 8
    max_generated_gap_commands: int = 6
    max_chat_commands: int = 8
    max_collab_history_rounds: int = 20
    max_recent_incidents: int = 5
    max_patch_attempts: int = 3
    max_source_locations: int = 5
    max_unresolved_frames: int = 5

    # Plan 阶段 COLLECT_MORE 循环上限
    max_plan_rounds: int = 8

    # ── 验证策略配置 ──
    verify_max_retries: int = 3          # 即时验证最大重试次数
    verify_default_interval: int = 5     # 即时验证重试间隔（秒）
    watch_required_consecutive: int = 2  # 连续观察收敛需要的连续通过次数
    watch_default_interval: int = 60     # 连续观察采样间隔（秒）
    watch_max_duration: int = 900        # 单次连续观察最大时长（秒）

    # 总开关
    enabled: bool = True

    @classmethod
    def from_yaml(cls, path: str) -> "LimitsConfig":
        if not os.path.exists(path):
            logger.info(f"limits.yaml not found at {path}, using defaults")
            return cls()
        try:
            import yaml
        except ImportError:
            return cls()
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = yaml.safe_load(f) or {}
        # 字段名规范化:支持 - 和 _
        normalized = {k.replace("-", "_"): v for k, v in data.items()}
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in normalized.items() if k in valid}
        return cls(**filtered)


class LimitsEngine:
    """限制引擎:跟踪计数器,做限流决策"""

    def __init__(self, config: LimitsConfig):
        self.config = config
        self._lock = threading.Lock()

        # ── 计数器 ──
        # 动作时间戳队列,自动按时间窗口清理
        self._action_times: deque = deque()
        # 每个服务的重启时间戳
        self._restart_times: dict[str, deque] = defaultdict(deque)
        # 当前活跃 Incident 数
        self._active_incidents: int = 0
        # 最后一次失败时间
        self._last_failure_time: float = 0.0
        # token 计数
        self._token_times: deque = deque()  # (timestamp, tokens) tuples
        # Sprint 4: 自动合并次数
        self._auto_merge_times: deque = deque()

    # ── 工具函数 ──

    def _trim_deque(self, dq: deque, max_age: float):
        """删除 deque 头部超过 max_age 秒的旧记录"""
        now = time.time()
        while dq and (now - (dq[0][0] if isinstance(dq[0], tuple) else dq[0])) > max_age:
            dq.popleft()

    def _count_in_window(self, dq: deque, window: float) -> int:
        """统计窗口内的记录数"""
        self._trim_deque(dq, window)
        return len(dq)

    def _sum_tokens_in_window(self, window: float) -> int:
        """统计窗口内的 token 总数"""
        now = time.time()
        total = 0
        for ts, tokens in list(self._token_times):
            if now - ts <= window:
                total += tokens
        return total

    # ── 检查接口 ──

    def check_action(self, action_type: str, target_service: str = "") -> tuple[bool, str]:
        """检查是否允许执行一个动作

        返回 (是否允许, 拒绝理由或空字符串)
        """
        if not self.config.enabled:
            return True, ""

        with self._lock:
            # 1. 全局动作频率
            count_1h = self._count_in_window(self._action_times, 3600)
            if count_1h >= self.config.max_actions_per_hour:
                return False, (
                    f"超过每小时动作上限 ({count_1h}/{self.config.max_actions_per_hour})。"
                    f"为防止失控,Agent 每小时最多执行 {self.config.max_actions_per_hour} 个 L2+ 动作。"
                )

            count_24h = self._count_in_window(self._action_times, 86400)
            if count_24h >= self.config.max_actions_per_day:
                return False, (
                    f"超过每天动作上限 ({count_24h}/{self.config.max_actions_per_day})。"
                )

            # 2. 单服务重启限制
            if action_type == "restart" and target_service:
                svc_dq = self._restart_times[target_service]
                svc_1h = self._count_in_window(svc_dq, 3600)
                if svc_1h >= self.config.max_restarts_per_service_per_hour:
                    return False, (
                        f"服务 {target_service} 每小时已重启 {svc_1h} 次,达到上限。"
                        f"频繁重启往往不能解决问题,需要人类介入。"
                    )
                svc_24h = self._count_in_window(svc_dq, 86400)
                if svc_24h >= self.config.max_restarts_per_service_per_day:
                    return False, (
                        f"服务 {target_service} 今日已重启 {svc_24h} 次,达到上限。"
                    )

            # 3. 并发 Incident 限制
            if self._active_incidents >= self.config.max_concurrent_incidents:
                return False, (
                    f"已有 {self._active_incidents} 个 Incident 在处理,达到并发上限。"
                    f"请等当前 Incident 处理完再发起新的。"
                )

            # 4. 冷却期检查
            if self._last_failure_time > 0:
                elapsed = time.time() - self._last_failure_time
                if elapsed < self.config.cooldown_after_failure_seconds:
                    remaining = int(self.config.cooldown_after_failure_seconds - elapsed)
                    return False, (
                        f"上次修复失败,处于冷却期,还需 {remaining} 秒。"
                        f"冷却期内不再尝试自动修复,避免重复犯错。"
                    )

        return True, ""

    def check_llm_budget(self, estimated_tokens: int = 1000) -> tuple[bool, str]:
        """检查 token 预算"""
        if not self.config.enabled:
            return True, ""
        with self._lock:
            used_1h = self._sum_tokens_in_window(3600)
            if used_1h + estimated_tokens > self.config.llm_tokens_per_hour:
                return False, (
                    f"LLM token 预算告急:本小时已用 {used_1h},"
                    f"上限 {self.config.llm_tokens_per_hour}"
                )
            used_24h = self._sum_tokens_in_window(86400)
            if used_24h + estimated_tokens > self.config.llm_tokens_per_day:
                return False, (
                    f"LLM token 预算告急:今日已用 {used_24h},"
                    f"上限 {self.config.llm_tokens_per_day}"
                )
        return True, ""

    # ── 记录接口 ──

    def record_action(self, action_type: str, target_service: str = ""):
        """记录一次动作的执行"""
        with self._lock:
            now = time.time()
            self._action_times.append(now)
            if action_type == "restart" and target_service:
                self._restart_times[target_service].append(now)
            logger.debug(f"Recorded action: {action_type} {target_service}")

    def record_incident_start(self):
        with self._lock:
            self._active_incidents += 1

    def record_incident_end(self):
        with self._lock:
            self._active_incidents = max(0, self._active_incidents - 1)

    def record_failure(self):
        """记录一次修复失败,触发冷却期"""
        with self._lock:
            self._last_failure_time = time.time()
            logger.warning(f"Failure recorded, entering cooldown")

    def record_tokens(self, tokens: int):
        with self._lock:
            self._token_times.append((time.time(), tokens))

    def check_auto_merge(self) -> tuple[bool, str]:
        """Sprint 4: 检查是否允许再做一次自动 PR 合并"""
        if not self.config.enabled:
            return True, ""
        with self._lock:
            count_24h = self._count_in_window(self._auto_merge_times, 86400)
            limit = self.config.max_auto_merges_per_day
            if count_24h >= limit:
                return False, (
                    f"今日自动合并已达上限 ({count_24h}/{limit})。"
                    f"为防止失控,Agent 每天最多自动合并 {limit} 个 PR。"
                )
        return True, ""

    def record_auto_merge(self):
        """Sprint 4: 记录一次自动合并"""
        with self._lock:
            self._auto_merge_times.append(time.time())

    # ── 状态查询 ──

    def status(self) -> dict:
        """返回当前限制状态(给 status 命令用)"""
        with self._lock:
            return {
                "enabled": self.config.enabled,
                "actions_last_hour": self._count_in_window(self._action_times, 3600),
                "actions_last_day": self._count_in_window(self._action_times, 86400),
                "max_actions_per_hour": self.config.max_actions_per_hour,
                "active_incidents": self._active_incidents,
                "max_concurrent": self.config.max_concurrent_incidents,
                "tokens_last_hour": self._sum_tokens_in_window(3600),
                "tokens_per_hour_budget": self.config.llm_tokens_per_hour,
                "in_cooldown": (
                    self._last_failure_time > 0 and
                    time.time() - self._last_failure_time < self.config.cooldown_after_failure_seconds
                ),
                "cooldown_remaining": max(0, int(
                    self.config.cooldown_after_failure_seconds -
                    (time.time() - self._last_failure_time)
                )) if self._last_failure_time > 0 else 0,
            }
