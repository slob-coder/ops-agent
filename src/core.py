"""
OpsAgent 核心骨架 — 类定义、初始化、主循环、状态管理
"""

import os
import re
import time
import logging
from pathlib import Path
from datetime import datetime

from src.infra.llm import LLMClient, LLMInterrupted, LLMDegraded
from src.infra.notebook import Notebook
from src.infra.tools import ToolBox, TargetConfig, CommandInterrupted
from src.safety.trust import TrustEngine, ActionPlan, ALLOW, NOTIFY_THEN_DO, ASK, DENY
from src.infra.chat import HumanChannel

from src.agent.prompt_engine import PromptsMixin
from src.agent.pipeline import PipelineMixin
from src.agent.pr_workflow import PRWorkflowMixin
from src.agent.human import HumanInteractionMixin
from src.agent.metrics import MetricsMixin
from src.agent.parsers import ParsersMixin
from src.agent.agents_md import AgentsMdMixin
from src.context_limits import get_context_limits, reload_context_limits

# ─── 日志配置 ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ops-agent")


class OpsAgent(
    PromptsMixin,
    PipelineMixin,
    PRWorkflowMixin,
    HumanInteractionMixin,
    MetricsMixin,
    ParsersMixin,
    AgentsMdMixin,
):
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
        # observations 滚动摘要（跨 COLLECT_MORE 轮次积累的历史摘要）
        self._obs_summary = ""

        # ── 多目标管理 ──
        if not targets:
            targets = [fallback_target or TargetConfig.local()]
        self.targets: list = targets
        self.toolboxes: dict = {
            t.name: ToolBox(t) for t in targets
        }
        # 当前正在巡检的目标索引(round-robin)
        self._target_index = 0
        # 当前激活的目标(_loop_once 期间使用)
        self.current_target = targets[0]
        self.tools = self.toolboxes[targets[0].name]

        # ── 限制引擎 ──
        from src.safety.limits import LimitsEngine, LimitsConfig
        limits_path = str(Path(notebook_path) / "config" / "limits.yaml")
        self.limits = LimitsEngine(LimitsConfig.from_yaml(limits_path))

        # ── 紧急停止 ──
        from src.safety.safety import EmergencyStop
        self.emergency = EmergencyStop(notebook_path)

        # ── 状态 ──
        self.mode = self.PATROL
        self.readonly = readonly
        self.paused = False
        self._free_chat_history: list[dict] = []  # 自由对话多轮上下文
        self.current_incident = None
        self.current_issue = ""
        self._running = True
        self._prompts = {}

        # ── 上下文窗口限制 ──
        self.ctx_limits = get_context_limits(notebook_path)

        # ── 异常指纹静默（修复：避免异常持续存在时反复开 incident）──
        # 结构: fingerprint -> last_fired_timestamp
        self._issue_fingerprints: dict = {}
        # 静默窗口：优先 limits.yaml 配置
        self._silence_window_seconds = self.limits.config.silence_window_seconds
        # 标记 _loop_once 本轮是否已经睡过,避免 run() 里再睡一次
        self._already_slept_this_loop = False

        # ── Sprint 3: 补丁生成与本地验证 ──
        self._last_locate_result = None  # Sprint 2 在 _diagnose 中填充
        self._last_error_text = ""       # Sprint 4: 复发检测的 baseline 文本
        try:
            from src.safety.patch_generator import PatchGenerator
            from src.safety.patch_applier import PatchApplier
            from src.safety.patch_loop import PatchLoop
            self.patch_loop = PatchLoop(
                generator=PatchGenerator(self.llm),
                applier=PatchApplier(),
                logger_fn=lambda msg: self.chat.log(msg) if self.chat else logger.info(msg),
                max_attempts=self.limits.config.max_patch_attempts,
            )
        except Exception as e:
            logger.warning(f"PatchLoop init failed (Sprint 3 disabled): {e}")
            self.patch_loop = None

        # ── Sprint 4: PR 工作流 + 生产观察 ──
        try:
            from src.infra.deploy_watcher import DeployWatcher
            from src.infra.production_watcher import ProductionWatcher
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
            from src.reliability.pending_events import PendingEventQueue
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
            from src.reliability.audit import AuditLog
            self.audit = AuditLog(str(Path(notebook_path) / "audit"))
        except Exception as e:
            logger.warning(f"audit init failed: {e}")
            self.audit = None
        try:
            from src.infra.notifier import NotifierConfig, make_notifier, PolicyNotifier
            ncfg = NotifierConfig.from_yaml(
                str(Path(notebook_path) / "config" / "notifier.yaml")
            )
            self.notifier = PolicyNotifier(make_notifier(ncfg), ncfg)
        except Exception as e:
            logger.warning(f"notifier init failed: {e}")
            self.notifier = None
        try:
            from src.reporter import DailyReporter
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

    # ═══════════════════════════════════════════
    #  目标管理
    # ═══════════════════════════════════════════

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
            explore_text += f"\n### {name}\n```\n{result.output[:self.ctx_limits.explore_output_chars]}\n```\n"

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

        # 为 source_repos 生成 AGENTS.md（项目地图）
        self._check_and_generate_agents_md()

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

        # ── 自修复 probation 自检 ──
        # 如果上次退出是因为自修复合并,新进程启动时要先跑一遍测试确认没改坏
        selfdev_path = os.environ.get("OPS_AGENT_SELFDEV_PATH", "")
        if selfdev_path:
            try:
                from src.repair.self_repair import run_probation_if_pending
                run_probation_if_pending(self, selfdev_path)
            except Exception as e:
                logger.warning(f"probation check crashed: {e}")

        self.onboard()
        self.chat.say("已上岗，进入巡检模式。", "success")

        while self._running:
            try:
                self._already_slept_this_loop = False
                self._loop_once()
                self.last_loop_time = time.time()
                self.save_state()  # Sprint 5: 每轮 checkpoint
                # 兜底:如果本轮没睡过且当前在巡检,强制 sleep 一个 patrol 间隔。
                if (not self._already_slept_this_loop
                        and self.mode == self.PATROL
                        and not self.paused
                        and self._running):
                    self._interruptible_sleep(self.INTERVALS.get("patrol", 60))
            except KeyboardInterrupt:
                self.chat.say("收到退出信号，下班了。", "info")
                break
            except (LLMInterrupted, CommandInterrupted) as e:
                # 被人类打断 —— 优雅处理
                self.chat.log(f"已中断当前任务（{type(e).__name__}）")
                if self.current_incident:
                    self.notebook.append_to_incident(
                        self.current_incident,
                        f"\n## 被人类中断 @ {datetime.now().strftime('%H:%M:%S')}\n"
                    )
                    self._close_incident("被人类中断")
                self.mode = self.PATROL
                self.current_issue = ""
                self._drain_human_messages()
            except LLMDegraded as e:
                # Sprint 5: LLM 不可用 → 降级到只读 + 持续告警
                logger.error(f"LLM degraded: {e}")
                if not self.llm_degraded:
                    self.llm_degraded = True
                    self.readonly = True
                    self.chat.say(
                        f"🚨 LLM 调用持续失败，已切换到只读模式。\n"
                        f"原因: {e}\n我会每 5 分钟尝试自动恢复。请检查 API key / 网络。",
                        "critical",
                    )
                self._interruptible_sleep(300)
            except Exception as e:
                logger.error(f"主循环异常: {e}", exc_info=True)
                self.chat.say(f"我遇到了内部错误：{e}，继续工作。", "warning")
                self._interruptible_sleep(10)

        self.stop_health_server()
        self.chat.stop()

    def _interruptible_sleep(self, seconds: float):
        """可被人类输入中断的睡眠"""
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

        # ── 静默窗口检查 ──
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
        self._issue_fingerprints[fp] = now_ts

        self.chat.notify(f"[{self.current_target.name}] 发现异常（严重度 {severity}/10）：{summary}", "warning")
        self.mode = self.INVESTIGATE

        # 创建 Incident
        self.current_incident = self.notebook.create_incident(
            f"{self.current_target.name}-{summary}"
        )
        # trace 文件绑定到当前 incident
        self.chat._trace_file = self.current_incident
        self.limits.record_incident_start()
        self.notebook.append_to_incident(
            self.current_incident,
            f"- 目标: {self.current_target.name} ({self.current_target.mode})\n"
            f"- {datetime.now().strftime('%H:%M')} 发现异常：{summary}\n"
            f"- 严重度：{severity}/10\n"
            f"- 原始评估：{assessment.get('details', '')}\n",
        )

        # ── 进入状态机 ──
        self.chat.say("🔍 进入调查模式", "observe")
        self._incident_loop(assessment, observations, summary)

        # ── 清理 ──
        self.mode = self.PATROL
        self.current_issue = ""

    # ═══════════════════════════════════════════
    #  状态机驱动的 Incident 处理
    # ═══════════════════════════════════════════

    def _incident_loop(self, assessment: dict, observations: str, summary: str):
        """状态机驱动的 incident 处理循环

        状态: DIAGNOSE → DECIDE → PLAN → EXECUTE → VERIFY → REFLECT
        DECIDE 是路由器，根据 diagnosis.next_action 分流。
        """
        state = "DIAGNOSE"
        diagnosis = None
        plan = None
        exec_result = ""
        before_state = ""
        fix_verified = False
        self._obs_summary = ""  # 每个 incident 重置滚动摘要

        self._emit_audit("incident_opened", target=self.current_target.name if self.current_target else "")

        max_total_rounds = self.limits.config.max_total_rounds
        diagnose_rounds = 0       # 诊断轮次计数
        fix_attempts = 0          # 修复尝试计数
        max_diagnose_rounds = self.limits.config.max_diagnose_rounds
        max_fix_attempts = self.limits.config.max_fix_attempts

        for _ in range(max_total_rounds):
            # 每个状态转换前检查人类输入
            if self._drain_human_messages():
                # 只有明确的中断指令(stop/pause/quit)才退出 incident_loop
                # 普通消息/空行/误触不应中断状态机
                if self.mode == self.PATROL or self.paused or not self._running:
                    return

            # ── DIAGNOSE ──
            if state == "DIAGNOSE":
                diagnose_rounds += 1
                diagnosis = self._diagnose(assessment, observations)

                conf = diagnosis.get("confidence", 0)
                hyp = diagnosis.get("hypothesis", "")
                self.chat.progress(
                    f"诊断 (第{diagnose_rounds}轮): 把握度 {conf}% | {hyp}"
                )
                state = "DECIDE"

            # ── DECIDE（核心路由器）──
            elif state == "DECIDE":
                next_action = diagnosis.get("next_action", "FIX")

                self.notebook.append_to_incident(
                    self.current_incident,
                    f"\n## 诊断 (第{diagnose_rounds}轮)\n"
                    f"- 假设: {diagnosis.get('hypothesis', '')}\n"
                    f"- 把握度: {diagnosis.get('confidence', 0)}%\n"
                    f"- 类型: {diagnosis.get('type', 'unknown')}\n"
                    f"- 决策: {next_action}\n",
                )

                if next_action == "COLLECT_MORE":
                    if diagnose_rounds >= max_diagnose_rounds:
                        self.chat.say(
                            f"已调查 {diagnose_rounds} 轮仍无定论，"
                            f"尝试基于当前信息修复",
                            "warning",
                        )
                        state = "PLAN"
                        continue

                    # 执行 gaps 中的命令，补充 observations
                    gaps = diagnosis.get("gaps", [])
                    if not gaps:
                        self.chat.say(
                            "LLM 说需要更多信息但没给出命令，尝试修复",
                            "warning",
                        )
                        state = "PLAN"
                        continue

                    self.chat.progress(
                        f"补充收集信息... (第 {diagnose_rounds + 1} 轮)"
                    )
                    extra = self._collect_gap_commands(gaps)
                    if extra:
                        # 滚动摘要：压缩历史 observations，保留最新 gap 原始数据
                        self._obs_summary = self._summarize_observations(
                            observations, diagnosis, self._obs_summary,
                        )
                        observations = (
                            "## 历史调查摘要\n" + self._obs_summary
                            + f"\n\n## 最新收集 (第{diagnose_rounds}轮)\n"
                            + extra
                        )
                        self.notebook.append_to_incident(
                            self.current_incident,
                            f"\n### 补充数据 (第{diagnose_rounds}轮)\n"
                            f"```\n{extra[:self.ctx_limits.gap_output_incident_chars]}\n```\n",
                        )
                    state = "DIAGNOSE"  # 回去重新诊断

                elif next_action == "MONITOR":
                    monitor_seconds = 60
                    self.chat.say(
                        f"诊断建议观察等待，{monitor_seconds}s 后重新检查",
                        "info",
                    )
                    self.notebook.append_to_incident(
                        self.current_incident,
                        f"\n## 进入观察期 ({monitor_seconds}s)\n",
                    )
                    self._interruptible_sleep(monitor_seconds)
                    # 重新观察
                    observations = self._observe() or observations
                    state = "DIAGNOSE"

                elif next_action == "ESCALATE":
                    self.chat.notify(
                        f"⚠️ 需要人类介入:\n{summary}\n"
                        f"诊断: {diagnosis.get('hypothesis', '')}\n"
                        f"原因: {diagnosis.get('facts', '')}",
                        "critical",
                    )
                    self.notebook.append_to_incident(
                        self.current_incident, "\n## 已升级给人类\n"
                    )
                    # 不关闭 incident，人类可能会接手
                    return

                else:  # FIX
                    self.chat.say(
                        f"诊断完成: {diagnosis.get('hypothesis', '')}",
                        "info",
                    )

                    # escalate=true 时通知人类但不阻塞
                    if diagnosis.get("escalate"):
                        self.chat.notify(
                            f"⚠️ 复杂问题，我会继续尝试解决:\n{summary}\n"
                            f"诊断: {diagnosis.get('hypothesis', '')}\n"
                            f"把握度: {diagnosis.get('confidence', 0)}%\n"
                            f"如需干预请随时输入指令。",
                            "warning",
                        )

                    # Sprint 3: 代码 bug 自动补丁
                    rtype = diagnosis.get("type", "unknown")
                    self.chat.say(
                        f"🔧 类型: {rtype} | 开始自主修复", "action"
                    )
                    self._maybe_run_patch_loop(diagnosis)

                    state = "PLAN"

            # ── PLAN ──
            elif state == "PLAN":
                fix_attempts += 1
                if fix_attempts > max_fix_attempts:
                    self.chat.notify(
                        f"⚠️ {max_fix_attempts} 次修复尝试均失败，"
                        f"请关注:\n{summary}",
                        "critical",
                    )
                    self.notebook.append_to_incident(
                        self.current_incident,
                        f"\n## 修复失败（{max_fix_attempts}次尝试）\n"
                        f"已通知人类。\n",
                    )
                    break

                self.mode = self.INCIDENT
                plan = self._plan(diagnosis)
                if not plan:
                    self.chat.progress(
                        f"方案生成失败 ({fix_attempts}/{max_fix_attempts})"
                    )
                    if fix_attempts < max_fix_attempts:
                        state = "PLAN"
                        continue
                    self.chat.notify(
                        f"⚠️ 无法制定修复方案:\n{summary}", "critical"
                    )
                    break

                self.notebook.append_to_incident(
                    self.current_incident,
                    f"\n## 行动计划 (尝试 {fix_attempts})\n"
                    f"{plan.to_markdown()}\n",
                )

                # 只读模式检查
                if self.readonly:
                    self.chat.say(
                        f"只读模式，不执行操作。方案：\n{plan.to_markdown()}",
                        "info",
                    )
                    break

                # 信任检查
                decision = self.trust.check(plan)
                if decision == DENY:
                    self.chat.say(
                        f"操作被授权规则拒绝：{plan.action}", "warning"
                    )
                    break

                # 限制检查
                action_type = self._classify_action(plan.action)
                target_service = self._extract_service_name(plan.action)
                allowed, limit_reason = self.limits.check_action(
                    action_type, target_service
                )
                if not allowed:
                    self.chat.notify(
                        f"⛔ 限制引擎拒绝: {limit_reason}", "critical"
                    )
                    break

                # 人类批准
                if decision == ASK:
                    approved = self.chat.request_approval(plan.to_markdown())
                    if not approved:
                        self.notebook.append_to_incident(
                            self.current_incident, "\n（人类否决）\n"
                        )
                        break
                if decision == NOTIFY_THEN_DO:
                    self.chat.say(f"即将执行：{plan.action}", "action")

                state = "EXECUTE"

            # ── EXECUTE ──
            elif state == "EXECUTE":
                self.chat.progress("执行中...")
                before_state = self._targeted_observe(plan)
                exec_result, exec_all_success = self._execute(plan)

                action_type = self._classify_action(plan.action)
                target_service = self._extract_service_name(plan.action)
                self.limits.record_action(action_type, target_service)

                self.notebook.append_to_incident(
                    self.current_incident,
                    f"\n## 执行结果 (尝试 {fix_attempts})\n"
                    f"```\n{exec_result[:self.ctx_limits.exec_result_chars]}\n```\n",
                )
                self.chat.trace(
                    "EXECUTE",
                    f"命令: {plan.action}\n结果:\n{exec_result[:self.ctx_limits.exec_result_chars]}",
                )

                if not exec_all_success:
                    # 执行失败：先回滚，再回 DIAGNOSE 重新分析
                    self.chat.say("⚠️ 执行步骤失败，执行回滚...", "warning")
                    if plan.rollback_steps:
                        rollback_result = self._execute_rollback(plan)
                        self.notebook.append_to_incident(
                            self.current_incident,
                            f"\n## 回滚结果 (尝试 {fix_attempts})\n"
                            f"```\n{rollback_result[:self.ctx_limits.exec_result_chars]}\n```\n",
                        )
                    else:
                        self.chat.say("无回滚步骤可用", "warning")

                    # 把失败信息加入 observations，回到 DIAGNOSE
                    observations = (
                        observations
                        + f"\n\n## 修复执行失败 (尝试 {fix_attempts})\n"
                        f"命令: {plan.action}\n"
                        f"结果: {exec_result[:self.ctx_limits.exec_result_for_rediagnose_chars]}\n"
                        f"已回滚: {'是' if plan.rollback_steps else '无回滚步骤'}"
                    )
                    self.chat.progress("执行失败，重新诊断...")
                    state = "DIAGNOSE"
                else:
                    state = "VERIFY"

            # ── VERIFY（多次重试）──
            elif state == "VERIFY":
                verified = self._verify_with_retry(
                    plan, before_state, max_retries=3, interval=5
                )

                if verified:
                    fix_verified = True
                    self.chat.say("✅ 验证通过，问题已修复！", "success")
                    self.notebook.append_to_incident(
                        self.current_incident, "\n## 验证通过\n"
                    )
                    self._clear_issue_fingerprint(
                        self.current_target.name, self.current_issue
                    )
                    state = "REFLECT"
                else:
                    self.chat.say(
                        f"验证未通过 (尝试 {fix_attempts}/{max_fix_attempts})",
                        "warning",
                    )
                    self.notebook.append_to_incident(
                        self.current_incident,
                        f"\n## 验证未通过 (尝试 {fix_attempts})\n",
                    )
                    self.limits.record_failure()

                    if fix_attempts < max_fix_attempts:
                        # 把失败信息加入 observations，回到 DIAGNOSE 重新分析
                        observations = (
                            observations
                            + f"\n\n## 修复失败 (尝试 {fix_attempts})\n"
                            f"命令: {plan.action}\n"
                            f"结果: {exec_result[:self.ctx_limits.exec_result_for_rediagnose_chars]}\n验证: 未通过"
                        )
                        self.chat.progress("验证未通过，重新诊断...")
                        state = "DIAGNOSE"
                    else:
                        self.chat.notify(
                            f"⚠️ {max_fix_attempts} 次修复均未通过验证:\n"
                            f"{summary}",
                            "critical",
                        )
                        state = "REFLECT"

            # ── REFLECT ──
            elif state == "REFLECT":
                self._reflect()
                break

        # ── 关闭或挂起 Incident ──
        if self.current_incident:
            if fix_verified:
                self._close_incident(f"已解决: {summary}")
            else:
                self.notebook.append_to_incident(
                    self.current_incident,
                    f"\n## 状态：未解决\n"
                    f"问题尚未修复，将在下轮巡检重新处理。\n",
                )
                self.limits.record_incident_end()

    # ═══════════════════════════════════════════
    #  Sprint 5: 状态持久化 / 崩溃恢复
    # ═══════════════════════════════════════════

    def _build_state_snapshot(self):
        """构造当前 AgentState"""
        from src.reliability.state import AgentState
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
            from src.reliability.state import AgentState
            prev = AgentState.load(self.state_path)
        except Exception as e:
            logger.debug(f"recover_state load failed: {e}")
            return False
        if not prev:
            return False
        try:
            if prev.current_target_name and prev.current_target_name in self.toolboxes:
                self._switch_target(prev.current_target_name)
            self.mode = prev.mode or self.PATROL
            # 不恢复 paused 状态：重启后应默认巡检，避免上次 pause 导致永久静默
            if prev.paused:
                logger.info("上次退出时处于暂停状态，重启后自动恢复巡检")
            self.readonly = self.readonly or prev.readonly
            self.current_incident = prev.current_incident or None
            self.current_issue = prev.current_issue or ""
            self._last_error_text = prev.last_error_text or ""
            for ts in prev.auto_merge_timestamps:
                try:
                    self.limits._auto_merge_times.append(float(ts))
                except Exception:
                    pass
            return prev.has_active_work()
        except Exception as e:
            logger.warning(f"recover_state apply failed: {e}")
            return False

    def snapshot_state(self) -> dict:
        """把自己当前的运行时状态 dump 成 dict,给自修复会话用。"""
        limits_snap = {}
        try:
            limits_snap = self.limits.status()
        except Exception:
            pass
        return {
            "mode": self.mode,
            "paused": self.paused,
            "readonly": self.readonly,
            "llm_degraded": getattr(self, "llm_degraded", False),
            "current_target": self.current_target.name if self.current_target else None,
            "target_count": len(self.targets),
            "current_incident": self.current_incident,
            "current_issue": self.current_issue,
            "last_loop_time": self.last_loop_time,
            "uptime_seconds": int(time.time() - self.start_time),
            "silence_window_seconds": self._silence_window_seconds,
            "silenced_fingerprints_count": len(self._issue_fingerprints),
            "limits": limits_snap,
            "intervals": dict(self.INTERVALS),
        }
