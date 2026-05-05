"""
Prompt 管理 & LLM 调用 Mixin
"""

import re
import logging
from pathlib import Path

from src.i18n import get_lang, t as _

logger = logging.getLogger("ops-agent")


class PromptsMixin:
    """Prompt 模板管理、system prompt 构建、统一 LLM 调用入口"""

    # ═══════════════════════════════════════════
    #  Prompt 管理
    # ═══════════════════════════════════════════

    def _load_prompt(self, name: str) -> str:
        """加载 prompt 模板，按语言优先级查找"""
        if name not in self._prompts:
            prompts_root = Path(__file__).parent.parent.parent / "prompts"
            lang = get_lang()

            # 1. 语言目录: prompts/{lang}/{name}.md
            lang_path = prompts_root / lang / f"{name}.md"
            # 2. 旧路径: prompts/{name}.md（兼容 fallback）
            fallback_path = prompts_root / f"{name}.md"
            # 3. 中文兜底: prompts/zh/{name}.md
            zh_path = prompts_root / "zh" / f"{name}.md"

            for p in [lang_path, fallback_path, zh_path]:
                if p.exists():
                    self._prompts[name] = p.read_text(encoding="utf-8")
                    break
            else:
                raise FileNotFoundError(f"Prompt not found: {name}")

        return self._prompts[name]

    def _fill_prompt(self, name: str, **kwargs) -> str:
        """填充 prompt 模板中的变量"""
        template = self._load_prompt(name)
        for key, value in kwargs.items():
            template = template.replace(f"{{{key}}}", str(value))
        # 清理未填充的变量
        template = re.sub(r"\{[a-z_]+\}", _("prompt.none"), template)
        return template

    def _build_system_prompt(self) -> str:
        """构建 system prompt —— Agent 的完整自我认知

        每次 LLM 调用都会带上这个 system prompt,让 LLM 知道:
        - 我是谁
        - 我现在管什么目标(类型、连接方式)
        - 我现在在做什么(工作模式、活跃 Incident)
        - 我有什么工具
        - 我的行为准则和数值约束
        - 系统拓扑
        """
        system_map = self.notebook.read("system-map.md")
        permissions = self.notebook.read("config/permissions.md")

        # ── 当前目标信息 ──
        target_info = self._build_target_context()

        # ── 限制状态 ──
        limits_status = self._build_limits_context()

        return self._fill_prompt(
            "system",
            mode=self.mode,
            readonly=_("prompt.readonly_yes") if self.readonly else _("prompt.readonly_no"),
            active_incident=self.current_incident or _("prompt.no_incident"),
            permissions=permissions or _("prompt.no_permissions"),
            system_map=system_map or _("prompt.no_system_map"),
            target_info=target_info,
            limits_status=limits_status,
            notebook_path=str(self.notebook.path),
        )

    def _build_target_context(self) -> str:
        """生成当前目标的描述,告诉 LLM 用什么命令前缀"""
        t = self.current_target
        lines = [_("prompt.target_managing", name=t.name, mode=t.mode)]
        if t.description:
            lines.append(_("prompt.target_description", description=t.description))

        if t.mode == "ssh":
            lines.append(_("prompt.target_ssh", host=t.host))
            lines.append(_("prompt.target_ssh_hint"))
        elif t.mode == "docker":
            lines.append(_("prompt.target_docker", host_info="(本地)" if not t.docker_host else f"({t.docker_host})"))
            lines.append(_("prompt.target_docker_hint"))
            lines.append(_("prompt.target_docker_ps"))
            lines.append(_("prompt.target_docker_exec"))
            lines.append(_("prompt.target_docker_restart"))
            lines.append(_("prompt.target_docker_inspect"))
            if t.compose_file:
                lines.append(_("prompt.target_docker_compose", compose_file=t.compose_file))
        elif t.mode == "k8s":
            lines.append(_("prompt.target_k8s", context=t.kubectl_context, namespace=t.namespace))
            lines.append(_("prompt.target_k8s_hint"))
            lines.append(_("prompt.target_k8s_get", namespace=t.namespace))
            lines.append(_("prompt.target_k8s_logs", namespace=t.namespace))
            lines.append(_("prompt.target_k8s_describe", namespace=t.namespace))
            lines.append(_("prompt.target_k8s_exec", namespace=t.namespace))
            lines.append(_("prompt.target_k8s_rollout", namespace=t.namespace))
        else:
            lines.append(_("prompt.target_local"))

        # 列出该目标管理的所有目标(让 LLM 知道还可以切换)
        if len(self.targets) > 1:
            others = [t.name for t in self.targets if t.name != self.current_target.name]
            lines.append(_("prompt.target_others", others=", ".join(others)))
            lines.append(_("prompt.target_others_hint"))

        # 源码地图
        if self.current_target.source_repos:
            lines.append(_("prompt.target_source_repo"))
            for repo in self.current_target.source_repos:
                lines.append(
                    _("prompt.target_source_repo_item",
                      name=repo.get('name', '?'),
                      language=repo.get('language', '?'),
                      path=repo.get('path', '?'))
                )

        return "\n".join(lines)

    def _build_limits_context(self) -> str:
        """生成限制状态摘要"""
        s = self.limits.status()
        if not s["enabled"]:
            return _("prompt.limits_disabled")
        lines = [
            _("prompt.limits_actions",
              used_hour=s['actions_last_hour'],
              max_hour=s['max_actions_per_hour'],
              used_day=s['actions_last_day']),
            _("prompt.limits_incidents",
              active=s['active_incidents'],
              max=s['max_concurrent']),
            _("prompt.limits_tokens",
              used=s['tokens_last_hour'],
              budget=s['tokens_per_hour_budget']),
        ]
        if s["in_cooldown"]:
            lines.append(_("prompt.limits_cooldown", remaining=s['cooldown_remaining']))
        return "\n".join(lines)

    def _ask_llm(self, prompt: str, max_tokens: int = 0,
                 allow_interrupt: bool = True,
                 phase: str = "") -> str:
        """统一的 LLM 调用入口 —— 始终携带 system prompt

        这是整个 Agent 调用 LLM 的唯一入口。确保每次调用都：
        1. 带上 system prompt（Agent 的自我认知）
        2. 带上 user prompt（具体任务指令）
        3. 流式生成时自动检查人类中断（可被随时打断）
        4. 如果指定了 phase，自动将 prompt/response 写入 trace 文件
        5. 解析前校验 response，校验失败时记录结构化日志
        """
        import time as _time

        # 构建 system prompt
        system = self._build_system_prompt()

        # trace: 记录 system prompt（方案 A）
        if phase:
            self.chat.trace(
                f"{phase} [SYSTEM]",
                f"```\n{system}\n```",
            )

        # trace: 记录请求
        if phase:
            self.chat.trace(
                f"{phase} [REQUEST]",
                f"```\n{prompt}\n```",
            )

        # 终端提示：让人类知道有一次 LLM 交互
        label = phase or "LLM"
        self.chat.llm_log(label)

        check = self._interrupt_check if allow_interrupt else None

        # 计时
        _start = _time.monotonic()
        response = self.llm.ask(
            prompt, system=system, max_tokens=max_tokens,
            interrupt_check=check,
        )
        _duration_ms = int((_time.monotonic() - _start) * 1000)

        # trace: 记录响应
        if phase:
            self.chat.trace(
                f"{phase} [RESPONSE]",
                f"```\n{response}\n```",
            )

        # 校验 response（解析前）
        if phase and hasattr(self, 'llm_validator') and self.llm_validator:
            vresult = self.llm_validator.validate(
                phase=phase, response=response,
                provider=getattr(self.llm, 'provider', ''),
                model=getattr(self.llm, 'model', ''),
                prompt_chars=len(prompt),
                duration_ms=_duration_ms,
                incident=getattr(self, 'current_incident', '') or '',
            )
            if not vresult.passed:
                self.llm_validator.log_error(
                    result=vresult,
                    provider=getattr(self.llm, 'provider', ''),
                    model=getattr(self.llm, 'model', ''),
                    prompt_chars=len(prompt),
                    response_chars=len(response) if response else 0,
                    duration_ms=_duration_ms,
                    incident=getattr(self, 'current_incident', '') or '',
                    request_snippet=prompt,
                    response_snippet=response or '',
                )

        return response

    def _interrupt_check(self) -> bool:
        """供 LLM 流式调用和 SSH 命令使用的中断检查回调

        返回 True 时调用方应立即停止当前操作。
        触发条件：人类输入了任何指令（inbox 非空 或 interrupted 标志被设置）。
        """
        return self.chat.has_pending() or self.chat.is_interrupted()

    def _run_cmd(self, cmd: str, timeout: int = 30):
        """统一的命令执行入口，自动接入中断检查"""
        result = self.tools.run(
            cmd, timeout=timeout,
            interrupt_check=self._interrupt_check,
        )
        self._emit_audit("cmd_executed", cmd=cmd[:200], exit_code=result.returncode)
        return result
