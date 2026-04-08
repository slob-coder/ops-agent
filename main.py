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

from llm import LLMClient
from notebook import Notebook
from tools import ToolBox, TargetConfig
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

    def __init__(self, notebook_path: str, target: TargetConfig, readonly: bool = False):
        self.notebook = Notebook(notebook_path)
        self.tools = ToolBox(target)
        self.llm = LLMClient()
        self.trust = TrustEngine(self.notebook, self.llm)
        self.chat = HumanChannel(self.notebook)

        self.mode = self.PATROL
        self.readonly = readonly
        self.current_incident = None     # 当前活跃 Incident 文件名
        self.current_issue = ""          # 当前正在调查的问题描述
        self._running = True
        self._prompts = {}

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

        每次 LLM 调用都会带上这个 system prompt，让 LLM 知道：
        - 我是谁（角色、身份）
        - 我现在在做什么（工作模式、活跃 Incident）
        - 我有什么工具（可用的 shell 命令、信任等级）
        - 我的行为准则（授权规则、输出规范）
        - 我负责的系统长什么样（system-map）
        """
        system_map = self.notebook.read("system-map.md")
        permissions = self.notebook.read("config/permissions.md")

        return self._fill_prompt(
            "system",
            mode=self.mode,
            readonly="是（只读模式，不执行任何修改操作）" if self.readonly else "否",
            active_incident=self.current_incident or "无",
            permissions=permissions or "（未配置，使用默认策略）",
            system_map=system_map or "（尚未探索，系统拓扑未知）",
        )

    def _ask_llm(self, prompt: str, max_tokens: int = 4096) -> str:
        """统一的 LLM 调用入口 —— 始终携带 system prompt

        这是整个 Agent 调用 LLM 的唯一入口。确保每次调用都：
        1. 带上 system prompt（Agent 的自我认知）
        2. 带上 user prompt（具体任务指令）
        """
        system = self._build_system_prompt()
        return self.llm.ask(prompt, system=system, max_tokens=max_tokens)

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
        self.chat.say("数字运维员工启动。输入消息随时和我对话。", "info")
        self.onboard()

        while self._running:
            try:
                self._loop_once()
            except KeyboardInterrupt:
                self.chat.say("收到退出信号，下班了。", "info")
                break
            except Exception as e:
                logger.error(f"主循环异常: {e}", exc_info=True)
                self.chat.say(f"我遇到了内部错误：{e}，继续工作。", "warning")
                time.sleep(10)

        self.chat.stop()

    def _loop_once(self):
        """主循环的一次迭代"""

        # ── 第一优先级：听人说话 ──
        human_msg = self.chat.check_inbox()
        if human_msg:
            self._handle_human_message(human_msg)
            return

        # ── 第二步：感知 ──
        observations = self._observe()
        if not observations:
            time.sleep(self.INTERVALS.get(self.mode, 60))
            return

        # ── 第三步：判断 ──
        assessment = self._assess(observations)

        if assessment.get("status") == "NORMAL":
            time.sleep(self.INTERVALS.get(self.mode, 60))
            return

        # ── 发现异常 ──
        severity = assessment.get("severity", 5)
        summary = assessment.get("summary", "发现异常")
        self.current_issue = summary

        self.chat.notify(f"发现异常（严重度 {severity}/10）：{summary}", "warning")
        self.mode = self.INVESTIGATE

        # 创建 Incident
        self.current_incident = self.notebook.create_incident(summary[:40])
        self.notebook.append_to_incident(
            self.current_incident,
            f"- {datetime.now().strftime('%H:%M')} 发现异常：{summary}\n"
            f"- 严重度：{severity}/10\n"
            f"- 原始评估：{assessment.get('details', '')}\n",
        )

        # ── 诊断 ──
        diagnosis = self._diagnose(assessment, observations)

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

        self.notebook.append_to_incident(
            self.current_incident,
            f"\n## 执行结果\n```\n{exec_result}\n```\n",
        )

        # ── 验证 ──
        self.chat.say("修复完成，开始验证...", "info")
        time.sleep(3)  # 等一小会儿让系统稳定
        after_state = self._quick_observe()
        verified = self._verify(action_plan, before_state, after_state)

        if verified:
            self.chat.say("验证通过，问题已修复！", "success")
            self.notebook.append_to_incident(self.current_incident, "\n## 验证通过\n")
        else:
            self.chat.say("验证未通过，尝试回滚...", "warning")
            self.notebook.append_to_incident(self.current_incident, "\n## 验证未通过\n")
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
            result = self.tools.run(cmd, timeout=15)
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

    def _diagnose(self, assessment: dict, observations: str) -> dict:
        """深度诊断"""
        system_map = self.notebook.read("system-map.md")
        summary = assessment.get("summary", "")

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
        )

        response = self._ask_llm(prompt)
        return self._parse_diagnosis(response)

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
            result = self.tools.run(cmd)
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

    # ═══════════════════════════════════════════
    #  人类消息处理
    # ═══════════════════════════════════════════

    def _handle_human_message(self, msg: str):
        """处理人类的消息"""
        lower = msg.lower().strip()

        # 特殊命令
        if lower in ("quit", "exit", "bye"):
            self.chat.say("收到，下班了。再见！", "info")
            self._running = False
            return

        if lower == "status":
            self._report_status()
            return

        if lower == "readonly on":
            self.readonly = True
            self.chat.say("已切换到只读模式。", "info")
            return

        if lower == "readonly off":
            self.readonly = False
            self.chat.say("已切换到正常模式。", "info")
            return

        if lower == "stop":
            self.mode = self.PATROL
            self.current_issue = ""
            self.chat.say("已停止当前调查，回到巡检模式。", "info")
            return

        # 通用对话：让 LLM 回答
        context = (
            f"系统信息：\n{self.notebook.read('system-map.md')[:1000]}\n\n"
            f"当前状态：模式={self.mode}, 只读={self.readonly}\n"
            f"活跃 Incident: {self.current_incident or '无'}\n"
        )

        prompt = f"""你是一名运维工程师，人类同事问了你一个问题。
