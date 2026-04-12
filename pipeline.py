"""
OODA 循环 Mixin — observe → assess → diagnose → plan → execute → verify → reflect
"""

import os
import re
import time
import logging
from datetime import datetime

logger = logging.getLogger("ops-agent")


class PipelineMixin:
    """完整的 OODA 修复流水线"""

    def _observe(self) -> str:
        """感知：让 LLM 决定看什么，然后执行"""
        self.chat.progress("分析观察目标...")
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

        response = self._ask_llm(prompt, phase="OBSERVE")

        # 提取命令列表
        commands = self._extract_commands(response)
        if not commands:
            return ""

        # 执行命令、收集输出
        outputs = []
        for cmd in commands[:10]:  # 最多执行 10 条
            result = self._run_cmd(cmd, timeout=15)
            self.chat.trace("OBSERVE", f"$ {cmd}\n{str(result)[:1500]}")
            outputs.append(str(result))

        return "\n\n".join(outputs)

    def _assess(self, observations: str) -> dict:
        """判断观察结果是否正常"""
        self.chat.progress("评估观察结果...")
        system_map = self.notebook.read("system-map.md")
        recent = self._recent_incidents_summary()

        prompt = self._fill_prompt(
            "assess",
            system_map=system_map,
            observations=observations,
            recent_incidents=recent,
        )

        response = self._ask_llm(prompt, phase="ASSESS")
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
        self.chat.progress("诊断中...")
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
            self.chat.progress(
                f"已定位源码: {top.repo_name}:{os.path.basename(top.local_file)}"
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

        # trace 记录源码上下文
        self.chat.trace("DIAGNOSE", f"源码上下文:\n{source_text[:2000]}")

        prompt = self._fill_prompt(
            "diagnose",
            assessment=str(assessment),
            observations=observations[:3000],
            relevant_playbooks=playbook_content or "（无匹配的 Playbook）",
            similar_incidents=incidents_content or "（无历史记录）",
            system_map=system_map,
            source_locations=source_text,
        )

        response = self._ask_llm(prompt, phase="DIAGNOSE")
        result = self._parse_diagnosis(response)

        # 屏幕只显示结论
        conf = result.get("confidence", 0)
        rtype = result.get("type", "unknown")
        hypothesis = result.get("hypothesis", "")[:80]
        self.chat.progress(f"把握度 {conf}% | 类型: {rtype} | {hypothesis}")

        return result

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

    def _plan(self, diagnosis: dict):
        """制定修复方案"""
        self.chat.progress("制定修复方案...")
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

        response = self._ask_llm(prompt, phase="PLAN")
        plan = self._parse_plan(response)
        if plan:
            self.chat.say(
                f"方案: {plan.action[:120]}  (L{plan.trust_level})",
                "action",
            )
        return plan

    def _execute(self, plan) -> str:
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

    def _verify(self, plan, before: str, after: str) -> bool:
        """验证修复结果"""
        self.chat.progress("验证修复效果...")
        prompt = self._fill_prompt(
            "verify",
            action_result=plan.action,
            before_state=before[:2000],
            after_state=after[:2000],
            verification_criteria=plan.verification,
        )

        response = self._ask_llm(prompt, phase="VERIFY")
        return "SUCCESS" in response.upper() and "FAILED" not in response.upper()

    def _reflect(self):
        """复盘总结"""
        if not self.current_incident:
            return

        self.chat.progress("复盘总结...")
        incident_record = self.notebook.read(f"incidents/active/{self.current_incident}")
        playbook_list = self.notebook.read_playbooks_summary()

        prompt = self._fill_prompt(
            "reflect",
            incident_record=incident_record[:4000],
            playbook_list=playbook_list,
        )

        response = self._ask_llm(prompt, phase="REFLECT")

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
            self.chat._trace_file = "patrol"  # trace 恢复到默认
            self.limits.record_incident_end()

    def _generate_gap_commands(self, gaps_text: str) -> list:
        """根据诊断中的'缺失信息'描述，让 LLM 生成具体的收集命令"""
        prompt = (
            f"以下是排查运维问题时发现缺失的信息:\n\n{gaps_text}\n\n"
            f"目标: {self.current_target.name} ({self.current_target.mode})\n\n"
            f"请生成具体的 shell 命令来收集这些信息。\n"
            f"每行一条命令,放在 ```commands 代码块中。只输出只读命令(不要修改任何东西)。\n"
            f"最多 6 条命令。"
        )
        try:
            response = self._ask_llm(prompt, max_tokens=500, phase="GAP_COMMANDS")
            return self._extract_commands(response)[:6]
        except Exception:
            return []

    def _note(self, text: str) -> None:
        """便捷:把一行文本追加到当前 incident,失败静默"""
        if not self.current_incident:
            return
        try:
            self.notebook.append_to_incident(self.current_incident, f"- {text}\n")
        except Exception:
            pass
