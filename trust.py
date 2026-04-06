"""
TrustEngine — 信任度引擎
读 permissions.md + Agent 历史记录，用 LLM 判断动作是否允许执行。
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger("ops-agent.trust")

# 信任决策
ALLOW = "allow"                  # 直接执行
NOTIFY_THEN_DO = "notify_then_do"  # 通知人类后执行
ASK = "ask"                      # 必须人类批准
DENY = "deny"                    # 硬拒绝


@dataclass
class ActionPlan:
    """一个待执行的动作方案"""
    action: str          # 要执行的命令或操作描述
    reason: str          # 为什么要这么做
    rollback: str        # 回滚方案
    expected: str        # 预期结果
    trust_level: int     # 信任等级 0-4
    verification: str    # 验证方式

    def to_markdown(self) -> str:
        return (
            f"**动作**: {self.action}\n"
            f"**理由**: {self.reason}\n"
            f"**预期结果**: {self.expected}\n"
            f"**回滚方案**: {self.rollback}\n"
            f"**验证方式**: {self.verification}\n"
            f"**信任等级**: L{self.trust_level}"
        )


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
            logger.info(f"Trust decision for '{action_plan.action}': {decision}")
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
