"""
TrustEngine — 信任度引擎
读 permissions.md + Agent 历史记录，用 LLM 判断动作是否允许执行。
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("ops-agent.trust")

# 信任决策
ALLOW = "allow"                  # 直接执行
NOTIFY_THEN_DO = "notify_then_do"  # 通知人类后执行
ASK = "ask"                      # 必须人类批准
DENY = "deny"                    # 硬拒绝


@dataclass
class VerifyResult:
    """验证结果 — 结构化表示 verify prompt 的输出

    三种结果：
    - SUCCESS: 验证通过
    - FAILED: 验证失败
    - UNCERTAIN: 无法确定，需要继续观察
    """
    result: str = "UNCERTAIN"        # SUCCESS | FAILED | UNCERTAIN
    evidence: str = ""               # 判断依据
    continue_watch: bool = False     # 是否需要继续观察
    watch_duration: int = 0          # 建议观察秒数
    watch_interval: int = 60         # 建议采样间隔秒数
    rollback_needed: bool = False    # 是否需要回滚
    rollback_reason: str = ""        # 回滚理由

    @property
    def passed(self) -> bool:
        return self.result == "SUCCESS"

    @property
    def failed(self) -> bool:
        return self.result == "FAILED"

    @property
    def needs_watch(self) -> bool:
        return self.continue_watch


@dataclass
class ActionPlan:
    """一个待执行的动作方案（结构化版本）

    steps / rollback_steps / verify_steps 均为 list[dict]，
    每个 dict 至少包含 "command" 键。

    steps:          [{"command": "...", "purpose": "...", "wait_seconds": 0}]
    rollback_steps: [{"command": "...", "purpose": "..."}]
    verify_steps:   [{"command": "...", "expect": "...",
                       "delay_seconds": 0,          # 执行前等待时间
                       "watch": false,              # 是否需要连续观察
                       "watch_duration": 0,         # 连续观察总时长（秒）
                       "watch_interval": 60,        # 观察采样间隔（秒）
                       "watch_converge": 2}]        # 连续通过次数算收敛
    """
    steps: list = field(default_factory=list)
    rollback_steps: list = field(default_factory=list)
    verify_steps: list = field(default_factory=list)
    expected: str = ""
    trust_level: int = 2
    reason: str = ""
    next_action: str = "READY"  # READY | COLLECT_MORE | ESCALATE
    gaps: list = field(default_factory=list)  # [{"description": "...", "command": "..."}]

    @property
    def action(self) -> str:
        """所有修复命令拼接（供 trust check / classify / trace 使用）"""
        return "\n".join(s.get("command", "") for s in self.steps if s.get("command"))

    @property
    def verification(self) -> str:
        """所有验证命令拼接"""
        return "\n".join(s.get("command", "") for s in self.verify_steps if s.get("command"))

    @property
    def rollback(self) -> str:
        """所有回滚命令拼接"""
        return "\n".join(s.get("command", "") for s in self.rollback_steps if s.get("command"))

    @property
    def has_watch_steps(self) -> bool:
        """是否有需要连续观察的验证步骤"""
        return any(s.get("watch") for s in self.verify_steps)

    @property
    def max_watch_duration(self) -> int:
        """所有 watch 步骤中最长的观察时长"""
        durations = [s.get("watch_duration", 0) for s in self.verify_steps if s.get("watch")]
        return max(durations) if durations else 0

    def to_markdown(self) -> str:
        lines = [f"**理由**: {self.reason}"]
        lines.append("**修复步骤**:")
        for i, s in enumerate(self.steps, 1):
            wait = s.get("wait_seconds", 0)
            wait_hint = f" (等待 {wait}s)" if wait else ""
            lines.append(f"  {i}. `{s.get('command', '')}` — {s.get('purpose', '')}{wait_hint}")
        lines.append(f"**预期结果**: {self.expected}")
        if self.rollback_steps:
            lines.append("**回滚方案**:")
            for s in self.rollback_steps:
                lines.append(f"  - `{s.get('command', '')}`")
        if self.verify_steps:
            lines.append("**验证方法**:")
            for s in self.verify_steps:
                delay = s.get("delay_seconds", 0)
                delay_hint = f" (等待{delay}s后)" if delay else ""
                watch = s.get("watch", False)
                watch_hint = ""
                if watch:
                    wd = s.get("watch_duration", 0)
                    wi = s.get("watch_interval", 60)
                    wc = s.get("watch_converge", 2)
                    watch_hint = f" [连续观察: {wd}s, 每{wi}s采样, 连续{wc}次通过]"
                lines.append(f"  - `{s.get('command', '')}`{delay_hint}{watch_hint} (期望: {s.get('expect', '成功')})")
        lines.append(f"**信任等级**: L{self.trust_level}")
        return "\n".join(lines)


class TrustEngine:
    """信任度引擎"""

    def __init__(self, notebook, llm):
        self.notebook = notebook
        self.llm = llm

    def check(self, action_plan: ActionPlan) -> str:
        """判断一个动作该怎么处理 → allow / notify_then_do / ask / deny"""

        permissions = self.notebook.read("config/permissions.md")
        readme = self.notebook.read("README.md")

        if not permissions.strip():
            # 没有 permissions 文件时的默认策略
            return self._default_check(action_plan)

        prompt = f"""根据授权规则和 Agent 当前状态，判断这个动作应该怎么处理。

## 授权规则
{permissions}

## Agent 当前状态
{readme}

## 要执行的动作
{action_plan.to_markdown()}

## 请回答
只输出以下四个选项之一，然后换行给出一句话理由：
- allow — 直接执行
- notify_then_do — 通知人类后直接执行
- ask — 必须等人类批准
- deny — 拒绝执行"""

        system = (
            "你是运维 Agent 的权限审核模块。你的职责是根据授权规则和 Agent 的历史表现，"
            "判断一个运维操作是否允许执行。你必须严格遵守 permissions.md 中的规则，"
            "不得擅自放宽或收紧。当规则未明确覆盖某个操作时，默认选择 ask（请求人类批准）。"
        )

        try:
            response = self.llm.ask(prompt, system=system, max_tokens=200)
            decision = self._parse_decision(response)
            logger.info(f"Trust decision for '{action_plan.action[:80]}': {decision}")
            return decision
        except Exception as e:
            logger.error(f"Trust check failed: {e}, defaulting to 'ask'")
            return ASK

    def _parse_decision(self, response: str) -> str:
        """从 LLM 回答中解析决策"""
        first_line = response.strip().split("\n")[0].lower().strip()
        for decision in [ALLOW, NOTIFY_THEN_DO, ASK, DENY]:
            if decision in first_line:
                return decision
        # 解析失败默认 ask
        return ASK

    def _default_check(self, action_plan: ActionPlan) -> str:
        """没有 permissions.md 时的默认策略"""
        if action_plan.trust_level <= 0:
            return ALLOW
        elif action_plan.trust_level == 1:
            return ALLOW
        elif action_plan.trust_level == 2:
            return NOTIFY_THEN_DO
        elif action_plan.trust_level == 3:
            return ASK
        else:
            return DENY
