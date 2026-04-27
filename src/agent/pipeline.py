"""
OODA 循环 Mixin — observe → assess → diagnose → plan → execute → verify → reflect

Sprint 7-8: Smart Notebook 集成点
- _observe: 日志行经过感知漏斗 (assess_logs)
- _diagnose: 知识图谱增强上下文 (gather_knowledge)
- _reflect: 关闭后触发织网 + 蒸馏 (weave_content / run_maintenance)
- _loop_once: 巡检间隙执行维护 (run_maintenance)
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
        max_cmds = self.limits.config.max_observe_commands
        for cmd in commands[:max_cmds]:
            result = self._run_cmd(cmd, timeout=15)
            self.chat.trace("OBSERVE", f"$ {cmd}\n{str(result)[:self.ctx_limits.observe_output_chars]}")
            outputs.append(str(result))

        raw_observations = "\n\n".join(outputs)

        # Sprint 7-8: Smart 感知漏斗 — 过滤噪音、检测异常
        if hasattr(self.notebook, "assess_logs") and raw_observations:
            try:
                log_lines = raw_observations.split("\n")
                anomalies = self.notebook.assess_logs(
                    log_lines, source=getattr(self.current_target, "name", ""),
                )
                if anomalies:
                    anomaly_text = "\n".join(
                        f"⚡ [{a.level}] {a.signature}: {a.description}"
                        for a in anomalies
                        if hasattr(a, "signature")
                    )
                    if anomaly_text:
                        raw_observations += (
                            f"\n\n## Smart 感知漏斗检测\n{anomaly_text}"
                        )
                        self.chat.trace(
                            "OBSERVE",
                            f"感知漏斗发现 {len(anomalies)} 个异常信号",
                        )
            except Exception as e:
                logger.debug(f"Smart assess_logs in _observe failed: {e}")

        return raw_observations

    def _assess(self, observations: str) -> dict:
        """判断观察结果是否正常"""
        self.chat.progress("评估观察结果...")
        system_map = self.notebook.read("system-map.md")
        recent = self._recent_incidents_summary()
        silences = self.notebook.read("incidents/silence.yml")

        prompt = self._fill_prompt(
            "assess",
            system_map=system_map,
            observations=observations,
            recent_incidents=recent,
            silences=silences or "# 暂无静默规则",
        )

        response = self._ask_llm(prompt, phase="ASSESS")
        return self._parse_assessment(response)

    def _locate_source_from_text(self, text: str):
        """Sprint 2: 从一段日志/观察文本里抽取异常栈并定位到本地源码

        返回 (LocateResult | None, ParsedTrace | None)。
        任何失败都返回 (None, None),不影响诊断流程继续。
        """
        try:
            from src.repair.stack_parser import StackTraceParser
            from src.repair.source_locator import SourceLocator
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

        # Sprint 7-8: Smart 知识图谱增强 — 三层检索 + 结构化上下文
        smart_knowledge = None
        if hasattr(self.notebook, "gather_knowledge"):
            try:
                smart_knowledge = self.notebook.gather_knowledge(
                    summary + " " + observations[:500]
                )
            except Exception as e:
                logger.debug(f"Smart gather_knowledge failed: {e}")

        # 搜索相关 Playbook（Smart 提供更丰富的结果，回退到 Basic）
        relevant_files = self.notebook.find_relevant(
            summary + " " + observations[:self.ctx_limits.playbook_search_chars]
        )
        playbook_content = ""
        if smart_knowledge and hasattr(smart_knowledge, "playbook_contents"):
            # Smart: 结构化 playbook 内容
            for name, content in smart_knowledge.playbook_contents.items():
                playbook_content += f"\n### {name}\n{content[:self.ctx_limits.playbook_content_chars]}\n"
        if not playbook_content:
            for f in relevant_files:
                if "playbook" in f or "lesson" in f:
                    playbook_content += f"\n### {f}\n{self.notebook.read(f)[:self.ctx_limits.playbook_content_chars]}\n"

        # 搜索历史 Incident
        incidents_content = ""
        if smart_knowledge and hasattr(smart_knowledge, "similar_incidents"):
            for inc in smart_knowledge.similar_incidents:
                if isinstance(inc, str):
                    content = self.notebook.read(inc)
                    incidents_content += f"\n### {inc}\n{content[:self.ctx_limits.incident_history_chars]}\n"
        if not incidents_content:
            for f in relevant_files:
                if "incidents" in f:
                    incidents_content += f"\n### {f}\n{self.notebook.read(f)[:self.ctx_limits.incident_history_chars]}\n"

        # Smart: 附加 reflections 上下文
        reflections_content = ""
        if smart_knowledge and hasattr(smart_knowledge, "reflections"):
            for ref in (smart_knowledge.reflections or []):
                reflections_content += f"- {ref}\n"

        # trace 记录源码上下文
        self.chat.trace("DIAGNOSE", f"源码上下文:\n{source_text[:self.ctx_limits.source_context_trace_chars]}")

        # 加载项目地图 (AGENTS.md)：有源码定位时按 repo 加载（智能裁剪），否则加载全文
        project_map = ""
        if locate_result and locate_result.locations:
            repo_name = locate_result.locations[0].repo_name
            keywords = [summary, assessment.get("summary", "")]
            project_map = self._load_agents_md_section(repo_name, keywords)
        elif self.current_target and self.current_target.source_repos:
            project_map = self._load_agents_md()

        # Sprint 7-8: 将 reflections 注入到相似 incident 上下文中
        if reflections_content:
            incidents_content += f"\n### 相关经验洞察\n{reflections_content}\n"

        max_obs = getattr(self.limits.config, "max_observations_chars", 8000)
        prompt = self._fill_prompt(
            "diagnose",
            assessment=str(assessment),
            observations=observations[:max_obs],
            relevant_playbooks=playbook_content or "（无匹配的 Playbook）",
            similar_incidents=incidents_content or "（无历史记录）",
            system_map=system_map,
            source_locations=source_text,
            project_map=project_map or "（无项目地图）",
        )

        response = self._ask_llm(prompt, phase="DIAGNOSE")
        result = self._parse_diagnosis(response)

        # 屏幕只显示结论
        conf = result.get("confidence", 0)
        rtype = result.get("type", "unknown")
        hypothesis = result.get("hypothesis", "")
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
                locations=result.locations[:self.limits.config.max_source_locations],
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
                f"- 尝试次数: {verified.attempts}/{self.limits.config.max_patch_attempts}\n"
                f"- 修改说明: {verified.patch.description}\n"
                f"- 修改文件: {', '.join(verified.patch.files_changed)}\n"
            )
            try:
                self.notebook.append_to_incident(self.current_incident, note)
            except Exception:
                pass
            self.chat.say(
                f"✓ 补丁本地验证通过 ({verified.result.short_summary()})", "success"
            )
            # 直接推送 + 部署（不走 PR 流程）
            self._deploy_patch(verified, repo)
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
            if "playbook" in f or "lesson" in f:
                playbook += self.notebook.read(f) + "\n"

        # 提取构建/部署配置
        build_deploy_context = self._get_build_deploy_context()

        # 加载项目地图：code_bug 类型时智能裁剪
        project_map = ""
        if diagnosis.get("type") == "code_bug":
            keywords = [diagnosis.get("hypothesis", ""), diagnosis.get("facts", "")]
            project_map = self._load_agents_md_section(keywords=keywords)
        elif self.current_target and self.current_target.source_repos:
            project_map = self._load_agents_md()

        # 复用 diagnose 阶段的源码定位结果
        source_text = "（无）"
        if hasattr(self, "_last_locate_result") and self._last_locate_result and self._last_locate_result.locations:
            source_text = self._last_locate_result.render()

        prompt = self._fill_prompt(
            "plan",
            diagnosis=str(diagnosis),
            matched_playbook=playbook or "（无匹配的 Playbook）",
            permissions=permissions,
            build_deploy_context=build_deploy_context,
            project_map=project_map or "（无项目地图）",
            source_locations=source_text,
        )

        response = self._ask_llm(prompt, phase="PLAN")
        plan = self._parse_plan(response)
        if plan:
            self.chat.say(
                f"方案: {plan.action}  (L{plan.trust_level})",
                "action",
            )
        return plan

    def _get_build_deploy_context(self) -> str:
        """提取当前目标的构建/部署配置（用于 plan prompt）"""
        if not self.current_target or not self.current_target.source_repos:
            return "（未配置源码仓库，不涉及构建/部署）"

        repos = self.current_target.get_source_repos()
        if not repos:
            return "（未配置源码仓库，不涉及构建/部署）"

        lines = ["当前目标关联的源码仓库："]
        for repo in repos:
            lines.append(f"\n### {repo.name}")
            if repo.language:
                lines.append(f"- 语言: {repo.language}")
            if repo.build_cmd:
                lines.append(f"- 构建命令: `{repo.build_cmd}`")
            else:
                lines.append(f"- 构建命令: （未配置）")
            if repo.test_cmd:
                lines.append(f"- 测试命令: `{repo.test_cmd}`")
            if repo.deploy_cmd:
                lines.append(f"- 部署命令: `{repo.deploy_cmd}`")
            else:
                lines.append(f"- 部署命令: （未配置，需手动重启服务）")
            if repo.runtime_service:
                lines.append(f"- 运行时服务: {repo.runtime_service}")

        return "\n".join(lines)

    def _execute(self, plan) -> tuple:
        """执行修复动作 — 按 plan.steps 逐步执行

        Returns:
            (result_text: str, all_success: bool)
        """
        results = []
        all_success = True
        max_timeout_retries = 2  # 超时最多重试 2 次（共 3 次尝试）

        for i, step in enumerate(plan.steps, 1):
            cmd = step.get("command", "")
            purpose = step.get("purpose", "")
            wait = step.get("wait_seconds", 0)

            if not cmd:
                continue

            self.chat.trace("EXECUTE", f"STEP {i}: {cmd} ({purpose})")

            # 超时重试机制：首次用默认 timeout，超时后逐步加倍重试
            base_timeout = 30
            result = None
            for attempt in range(1, max_timeout_retries + 2):  # 1 + max_timeout_retries
                timeout = base_timeout * (2 ** (attempt - 1))  # 30, 60, 120...
                result = self._run_cmd(cmd, timeout=timeout)

                # 判断是否超时：returncode 为特定值或输出包含 timeout 关键字
                is_timeout = (
                    result.returncode == -1
                    or "timeout" in str(result).lower()
                    or "timed out" in str(result).lower()
                )
                if not is_timeout or attempt > max_timeout_retries:
                    break

                logger.warning(
                    f"Step {i} timed out (attempt {attempt}/{max_timeout_retries + 1}, "
                    f"timeout={timeout}s), retrying with longer timeout..."
                )
                self.chat.progress(
                    f"步骤 {i} 超时 (尝试 {attempt}/{max_timeout_retries + 1})，延长超时重试..."
                )

            results.append(f"STEP {i}: {cmd}\n{str(result)}")

            if not result.success:
                logger.warning(f"Step {i} failed: {cmd}")
                all_success = False
                break

            if wait > 0:
                self.chat.progress(f"步骤 {i} 完成，等待 {wait}s...")
                self._interruptible_sleep(wait)

        return "\n\n".join(results), all_success

    def _execute_rollback(self, plan) -> str:
        """执行回滚步骤 — 逐步执行 plan.rollback_steps"""
        results = []
        for i, step in enumerate(plan.rollback_steps, 1):
            cmd = step.get("command", "") if isinstance(step, dict) else str(step)
            purpose = step.get("purpose", "") if isinstance(step, dict) else ""

            if not cmd:
                continue

            self.chat.trace("ROLLBACK", f"STEP {i}: {cmd} ({purpose})")
            result = self._run_cmd(cmd, timeout=30)
            results.append(f"ROLLBACK STEP {i}: {cmd}\n{str(result)}")

            if not result.success:
                logger.warning(f"Rollback step {i} failed: {cmd}")
                # 回滚失败继续执行后续步骤，尽力恢复

        return "\n\n".join(results)

    def _verify_with_retry(self, plan, before_state: str,
                           max_retries: int = 3, interval: int = 5) -> bool:
        """验证修复效果 — 支持多次重试"""
        for attempt in range(1, max_retries + 1):
            self.chat.progress(f"验证中... (第 {attempt}/{max_retries} 次)")
            self._interruptible_sleep(interval)

            after_state = self._targeted_observe(plan)

            prompt = self._fill_prompt(
                "verify",
                action_result=plan.action,
                before_state=before_state[:self.ctx_limits.verify_state_chars],
                after_state=after_state[:self.ctx_limits.verify_state_chars],
                verification_criteria=plan.verification,
            )
            response = self._ask_llm(prompt, phase="VERIFY")
            passed = "SUCCESS" in response.upper() and "FAILED" not in response.upper()

            self.chat.trace(
                "VERIFY",
                f"attempt={attempt} result={'PASS' if passed else 'FAIL'}\n{response[:self.ctx_limits.verify_response_trace_chars]}",
            )

            if passed:
                return True

            if attempt < max_retries:
                self.chat.progress(f"验证未通过，{interval}s 后重试...")
                if self.current_incident:
                    self.notebook.append_to_incident(
                        self.current_incident,
                        f"\n### 验证重试 {attempt}/{max_retries}: 未通过\n",
                    )

        return False

    def _reflect(self):
        """复盘总结"""
        if not self.current_incident:
            return

        self.chat.progress("复盘总结...")
        incident_record = self.notebook.read(f"incidents/active/{self.current_incident}")
        playbook_list = self.notebook.read_playbooks_summary()

        prompt = self._fill_prompt(
            "reflect",
            incident_record=incident_record[:self.ctx_limits.reflect_incident_chars],
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

        # 更新 README 成长数据
        self.notebook.update_readme_growth()

        # Sprint 7-8: Smart 反思增强 — 知识织网 + 蒸馏 + 洞察持久化
        if hasattr(self.notebook, "weave_content"):
            try:
                self.notebook.weave_content(
                    f"incidents/active/{self.current_incident}",
                    doc_type="incident",
                    summary=response[:500],
                )
                self.chat.trace("REFLECT", "知识织网完成")
            except Exception as e:
                logger.debug(f"Smart weave_content in _reflect failed: {e}")

        if hasattr(self.notebook, "run_maintenance"):
            try:
                result = self.notebook.run_maintenance()
                distilled = result.get("distilled_playbooks", 0)
                if distilled:
                    self.chat.trace(
                        "REFLECT",
                        f"Playbook 蒸馏: 生成 {distilled} 个新 Playbook",
                    )
            except Exception as e:
                logger.debug(f"Smart run_maintenance in _reflect failed: {e}")

    def _close_incident(self, summary: str):
        """关闭并归档 Incident"""
        if self.current_incident:
            self._emit_audit("incident_closed", incident=self.current_incident, summary=summary[:200])
            self.notebook.close_incident(self.current_incident, summary)
            self.current_incident = None
            self.chat._trace_file = "patrol"  # trace 恢复到默认
            self.limits.record_incident_end()

    def _summarize_observations(
        self, observations: str, diagnosis: dict, prev_summary: str = "",
    ) -> str:
        """把当前 observations 压缩为关键事实摘要，供下轮诊断使用。

        采用滚动摘要策略：每轮只保留最新 gap 原始数据，历史信息全部
        压缩进摘要。无论多少轮 COLLECT_MORE，总长度都有上界。
        """
        max_obs = getattr(self.limits.config, "max_observations_chars", 8000)
        summary_budget = max(500, max_obs // 4)  # 摘要最多占 1/4 空间

        diag_json = ""
        try:
            import json
            diag_json = json.dumps(diagnosis, ensure_ascii=False)[:self.ctx_limits.diagnosis_json_chars]
        except Exception:
            diag_json = str(diagnosis)[:self.ctx_limits.diagnosis_json_chars]

        prev_part = ""
        if prev_summary:
            prev_part = f"\n## 上轮摘要\n{prev_summary[:self.ctx_limits.prev_summary_chars]}\n"

        prompt = (
            f"你是运维助手。将以下观测数据和诊断结论压缩为 {summary_budget} 字以内的关键事实摘要。\n"
            f"保留：异常现象、已确认的事实、已排除的假设、关键数值、表结构信息。\n"
            f"丢弃：正常的输出、重复信息、格式装饰、健康检查日志。\n"
            f"{prev_part}\n"
            f"## 当前诊断结论\n{diag_json}\n\n"
            f"## 观测数据\n{observations[:max_obs]}"
        )
        try:
            resp = self._ask_llm(prompt, max_tokens=800, phase="SUMMARIZE")
            return resp[:summary_budget]
        except Exception as e:
            logger.warning(f"observations 摘要失败，回退截断: {e}")
            return observations[:summary_budget]

    def _collect_gap_commands(self, gaps: list) -> str:
        """执行诊断中 gaps 列出的命令，返回收集到的输出

        gaps 格式: [{"description": "...", "command": "..."}]
        """
        outputs = []
        max_gap = self.limits.config.max_gap_commands
        for gap in gaps[:max_gap]:
            cmd = gap.get("command", "")
            if not cmd:
                continue
            result = self._run_cmd(cmd, timeout=15)
            self.chat.trace("INVESTIGATE", f"$ {cmd}\n{str(result)[:self.ctx_limits.gap_output_trace_chars]}")
            outputs.append(f"$ {cmd}\n{str(result)}")

        # 如果 gaps 里没有 command（只有 description），让 LLM 生成
        if not outputs:
            descriptions = "\n".join(g.get("description", "") for g in gaps if g.get("description"))
            if descriptions:
                cmds = self._generate_gap_commands(descriptions)
                max_gen = self.limits.config.max_generated_gap_commands
                for cmd in cmds[:max_gen]:
                    result = self._run_cmd(cmd, timeout=15)
                    self.chat.trace("INVESTIGATE", f"$ {cmd}\n{str(result)[:self.ctx_limits.gap_output_trace_chars]}")
                    outputs.append(f"$ {cmd}\n{str(result)}")

        return "\n\n".join(outputs)

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
            return self._extract_commands(response)[:self.limits.config.max_generated_gap_commands]
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

    # ═══════════════════════════════════════════════════════════
    #  补丁部署（替代 PR 工作流的快速路径）
    # ═══════════════════════════════════════════════════════════

    def _deploy_patch(self, verified, repo) -> None:
        """补丁验证通过后：merge → push → deploy → 验证

        每个环节失败都通知人类。
        """
        branch = verified.result.branch_name
        commit_sha = verified.result.commit_sha
        repo_path = repo.path
        main_branch = repo.branch or "main"

        # 清理 patch_applier 遗留的 stash（如果有）
        stash_list = str(self._run_cmd(
            f"git -C {repo_path} stash list", timeout=5
        ))
        if "ops-agent-pre-patch" in stash_list:
            self._run_cmd(f"git -C {repo_path} stash drop", timeout=5)

        # ── 1. 合并到主分支 ──
        self.chat.say(f"合并 {branch} → {main_branch}...", "info")
        try:
            self._run_cmd(
                f"git -C {repo_path} checkout {main_branch}", timeout=10
            )
            merge_out = str(self._run_cmd(
                f"git -C {repo_path} merge {branch} --no-edit", timeout=30
            ))
            if "CONFLICT" in merge_out.upper():
                self._run_cmd(
                    f"git -C {repo_path} merge --abort", timeout=10
                )
                self.chat.notify(
                    f"🚨 合并冲突: {branch} → {main_branch}\n"
                    f"已中止合并，fix 分支保留在本地。\n"
                    f"请手动合并: cd {repo_path} && git merge {branch}\n"
                    f"补丁说明: {verified.patch.description}",
                    "critical",
                )
                return
        except Exception as e:
            self.chat.notify(
                f"🚨 分支操作失败: {e}\n"
                f"fix 分支: {branch}，请手动处理",
                "critical",
            )
            return

        # ── 2. 推送 ──
        self.chat.say("推送代码...", "info")
        try:
            self._run_cmd(f"git -C {repo_path} push", timeout=30)
            self.chat.say(f"✓ 代码已推送 ({commit_sha[:12]})", "success")
        except Exception as e:
            self._run_cmd(
                f"git -C {repo_path} reset --hard ORIG_HEAD", timeout=10
            )
            self.chat.notify(
                f"🚨 git push 失败: {e}\n"
                f"本地已回滚到合并前状态。\n"
                f"fix 分支 {branch} 保留在本地，请手动推送。\n"
                f"补丁说明: {verified.patch.description}",
                "critical",
            )
            return

        # ── 3. 部署 ──
        deploy_cmd = getattr(repo, "deploy_cmd", "")
        if not deploy_cmd:
            self.chat.notify(
                f"⚠️ 代码已推送，但未配置 deploy_cmd，请手动部署。\n"
                f"仓库: {repo_path} | commit: {commit_sha[:12]}\n"
                f"补丁说明: {verified.patch.description}",
                "warning",
            )
            return

        self.chat.say(f"执行部署: {deploy_cmd}", "action")
        try:
            self._run_cmd(deploy_cmd, timeout=300)
            self.chat.say("✓ 部署命令执行完成", "success")
        except Exception as e:
            self.chat.notify(
                f"🚨 部署失败: {e}\n"
                f"代码已推送但未生效，正在自动回滚...",
                "critical",
            )
            self._rollback_deployment(repo, main_branch, commit_sha, f"部署失败: {e}")
            return

        # ── 4. 验证 ──
        self.chat.say("等待服务启动，验证修复效果...", "info")
        self._interruptible_sleep(15)

        observations = self._observe()
        is_normal = True
        if observations:
            assessment = self._assess(observations)
            is_normal = assessment.get("severity") == "normal"

        if is_normal:
            self.chat.notify(
                f"✅ 自动修复成功并已部署！\n"
                f"仓库: {repo.name} | commit: {commit_sha[:12]}\n"
                f"补丁说明: {verified.patch.description}",
                "success",
            )
            try:
                self._close_incident("自动修复成功并通过验证")
            except Exception:
                pass
        else:
            obs_hint = f"\n当前观察: {observations}" if observations else ""
            self.chat.notify(
                f"⚠️ 部署后验证发现问题，正在自动回滚...{obs_hint}",
                "critical",
            )
            self._rollback_deployment(
                repo, main_branch, commit_sha, "部署后验证失败"
            )

        # ── 5. 清理 fix 分支 ──
        try:
            self._run_cmd(
                f"git -C {repo_path} branch -d {branch}", timeout=10
            )
        except Exception:
            pass

    def _rollback_deployment(self, repo, main_branch, original_sha, reason):
        """回滚部署：revert → push → redeploy → 通知人类"""
        repo_path = repo.path
        deploy_cmd = getattr(repo, "deploy_cmd", "")

        # 1. revert + push
        try:
            self._run_cmd(
                f"git -C {repo_path} revert --no-edit {original_sha}", timeout=10
            )
            self._run_cmd(f"git -C {repo_path} push", timeout=30)
            self.chat.say(f"代码已回滚 (revert {original_sha[:12]})", "info")
        except Exception as e:
            self.chat.notify(
                f"🚨 代码回滚失败！需要人工立即介入！\n"
                f"仓库: {repo_path}\n"
                f"失败原因: {e}\n"
                f"原始补丁 commit: {original_sha[:12]}\n"
                f"回滚原因: {reason}",
                "critical",
            )
            return

        # 2. 重新部署回滚后的代码
        if deploy_cmd:
            try:
                self._run_cmd(deploy_cmd, timeout=300)
                self.chat.say("回滚后重新部署完成", "info")
            except Exception as e:
                self.chat.notify(
                    f"🚨 回滚后重新部署也失败了！服务可能异常！\n"
                    f"仓库: {repo_path}\n"
                    f"deploy_cmd: {deploy_cmd}\n"
                    f"错误: {e}\n"
                    f"请立即手动检查服务状态！",
                    "critical",
                )
                return

        # 3. 通知人类
        self.chat.notify(
            f"⚠️ 补丁已自动回滚\n"
            f"原因: {reason}\n"
            f"代码: revert {original_sha[:12]} → 已推送并重新部署\n"
            f"请评估根因并决定是否再次尝试修复。",
            "warning",
        )
