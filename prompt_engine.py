"""
Prompt 管理 & LLM 调用 Mixin
"""

import re
import logging
from pathlib import Path

logger = logging.getLogger("ops-agent")


class PromptsMixin:
    """Prompt 模板管理、system prompt 构建、统一 LLM 调用入口"""

    # ═══════════════════════════════════════════
    #  Prompt 管理
    # ═══════════════════════════════════════════

    def _load_prompt(self, name: str) -> str:
        """加载 prompt 模板"""
        if name not in self._prompts:
            prompt_path = Path(__file__).parent / "prompts" / f"{name}.md"
            self._prompts[name] = prompt_path.read_text(encoding="utf-8")
        return self._prompts[name]

    def _fill_prompt(self, name: str, **kwargs) -> str:
        """填充 prompt 模板中的变量"""
        template = self._load_prompt(name)
        for key, value in kwargs.items():
            template = template.replace(f"{{{key}}}", str(value))
        # 清理未填充的变量
        template = re.sub(r"\{[a-z_]+\}", "(无)", template)
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
            readonly="是(只读模式,不执行任何修改操作)" if self.readonly else "否",
            active_incident=self.current_incident or "无",
            permissions=permissions or "(未配置,使用默认策略)",
            system_map=system_map or "(尚未探索,系统拓扑未知)",
            target_info=target_info,
            limits_status=limits_status,
        )

    def _build_target_context(self) -> str:
        """生成当前目标的描述,告诉 LLM 用什么命令前缀"""
        t = self.current_target
        lines = [f"当前正在管理的目标: **{t.name}** (类型: {t.mode})"]
        if t.description:
            lines.append(f"描述: {t.description}")

        if t.mode == "ssh":
            lines.append(f"连接方式: SSH 到 {t.host}")
            lines.append("命令直接写 shell,Agent 会自动通过 SSH 在远端执行。")
        elif t.mode == "docker":
            lines.append(f"连接方式: Docker {'(本地)' if not t.docker_host else f'({t.docker_host})'}")
            lines.append("命令运行在工作站本地。要操作容器请用:")
            lines.append("  - `docker ps` / `docker logs <容器名> --tail 100`")
            lines.append("  - `docker exec <容器名> <命令>` 进入容器执行")
            lines.append("  - `docker restart <容器名>` 重启容器")
            lines.append("  - `docker inspect <容器名>` 查看详情")
            if t.compose_file:
                lines.append(f"  - 有 compose 文件: `docker compose -f {t.compose_file} <命令>`")
        elif t.mode == "k8s":
            lines.append(f"连接方式: Kubernetes (context={t.kubectl_context}, ns={t.namespace})")
            lines.append("命令运行在工作站本地。要操作集群请用:")
            lines.append(f"  - `kubectl get pods -n {t.namespace}` / `kubectl get all`")
            lines.append(f"  - `kubectl logs <pod> -n {t.namespace} --tail=100`")
            lines.append(f"  - `kubectl describe pod <pod> -n {t.namespace}`")
            lines.append(f"  - `kubectl exec <pod> -n {t.namespace} -- <命令>`")
            lines.append(f"  - `kubectl rollout restart deployment/<名> -n {t.namespace}` 滚动重启")
        else:
            lines.append("连接方式: 本地工作站")

        # 列出该目标管理的所有目标(让 LLM 知道还可以切换)
        if len(self.targets) > 1:
            others = [t.name for t in self.targets if t.name != self.current_target.name]
            lines.append(f"\n你还管理着其他目标: {', '.join(others)}")
            lines.append("(每轮巡检会自动轮换。如果人类问起其他目标,你需要先用相应的命令前缀)")

        # 源码地图
        if self.current_target.source_repos:
            lines.append("\n这台目标对应的源代码:")
            for repo in self.current_target.source_repos:
                lines.append(
                    f"  - {repo.get('name', '?')}: {repo.get('language', '?')},"
                    f" 路径 {repo.get('path', '?')}"
                )

        return "\n".join(lines)

    def _build_limits_context(self) -> str:
        """生成限制状态摘要"""
        s = self.limits.status()
        if not s["enabled"]:
            return "(限制引擎已禁用)"
        lines = [
            f"动作配额: 本小时已用 {s['actions_last_hour']}/{s['max_actions_per_hour']},"
            f" 今日已用 {s['actions_last_day']}",
            f"并发 Incident: {s['active_incidents']}/{s['max_concurrent']}",
            f"Token 用量(本小时): {s['tokens_last_hour']}/{s['tokens_per_hour_budget']}",
        ]
        if s["in_cooldown"]:
            lines.append(f"⚠️ 处于失败冷却期,还需 {s['cooldown_remaining']} 秒")
        return "\n".join(lines)

    def _ask_llm(self, prompt: str, max_tokens: int = 4096,
                 allow_interrupt: bool = True) -> str:
        """统一的 LLM 调用入口 —— 始终携带 system prompt

        这是整个 Agent 调用 LLM 的唯一入口。确保每次调用都：
        1. 带上 system prompt（Agent 的自我认知）
        2. 带上 user prompt（具体任务指令）
        3. 流式生成时自动检查人类中断（可被随时打断）
        """
        system = self._build_system_prompt()
        check = self._interrupt_check if allow_interrupt else None
        return self.llm.ask(
            prompt, system=system, max_tokens=max_tokens,
            interrupt_check=check,
        )

    def _interrupt_check(self) -> bool:
        """供 LLM 流式调用和 SSH 命令使用的中断检查回调

        返回 True 时调用方应立即停止当前操作。
        触发条件：人类输入了任何指令（inbox 非空 或 interrupted 标志被设置）。
        """
        return self.chat.has_pending() or self.chat.is_interrupted()

    def _run_cmd(self, cmd: str, timeout: int = 30):
        """统一的命令执行入口，自动接入中断检查"""
        return self.tools.run(
            cmd, timeout=timeout,
            interrupt_check=self._interrupt_check,
        )
