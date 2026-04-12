"""
OpsAgent 核心骨架 — 类定义、初始化、主循环、状态管理
"""

import os
import re
import time
import logging
from pathlib import Path
from datetime import datetime

from llm import LLMClient, LLMInterrupted, LLMDegraded
from notebook import Notebook
from tools import ToolBox, TargetConfig, CommandInterrupted
from trust import TrustEngine, ActionPlan, ALLOW, NOTIFY_THEN_DO, ASK, DENY
from chat import HumanChannel

from prompt_engine import PromptsMixin
from pipeline import PipelineMixin
from pr_workflow import PRWorkflowMixin
from human import HumanInteractionMixin
from metrics import MetricsMixin
from parsers import ParsersMixin

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

        # ── 自修复 probation 自检 ──
        # 如果上次退出是因为自修复合并,新进程启动时要先跑一遍测试确认没改坏
        selfdev_path = os.environ.get("OPS_AGENT_SELFDEV_PATH", "")
        if selfdev_path:
            try:
                from self_repair import run_probation_if_pending
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
            f"{self.current_target.name}-{summary[:40]}"
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

        # ── 诊断（含深度调查循环）──
        self.chat.say("🔍 进入调查模式", "observe")
        diagnosis = self._diagnose(assessment, observations)

        # 诊断后允许人类插话
        if self._drain_human_messages():
            return

        # ── 深度调查循环：confidence 不够就自己补充信息重新诊断 ──
        max_investigate_rounds = 3
        for round_i in range(max_investigate_rounds):
            if diagnosis.get("confidence", 0) >= 50:
                break  # 够了，继续走修复流程

            gaps = diagnosis.get("gaps", "")
            if not gaps:
                break  # LLM 都没说缺什么信息，不必再挖

            self.chat.progress(
                f"把握度 {diagnosis.get('confidence', 0)}%，"
                f"补充收集信息... (第 {round_i + 2} 轮)"
            )
            self.notebook.append_to_incident(
                self.current_incident,
                f"\n## 补充调查 (第 {round_i + 2} 轮)\n"
                f"把握度: {diagnosis.get('confidence', 0)}%\n"
                f"缺失信息: {gaps[:500]}\n",
            )

            # 从 gaps 中提取 LLM 建议的命令并执行
            extra_commands = self._extract_commands(gaps)
            if not extra_commands:
                # gaps 是描述性文字没有具体命令，让 LLM 基于 gaps 生成命令
                extra_commands = self._generate_gap_commands(gaps)

            extra_observations = []
            for cmd in extra_commands[:8]:
                result = self._run_cmd(cmd, timeout=15)
                self.chat.trace("INVESTIGATE", f"$ {cmd}\n{str(result)[:1500]}")
                extra_observations.append(str(result))

            supplemental = "\n\n".join(extra_observations)
            if supplemental.strip():
                observations = observations + "\n\n## 补充收集\n" + supplemental
                self.notebook.append_to_incident(
                    self.current_incident,
                    f"\n### 补充数据\n```\n{supplemental[:2000]}\n```\n",
                )

            # 重新诊断（带上更丰富的 observations）
            diagnosis = self._diagnose(assessment, observations)

            if self._drain_human_messages():
                return

        # ── 诊断结论 ──
        conf = diagnosis.get("confidence", 0)
        rtype = diagnosis.get("type", "unknown")
        hyp = diagnosis.get("hypothesis", "")[:100]
        self.chat.say(f"诊断完成: {hyp}", "info")

        # 只有 escalate=YES 才真正通知人类（不阻塞，继续尝试修复）
        if diagnosis.get("escalate") == "YES":
            self.notebook.append_to_incident(
                self.current_incident,
                f"\n## 复杂问题（已通知人类，继续自主处理）\n"
                f"{diagnosis.get('hypothesis', '无法确定根因')}\n",
            )
            self.chat.notify(
                f"⚠️ 复杂问题，我会继续尝试解决:\n{summary}\n"
                f"诊断: {hyp}\n"
                f"把握度: {conf}%\n"
                f"如需干预请随时输入指令。",
                "warning",
            )
            # 不 return，继续走下面的修复流程

        # ── Sprint 3: 本地补丁生成与验证 ──
        self.chat.say(f"🔧 类型: {rtype} | 开始自主修复", "action")
        self._maybe_run_patch_loop(diagnosis)

        if self._drain_human_messages():
            return

        # ── 制定方案 + 执行 + 验证（最多重试 2 次）──
        self._fix_verified = False
        self.mode = self.INCIDENT
        max_fix_attempts = 2
        for fix_attempt in range(1, max_fix_attempts + 1):
            action_plan = self._plan(diagnosis)

            if not action_plan:
                if fix_attempt < max_fix_attempts:
                    self.chat.progress(f"方案生成失败，重新尝试... ({fix_attempt}/{max_fix_attempts})")
                    continue
                self.chat.notify(
                    f"⚠️ 无法制定修复方案，请关注:\n{summary}", "critical"
                )
                self.notebook.append_to_incident(
                    self.current_incident,
                    "\n## 无法制定修复方案\n已通知人类。\n",
                )
                break

            self.notebook.append_to_incident(
                self.current_incident,
                f"\n## 行动计划 (尝试 {fix_attempt})\n{action_plan.to_markdown()}\n",
            )

            # ── 信任检查 ──
            if self.readonly:
                self.chat.say(
                    f"只读模式，不执行操作。方案：\n   {action_plan.action}", "info"
                )
                self.notebook.append_to_incident(
                    self.current_incident, "\n（只读模式，未执行）\n"
                )
                break

            decision = self.trust.check(action_plan)

            if decision == DENY:
                self.chat.say(
                    f"操作被授权规则拒绝：{action_plan.action}", "warning"
                )
                self.notebook.append_to_incident(
                    self.current_incident, "\n（操作被拒绝）\n"
                )
                break

            # ── 限制检查 ──
            action_type = self._classify_action(action_plan.action)
            target_service = self._extract_service_name(action_plan.action)
            allowed, reason = self.limits.check_action(action_type, target_service)
            if not allowed:
                self.chat.notify(
                    f"⛔ 限制引擎拒绝执行: {reason}\n"
                    f"动作: {action_plan.action}",
                    "critical",
                )
                self.notebook.append_to_incident(
                    self.current_incident,
                    f"\n## 限制拒绝\n{reason}\n",
                )
                break

            # ── 权限需要人类批准（唯一阻塞等人类的地方）──
            if decision == ASK:
                approved = self.chat.request_approval(action_plan.to_markdown())
                if not approved:
                    self.notebook.append_to_incident(
                        self.current_incident, "\n（人类否决）\n"
                    )
                    break

            if decision == NOTIFY_THEN_DO:
                self.chat.say(f"即将执行：{action_plan.action}", "action")

            # ── 执行 ──
            self.chat.progress("执行中...")
            before_state = self._quick_observe()
            exec_result = self._execute(action_plan)

            self.limits.record_action(action_type, target_service)

            self.notebook.append_to_incident(
                self.current_incident,
                f"\n## 执行结果 (尝试 {fix_attempt})\n```\n{exec_result}\n```\n",
            )
            self.chat.trace(
                "EXECUTE",
                f"命令: {action_plan.action}\n结果:\n{exec_result[:2000]}",
            )

            # ── 验证 ──
            self.chat.progress("验证中...")
            self._interruptible_sleep(3)
            after_state = self._quick_observe()
            verified = self._verify(action_plan, before_state, after_state)

            if verified:
                self.chat.say("✅ 验证通过，问题已修复！", "success")
                self.notebook.append_to_incident(
                    self.current_incident, "\n## 验证通过\n"
                )
                self._clear_issue_fingerprint(
                    self.current_target.name, self.current_issue
                )
                self._fix_verified = True
                break
            else:
                self.chat.say(
                    f"验证未通过 (尝试 {fix_attempt}/{max_fix_attempts})", "warning"
                )
                self.notebook.append_to_incident(
                    self.current_incident,
                    f"\n## 验证未通过 (尝试 {fix_attempt})\n",
                )
                self.limits.record_failure()

                if fix_attempt < max_fix_attempts:
                    # 用执行结果作为新观察，重新诊断
                    self.chat.progress("验证未通过，重新诊断...")
                    new_obs = (
                        f"上次修复尝试:\n命令: {action_plan.action}\n"
                        f"结果: {exec_result[:1000]}\n验证: 未通过"
                    )
                    observations = observations + "\n\n## 修复后观察\n" + new_obs
                    diagnosis = self._diagnose(assessment, observations)
                    if self._drain_human_messages():
                        return
                else:
                    # 最后一次也失败，通知人类（不阻塞）
                    self.chat.notify(
                        f"⚠️ {max_fix_attempts} 次修复尝试均未通过验证，请关注:\n{summary}",
                        "critical",
                    )

        # ── 复盘 ──
        self._reflect()

        # ── 关闭或挂起 Incident ──
        if self.current_incident:
            if self._fix_verified:
                self._close_incident(f"已解决: {summary[:60]}")
            else:
                self.notebook.append_to_incident(
                    self.current_incident,
                    f"\n## 状态：未解决\n问题尚未修复，将在下轮巡检重新处理。\n",
                )
                self.limits.record_incident_end()
        self.mode = self.PATROL
        self.current_issue = ""

    # ═══════════════════════════════════════════
    #  Sprint 5: 状态持久化 / 崩溃恢复
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
