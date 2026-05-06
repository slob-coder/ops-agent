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

from src.i18n import t

logger = logging.getLogger("ops-agent")


class PipelineMixin:
    """完整的 OODA 修复流水线"""

    def _observe(self) -> str:
        """感知：patrol 模式使用分层调度，investigate/incident 模式由 LLM 选择"""
        watchlist = self.notebook.read("config/watchlist.md")

        if self.mode == "patrol":
            # ── patrol: 分层调度，确保 watchlist 命令全覆盖 ──
            self._patrol_round = getattr(self, "_patrol_round", 0) + 1
            commands = self._parse_watchlist_commands(watchlist, self._patrol_round)

            if not commands:
                # fallback: 没有可解析的命令时仍让 LLM 选
                self.chat.progress(t("pipeline.observe_analyzing"))
                recent = self._recent_incidents_summary()
                prompt = self._fill_prompt(
                    "observe",
                    watchlist=watchlist,
                    current_issue=self.current_issue,
                    recent_incidents=recent,
                )
                response = self._ask_llm(prompt, phase="OBSERVE")
                commands = self._extract_commands(response)

            tier_info = t("pipeline.patrol_tier_info", round=self._patrol_round, count=len(commands))
            self.chat.progress(t("pipeline.observe_patrol", info=tier_info))
        else:
            # ── investigate/incident: LLM 围绕当前问题选择 ──
            self.chat.progress(t("pipeline.observe_analyzing"))
            recent = self._recent_incidents_summary()
            prompt = self._fill_prompt(
                "observe",
                watchlist=watchlist,
                current_issue=self.current_issue,
                recent_incidents=recent,
            )
            response = self._ask_llm(prompt, phase="OBSERVE")
            commands = self._extract_commands(response)

        if not commands:
            return ""

        # 执行命令、收集输出
        outputs = []
        max_cmds = self.limits.config.max_observe_commands
        for cmd in commands[:max_cmds]:
            self.chat.cmd_log(cmd)
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
                            t("pipeline.funnel_anomalies", count=len(anomalies)),
                        )
            except Exception as e:
                logger.debug(f"Smart assess_logs in _observe failed: {e}")

        return raw_observations

    def _assess(self, observations: str) -> dict:
        """判断观察结果是否正常"""
        self.chat.progress(t("pipeline.assess_progress"))
        recent = self._recent_incidents_summary()
        silences = self.notebook.read("incidents/silence.yml")

        prompt = self._fill_prompt(
            "assess",
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

        定位策略（按优先级）:
        1. 异常栈反向定位（原有逻辑）
        2. 关键词源码搜索 fallback（stack trace 定位失败时）
        """
        try:
            from src.repair.stack_parser import StackTraceParser
            from src.repair.source_locator import SourceLocator
        except Exception as e:
            logger.debug(f"sprint2 modules import failed: {e}")
            return None, None

        if not text or not self.current_target:
            return None, None

        # 策略 1: 异常栈反向定位
        parsed = None
        try:
            parsed = StackTraceParser().extract_and_parse(text)
        except Exception as e:
            logger.debug(f"stack parse failed: {e}")

        if parsed and parsed.frames:
            try:
                repos = self.current_target.get_source_repos()
            except Exception:
                repos = []
            try:
                result = SourceLocator(repos).locate(parsed.frames)
                if result and result.locations:
                    return result, parsed
            except Exception as e:
                logger.debug(f"source locate failed: {e}")
                return None, parsed
            # stack trace 解析成功但定位失败，返回 parsed 供上层使用
            return None, parsed

        # 策略 2: 关键词源码搜索 fallback
        keywords = self._extract_error_keywords(text)
        if keywords:
            logger.debug(f"stack trace 定位失败，尝试关键词搜索: {keywords}")
            keyword_result = self._search_source_by_keywords(keywords)
            if keyword_result:
                self.chat.trace("DIAGNOSE", f"关键词搜索定位到 {len(keyword_result.locations)} 个源码位置: {keywords}")
                return keyword_result, None

        return None, None

    # ─── 关键词提取与源码搜索 fallback ───

    # 常见错误信息中需要跳过的通用词
    _KEYWORD_STOPWORDS = frozenset({
        "error", "failed", "exception", "does not exist", "not found",
        "invalid", "missing", "denied", "refused", "timeout", "unavailable",
        "null", "none", "undefined", "table", "column", "database", "schema",
        "index", "constraint", "key", "value", "type", "function", "module",
        "the", "a", "an", "is", "are", "was", "were", "has", "have", "had",
        "no", "not", "or", "and", "in", "on", "at", "to", "for", "of", "with",
    })

    def _extract_error_keywords(self, text: str) -> list[str]:
        """从错误信息中提取可用于源码搜索的关键标识词

        策略:
        1. 提取引号内的标识符（如 "account_id" does not exist → account_id）
        2. 提取蛇形命名标识符（含 _ 的长词，如 platform_account_id）
        3. 提取驼峰命名标识符（含大小写混合的长词，如 NullPointerException）
        过滤掉通用停用词和短词（< 3 字符）。
        """
        import re

        if not text:
            return []

        keywords = []

        # 1. 引号内的内容
        for m in re.finditer(r'["\x27`](\w+)["\x27`]', text):
            word = m.group(1)
            if len(word) >= 3 and word.lower() not in self._KEYWORD_STOPWORDS:
                keywords.append(word)

        # 2. 蛇形命名（至少含一个下划线，长度 >= 4）
        for m in re.finditer(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b', text):
            word = m.group(1)
            if word.lower() not in self._KEYWORD_STOPWORDS:
                keywords.append(word)

        # 3. 驼峰命名（大小写混合，长度 >= 5）
        for m in re.finditer(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', text):
            word = m.group(1)
            if word not in self._KEYWORD_STOPWORDS:
                keywords.append(word)

        # 去重并保持顺序，最多 5 个关键词
        seen = set()
        unique = []
        for k in keywords:
            kl = k.lower()
            if kl not in seen:
                seen.add(kl)
                unique.append(k)
        return unique[:5]

    def _search_source_by_keywords(self, keywords: list[str]) -> "LocateResult | None":
        """在 source_repos 中用关键词搜索匹配的源码文件

        返回 LocateResult（复用 SourceLocator 的数据结构），
        定位结果不包含行号精确信息（frame.line 设为 0），
        但包含文件上下文代码片段。

        搜索策略：先搜最具体的关键词（含下划线的标识符），
        再 fallback 到通用词。优先返回匹配更多关键词的文件。
        """
        from src.repair.source_locator import LocateResult, SourceLocation
        from src.repair.stack_parser import StackFrame

        if not self.current_target or not keywords:
            return None

        try:
            repos = self.current_target.get_source_repos()
        except Exception:
            return None

        if not repos:
            return None

        # 代码文件扩展名
        CODE_EXTENSIONS = {
            ".py", ".go", ".java", ".js", ".ts", ".jsx", ".tsx",
            ".rs", ".rb", ".php", ".cs", ".c", ".cpp", ".h", ".hpp",
            ".sql", ".yaml", ".yml", ".toml", ".json",
        }

        locations = []
        max_files = 5
        context_lines = 3  # 匹配行前后各 3 行
        max_render_chars = 2000  # 单个 location 的渲染上限

        # 分层搜索：先搜最具体的关键词（蛇形命名），再搜通用词
        # 蛇形命名（含 _）比通用词（如 postgres）更精确
        specific_keywords = [k for k in keywords if '_' in k]
        general_keywords = [k for k in keywords if '_' not in k]

        for repo in repos:
            if not repo.path or not os.path.isdir(repo.path):
                continue
            if len(locations) >= max_files:
                break

            # 第一轮：只搜具体关键词（如 last_accessed）
            search_keywords = specific_keywords if specific_keywords else keywords
            pattern = "|".join(search_keywords)

            _grep_excludes = (
                "--exclude-dir=.git --exclude-dir=node_modules --exclude-dir=__pycache__ "
                "--exclude-dir=venv --exclude-dir=.venv --exclude-dir=dist --exclude-dir=build "
                "--exclude-dir=target --exclude-dir=.idea --exclude-dir=.vscode "
                "--exclude='*.min.js' --exclude='*.min.css' --exclude='*.lock' "
                "--exclude='*.pb.go' --exclude='package-lock.json' --exclude='yarn.lock'"
            )
            try:
                result = self._run_cmd(
                    f"grep -rn -E '{pattern}' {_grep_excludes} "
                    f"{repo.path} 2>/dev/null | head -30",
                    timeout=15,
                )
                grep_output = str(result)
            except Exception as e:
                logger.debug(f"keyword grep failed for repo {repo.name}: {e}")
                continue

            # 第一轮无结果，fallback 到通用词
            if (not grep_output or grep_output.strip() == "") and general_keywords and specific_keywords:
                pattern2 = "|".join(general_keywords)
                try:
                    result = self._run_cmd(
                        f"grep -rn -E '{pattern2}' {_grep_excludes} "
                        f"{repo.path} 2>/dev/null | head -30",
                        timeout=15,
                    )
                    grep_output = str(result)
                except Exception:
                    continue

            if not grep_output or grep_output.strip() == "":
                continue

            # 解析 grep 输出，按文件分组
            file_matches: dict[str, list[tuple[int, str]]] = {}
            for line in grep_output.splitlines()[:30]:
                # 格式: /path/to/file.go:42:content
                m = re.match(r'(.+?):(\d+):(.*)', line)
                if m:
                    fpath, lineno, content = m.group(1), int(m.group(2)), m.group(3)
                    if fpath not in file_matches:
                        file_matches[fpath] = []
                    file_matches[fpath].append((lineno, content))

            # 为每个匹配文件构建 SourceLocation
            for fpath, matches in file_matches.items():
                if len(locations) >= max_files:
                    break

                # 读取文件上下文（取第一个匹配行附近）
                first_match_line = matches[0][0]
                try:
                    with open(fpath, "r", errors="replace") as f:
                        all_lines = f.readlines()
                except Exception:
                    continue

                # 上下文范围
                start = max(0, first_match_line - context_lines - 1)
                end = min(len(all_lines), first_match_line + context_lines)

                before_lines = all_lines[start:first_match_line - 1]
                target_line = all_lines[first_match_line - 1] if first_match_line <= len(all_lines) else ""
                after_lines = all_lines[first_match_line:end]

                # 汇总所有匹配行号（供 LLM 快速定位）
                match_summary = ", ".join(f"L{ln}" for ln, _ in matches)

                frame = StackFrame(
                    file=os.path.relpath(fpath, repo.path),
                    line=first_match_line,
                    function="",
                    language=repo.language or "",
                )
                loc = SourceLocation(
                    frame=frame,
                    local_file=fpath,
                    repo_name=repo.name,
                    repo_path=repo.path,
                    context_before="".join(before_lines).rstrip("\n"),
                    target_line=target_line.rstrip(),
                    context_after="".join(after_lines).rstrip("\n"),
                    function_definition=f"(关键词搜索定位: {match_summary})",
                    start_line=start + 1,
                )
                locations.append(loc)

        if not locations:
            return None

        return LocateResult(locations=locations)

    def _diagnose(self, assessment: dict, observations: str) -> dict:
        """深度诊断"""
        self.chat.progress(t("pipeline.diagnose_progress"))
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
                t("pipeline.diagnose_source_located", repo=top.repo_name, file=os.path.basename(top.local_file), line=top.frame.line)
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
            source_locations=source_text,
            project_map=project_map or "（无项目地图）",
        )

        response = self._ask_llm(prompt, phase="DIAGNOSE")
        logger.info(f"diagnose LLM response ({len(response)} chars):\n{response[:3000]}")
        result = self._parse_diagnosis(response)

        # 解析失败时重试一次
        if result.get("hypothesis") == "JSON 解析失败，无法提取诊断":
            logger.warning("diagnose JSON 解析失败，重试一次")
            self.chat.say(t("pipeline.diagnose_json_retry"), "warning")
            retry_prompt = prompt + "\n\n[重要提醒] 上次你的输出不是合法 JSON，请**只输出 JSON 对象**，不要加任何解释文字。确保 JSON 完整，不要截断。"
            response = self._ask_llm(retry_prompt, phase="DIAGNOSE_RETRY")
            logger.info(f"diagnose LLM retry response ({len(response)} chars):\n{response[:3000]}")
            result = self._parse_diagnosis(response)

        # 屏幕只显示结论
        conf = result.get("confidence", 0)
        rtype = result.get("type", "unknown")
        hypothesis = result.get("hypothesis", "")
        self.chat.progress(t("pipeline.diagnose_confidence", conf=conf, rtype=rtype, hypothesis=hypothesis))

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

        self.chat.say(t("pipeline.patch_bug_detected"), "info")
        try:
            verified = self.patch_loop.run(
                diagnosis=diagnosis,
                locations=result.locations[:self.limits.config.max_source_locations],
                repo=repo,
                incident_id=self.current_incident or "incident",
            )
        except Exception as e:
            logger.exception("patch loop crashed")
            self.chat.say(t("pipeline.patch_loop_error", error=e), "warning")
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
                t("pipeline.patch_verified", summary=verified.result.short_summary()), "success"
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
            self.chat.say(t("pipeline.patch_failed"), "warning")

    def _plan(self, diagnosis: dict):
        """制定修复方案（支持 COLLECT_MORE 多轮收集上下文）"""
        max_plan_rounds = self.limits.config.max_plan_rounds
        gap_results = ""
        plan_history: list[dict] = []  # 进展检测：记录每轮的 next_action + gaps 摘要

        for plan_round in range(1, max_plan_rounds + 1):
            self.chat.progress(t("pipeline.plan_progress", round=plan_round))

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

            # 修复点 3: source_locations 为空时，用诊断关键词搜索源码作为 fallback
            code_search_text = "（无）"
            if source_text == "（无）":
                code_search_text = self._search_source_snippets_from_diagnosis(diagnosis)
                if code_search_text != "（无）":
                    self.chat.trace("PLAN", f"源码定位为空，关键词搜索 fallback 找到相关代码")

            # 修复点 4: 构造已确认事实清单，防止 LLM 反复 COLLECT_MORE 确认已知信息
            confirmed_facts = self._build_confirmed_facts(diagnosis, code_search_text)

            prompt = self._fill_prompt(
                "plan",
                diagnosis=str(diagnosis),
                matched_playbook=playbook or "（无匹配的 Playbook）",
                build_deploy_context=build_deploy_context,
                project_map=project_map or "（无项目地图）",
                source_locations=source_text,
                code_search_results=code_search_text,
                confirmed_facts=confirmed_facts,
                gap_results=gap_results or "（无）",
            )

            response = self._ask_llm(prompt, phase=f"PLAN_R{plan_round}")
            plan = self._parse_plan(response)

            # 解析失败时重试一次
            if plan is None:
                logger.warning("plan 解析未返回有效计划，重试一次")
                self.chat.say(t("pipeline.plan_json_retry"), "warning")
                retry_prompt = prompt + "\n\n[重要提醒] 上次你的输出不是合法 JSON，请**只输出 JSON 对象**，不要加任何解释文字或代码查看请求。确保 JSON 完整，不要截断。"
                response = self._ask_llm(retry_prompt, phase=f"PLAN_R{plan_round}_RETRY")
                plan = self._parse_plan(response)
                if plan is None:
                    continue

            # 修复点 5: 进展检测——连续 COLLECT_MORE 且 gaps 描述相似 → 强制出方案
            plan_history.append({
                "action": plan.next_action,
                "gaps_desc": [g.get("description", "")[:50] for g in (plan.gaps or [])],
            })
            if self._detect_plan_stagnation(plan_history):
                self.chat.say(t("pipeline.plan_stagnation"), "warning")
                # 在 prompt 中追加强制指令，重新调用 LLM
                force_prompt = prompt + (
                    "\n\n[重要] 你已经收集了足够的上下文信息，不要再请求更多信息。"
                    "基于已有信息直接制定修复方案（next_action=READY）。"
                    "如果确实无法修复，设 next_action=ESCALATE。"
                )
                response = self._ask_llm(force_prompt, phase=f"PLAN_R{plan_round}_FORCE")
                plan = self._parse_plan(response)
                if plan is None:
                    # 强制也失败，最后一次机会
                    continue
                # 如果 LLM 仍然 COLLECT_MORE，强制转为 ESCALATE
                if plan.next_action == "COLLECT_MORE":
                    plan.next_action = "ESCALATE"

            # COLLECT_MORE: 执行 gap 命令，收集上下文后重新规划
            if plan.next_action == "COLLECT_MORE" and plan.gaps and plan_round < max_plan_rounds:
                self.chat.say(t("pipeline.plan_collecting", round=plan_round, count=len(plan.gaps)), "info")
                new_results = self._collect_gap_commands(plan.gaps)
                if gap_results:
                    gap_results = gap_results + "\n\n---\n\n" + new_results
                else:
                    gap_results = new_results
                continue

            # READY 或 ESCALATE 或最后一轮 → 退出循环
            if plan and plan.next_action == "ESCALATE":
                self.chat.say(t("pipeline.plan_beyond_auto"), "warning")
                return None

            break

        if plan:
            # 防御：无有效 steps（READY 空 steps，或 COLLECT_MORE 超轮次）
            if not plan.steps:
                if plan.next_action == "COLLECT_MORE":
                    # 超过 max_plan_rounds 仍然 COLLECT_MORE → ESCALATE
                    logger.warning("plan 连续 COLLECT_MORE 超出轮次限制，升级为人工介入")
                    self.chat.say(t("pipeline.plan_insufficient"), "warning")
                    return None
                elif plan.next_action == "READY" and plan.verify_steps:
                    # 纯验证计划：服务已自愈，跳过执行直接验证
                    logger.info("plan READY 无修复 steps 但有 verify_steps，跳过执行进入验证")
                    self.chat.say(t("pipeline.plan_self_healed"), "info")
                elif plan.next_action == "READY":
                    logger.warning("plan READY 但无有效 steps 也没有 verify_steps")
                    self.chat.say(t("pipeline.plan_no_steps"), "warning")
                    if plan.gaps:
                        plan.next_action = "COLLECT_MORE"
                    else:
                        return None
            self.chat.say(
                t("pipeline.plan_summary", action=plan.action, trust_level=plan.trust_level),
                "action",
            )
        return plan

    # ─── Plan 阶段辅助方法 ───

    def _search_source_snippets_from_diagnosis(self, diagnosis: dict) -> str:
        """修复点 3: 从诊断结论提取关键词，在 source_repos 中搜索相关代码

        当 diagnose 阶段的源码定位失败时，plan 阶段的 fallback。
        返回搜索结果文本，无结果时返回 "（无）"。
        """
        if not self.current_target:
            return "（无）"

        # 从诊断中提取搜索关键词
        search_parts = []
        for key in ("hypothesis", "facts"):
            val = diagnosis.get(key, "")
            if val:
                search_parts.append(val)
        # 也从 _last_error_text 提取
        if hasattr(self, "_last_error_text") and self._last_error_text:
            search_parts.append(self._last_error_text[:500])

        combined_text = "\n".join(search_parts)
        keywords = self._extract_error_keywords(combined_text)

        if not keywords:
            return "（无）"

        # 复用 diagnose 阶段的关键词搜索
        result = self._search_source_by_keywords(keywords)
        if not result or not result.locations:
            return "（无）"

        # 渲染搜索结果，添加明确的 fallback 标记
        parts = [f"（以下通过关键词搜索定位，关键词: {', '.join(keywords)}）"]
        for loc in result.locations[:5]:
            parts.append(loc.render(max_chars=1500))
        rendered = "\n\n".join(parts)

        # 限制总长度
        if len(rendered) > 4000:
            rendered = rendered[:4000] + "\n... (truncated)"

        return rendered

    def _build_confirmed_facts(self, diagnosis: dict, code_search_text: str) -> str:
        """修复点 4: 从诊断结论和代码搜索结果中构造已确认事实清单

        防止 LLM 在 COLLECT_MORE 中反复请求查看已确认的信息。
        """
        facts = []

        # 诊断事实
        diag_facts = diagnosis.get("facts", "")
        if diag_facts:
            facts.append(f"- 现象: {diag_facts}")

        # 诊断结论
        hypothesis = diagnosis.get("hypothesis", "")
        if hypothesis:
            facts.append(f"- 根因: {hypothesis}")

        # 诊断类型和置信度
        dtype = diagnosis.get("type", "")
        conf = diagnosis.get("confidence", 0)
        if dtype:
            facts.append(f"- 类型: {dtype} (把握度 {conf}%)")

        # 代码搜索找到的文件
        if code_search_text != "（无）":
            # 提取文件名
            files = re.findall(r'### (\S+)', code_search_text)
            if files:
                facts.append(f"- 需修改的文件: {', '.join(files[:5])}")

        return "\n".join(facts) if facts else "（无）"

    def _detect_plan_stagnation(self, plan_history: list[dict]) -> bool:
        """修复点 5: 检测 Plan 阶段是否在原地打转

        判断条件: 连续 2 轮 COLLECT_MORE 且 gaps 描述有重叠。
        """
        if len(plan_history) < 2:
            return False

        last_two = plan_history[-2:]
        if last_two[0]["action"] != "COLLECT_MORE" or last_two[1]["action"] != "COLLECT_MORE":
            return False

        # 检查 gaps 描述是否有重叠
        gaps_a = set(desc.lower().strip() for desc in last_two[0]["gaps_desc"] if desc.strip())
        gaps_b = set(desc.lower().strip() for desc in last_two[1]["gaps_desc"] if desc.strip())

        if not gaps_a or not gaps_b:
            return False

        # 有任意重叠即判定为打转
        overlap = gaps_a & gaps_b
        if overlap:
            logger.debug(f"Plan stagnation detected: overlapping gaps: {overlap}")
            return True

        # 模糊匹配：描述的前 20 字符相同也算重叠
        prefixes_a = {d[:20] for d in gaps_a if len(d) >= 10}
        prefixes_b = {d[:20] for d in gaps_b if len(d) >= 10}
        if prefixes_a & prefixes_b:
            logger.debug(f"Plan stagnation detected: overlapping gap prefixes")
            return True

        return False

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

            self.chat.cmd_log(f"步骤{i}: {cmd}")
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
                    t("pipeline.step_timeout_retry", step=i, attempt=attempt, max=max_timeout_retries + 1)
                )

            results.append(f"STEP {i}: {cmd}\n{str(result)}")

            # 判断步骤是否失败：先检查 tolerate_exit_codes，再检查 success
            tolerate_codes = tuple(step.get("tolerate_exit_codes", []))
            step_failed = not result.success and not result.is_tolerable(tolerate_codes)
            if step_failed:
                logger.warning(f"Step {i} failed: {cmd} (exit={result.returncode})")
                all_success = False
                break

            if wait > 0:
                self.chat.progress(t("pipeline.step_complete", step=i, wait=wait))
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

            self.chat.cmd_log(f"回滚{i}: {cmd}")
            self.chat.trace("ROLLBACK", f"STEP {i}: {cmd} ({purpose})")
            result = self._run_cmd(cmd, timeout=30)
            results.append(f"ROLLBACK STEP {i}: {cmd}\n{str(result)}")

            if not result.success:
                logger.warning(f"Rollback step {i} failed: {cmd}")
                # 回滚失败继续执行后续步骤，尽力恢复

        return "\n\n".join(results)

    def _verify_with_retry(self, plan, before_state: str,
                           max_retries: int = 0, interval: int = 0) -> bool:
        """验证修复效果 — 兼容旧接口，内部委托给 _verify_with_strategy"""
        result = self._verify_with_strategy(plan, before_state)
        return result.passed

    def _verify_with_strategy(self, plan, before_state: str) -> "VerifyResult":
        """验证修复效果 — 支持即时验证 + 连续观察

        Phase 1: 即时验证（支持 delay_seconds + 重试）
        Phase 2: 连续观察（如果 plan 或 verify prompt 要求）
        """
        from src.safety.trust import VerifyResult

        cfg = self.limits.config
        max_retries = cfg.verify_max_retries
        interval = cfg.verify_default_interval

        # ── Phase 1: 即时验证 ──
        # 执行有 delay_seconds 的步骤前先等待
        max_delay = max(
            (s.get("delay_seconds", 0) for s in plan.verify_steps),
            default=0,
        )
        if max_delay > 0:
            self.chat.progress(t("pipeline.verify_waiting", delay=max_delay))
            self._interruptible_sleep(max_delay)

        result = VerifyResult(result="UNCERTAIN")
        for attempt in range(1, max_retries + 1):
            self.chat.progress(t("pipeline.verify_progress", attempt=attempt, max=max_retries))

            after_state = self._targeted_observe(plan)

            prompt = self._fill_prompt(
                "verify",
                action_result=plan.action,
                before_state=before_state[:self.ctx_limits.verify_state_chars],
                after_state=after_state[:self.ctx_limits.verify_state_chars],
                verification_criteria=plan.verification,
            )
            response = self._ask_llm(prompt, phase="VERIFY")
            result = self._parse_verify_response(response)

            self.chat.trace(
                "VERIFY",
                f"attempt={attempt} result={result.result} "
                f"watch={result.continue_watch} watch_duration={result.watch_duration}\n"
                f"{response[:self.ctx_limits.verify_response_trace_chars]}",
            )

            if result.passed:
                break

            if result.failed and attempt < max_retries:
                self.chat.progress(t("pipeline.verify_retry", interval=interval))
                if self.current_incident:
                    self.notebook.append_to_incident(
                        self.current_incident,
                        f"\n### 验证重试 {attempt}/{max_retries}: 未通过\n",
                    )
                self._interruptible_sleep(interval)

        # ── Phase 2: 连续观察 ──
        # 触发条件：plan 中有 watch 步骤，或 verify prompt 建议继续观察
        needs_watch = plan.has_watch_steps or result.needs_watch
        if needs_watch and not result.failed:
            # 确定观察参数
            if plan.has_watch_steps:
                # 优先用 plan 中声明的参数
                watch_step = next(s for s in plan.verify_steps if s.get("watch"))
                watch_duration = min(
                    watch_step.get("watch_duration", 300),
                    cfg.watch_max_duration,
                )
                watch_interval = watch_step.get("watch_interval", cfg.watch_default_interval)
                watch_converge = watch_step.get("watch_converge", cfg.watch_required_consecutive)
            else:
                # 用 verify prompt 的建议
                watch_duration = min(
                    result.watch_duration or 300,
                    cfg.watch_max_duration,
                )
                watch_interval = result.watch_interval or min(
                    cfg.watch_default_interval,
                    max(5, watch_duration // 5),  # 自动调整间隔：至少5次采样
                )
                watch_converge = cfg.watch_required_consecutive

            result = self._watch_verify(
                plan, watch_duration, watch_interval, watch_converge,
            )

        return result

    def _watch_verify(self, plan, duration: int, interval: int,
                      required_consecutive: int) -> "VerifyResult":
        """连续观察：在 duration 秒内每隔 interval 秒采样一次

        收敛条件：连续 required_consecutive 次验证通过
        恶化检测：如果状态比修复前更差，立即返回 FAILED
        """
        import math
        from src.safety.trust import VerifyResult

        cfg = self.limits.config
        # 限制最大观察时长
        duration = min(duration, cfg.watch_max_duration)
        # 限制最短采样间隔（避免过于密集）
        interval = max(interval, 5)

        checks = math.ceil(duration / interval)
        consecutive_pass = 0
        watch_log = []

        self.chat.progress(t("pipeline.watch_enter", duration=duration, interval=interval, required=required_consecutive))

        for i in range(1, checks + 1):
            if i > 1:  # 第一次不需要等待
                self._interruptible_sleep(interval)

            after_state = self._targeted_observe(plan)
            # 用 expect 做轻量检查（不走完整 LLM）
            passed = self._quick_verify_check(plan, after_state)
            watch_log.append(f"  采样 {i}/{checks}: {'✅' if passed else '❌'}")

            if passed:
                consecutive_pass += 1
                if consecutive_pass >= required_consecutive:
                    self.chat.progress(
                        t("pipeline.watch_converged", count=consecutive_pass)
                    )
                    evidence = "\n".join(watch_log)
                    if self.current_incident:
                        self.notebook.append_to_incident(
                            self.current_incident,
                            f"\n## 连续观察通过\n{evidence}\n",
                        )
                    return VerifyResult(
                        result="SUCCESS",
                        evidence=f"连续观察{duration}s，连续{consecutive_pass}次验证通过",
                    )
            else:
                consecutive_pass = 0
                # 检测是否恶化（比之前状态明显更差）
                if self._is_degrading(after_state):
                    self.chat.say(t("pipeline.watch_degrading"), "warning")
                    if self.current_incident:
                        self.notebook.append_to_incident(
                            self.current_incident,
                            f"\n## 连续观察: 状态恶化\n采样 {i}/{checks}\n",
                        )
                    return VerifyResult(
                        result="FAILED",
                        evidence=f"连续观察期间状态恶化 (采样 {i}/{checks})",
                        rollback_needed=True,
                        rollback_reason="观察期间状态恶化",
                    )

        # 超时未收敛
        evidence = "\n".join(watch_log)
        if self.current_incident:
            self.notebook.append_to_incident(
                self.current_incident,
                f"\n## 连续观察超时\n观察{duration}s未收敛\n{evidence}\n",
            )
        return VerifyResult(
            result="UNCERTAIN",
            evidence=f"连续观察{duration}s未收敛 ({checks}次采样)",
        )

    def _quick_verify_check(self, plan, after_state: str) -> bool:
        """轻量验证：用 verify_steps 的 expect 做字符串匹配

        不走 LLM，直接检查命令输出是否包含期望字符串。
        所有 expect 都匹配才算通过，没有 expect 则默认通过。
        """
        expects = [s.get("expect", "") for s in plan.verify_steps if s.get("expect")]
        if not expects:
            # 没有 expect，默认通过（交由 LLM 判断）
            return True

        for exp in expects:
            if exp and str(exp).lower() not in after_state.lower():
                return False
        return True

    def _is_degrading(self, after_state: str) -> bool:
        """检测状态是否恶化

        简单启发式：检查是否出现明显的恶化信号。
        不做 LLM 调用，纯字符串匹配。
        """
        degradation_signals = [
            "connection refused",
            "no route to host",
            "kernel panic",
            "out of memory",
            "oom-killer",
            "segmentation fault",
            "core dumped",
            "critical error",
            "fatal error",
            "service failed",
            "failed with result",
        ]
        state_lower = after_state.lower()
        return any(sig in state_lower for sig in degradation_signals)

    def _parse_verify_response(self, response: str) -> "VerifyResult":
        """解析 verify prompt 的 LLM 输出为 VerifyResult — JSON 结构化解析"""
        from src.safety.trust import VerifyResult

        data = self._extract_json(response)

        if data and isinstance(data, dict):
            # JSON 解析成功
            result_str = str(data.get("result", "UNCERTAIN")).upper()
            if result_str not in ("SUCCESS", "FAILED", "UNCERTAIN"):
                result_str = "UNCERTAIN"

            return VerifyResult(
                result=result_str,
                evidence=str(data.get("evidence", "")),
                continue_watch=bool(data.get("continue_watch", False)),
                watch_duration=int(data.get("watch_duration", 0)),
                watch_interval=int(data.get("watch_interval", 0)),
                rollback_needed=bool(data.get("rollback_needed", False)),
                rollback_reason=str(data.get("rollback_reason", "")),
            )

        # Fallback: JSON 解析失败时退回关键词匹配（兼容旧 prompt）
        import re
        upper = response.upper()

        if "SUCCESS" in upper and "FAILED" not in upper:
            result_str = "SUCCESS"
        elif "FAILED" in upper:
            result_str = "FAILED"
        else:
            result_str = "UNCERTAIN"

        continue_watch = False
        cw_match = re.search(r'CONTINUE_WATCH:\s*(YES|NO)', upper)
        if cw_match and cw_match.group(1) == "YES":
            continue_watch = True

        watch_duration = 0
        wd_match = re.search(r'WATCH_DURATION:\s*(\d+)', upper)
        if wd_match:
            watch_duration = int(wd_match.group(1))

        watch_interval = 0
        wi_match = re.search(r'WATCH_INTERVAL:\s*(\d+)', upper)
        if wi_match:
            watch_interval = int(wi_match.group(1))

        evidence = ""
        ev_match = re.search(r'EVIDENCE:\s*(.+?)(?:\n|$)', response, re.IGNORECASE)
        if ev_match:
            evidence = ev_match.group(1).strip()

        rollback_needed = False
        rb_match = re.search(r'ROLLBACK_NEEDED:\s*(YES|NO)', upper)
        if rb_match and rb_match.group(1) == "YES":
            rollback_needed = True

        rollback_reason = ""
        rbr_match = re.search(r'ROLLBACK_REASON:\s*(.+?)(?:\n|$)', response, re.IGNORECASE)
        if rbr_match:
            rollback_reason = rbr_match.group(1).strip()

        return VerifyResult(
            result=result_str,
            evidence=evidence,
            continue_watch=continue_watch,
            watch_duration=watch_duration,
            watch_interval=watch_interval,
            rollback_needed=rollback_needed,
            rollback_reason=rollback_reason,
        )

    def _reflect(self):
        """复盘总结"""
        if not self.current_incident:
            return

        self.chat.progress(t("pipeline.reflect_progress"))
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

        # 洞察持久化：从复盘中提取洞察推入 DurabilityGate
        if hasattr(self.notebook, "process_insight"):
            try:
                insight = self._build_reflect_insight(response)
                if insight:
                    level = self.notebook.process_insight(insight)
                    if level:
                        self.chat.trace("REFLECT", f"洞察持久化: {insight.id} → {level.value}")
            except Exception as e:
                logger.debug(f"Smart process_insight in _reflect failed: {e}")

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

    def _build_reflect_insight(self, reflect_text: str):
        """从复盘文本构造 Insight 对象。仅 Smart 模式调用。

        smart_notebook 未安装时返回 None，不影响基础流程。
        """
        try:
            from smart_notebook.core.types import Insight
        except ImportError:
            return None

        import hashlib
        slug = re.sub(r'[^a-z0-9\u4e00-\u9fff]', '',
                      reflect_text[:40].lower().replace(' ', '-'))
        h = hashlib.md5(reflect_text.encode()).hexdigest()[:6]
        text_lower = reflect_text.lower()
        if any(k in text_lower for k in ("误报", "false positive", "fp")):
            cat = "false-positives"
        elif any(k in text_lower for k in ("patch", "补丁", "代码修改")):
            cat = "patch-quality"
        elif any(k in text_lower for k in ("命令", "tool", "ssh", "docker")):
            cat = "tool-usage"
        else:
            cat = "diagnosis-patterns"

        return Insight(
            id=f"{slug}-{h}" if slug else h,
            category=cat,
            content=reflect_text[:500],
            evidence_links=[f"[[incidents/active/{self.current_incident}]]"],
        )

    def _close_incident(self, summary: str, skip_reflect: bool = False):
        """关闭并归档 Incident，始终执行反思（除非显式跳过）"""
        if self.current_incident:
            # 关闭前执行反思，即使是中断/否决也要沉淀经验
            if not skip_reflect:
                try:
                    self._reflect()
                except Exception as e:
                    logger.warning(f"reflect on close failed: {e}")
            self._emit_audit("incident_closed", incident=self.current_incident, summary=summary[:200])
            self.notebook.close_incident(self.current_incident, summary)
            self.current_incident = None
            self.chat._trace_file = "patrol"  # trace 恢复到默认
            self.limits.record_incident_end()
            # 成长统计打点
            if hasattr(self, "_weekly_stats"):
                self._weekly_stats["fixed"] += 1
                self._weekly_stats["total"] += 1

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
            self.chat.cmd_log(cmd)
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
                    self.chat.cmd_log(cmd)
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
        self.chat.say(t("pipeline.deploy_merging", branch=branch, main=main_branch), "info")
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
                    t("pipeline.deploy_merge_conflict", branch=branch, main=main_branch, path=repo_path, desc=verified.patch.description),
                    "critical",
                )
                return
        except Exception as e:
            self.chat.notify(
                t("pipeline.deploy_branch_failed", error=e, branch=branch),
                "critical",
            )
            return

        # ── 2. 推送 ──
        self.chat.say(t("pipeline.deploy_pushing"), "info")
        try:
            self._run_cmd(f"git -C {repo_path} push", timeout=30)
            self.chat.say(t("pipeline.deploy_pushed", sha=commit_sha[:12]), "success")
        except Exception as e:
            self._run_cmd(
                f"git -C {repo_path} reset --hard ORIG_HEAD", timeout=10
            )
            self.chat.notify(
                t("pipeline.deploy_push_failed", error=e, branch=branch, desc=verified.patch.description),
                "critical",
            )
            return

        # ── 3. 部署 ──
        deploy_cmd = getattr(repo, "deploy_cmd", "")
        if not deploy_cmd:
            self.chat.notify(
                t("pipeline.deploy_no_cmd", path=repo_path, sha=commit_sha[:12], desc=verified.patch.description),
                "warning",
            )
            return

        self.chat.say(t("pipeline.deploy_exec", cmd=deploy_cmd), "action")
        try:
            self._run_cmd(deploy_cmd, timeout=300)
            self.chat.say(t("pipeline.deploy_done"), "success")
        except Exception as e:
            self.chat.notify(
                t("pipeline.deploy_failed", error=e),
                "critical",
            )
            self._rollback_deployment(repo, main_branch, commit_sha, t("pipeline.deploy_failed", error=e))
            return

        # ── 4. 验证 ──
        self.chat.say(t("pipeline.deploy_verify_wait"), "info")
        self._interruptible_sleep(15)

        observations = self._observe()
        is_normal = True
        if observations:
            assessment = self._assess(observations)
            is_normal = assessment.get("status") == "NORMAL"

        if is_normal:
            self.chat.notify(
                t("pipeline.deploy_auto_success", repo=repo.name, sha=commit_sha[:12], desc=verified.patch.description),
                "success",
            )
            try:
                self._close_incident(t("pipeline.close_auto_fix"))
            except Exception:
                pass
        else:
            obs_hint = f"\n当前观察: {observations}" if observations else ""
            self.chat.notify(
                t("pipeline.deploy_post_verify_issue", hint=obs_hint),
                "critical",
            )
            self._rollback_deployment(
                repo, main_branch, commit_sha, "部署后验证失败"  # internal reason, kept as-is
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
            self.chat.say(t("pipeline.deploy_rollback", sha=original_sha[:12]), "info")
        except Exception as e:
            self.chat.notify(
                t("pipeline.deploy_rollback_failed", path=repo_path, error=e, sha=original_sha[:12], reason=reason),
                "critical",
            )
            return

        # 2. 重新部署回滚后的代码
        if deploy_cmd:
            try:
                self._run_cmd(deploy_cmd, timeout=300)
                self.chat.say(t("pipeline.deploy_redeploy_done"), "info")
            except Exception as e:
                self.chat.notify(
                    t("pipeline.deploy_redeploy_failed", path=repo_path, cmd=deploy_cmd, error=e),
                    "critical",
                )
                return

        # 3. 通知人类
        self.chat.notify(
            t("pipeline.deploy_rollback_notify", reason=reason, sha=original_sha[:12]),
            "warning",
        )