根据你的系统知识和当前状态回答。如果需要执行命令来回答，
列出命令并执行。如果不确定，直接说不确定。

## 系统上下文
{context}

## 人类的问题
{msg}

请直接回答，保持简洁友好。如果需要执行命令，用 ```commands 格式列出。"""

        response = self._ask_llm(prompt)

        # 如果回答中包含命令，执行它们
        commands = self._extract_commands(response)
        if commands:
            cmd_results = []
            for cmd in commands[:5]:
                result = self.tools.run(cmd, timeout=15)
                cmd_results.append(str(result))

            # 让 LLM 基于命令结果给出最终回答
            followup = f"""刚才的问题是：{msg}
命令执行结果：
{chr(10).join(cmd_results)}

请基于结果回答人类的问题。"""
            final = self._ask_llm(followup)
            self.chat.say(final)
        else:
            self.chat.say(response)

    def _report_status(self):
        """汇报当前状态"""
        active_incidents = self.notebook.list_dir("incidents/active")
        archived = self.notebook.list_dir("incidents/archive")
        playbooks = self.notebook.list_dir("playbook")

        self.chat.say(
            f"当前状态：\n"
            f"   模式：{self.mode}\n"
            f"   只读：{'是' if self.readonly else '否'}\n"
            f"   活跃 Incident：{len(active_incidents)} 个\n"
            f"   历史 Incident：{len(archived)} 个\n"
            f"   Playbook：{len(playbooks)} 个\n"
            f"   当前问题：{self.current_issue or '无'}",
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
            result = self.tools.run(cmd, timeout=10)
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
        }

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
    parser.add_argument("--target", default="", help="目标系统（SSH: user@host）")
    parser.add_argument("--port", type=int, default=22, help="SSH 端口")
    parser.add_argument("--key", default="", help="SSH 密钥路径")
    parser.add_argument("--password", action="store_true",
                        help="使用密码认证（将交互式提示输入，需要 sshpass）")
    parser.add_argument("--readonly", action="store_true", help="只读模式")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # 配置目标
    if args.target:
        password = ""
        if args.password:
            import getpass
            password = getpass.getpass(f"SSH password for {args.target}: ")
        elif os.getenv("OPS_SSH_PASSWORD"):
            # 也支持通过环境变量传入密码（方便 Docker 部署）
            password = os.getenv("OPS_SSH_PASSWORD", "")

        target = TargetConfig.ssh(args.target, args.port, args.key, password)
    else:
        target = TargetConfig.local()

    # 启动 Agent
    agent = OpsAgent(
        notebook_path=args.notebook,
        target=target,
        readonly=args.readonly,
    )

    # 优雅退出
    def handler(sig, frame):
        agent._running = False

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    agent.run()


if __name__ == "__main__":
    main()
