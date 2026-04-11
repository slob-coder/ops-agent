#!/usr/bin/env python3
"""
OpsAgent — 数字运维员工
一个实时在岗、会成长、在人类监督下工作的运维 Agent。

用法:
  # 本地模式（监控本机）
  python main.py --notebook ./notebook

  # SSH 远程模式
  python main.py --notebook ./notebook --target user@192.168.1.100

  # 只读模式（不执行任何修改）
  python main.py --notebook ./notebook --readonly
"""

import os
import re
import sys
import time
import signal
import logging
import argparse
from pathlib import Path
from datetime import datetime

from llm import LLMClient, LLMInterrupted, LLMDegraded
from notebook import Notebook
from tools import ToolBox, TargetConfig, CommandInterrupted
from trust import TrustEngine, ActionPlan, ALLOW, NOTIFY_THEN_DO, ASK, DENY
from chat import HumanChannel

# ─── 日志配置 ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ops-agent")


class OpsAgent:
    """数字运维员工"""

    # 工作模式
    PATROL = "patrol"
    INVESTIGATE = "investigate"
    INCIDENT = "incident"

    # 巡检间隔（秒）
    INTERVALS = {
        "patrol": 60,
        "investigate": 5,
        "incident": 2,
    }

    def __init__(self, notebook_path: str, targets: list = None,
                 readonly: bool = False, fallback_target=None):
        """
        参数:
            notebook_path: Notebook 目录
            targets: list[TargetConfig],多个目标。如果为空,使用 fallback_target
            readonly: 只读模式
            fallback_target: 当 targets 为空时的兜底目标(用于命令行 --target 启动)
        """
        self.notebook = Notebook(notebook_path)
        self.llm = LLMClient()
        self.trust = TrustEngine(self.notebook, self.llm)
        self.chat = HumanChannel(self.notebook)

        # ── 多目标管理 ──
        if not targets:
            targets = [fallback_target or TargetConfig.local()]
        self.targets: list[TargetConfig] = targets
        self.toolboxes: dict[str, ToolBox] = {
            t.name: ToolBox(t) for t in targets
        }
        # 当前正在巡检的目标索引(round-robin)
        self._target_index = 0
        # 当前激活的目标(_loop_once 期间使用)
        self.current_target: TargetConfig = targets[0]
        self.tools: ToolBox = self.toolboxes[targets[0].name]

        # ── 限制引擎 ──
        from limits import LimitsEngine, LimitsConfig
        limits_path = str(Path(notebook_path) / "config" / "limits.yaml")
        self.limits = LimitsEngine(LimitsConfig.from_yaml(limits_path))

        # ── 紧急停止 ──
        from safety import EmergencyStop
        self.emergency = EmergencyStop(notebook_path)

        # ── 状态 ──
        self.mode = self.PATROL
        self.readonly = readonly
        self.paused = False
        self.current_incident = None
        self.current_issue = ""
        self._running = True
        self._prompts = {}

        # ── 异常指纹静默（修复：避免异常持续存在时反复开 incident）──
        # 结构: fingerprint -> last_fired_timestamp
        self._issue_fingerprints: dict = {}
        # 默认静默窗口 30 分钟,可被 limits.yaml 的 silence_window_seconds 覆盖
        self._silence_window_seconds = getattr(
            self.limits.config, "silence_window_seconds", 1800
        )
        # 标记 _loop_once 本轮是否已经睡过,避免 run() 里再睡一次
        self._already_slept_this_loop = False

        # ── Sprint 3: 补丁生成与本地验证 ──
        self._last_locate_result = None  # Sprint 2 在 _diagnose 中填充
        self._last_error_text = ""       # Sprint 4: 复发检测的 baseline 文本
        try:
            from patch_generator import PatchGenerator
            from patch_applier import PatchApplier
            from patch_loop import PatchLoop
            self.patch_loop = PatchLoop(
                generator=PatchGenerator(self.llm),
                applier=PatchApplier(),
                logger_fn=lambda msg: self.chat.log(msg) if self.chat else logger.info(msg),
            )
        except Exception as e:
            logger.warning(f"PatchLoop init failed (Sprint 3 disabled): {e}")
            self.patch_loop = None

        # ── Sprint 4: PR 工作流 + 生产观察 ──
        try:
            from deploy_watcher import DeployWatcher
            from production_watcher import ProductionWatcher
            self.deploy_watcher = DeployWatcher()
            self.prod_watcher = ProductionWatcher()
        except Exception as e:
            logger.warning(f"Sprint 4 watchers init failed: {e}")
            self.deploy_watcher = None
            self.prod_watcher = None

        # ── Sprint 5: 可靠性 ──
        self.start_time = time.time()
        self.last_loop_time = 0.0
        self.state_path = str(Path(notebook_path) / "state.json")
        try:
            from pending_events import PendingEventQueue
            self.pending_queue = PendingEventQueue(
                str(Path(notebook_path) / "pending-events.jsonl")
            )
        except Exception as e:
            logger.warning(f"pending queue init failed: {e}")
            self.pending_queue = None
        self.health_server = None
        self.llm_degraded = False

        # ── Sprint 6: 可观测性 ──
        try:
            from audit import AuditLog
            self.audit = AuditLog(str(Path(notebook_path) / "audit"))
        except Exception as e:
            logger.warning(f"audit init failed: {e}")
            self.audit = None
        try:
            from notifier import NotifierConfig, make_notifier, PolicyNotifier
            ncfg = NotifierConfig.from_yaml(
                str(Path(notebook_path) / "config" / "notifier.yaml")
            )
            self.notifier = PolicyNotifier(make_notifier(ncfg), ncfg)
        except Exception as e:
            logger.warning(f"notifier init failed: {e}")
            self.notifier = None
        try:
            from reporter import DailyReporter
            self.reporter = DailyReporter(
                audit=self.audit, llm=self.llm,
                notifier=self.notifier, limits=self.limits,
            ) if self.audit else None
        except Exception as e:
            logger.warning(f"reporter init failed: {e}")
            self.reporter = None
        # 计数器(用于 /metrics)
        self._counter_actions: dict = {}
        self._counter_incidents: dict = {}

    def _switch_target(self, name: str) -> bool:
        """切换当前激活的目标"""
        if name not in self.toolboxes:
            return False
        self.current_target = next(t for t in self.targets if t.name == name)
        self.tools = self.toolboxes[name]
        return True

    def _next_target(self):
        """轮询切换到下一个目标(round-robin)"""
        self._target_index = (self._target_index + 1) % len(self.targets)
        target = self.targets[self._target_index]
        self.current_target = target
        self.tools = self.toolboxes[target.name]

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

    # ═══════════════════════════════════════════
    #  入职
    # ═══════════════════════════════════════════

    def onboard(self):
        """首次运行：探索环境、生成 system-map"""
        if self.notebook.exists("system-map.md"):
            self.chat.say("我已经入职过了，读取现有笔记本继续工作。")
            return

        self.chat.say("首次运行，开始入职探索...", "info")
        results = self.tools.explore()

        # 把探索结果整理成文本
        explore_text = ""
        for name, result in results.items():
            explore_text += f"\n### {name}\n```\n{result.output[:1000]}\n```\n"

        # 让 LLM 生成 system-map
        self.chat.say("正在分析系统环境...", "info")
        prompt = f"""你是一名运维工程师，刚刚登录到一台新服务器并执行了一系列探索命令。
请根据以下输出，写一份系统拓扑说明（system-map.md）。

要求：
1. 总结这台机器的基本信息（OS、硬件资源）
2. 列出正在运行的关键服务及其关系
3. 标注各服务的日志文件位置
4. 标注监听的端口
5. 记录发现的异常或需要关注的点

## 探索结果
{explore_text}

请直接输出 markdown 内容，不要加额外说明。"""

        system_map = self._ask_llm(prompt)
        self.notebook.write("system-map.md", system_map)

        # 让 LLM 生成 watchlist
        prompt2 = f"""基于这份系统拓扑，帮我配置巡检观察源（watchlist.md）。

## 系统拓扑
{system_map}

为每个关键服务配置：
1. 日志文件的 tail 命令
2. 健康检查命令（如 curl health endpoint）
3. 建议的巡检间隔

保持现有 watchlist.md 的格式，只更新"自定义观察源"部分。

当前 watchlist.md 内容：
{self.notebook.read('config/watchlist.md')}

请直接输出完整的 watchlist.md 内容。"""

        watchlist = self._ask_llm(prompt2)
        self.notebook.write("config/watchlist.md", watchlist)

        # 写 README
        now = datetime.now().strftime("%Y-%m-%d")
        readme = f"""# 我是谁
我是这台服务器的运维员工，入职日期 {now}。

# 当前状态
- 模式：巡检（patrol）
- 成长层级：L1 新人
- 已处理 Incident：0 个
- 自主成功：0 个

# 工作准则
- 发现问题先诊断再行动，不确定就问人类
- L2 操作（重启/改配置）按 permissions.md 规则执行
- L3 操作（改代码）必须先请示
- 永远记录，永远复盘
"""
        self.notebook.write("README.md", readme)
        self.notebook.commit("Onboarding complete")

        self.chat.say("入职完成！已生成 system-map.md 和 watchlist.md，开始巡检。", "success")

    # ═══════════════════════════════════════════
    #  主循环
    # ═══════════════════════════════════════════

    def run(self):
        """永不停歇的主循环"""
        self.chat.banner("OpsAgent")
        # Sprint 5: 崩溃恢复
        try:
            recovered = self.recover_state()
            if recovered:
                self.chat.say(
                    f"⚠️ 检测到上次未完成的工作 (incident={self.current_incident})"
                    f",已恢复状态",
                    "warning",
                )
        except Exception as e:
            logger.warning(f"recover_state crashed: {e}")
        # Sprint 5: 启动健康检查端点
        try:
            self.start_health_server()
        except Exception as e:
            logger.warning(f"start_health_server crashed: {e}")

        self.onboard()
        self.chat.say("已上岗，进入巡检模式。", "success")

        while self._running:
            try:
                self._already_slept_this_loop = False
                self._loop_once()
                self.last_loop_time = time.time()
                self.save_state()  # Sprint 5: 每轮 checkpoint
                # 兜底:如果本轮没睡过且当前在巡检,强制 sleep 一个 patrol 间隔。
                # 修复的核心:异常处理路径(escalate/DENY/ASK 否决/readonly 等)
                # 过去直接 return,主循环立刻重进 _loop_once → 反复开 incident。
                if (not self._already_slept_this_loop
                        and self.mode == self.PATROL
                        and not self.paused
                        and self._running):
                    self._interruptible_sleep(self.INTERVALS.get("patrol", 60))
            except KeyboardInterrupt:
                self.chat.say("收到退出信号，下班了。", "info")
                break
            except (LLMInterrupted, CommandInterrupted) as e:
                # 被人类打断 —— 优雅处理：放弃当前任务，立刻处理人类消息
                self.chat.log(f"已中断当前任务（{type(e).__name__}）")
                # 如果当前在调查 Incident，标记为被中断
                if self.current_incident:
                    self.notebook.append_to_incident(
                        self.current_incident,
                        f"\n## 被人类中断 @ {datetime.now().strftime('%H:%M:%S')}\n"
                    )
                # 重置工作模式
                self.mode = self.PATROL
                self.current_issue = ""
                # 处理触发中断的人类消息
                self._drain_human_messages()
            except LLMDegraded as e:
                # Sprint 5: LLM 不可用 → 降级到只读 + 持续告警
                logger.error(f"LLM degraded: {e}")
                if not self.llm_degraded:
                    self.llm_degraded = True
                    self.readonly = True
                    self.chat.escalate(
                        "LLM 调用持续失败,已切换到只读模式",
                        f"原因: {e}\n我会每 5 分钟尝试自动恢复。请检查 API key / 网络。",
                    )
                self._interruptible_sleep(300)
            except Exception as e:
                logger.error(f"主循环异常: {e}", exc_info=True)
                self.chat.say(f"我遇到了内部错误：{e}，继续工作。", "warning")
                self._interruptible_sleep(10)

        self.stop_health_server()
        self.chat.stop()

    def _interruptible_sleep(self, seconds: float):
        """可被人类输入中断的睡眠

        把长睡眠拆成小片段，每个片段都检查中断标志和 inbox。
        这样 Agent 在巡检间隔中也能秒级响应人类指令。
        """
        end = time.time() + seconds
        while time.time() < end and self._running:
            if self.chat.is_interrupted() or self.chat.has_pending():
                return
            time.sleep(0.2)

    def _drain_human_messages(self) -> bool:
        """处理所有积压的人类消息

        返回 True 表示处理了消息，调用方应当跳过本轮自主行为。
        """
        handled = False
        while True:
            msg = self.chat.check_inbox()
            if not msg:
                break
            self._handle_human_message(msg)
            handled = True
        if handled:
            self.chat.clear_interrupt()
        return handled

    def _loop_once(self):
        """主循环的一次迭代"""

        # ── 第一优先级：处理所有积压的人类消息 ──
        if self._drain_human_messages():
            return

        # ── 紧急停止检查 ──
        frozen, reason = self.emergency.check()
        if frozen and not self.readonly:
            self.readonly = True
            self.chat.say(f"🚨 紧急停止已激活: {reason}。已切换到只读模式。", "critical")
        elif not frozen and self.readonly and self.current_incident is None:
            # 文件被删了,自动解冻(仅当没有 incident 在处理时)
            pass

        # ── 暂停态：只响应人类，不主动巡检 ──
        if self.paused:
            self._interruptible_sleep(1)
            return

        # ── 多目标轮询：每轮切换到下一个目标 ──
        if self.mode == self.PATROL and len(self.targets) > 1:
            self._next_target()

        # ── 感知 ──
        self.chat.log(f"巡检中... [target={self.current_target.name}, mode={self.mode}]")
        observations = self._observe()

        # 巡检过程中可能有人插话
        if self._drain_human_messages():
            return

        if not observations:
            self._interruptible_sleep(self.INTERVALS.get(self.mode, 60))
            self._already_slept_this_loop = True
            return

        # ── 判断 ──
        assessment = self._assess(observations)

        # assess 后再次检查
        if self._drain_human_messages():
            return

        if assessment.get("status") == "NORMAL":
            self.chat.log("一切正常")
            self._interruptible_sleep(self.INTERVALS.get(self.mode, 60))
            self._already_slept_this_loop = True
            return

        # ── 发现异常 ──
        severity = assessment.get("severity", 5)
        summary = assessment.get("summary", "发现异常")
        self.current_issue = summary

        # ── 静默窗口检查(修复:同一目标同一症状短时间内不重复开 incident)──
        fp = self._issue_fingerprint(self.current_target.name, summary)
        now_ts = time.time()
        last_fired = self._issue_fingerprints.get(fp)
        if last_fired is not None and (now_ts - last_fired) < self._silence_window_seconds:
            remaining = int(self._silence_window_seconds - (now_ts - last_fired))
            self.chat.log(
                f"已知未解决异常(静默中,还剩 {remaining}s): "
                f"[{self.current_target.name}] {summary}"
            )
            self.current_issue = ""
            return
        # 记录首次出现时间,进入处理流程
        self._issue_fingerprints[fp] = now_ts

        self.chat.notify(f"[{self.current_target.name}] 发现异常（严重度 {severity}/10）：{summary}", "warning")
        self.mode = self.INVESTIGATE

        # 创建 Incident
        self.current_incident = self.notebook.create_incident(
            f"{self.current_target.name}-{summary[:40]}"
        )
        self.limits.record_incident_start()
        self.notebook.append_to_incident(
            self.current_incident,
            f"- 目标: {self.current_target.name} ({self.current_target.mode})\n"
            f"- {datetime.now().strftime('%H:%M')} 发现异常：{summary}\n"
            f"- 严重度：{severity}/10\n"
            f"- 原始评估：{assessment.get('details', '')}\n",
        )

        # ── 诊断 ──
        diagnosis = self._diagnose(assessment, observations)

        # 诊断后允许人类插话
        if self._drain_human_messages():
            return

        if diagnosis.get("escalate") == "YES" or diagnosis.get("confidence", 0) < 50:
            self.notebook.append_to_incident(
                self.current_incident,
                f"\n## 升级给人类\n{diagnosis.get('hypothesis', '无法确定根因')}\n",
            )
            self.chat.escalate(summary, diagnosis.get("hypothesis", ""))
            self.notebook.commit(f"Escalated: {summary}")
            self.mode = self.PATROL
            self.current_issue = ""
            return

        # ── Sprint 3: 本地补丁生成与验证(条件触发) ──
        self._maybe_run_patch_loop(diagnosis)

        # 补丁循环后允许人类插话
        if self._drain_human_messages():
            return

        # ── 制定方案 ──
        self.mode = self.INCIDENT
        action_plan = self._plan(diagnosis)

        if not action_plan:
            self.chat.say("无法制定修复方案，升级给人类。", "critical")
            self.mode = self.PATROL
            return

        self.notebook.append_to_incident(
            self.current_incident,
            f"\n## 行动计划\n{action_plan.to_markdown()}\n",
        )

        # ── 信任检查 ──
        if self.readonly:
            self.chat.say(f"只读模式，不执行操作。方案：\n   {action_plan.action}", "info")
            self.notebook.append_to_incident(self.current_incident, "\n（只读模式，未执行）\n")
            self._close_incident("只读模式，未执行修复。")
            self.mode = self.PATROL
            return

        decision = self.trust.check(action_plan)

        if decision == DENY:
            self.chat.say(f"操作被授权规则拒绝：{action_plan.action}", "warning")
            self.notebook.append_to_incident(self.current_incident, "\n（操作被拒绝）\n")
            self._close_incident("操作被授权规则拒绝。")
            self.mode = self.PATROL
            return

        # ── 限制检查(硬性数值约束)──
        action_type = self._classify_action(action_plan.action)
        target_service = self._extract_service_name(action_plan.action)
        allowed, reason = self.limits.check_action(action_type, target_service)
        if not allowed:
            self.chat.say(f"⛔ 限制引擎拒绝执行: {reason}", "critical")
            self.notebook.append_to_incident(
                self.current_incident,
                f"\n## 限制拒绝\n{reason}\n升级给人类。\n",
            )
            self.chat.escalate(
                "动作被限制引擎拒绝,需要人类决策",
                f"原因: {reason}\n动作: {action_plan.action}",
            )
            self._close_incident(f"被限制引擎拒绝: {reason}")
            self.mode = self.PATROL
            return

        if decision == ASK:
            approved = self.chat.request_approval(action_plan.to_markdown())
            if not approved:
                self.notebook.append_to_incident(self.current_incident, "\n（人类否决）\n")
                self._close_incident("操作被人类否决。")
                self.mode = self.PATROL
                return

        if decision == NOTIFY_THEN_DO:
            self.chat.say(f"即将执行：{action_plan.action}", "action")

        # ── 执行 ──
        before_state = self._quick_observe()
        exec_result = self._execute(action_plan)

        # 记录到限制引擎
        self.limits.record_action(action_type, target_service)

        self.notebook.append_to_incident(
            self.current_incident,
            f"\n## 执行结果\n```\n{exec_result}\n```\n",
        )

        # ── 验证 ──
        self.chat.say("修复完成，开始验证...", "info")
        self._interruptible_sleep(3)  # 等系统稳定
        after_state = self._quick_observe()
        verified = self._verify(action_plan, before_state, after_state)

        if verified:
            self.chat.say("验证通过，问题已修复！", "success")
            self.notebook.append_to_incident(self.current_incident, "\n## 验证通过\n")
            # 修复生效,清除指纹,让下次复发能立刻再次进入处理流程
            self._clear_issue_fingerprint(self.current_target.name, self.current_issue)
        else:
            self.chat.say("验证未通过，尝试回滚...", "warning")
            self.notebook.append_to_incident(self.current_incident, "\n## 验证未通过\n")
            # 触发失败冷却期
            self.limits.record_failure()
            # 简单回滚：通知人类
            self.chat.escalate(
                "修复未达预期效果",
                f"执行了 {action_plan.action}，但验证未通过。请检查。",
            )

        # ── 复盘 ──
        self._reflect()

        # ── 恢复巡检 ──
        self.mode = self.PATROL
        self.current_issue = ""

    # ═══════════════════════════════════════════
    #  各步骤实现
    # ═══════════════════════════════════════════

    def _observe(self) -> str:
        """感知：让 LLM 决定看什么，然后执行"""
        system_map = self.notebook.read("system-map.md")
        watchlist = self.notebook.read("config/watchlist.md")
        recent = self._recent_incidents_summary()

        prompt = self._fill_prompt(
            "observe",
            system_map=system_map,
            watchlist=watchlist,
            mode=self.mode,
            current_issue=self.current_issue,
            recent_incidents=recent,
        )

        response = self._ask_llm(prompt)

        # 提取命令列表
        commands = self._extract_commands(response)
        if not commands:
            return ""

        # 执行命令、收集输出
        outputs = []
        for cmd in commands[:10]:  # 最多执行 10 条
            result = self._run_cmd(cmd, timeout=15)
            outputs.append(str(result))

        return "\n\n".join(outputs)

    def _assess(self, observations: str) -> dict:
        """判断观察结果是否正常"""
        system_map = self.notebook.read("system-map.md")
        recent = self._recent_incidents_summary()

        prompt = self._fill_prompt(
            "assess",
            system_map=system_map,
            observations=observations,
            recent_incidents=recent,
        )

        response = self._ask_llm(prompt)
        return self._parse_assessment(response)

    def _locate_source_from_text(self, text: str):
        """Sprint 2: 从一段日志/观察文本里抽取异常栈并定位到本地源码

        返回 (LocateResult | None, ParsedTrace | None)。
        任何失败都返回 (None, None),不影响诊断流程继续。
        """
        try:
            from stack_parser import StackTraceParser
            from source_locator import SourceLocator
        except Exception as e:
            logger.debug(f"sprint2 modules import failed: {e}")
            return None, None

        if not text or not self.current_target:
            return None, None
        try:
            parsed = StackTraceParser().extract_and_parse(text)
        except Exception as e:
            logger.debug(f"stack parse failed: {e}")
            return None, None
        if not parsed.frames:
            return None, None
        try:
            repos = self.current_target.get_source_repos()
        except Exception:
            repos = []
        try:
            result = SourceLocator(repos).locate(parsed.frames)
        except Exception as e:
            logger.debug(f"source locate failed: {e}")
            return None, parsed
        return result, parsed

    def _diagnose(self, assessment: dict, observations: str) -> dict:
        """深度诊断"""
        system_map = self.notebook.read("system-map.md")
        summary = assessment.get("summary", "")

        # Sprint 2: 异常栈反向定位源码
        source_text = "(无)"
        locate_result, parsed = self._locate_source_from_text(
            (observations or "") + "\n" + (summary or "")
        )
        self._last_locate_result = locate_result  # Sprint 3 picks this up
        self._last_error_text = (observations or "") + "\n" + (summary or "")
        if locate_result and locate_result.locations:
            source_text = locate_result.render()
            top = locate_result.locations[0]
            self.chat.log(
                f"已定位异常源码: {top.repo_name}:{os.path.basename(top.local_file)}"
                f":{top.frame.line}"
            )
        elif parsed and parsed.frames:
            source_text = (
                f"（识别到 {parsed.language} 异常栈共 {len(parsed.frames)} 帧,"
                f"但未能映射到本地源码;请检查 targets.yaml 的 source_repos 配置）"
            )

        # 搜索相关 Playbook
        relevant_files = self.notebook.find_relevant(summary + " " + observations[:500])
        playbook_content = ""
        for f in relevant_files:
            if "playbook" in f:
                playbook_content += f"\n### {f}\n{self.notebook.read(f)[:1500]}\n"

        # 搜索历史 Incident
        incidents_content = ""
        for f in relevant_files:
            if "incidents" in f:
                incidents_content += f"\n### {f}\n{self.notebook.read(f)[:1000]}\n"

        prompt = self._fill_prompt(
            "diagnose",
            assessment=str(assessment),
            observations=observations[:3000],
            relevant_playbooks=playbook_content or "（无匹配的 Playbook）",
            similar_incidents=incidents_content or "（无历史记录）",
            system_map=system_map,
            source_locations=source_text,
        )

        response = self._ask_llm(prompt)
        return self._parse_diagnosis(response)

    def _maybe_run_patch_loop(self, diagnosis: dict) -> None:
        """Sprint 3: 如果诊断为 code_bug 且有源码定位,触发补丁生成 + 本地验证

        失败/跳过都不会中断主流程。所有结果只写入当前 Incident 笔记。
        """
        if not self.patch_loop or self.readonly:
            return
        if diagnosis.get("type") != "code_bug":
            return
        result = self._last_locate_result
        if not result or not result.locations:
            return

        # 选第一个定位的 location 所属的 repo
        repo_name = result.locations[0].repo_name
        repos = []
        try:
            repos = self.current_target.get_source_repos()
        except Exception:
            pass
        repo = next((r for r in repos if r.name == repo_name), None)
        if not repo:
            self.chat.log(f"PatchLoop: 找不到 repo {repo_name},跳过")
            return
        if not getattr(repo, "build_cmd", ""):
            self.chat.log(f"PatchLoop: repo {repo_name} 未配置 build_cmd,跳过")
            return

        self.chat.say("检测到代码 bug,启动本地补丁生成与验证...", "info")
        try:
            verified = self.patch_loop.run(
                diagnosis=diagnosis,
                locations=result.locations,
                repo=repo,
                incident_id=self.current_incident or "incident",
            )
        except Exception as e:
            logger.exception("patch loop crashed")
            self.chat.say(f"补丁循环异常: {e}", "warning")
            return

        if verified:
            note = (
                f"\n## 自动补丁(本地已验证)\n"
                f"- 仓库: {repo.name}\n"
                f"- 分支: {verified.result.branch_name}\n"
                f"- Commit: {verified.result.commit_sha[:12]}\n"
                f"- 阶段: {verified.result.stage}\n"
                f"- 尝试次数: {verified.attempts}/3\n"
                f"- 修改说明: {verified.patch.description[:300]}\n"
                f"- 修改文件: {', '.join(verified.patch.files_changed)}\n"
            )
            try:
                self.notebook.append_to_incident(self.current_incident, note)
            except Exception:
                pass
            self.chat.say(
                f"✓ 补丁本地验证通过 ({verified.result.short_summary()})", "success"
            )
            # Sprint 4: 推送 + PR + 部署观察
            self._run_pr_workflow(verified, repo)
            return
        else:
            try:
                self.notebook.append_to_incident(
                    self.current_incident,
                    "\n## 自动补丁(失败)\n三次尝试都未通过本地验证,继续走常规修复流程。\n",
                )
            except Exception:
                pass
            self.chat.say("✗ 补丁循环三次都未通过,降级走常规修复", "warning")

    def _make_git_host(self, repo):
        """Sprint 4: 工厂方法,根据 repo.git_host 创建 client。

        测试可以 monkey-patch 这个方法注入 NoopGitHost。
        """
        from git_host import make_client
        return make_client(getattr(repo, "git_host", "") or "noop")

    def _make_observe_fn(self, repo):
        """Sprint 4: 给 ProductionWatcher 用的 observe_fn

        默认从 repo.log_path 抓最后 200 行;失败返回空串。
        测试可以 monkey-patch 这个方法。
        """
        log_path = getattr(repo, "log_path", "")
        if not log_path:
            return lambda: ""
        def _obs():
            try:
                result = self._run_cmd(f"tail -n 200 {log_path}", timeout=10)
                return getattr(result, "stdout", "") or str(result)
            except Exception:
                return ""
        return _obs

    def _run_pr_workflow(self, verified, repo) -> None:
        """Sprint 4: VerifiedPatch → push → PR → merge → 部署观察 → (可选 revert)

        所有失败都降级,绝不抛异常。状态全部写入当前 Incident 笔记。
        """
        if not getattr(repo, "git_host", ""):
            self._note("Sprint 4 流程跳过: repo 未配置 git_host")
            return
        if not self.deploy_watcher or not self.prod_watcher:
            self._note("Sprint 4 流程跳过: watchers 未初始化")
            return

        # 1. 限流检查
        ok, reason = self.limits.check_auto_merge()
        if not ok:
            self._note(f"Sprint 4 流程跳过: {reason}")
            self.chat.say(f"⛔ 自动合并被限流拒绝: {reason}", "warning")
            return

        host = self._make_git_host(repo)
        repo_path = repo.path
        branch = verified.result.branch_name
        commit_sha = verified.result.commit_sha

        # 2. push
        self.chat.say(f"推送分支 {branch} 到远端...", "info")
        ok, push_out = host.push_branch(repo_path, branch)
        if not ok:
            self._note(f"push 失败,降级等人类: {push_out[:200]}")
            self.chat.escalate("git push 失败", push_out[:300])
            return

        # 3. 创建 PR
        title = f"fix(agent): {verified.patch.description[:60] or 'auto patch'}"
        body = self._build_pr_body(verified, repo, commit_sha)
        pr_result = host.create_pr(repo_path, branch, repo.base_branch or "main",
                                   title, body)
        if not pr_result.success:
            self._note(f"创建 PR 失败: {pr_result.error[:200]}")
            self.chat.escalate("create_pr 失败", pr_result.error[:300])
            return
        pr = pr_result.pr
        self.chat.say(f"✓ PR 已创建: {pr.url}", "success")
        self._note(f"PR 已创建: #{pr.number} {pr.url}")

        # 4. 再次检查 PR 状态(CI)
        status = host.get_pr_status(repo_path, pr.number)
        if not status.ci_passing:
            self._note(f"PR CI 未通过/进行中,降级等人类: state={status.state}")
            self.chat.say("PR CI 未就绪,不自动合并,等人类决定", "warning")
            return

        # 5. 合并
        ok, merge_out = host.merge_pr(repo_path, pr.number)
        if not ok:
            self._note(f"merge 失败(可能被分支保护): {merge_out[:200]}")
            self.chat.say("merge 被拒绝(可能是分支保护),已留 PR 等人类 review", "warning")
            return
        self.limits.record_auto_merge()
        self.chat.say(f"✓ PR #{pr.number} 已自动合并", "success")
        self._note(f"PR #{pr.number} 已自动合并")

        # 6. 等待部署信号
        signal = getattr(repo, "deploy_signal", {}) or {}
        if signal:
            self.chat.say(f"等待部署信号 ({signal.get('type', '?')})...", "info")
            dstatus = self.deploy_watcher.wait_for_deploy(signal, commit_sha)
            if not dstatus.deployed:
                self._note(f"部署信号超时: {dstatus.error}")
                self.chat.escalate("部署信号超时", dstatus.error)
                return
            self._note(f"部署确认: {dstatus.detail}")

        # 7. 生产观察期
        observe_fn = self._make_observe_fn(repo)
        self.chat.say("进入生产观察期...", "info")
        wresult = self.prod_watcher.watch(
            original_error_text=self._last_error_text or "",
            observe_fn=observe_fn,
            duration=300, interval=30,
        )

        if wresult.success:
            self._note(f"生产观察通过: {wresult.detail}")
            self.chat.say("✓ 生产观察期通过,Incident 关闭", "success")
            try:
                self._close_incident("自动修复成功并通过生产观察")
            except Exception:
                pass
            return

        # 8. 复发或观察失败 → revert
        from production_watcher import WatchOutcome
        if wresult.outcome == WatchOutcome.FAILED_RECURRENCE:
            self.chat.say("⚠ 检测到原异常复发,启动自动 revert", "critical")
            self._run_auto_revert(repo, host, commit_sha, branch,
                                  failure_reason=wresult.detail)
        elif wresult.outcome == WatchOutcome.NO_BASELINE:
            self._note("观察期无 baseline,无法判断复发,降级等人类")
            self.chat.say("无法做复发检测(无 baseline),已合并但需人类确认", "warning")
        else:
            self._note(f"观察期异常: {wresult.detail}")
            self.chat.escalate("生产观察期异常", wresult.detail)

    def _run_auto_revert(self, repo, host, commit_sha: str,
                         original_branch: str, failure_reason: str) -> None:
        """Sprint 4: 自动 revert 已合并的补丁 + 升级人类"""
        try:
            from revert_generator import RevertGenerator
            rg = RevertGenerator(host)
            result = rg.revert_and_merge(
                repo_path=repo.path, commit_sha=commit_sha,
                original_branch=original_branch,
                base_branch=repo.base_branch or "main",
                failure_reason=failure_reason,
            )
        except Exception as e:
            logger.exception("revert crashed")
            self._note(f"revert 异常: {e}")
            self.chat.escalate("revert 异常", str(e))
            return

        if result.success:
            self._note(
                f"已自动 revert: 分支 {result.revert_branch}, "
                f"PR #{result.pr.number if result.pr else '?'}"
            )
            self.limits.record_auto_merge()  # revert 也算一次
            self.chat.escalate(
                "已自动 revert 失败的补丁",
                f"原 commit: {commit_sha[:8]}\n原因: {failure_reason}\n"
                f"revert PR: {result.pr.url if result.pr else 'N/A'}\n"
                "请人工评估根因并决定是否再次尝试修复。",
            )
        else:
            self._note(f"revert 失败 stage={result.stage}: {result.error}")
            self.chat.escalate(
                "⚠️ 自动 revert 也失败了,需要人工立即介入",
                f"原 commit: {commit_sha}\n失败阶段: {result.stage}\n错误: {result.error}",
            )

    def _build_pr_body(self, verified, repo, commit_sha: str) -> str:
        """Sprint 4: 渲染 PR 描述(基于 templates/pr-body.md)"""
        from datetime import datetime
        try:
            tmpl_path = Path(__file__).parent / "templates" / "pr-body.md"
            tmpl = tmpl_path.read_text(encoding="utf-8")
        except Exception:
            tmpl = ("## OpsAgent auto patch\n\n"
                    "{patch_description}\n\nCommit: {commit_sha}\n")
        replacements = {
            "{incident_id}": str(self.current_incident or "?"),
            "{target_name}": getattr(self.current_target, "name", "?"),
            "{severity}": "auto",
            "{root_cause}": "(see incident notebook)",
            "{patch_description}": verified.patch.description or "(no description)",
            "{build_cmd}": getattr(repo, "build_cmd", "") or "n/a",
            "{test_cmd}": getattr(repo, "test_cmd", "") or "n/a",
            "{test_status}": "✅" if getattr(repo, "test_cmd", "") else "⚪ (跳过)",
            "{attempts}": str(verified.attempts),
            "{files_changed}": "\n".join(f"- `{f}`" for f in verified.patch.files_changed),
            "{commit_sha}": commit_sha,
            "{timestamp}": datetime.utcnow().isoformat() + "Z",
        }
        for k, v in replacements.items():
            tmpl = tmpl.replace(k, v)
        return tmpl

    def _note(self, text: str) -> None:
        """便捷:把一行文本追加到当前 incident,失败静默"""
        if not self.current_incident:
            return
        try:
            self.notebook.append_to_incident(self.current_incident, f"- {text}\n")
        except Exception:
            pass

    # ═══════════════════════════════════════════
    #  Sprint 5: 状态持久化 / 崩溃恢复 / 健康检查
    # ═══════════════════════════════════════════

    def _build_state_snapshot(self):
        """构造当前 AgentState"""
        from state import AgentState
        return AgentState(
            mode=self.mode,
            current_target_name=getattr(self.current_target, "name", "") or "",
            current_incident=self.current_incident or "",
            current_issue=self.current_issue or "",
            paused=self.paused,
            readonly=self.readonly,
            last_error_text=self._last_error_text or "",
            auto_merge_timestamps=list(getattr(self.limits, "_auto_merge_times", [])),
        )

    def save_state(self) -> bool:
        """checkpoint 当前状态。失败静默。"""
        try:
            return self._build_state_snapshot().save(self.state_path)
        except Exception as e:
            logger.debug(f"save_state failed: {e}")
            return False

    def recover_state(self) -> bool:
        """启动时尝试恢复上次状态。返回是否成功恢复了未完成的工作。"""
        try:
            from state import AgentState
            prev = AgentState.load(self.state_path)
        except Exception as e:
            logger.debug(f"recover_state load failed: {e}")
            return False
        if not prev:
            return False
        try:
            # 仅恢复"软状态" — 模式 / readonly / paused / 当前 incident 引用
            if prev.current_target_name and prev.current_target_name in self.toolboxes:
                self._switch_target(prev.current_target_name)
            self.mode = prev.mode or self.PATROL
            self.paused = prev.paused
            self.readonly = self.readonly or prev.readonly
            self.current_incident = prev.current_incident or None
            self.current_issue = prev.current_issue or ""
            self._last_error_text = prev.last_error_text or ""
            # 重放自动合并计数(粗略地把时间戳塞回 deque)
            for ts in prev.auto_merge_timestamps:
                try:
                    self.limits._auto_merge_times.append(float(ts))
                except Exception:
                    pass
            return prev.has_active_work()
        except Exception as e:
            logger.warning(f"recover_state apply failed: {e}")
            return False

    def health_snapshot(self) -> dict:
        """供 HealthServer 调用的只读快照"""
        try:
            active_count = 0
            try:
                active_count = len(self.notebook.list_dir("incidents/active"))
            except Exception:
                pass
            return {
                "status": "degraded" if self.llm_degraded else "ok",
                "mode": self.mode,
                "uptime": time.time() - self.start_time,
                "current_target": getattr(self.current_target, "name", ""),
                "current_incident": self.current_incident or "",
                "active_incidents": active_count,
                "paused": self.paused,
                "readonly": self.readonly,
                "last_loop_time": self.last_loop_time,
                "llm_degraded": self.llm_degraded,
                "pending_events": (self.pending_queue.size()
                                    if self.pending_queue else 0),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def start_health_server(self, host: str = "127.0.0.1", port: int = 9876) -> bool:
        """启动健康检查后台线程。失败返回 False。"""
        try:
            from health import HealthServer
            self.health_server = HealthServer(
                snapshot_fn=self.health_snapshot,
                metrics_fn=self.render_prometheus_metrics,
            )
            return self.health_server.start(host=host, port=port)
        except Exception as e:
            logger.warning(f"health server start failed: {e}")
            return False

    def stop_health_server(self):
        if self.health_server:
            try:
                self.health_server.stop()
            except Exception:
                pass
            self.health_server = None

    # ═══════════════════════════════════════════
    #  Sprint 6: 审计 / 通知 / Metrics
    # ═══════════════════════════════════════════

    def _emit_audit(self, event_type: str, **kwargs) -> None:
        if not self.audit:
            return
        try:
            kwargs.setdefault("target", getattr(self.current_target, "name", ""))
            if self.current_incident:
                kwargs.setdefault("incident", self.current_incident)
            self.audit.record(event_type, **kwargs)
        except Exception as e:
            logger.debug(f"audit emit failed: {e}")

        if event_type == "action_executed":
            tgt = kwargs.get("target", "")
            kind = kwargs.get("kind", "unknown")
            key = (tgt, kind)
            self._counter_actions[key] = self._counter_actions.get(key, 0) + 1
        elif event_type in ("incident_opened", "incident_closed"):
            tgt = kwargs.get("target", "")
            status = "opened" if event_type == "incident_opened" else "closed"
            key = (tgt, status)
            self._counter_incidents[key] = self._counter_incidents.get(key, 0) + 1

    def _emit_notify(self, event_type: str, title: str, content: str,
                     urgency: str = "info") -> bool:
        if not self.notifier:
            return False
        try:
            return self.notifier.maybe_notify(event_type, title, content, urgency)
        except Exception as e:
            logger.debug(f"notify failed: {e}")
            return False

    def render_prometheus_metrics(self) -> str:
        try:
            lines = []
            lines.append("# HELP ops_agent_uptime_seconds Agent uptime")
            lines.append("# TYPE ops_agent_uptime_seconds gauge")
            lines.append(f"ops_agent_uptime_seconds {time.time() - self.start_time:.0f}")

            lines.append("# HELP ops_agent_mode Current mode")
            lines.append("# TYPE ops_agent_mode gauge")
            lines.append(f'ops_agent_mode{{mode="{self.mode}"}} 1')

            lines.append("# HELP ops_agent_llm_degraded LLM degraded state")
            lines.append("# TYPE ops_agent_llm_degraded gauge")
            lines.append(f"ops_agent_llm_degraded {1 if self.llm_degraded else 0}")

            lines.append("# HELP ops_agent_actions_total Actions executed")
            lines.append("# TYPE ops_agent_actions_total counter")
            for (tgt, kind), v in self._counter_actions.items():
                lines.append(
                    f'ops_agent_actions_total{{target="{tgt}",kind="{kind}"}} {v}'
                )

            lines.append("# HELP ops_agent_incidents_total Incidents by status")
            lines.append("# TYPE ops_agent_incidents_total counter")
            for (tgt, status), v in self._counter_incidents.items():
                lines.append(
                    f'ops_agent_incidents_total{{target="{tgt}",status="{status}"}} {v}'
                )

            try:
                s = self.limits.status() or {}
                if "tokens_last_hour" in s:
                    lines.append("# HELP ops_agent_llm_tokens_last_hour Tokens used last hour")
                    lines.append("# TYPE ops_agent_llm_tokens_last_hour gauge")
                    lines.append(f"ops_agent_llm_tokens_last_hour {s['tokens_last_hour']}")
                if "active_incidents" in s:
                    lines.append("# HELP ops_agent_active_incidents Concurrent incidents")
                    lines.append("# TYPE ops_agent_active_incidents gauge")
                    lines.append(f"ops_agent_active_incidents {s['active_incidents']}")
            except Exception:
                pass

            if self.pending_queue:
                lines.append("# HELP ops_agent_pending_events Pending events")
                lines.append("# TYPE ops_agent_pending_events gauge")
                lines.append(f"ops_agent_pending_events {self.pending_queue.size()}")

            return "\n".join(lines) + "\n"
        except Exception as e:
            return f"# error rendering metrics: {e}\n"

    def maybe_send_daily_report(self) -> bool:
        if not self.reporter:
            return False
        try:
            if self.reporter.should_send_today():
                ok = self.reporter.send_report_for()
                if ok:
                    self._emit_audit("daily_report_sent")
                return ok
        except Exception as e:
            logger.debug(f"daily report failed: {e}")
        return False

    def _plan(self, diagnosis: dict) -> ActionPlan | None:
        """制定修复方案"""
        permissions = self.notebook.read("config/permissions.md")

        # 找匹配的 Playbook
        hypothesis = diagnosis.get("hypothesis", "")
        relevant_files = self.notebook.find_relevant(hypothesis)
        playbook = ""
        for f in relevant_files:
            if "playbook" in f:
                playbook += self.notebook.read(f) + "\n"

        prompt = self._fill_prompt(
            "plan",
            diagnosis=str(diagnosis),
            matched_playbook=playbook or "（无匹配的 Playbook）",
            permissions=permissions,
        )

        response = self._ask_llm(prompt)
        return self._parse_plan(response)

    def _execute(self, plan: ActionPlan) -> str:
        """执行修复动作"""
        # 从 plan.action 中提取命令
        commands = self._extract_commands(plan.action)
        if not commands:
            # 尝试直接执行
            commands = [plan.action]

        results = []
        for cmd in commands:
            result = self._run_cmd(cmd)
            results.append(str(result))
            if not result.success:
                logger.warning(f"Command failed: {cmd}")
                break

        return "\n".join(results)

    def _verify(self, plan: ActionPlan, before: str, after: str) -> bool:
        """验证修复结果"""
        prompt = self._fill_prompt(
            "verify",
            action_result=plan.action,
            before_state=before[:2000],
            after_state=after[:2000],
            verification_criteria=plan.verification,
        )

        response = self._ask_llm(prompt)
        return "SUCCESS" in response.upper() and "FAILED" not in response.upper()

    def _reflect(self):
        """复盘总结"""
        if not self.current_incident:
            return

        incident_record = self.notebook.read(f"incidents/active/{self.current_incident}")
        playbook_list = self.notebook.read_playbooks_summary()

        prompt = self._fill_prompt(
            "reflect",
            incident_record=incident_record[:4000],
            playbook_list=playbook_list,
        )

        response = self._ask_llm(prompt)

        # 追加复盘到 Incident
        self.notebook.append_to_incident(
            self.current_incident,
            f"\n## 复盘\n{response}\n",
        )

        # 解析 Playbook 更新指令
        self._apply_reflect_updates(response)

        # 关闭 Incident
        self._close_incident(response.split("\n")[0] if response else "已完成")

    def _close_incident(self, summary: str):
        """关闭并归档 Incident"""
        if self.current_incident:
            self.notebook.close_incident(self.current_incident, summary)
            self.current_incident = None
            self.limits.record_incident_end()

    def _issue_fingerprint(self, target_name: str, summary: str) -> str:
        """生成异常指纹用于静默去重。

        同一目标、同一症状归一化后应得到相同的指纹。
        为了对小幅抖动鲁棒(比如错误里带时间戳/行号),我们只取
        summary 中的字母数字字符,截断后与 target_name 拼接。
        """
        import hashlib
        normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", summary or "")[:120]
        raw = f"{target_name}::{normalized}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]

    def _clear_issue_fingerprint(self, target_name: str, summary: str):
        """修复验证通过后清除指纹,允许同类问题下次复发时立刻再次触发。"""
        fp = self._issue_fingerprint(target_name, summary)
        self._issue_fingerprints.pop(fp, None)

    def _classify_action(self, action_text: str) -> str:
        """从动作文本中识别动作类型(给限制引擎用)"""
        text = action_text.lower()
        if "restart" in text or "rollout restart" in text or "重启" in text:
            return "restart"
        if "edit" in text or "sed" in text or "改" in text:
            return "edit"
        if "git apply" in text or "git push" in text or "patch" in text:
            return "code"
        if "kill" in text:
            return "kill"
        return "other"

    def _extract_service_name(self, action_text: str) -> str:
        """从动作文本中提取服务名(给单服务限制用)"""
        # 匹配 systemctl restart <name>
        m = re.search(r"systemctl\s+(?:restart|reload|stop|start)\s+(\S+)", action_text)
        if m:
            return m.group(1).strip("'\"")
        # 匹配 docker restart <name>
        m = re.search(r"docker\s+(?:restart|stop|start|kill)\s+(\S+)", action_text)
        if m:
            return m.group(1).strip("'\"")
        # 匹配 kubectl rollout restart deployment/<name>
        m = re.search(r"kubectl\s+rollout\s+restart\s+\S+/(\S+)", action_text)
        if m:
            return m.group(1).strip("'\"")
        return ""

    # ═══════════════════════════════════════════
    #  人类消息处理
    # ═══════════════════════════════════════════

    def _handle_human_message(self, msg: str):
        """处理人类的消息"""
        lower = msg.lower().strip()

        # ═══ 控制指令 ═══

        if lower in ("quit", "exit", "bye", ":q"):
            self.chat.say("收到，下班了。再见！", "info")
            self._running = False
            return

        if lower in ("help", "?", "h"):
            self._show_help()
            return

        if lower == "status":
            self._report_status()
            return

        if lower == "pause":
            self.paused = True
            self.chat.say("已暂停自主巡检。我会继续响应你的指令。输入 resume 恢复。", "info")
            return

        if lower == "resume":
            self.paused = False
            self.chat.say("已恢复自主巡检。", "success")
            return

        if lower == "stop":
            if self.mode != self.PATROL:
                self.mode = self.PATROL
                self.current_issue = ""
                self.chat.say("已停止当前调查，回到巡检模式。", "info")
            else:
                self.chat.say("我现在就在巡检中。", "info")
            return

        if lower == "readonly on":
            self.readonly = True
            self.chat.say("已切换到只读模式（不会执行任何修改操作）。", "info")
            return

        if lower == "readonly off":
            self.readonly = False
            self.chat.say("已切换到正常模式。", "info")
            return

        if lower in ("clear silence", "unmute", "clear-silence"):
            n = len(self._issue_fingerprints)
            self._issue_fingerprints.clear()
            self.chat.say(f"已清空异常静默表({n} 条),下一轮巡检会重新判断。", "info")
            return

        if lower in ("show silence", "silence"):
            if not self._issue_fingerprints:
                self.chat.say("当前无静默中的异常。", "info")
            else:
                now_ts = time.time()
                lines = [f"静默中的异常({len(self._issue_fingerprints)} 条,窗口={self._silence_window_seconds}s):"]
                for fp, ts in sorted(self._issue_fingerprints.items(), key=lambda x: x[1], reverse=True):
                    remaining = max(0, int(self._silence_window_seconds - (now_ts - ts)))
                    lines.append(f"   {fp}  剩余 {remaining}s")
                self.chat.say("\n".join(lines), "info")
            return

        # ═══ 多目标管理指令 ═══

        if lower in ("targets", "list targets", "lt"):
            lines = ["当前管理的目标:"]
            for t in self.targets:
                marker = " ← 当前" if t.name == self.current_target.name else ""
                lines.append(f"   • {t.name} ({t.mode}, {t.description or '-'}){marker}")
            self.chat.say("\n".join(lines))
            return

        if lower.startswith("switch "):
            name = msg[7:].strip()
            if self._switch_target(name):
                self.chat.say(f"已切换到目标 {name}。", "success")
                # 重置目标轮询索引,让下次 round-robin 从这里开始
                for i, t in enumerate(self.targets):
                    if t.name == name:
                        self._target_index = i
                        break
            else:
                names = ", ".join(t.name for t in self.targets)
                self.chat.say(f"未知目标 '{name}'。可用目标: {names}", "warning")
            return

        # ═══ 限制和安全指令 ═══

        if lower == "limits":
            s = self.limits.status()
            lines = ["当前限制状态:"]
            lines.append(f"   动作配额: 本小时 {s['actions_last_hour']}/{s['max_actions_per_hour']}, 今日 {s['actions_last_day']}")
            lines.append(f"   并发 Incident: {s['active_incidents']}/{s['max_concurrent']}")
            lines.append(f"   Token(本小时): {s['tokens_last_hour']}/{s['tokens_per_hour_budget']}")
            if s['in_cooldown']:
                lines.append(f"   ⚠️ 失败冷却中,还需 {s['cooldown_remaining']} 秒")
            self.chat.say("\n".join(lines))
            return

        if lower == "freeze":
            self.emergency.trigger("人类手动触发")
            self.readonly = True
            self.chat.say("🚨 已紧急冻结。所有 L2+ 操作被禁止。输入 unfreeze 解除。", "critical")
            return

        if lower == "unfreeze":
            self.emergency.clear()
            self.readonly = False
            self.chat.say("已解除紧急冻结,恢复正常操作。", "success")
            return

        # ═══ Notebook 浏览指令 ═══

        if lower in ("list playbook", "list playbooks", "lp"):
            files = self.notebook.list_dir("playbook")
            if files:
                self.chat.say("当前 Playbook：\n" + "\n".join(f"   • {f}" for f in files))
            else:
                self.chat.say("还没有 Playbook。")
            return

        if lower in ("list incidents", "li"):
            active = self.notebook.list_dir("incidents/active")
            archive = self.notebook.list_dir("incidents/archive")
            msg_parts = []
            if active:
                msg_parts.append("活跃 Incident:\n" + "\n".join(f"   • {f}" for f in active))
            else:
                msg_parts.append("无活跃 Incident。")
            if archive:
                recent = archive[-5:]
                msg_parts.append("最近归档（5 条）:\n" + "\n".join(f"   • {f}" for f in recent))
            self.chat.say("\n".join(msg_parts))
            return

        if lower.startswith("show "):
            # show <文件名>：显示一个 Notebook 文件
            target = msg[5:].strip()
            content = self._find_and_read(target)
            if content:
                # 限长，避免刷屏
                preview = content[:2000] + ("\n...(已截断)" if len(content) > 2000 else "")
                self.chat.say(f"{target}:\n{preview}")
            else:
                self.chat.say(f"找不到 {target}", "warning")
            return

        # ═══ 通用对话 / 任务委派（让 LLM 处理） ═══

        self.chat.log("正在思考你的指令...")

        prompt = f"""人类同事给你发了一条消息。判断这是一个问题（要回答）还是一个任务（要执行）。

## 当前状态
- 工作模式: {self.mode}
- 只读模式: {self.readonly}
- 暂停: {self.paused}
- 活跃 Incident: {self.current_incident or '无'}

## 人类的消息
{msg}

请按以下格式回答：

如果是问题，并且你不需要执行命令就能回答：
```text
[直接回答]
```

如果你需要执行命令来回答或完成任务：
```commands
命令1
命令2
```
然后给出你打算做什么的简短说明。

记住：
- 简洁友好，不要长篇大论
- 只输出真正需要执行的命令
- 如果是修改类操作（L2+），先说明你打算做什么，等批准
"""

        response = self._ask_llm(prompt)
        commands = self._extract_commands(response)

        if commands:
            # 有命令要执行
            self.chat.say(f"我打算执行 {len(commands)} 条命令来回答你...")
            cmd_results = []
            for cmd in commands[:8]:
                # 命令执行也允许中断
                if self.chat.is_interrupted():
                    self.chat.say("收到新指令，停止当前任务。", "info")
                    return
                self.chat.log(f"执行: {cmd}")
                result = self._run_cmd(cmd, timeout=20)
                cmd_results.append(str(result))

            followup = f"""刚才的问题是：{msg}

执行了以下命令，结果如下：
{chr(10).join(cmd_results)}

请基于这些结果，简洁地回答人类的问题。直接给出结论，不要重复命令输出。"""
            final = self._ask_llm(followup)
            self.chat.say(final)
        else:
            # 纯文字回复
            # 去掉 ```text ``` 包裹
            text = re.sub(r"```(?:text)?\s*\n?(.*?)\n?```", r"\1", response, flags=re.DOTALL).strip()
            self.chat.say(text or response)

    def _show_help(self):
        """显示帮助"""
        self.chat.say(
            "可用指令：\n"
            "   ─── 控制 ───\n"
            "   status        查看我当前的状态\n"
            "   pause         暂停自主巡检\n"
            "   resume        恢复自主巡检\n"
            "   stop          中止当前调查回到巡检\n"
            "   readonly on/off  切换只读模式\n"
            "   freeze        紧急冻结(禁止所有 L2+ 操作)\n"
            "   unfreeze      解除紧急冻结\n"
            "   silence       查看静默中的异常指纹\n"
            "   clear silence 清空静默表,下一轮重新判断\n"
            "   quit          让我下班\n"
            "   ─── 多目标 ───\n"
            "   targets (lt)          列出所有管理的目标\n"
            "   switch <目标名>        切换当前激活的目标\n"
            "   limits                查看限制配额状态\n"
            "   ─── 查看 ───\n"
            "   list playbook (lp)    列出所有 Playbook\n"
            "   list incidents (li)   列出 Incident\n"
            "   show <文件名>          查看某个 Notebook 文件\n"
            "   ─── 自由对话 ───\n"
            "   直接打字提问或派发任务,我会自己想办法。",
        )

    def _find_and_read(self, name: str) -> str:
        """模糊查找并读取一个 Notebook 文件"""
        # 直接路径
        if self.notebook.exists(name):
            return self.notebook.read(name)
        # 在常见目录中找
        for prefix in ("playbook/", "incidents/active/", "incidents/archive/",
                       "lessons/", "config/", ""):
            for suffix in ("", ".md"):
                full = f"{prefix}{name}{suffix}"
                if self.notebook.exists(full):
                    return self.notebook.read(full)
        # 模糊匹配
        for d in ("playbook", "incidents/active", "incidents/archive", "lessons", "config"):
            for f in self.notebook.list_dir(d):
                if name.lower() in f.lower():
                    return self.notebook.read(f"{d}/{f}")
        return ""

    def _report_status(self):
        """汇报当前状态"""
        active_incidents = self.notebook.list_dir("incidents/active")
        archived = self.notebook.list_dir("incidents/archive")
        playbooks = self.notebook.list_dir("playbook")
        s = self.limits.status()

        target_list = ", ".join(t.name for t in self.targets)

        cooldown_line = ""
        if s['in_cooldown']:
            cooldown_line = f"\n   ⚠️ 失败冷却中,还需 {s['cooldown_remaining']} 秒"
        frozen_line = ""
        if self.emergency.frozen:
            frozen_line = "\n   🚨 紧急冻结已激活"

        self.chat.say(
            f"当前状态:\n"
            f"   模式: {self.mode}\n"
            f"   当前目标: {self.current_target.name} ({self.current_target.mode})\n"
            f"   全部目标: {target_list}\n"
            f"   暂停: {'是' if self.paused else '否'}\n"
            f"   只读: {'是' if self.readonly else '否'}\n"
            f"   活跃 Incident: {len(active_incidents)} 个\n"
            f"   历史 Incident: {len(archived)} 个\n"
            f"   Playbook: {len(playbooks)} 个\n"
            f"   动作配额(本小时): {s['actions_last_hour']}/{s['max_actions_per_hour']}\n"
            f"   当前问题: {self.current_issue or '无'}"
            f"{cooldown_line}{frozen_line}",
            "info",
        )

    # ═══════════════════════════════════════════
    #  辅助方法
    # ═══════════════════════════════════════════

    def _quick_observe(self) -> str:
        """快速观察当前状态（用于修复前后对比）"""
        commands = [
            "systemctl --failed --no-pager",
            "free -h",
            "df -h",
        ]
        outputs = []
        for cmd in commands:
            result = self._run_cmd(cmd, timeout=10)
            outputs.append(str(result))
        return "\n".join(outputs)

    def _recent_incidents_summary(self) -> str:
        """最近 Incident 摘要"""
        files = self.notebook.list_dir("incidents/archive")[-5:]  # 最近 5 个
        if not files:
            return "（暂无历史 Incident）"
        summaries = []
        for f in files:
            content = self.notebook.read(f"incidents/archive/{f}")
            first_line = content.split("\n")[0] if content else f
            summaries.append(f"- {first_line}")
        return "\n".join(summaries)

    def _extract_commands(self, text: str) -> list[str]:
        """从 LLM 输出中提取命令列表"""
        commands = []

        # 匹配 ```commands ... ``` 块
        blocks = re.findall(r"```(?:commands|bash|shell|sh)?\s*\n(.*?)```", text, re.DOTALL)
        for block in blocks:
            for line in block.strip().split("\n"):
                line = line.strip()
                if line and not line.startswith("#"):
                    commands.append(line)

        # 如果没有代码块，尝试匹配 STEP N: 格式
        if not commands:
            for match in re.finditer(r"STEP\s+\d+:\s*`?(.+?)`?\s*$", text, re.MULTILINE):
                commands.append(match.group(1).strip())

        return commands

    def _parse_assessment(self, response: str) -> dict:
        """解析 assess 的输出"""
        result = {"status": "NORMAL", "severity": 0, "summary": "", "details": "", "next_step": ""}
        for line in response.split("\n"):
            line = line.strip()
            if line.startswith("STATUS:"):
                val = line.split(":", 1)[1].strip().upper()
                result["status"] = "ABNORMAL" if "ABNORMAL" in val else "NORMAL"
            elif line.startswith("SEVERITY:"):
                try:
                    result["severity"] = int(re.search(r"\d+", line.split(":", 1)[1]).group())
                except (ValueError, AttributeError):
                    result["severity"] = 5
            elif line.startswith("SUMMARY:"):
                result["summary"] = line.split(":", 1)[1].strip()
            elif line.startswith("DETAILS:"):
                result["details"] = line.split(":", 1)[1].strip()
            elif line.startswith("NEXT_STEP:"):
                result["next_step"] = line.split(":", 1)[1].strip()
        return result

    def _parse_diagnosis(self, response: str) -> dict:
        """解析 diagnose 的输出"""
        result = {
            "facts": "",
            "hypothesis": "",
            "confidence": 50,
            "gaps": "",
            "escalate": "NO",
            "type": "unknown",
        }

        valid_types = {"code_bug", "runtime", "config", "resource", "external", "unknown"}

        sections = re.split(r"###?\s+", response)
        for section in sections:
            lower = section.lower()
            if "现象" in lower or "fact" in lower:
                result["facts"] = section.strip()
            elif "假设" in lower or "hypothesis" in lower:
                result["hypothesis"] = section.strip()
            elif "把握" in lower or "confidence" in lower:
                match = re.search(r"(\d+)\s*%", section)
                if match:
                    result["confidence"] = int(match.group(1))
                result["confidence_text"] = section.strip()
            elif "缺失" in lower or "gap" in lower:
                result["gaps"] = section.strip()
            elif "人类" in lower or "escalate" in lower:
                result["escalate"] = "YES" if "YES" in section.upper() else "NO"
            elif "类型" in lower or section.lstrip().lower().startswith("type"):
                # 抓第一个出现的合法关键词
                for vt in valid_types:
                    if re.search(rf"\b{vt}\b", section):
                        result["type"] = vt
                        break

        return result

    def _parse_plan(self, response: str) -> ActionPlan | None:
        """解析 plan 的输出为 ActionPlan"""
        # 提取步骤
        steps = self._extract_commands(response)
        action = "\n".join(steps) if steps else response[:500]

        # 提取各部分
        rollback = ""
        verification = ""
        trust_level = 2
        expected = ""

        for section in re.split(r"###?\s+", response):
            lower = section.lower()
            if "回滚" in lower or "rollback" in lower:
                rollback = section.strip()
            elif "验证" in lower or "verif" in lower:
                verification = section.strip()
            elif "信任" in lower or "trust" in lower:
                match = re.search(r"L(\d)", section)
                if match:
                    trust_level = int(match.group(1))
            elif "预期" in lower or "expect" in lower:
                expected = section.strip()

        if not action.strip():
            return None

        return ActionPlan(
            action=action,
            reason=response[:200],
            rollback=rollback or "联系人类",
            expected=expected or "系统恢复正常",
            trust_level=trust_level,
            verification=verification or "检查服务状态",
        )

    def _apply_reflect_updates(self, reflect_response: str):
        """从复盘结果中应用 Playbook 更新"""
        # 解析 NEW_PLAYBOOK 指令
        new_pb = re.search(
            r"NEW_PLAYBOOK:\s*(\S+\.md)\s*\nCONTENT:\s*\n(.*?)(?=\n###|\Z)",
            reflect_response,
            re.DOTALL,
        )
        if new_pb:
            filename = new_pb.group(1)
            content = new_pb.group(2).strip()
            self.notebook.write(f"playbook/{filename}", content)
            self.chat.say(f"创建了新 Playbook: {filename}", "success")

        # 解析 UPDATE_PLAYBOOK 指令
        update_pb = re.search(
            r"UPDATE_PLAYBOOK:\s*(\S+\.md)\s*\nAPPEND_CONTENT:\s*\n(.*?)(?=\n###|\Z)",
            reflect_response,
            re.DOTALL,
        )
        if update_pb:
            filename = update_pb.group(1)
            content = update_pb.group(2).strip()
            if self.notebook.exists(f"playbook/{filename}"):
                self.notebook.append(f"playbook/{filename}", f"\n{content}")
                self.chat.say(f"更新了 Playbook: {filename}", "success")


# ═══════════════════════════════════════════
#  启动入口
# ═══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="OpsAgent — 数字运维员工")
    parser.add_argument("--notebook", default="./notebook", help="Notebook 目录路径")
    parser.add_argument("--targets", default="",
                        help="targets.yaml 路径(多目标模式,推荐)")
    parser.add_argument("--target", default="",
                        help="单目标模式(SSH: user@host)。--targets 优先")
    parser.add_argument("--port", type=int, default=22, help="SSH 端口")
    parser.add_argument("--key", default="", help="SSH 密钥路径")
    parser.add_argument("--password", action="store_true",
                        help="使用密码认证(将交互式提示输入,需要 sshpass)")
    parser.add_argument("--readonly", action="store_true", help="只读模式")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── 加载目标 ──
    targets = []
    fallback = None

    # 优先尝试 --targets yaml 文件
    targets_file = args.targets
    if not targets_file:
        # 尝试默认路径 notebook/config/targets.yaml
        default_path = Path(args.notebook) / "config" / "targets.yaml"
        if default_path.exists():
            targets_file = str(default_path)

    if targets_file:
        from targets import load_targets
        loaded = load_targets(targets_file)
        targets = [TargetConfig.from_target(t) for t in loaded]
        if targets:
            print(f"✓ 已从 {targets_file} 加载 {len(targets)} 个目标")

    # 单目标兼容模式
    if not targets and args.target:
        password = ""
        if args.password:
            import getpass
            password = getpass.getpass(f"SSH password for {args.target}: ")
        elif os.getenv("OPS_SSH_PASSWORD"):
            password = os.getenv("OPS_SSH_PASSWORD", "")
        fallback = TargetConfig.ssh(args.target, args.port, args.key, password)

    if not targets and not fallback:
        fallback = TargetConfig.local()
        print("ℹ️  未指定目标,使用本机模式。如需多目标请创建 notebook/config/targets.yaml")

    # 启动 Agent
    agent = OpsAgent(
        notebook_path=args.notebook,
        targets=targets,
        readonly=args.readonly,
        fallback_target=fallback,
    )

    # 优雅退出
    def handler(sig, frame):
        agent._running = False

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    agent.run()


if __name__ == "__main__":
    main()
