"""
人类交互 Mixin — 指令处理、自由对话、协作排查
"""

import os
import re
import time
import logging
from pathlib import Path
from datetime import datetime

from src.i18n import t
from src.infra.llm import LLMDegraded

logger = logging.getLogger("ops-agent")


class HumanInteractionMixin:
    """处理所有人类指令、自由对话、协作排查模式"""

    def _handle_human_message(self, msg: str):
        """处理人类的消息"""
        lower = msg.lower().strip()

        # ═══ 控制指令 ═══

        if lower in ("quit", "exit", "bye", ":q"):
            self.chat.say(t("human.bye"), "info")
            self._running = False
            return

        if lower in ("help", "?", "h"):
            self._show_help()
            return

        if lower == "status":
            self._report_status()
            return

        if lower in ("new", "clear chat", "清除对话"):
            self._free_chat_history.clear()
            self.chat.say(t("human.clear_chat"), "info")
            return

        if lower == "pause":
            self.paused = True
            self.chat.say(t("human.paused"), "info")
            return

        if lower == "resume":
            self.paused = False
            self.chat.say(t("human.resumed"), "success")
            return

        if lower == "stop":
            if self.mode != self.PATROL:
                self.mode = self.PATROL
                self.current_issue = ""
                self.chat.say(t("human.stopped"), "info")
            else:
                self.chat.say(t("human.already_patrol"), "info")
            return

        if lower == "readonly on":
            self.readonly = True
            self.chat.say(t("human.readonly_on"), "info")
            return

        if lower == "readonly off":
            self.readonly = False
            self.chat.say(t("human.readonly_off"), "info")
            return

        if lower in ("clear silence", "unmute", "clear-silence"):
            n = len(self._issue_fingerprints)
            self._issue_fingerprints.clear()
            self.chat.say(t("human.silence_cleared", n=n), "info")
            return

        if lower in ("show silence", "silence"):
            if not self._issue_fingerprints:
                self.chat.say(t("human.no_silence"), "info")
            else:
                now_ts = time.time()
                lines = [t("human.silence_header", count=len(self._issue_fingerprints), window=self._silence_window_seconds)]
                for fp, ts in sorted(self._issue_fingerprints.items(), key=lambda x: x[1], reverse=True):
                    remaining = max(0, int(self._silence_window_seconds - (now_ts - ts)))
                    lines.append(t("human.silence_item", fp=fp, remaining=remaining))
                self.chat.say("\n".join(lines), "info")
            return

        # Sprint 7-8: 误报标记 — 记录到 Smart FP tracker
        if lower.startswith("fp ") or lower.startswith("误报 ") or lower.startswith("false-positive "):
            pattern = msg.split(None, 1)[1].strip() if len(msg.split(None, 1)) > 1 else ""
            if not pattern:
                self.chat.say(
                    t("human.fp_usage"),
                    "info",
                )
                return
            if hasattr(self.notebook, "record_fp_rejection"):
                result = self.notebook.record_fp_rejection(
                    pattern,
                    incident_path=self.current_incident or "",
                    context=f"human marked at {datetime.now().isoformat()}",
                )
                if result:
                    self.chat.say(t("human.fp_recorded", pattern=pattern), "info")
                    # 成长统计打点
                    if hasattr(self, "_weekly_stats"):
                        self._weekly_stats["fp"] += 1
                else:
                    self.chat.say(t("human.fp_record_failed", pattern=pattern), "warning")
            else:
                self.chat.say(t("human.fp_not_available"), "info")
            return

        # ═══ Smart 成长命令 ═══

        if lower in ("scorecard", "growth", "成长"):
            self._show_scorecard()
            return

        if lower in ("trust", "信任"):
            self._show_trust_level()
            return

        # ═══ 自修复命令 ═══
        if lower.startswith("self-fix") or lower.startswith("selffix"):
            # 提取描述部分
            parts = msg.split(None, 1)
            description = parts[1].strip() if len(parts) > 1 else ""
            if not description:
                self.chat.say(
                    t("human.selffix_usage"),
                    "info"
                )
                return
            self._run_self_repair(description)
            return

        # ═══ 多目标管理指令 ═══

        if lower in ("targets", "list targets", "lt"):
            lines = [t("human.targets_header")]
            for t_item in self.targets:
                marker = t("human.target_current") if t_item.name == self.current_target.name else ""
                lines.append(t("human.target_item", name=t_item.name, mode=t_item.mode, desc=t_item.description or '-', marker=marker))
            self.chat.say("\n".join(lines))
            return

        if lower.startswith("switch "):
            name = msg[7:].strip()
            if self._switch_target(name):
                self.chat.say(t("human.switch_ok", name=name), "success")
                # 重置目标轮询索引,让下次 round-robin 从这里开始
                for i, t in enumerate(self.targets):
                    if t.name == name:
                        self._target_index = i
                        break
            else:
                names = ", ".join(t_item.name for t_item in self.targets)
                self.chat.say(t("human.unknown_target", name=name, names=names), "warning")
            return

        # ═══ 限制和安全指令 ═══

        if lower == "limits":
            s = self.limits.status()
            lines = [t("human.limits_header")]
            lines.append(t("human.limits_actions", used_hour=s['actions_last_hour'], max_hour=s['max_actions_per_hour'], used_day=s['actions_last_day']))
            lines.append(t("human.limits_incidents", active=s['active_incidents'], max=s['max_concurrent']))
            lines.append(t("human.limits_tokens", used=s['tokens_last_hour'], budget=s['tokens_per_hour_budget']))
            if s['in_cooldown']:
                lines.append(t("human.limits_cooldown", remaining=s['cooldown_remaining']))
            self.chat.say("\n".join(lines))
            return

        if lower == "freeze":
            self.emergency.trigger(t("pipeline.human_manual_trigger"))
            self.readonly = True
            self.chat.say(t("human.freeze_msg"), "critical")
            return

        if lower == "unfreeze":
            self.emergency.clear()
            self.readonly = False
            self.chat.say(t("human.unfreeze_msg"), "success")
            return

        # ═══ Notebook 浏览指令 ═══

        if lower in ("list playbook", "list playbooks", "lp"):
            files = self.notebook.list_dir("playbook")
            if files:
                self.chat.say(t("human.playbook_list", items="\n".join(f"   • {f}" for f in files)))
            else:
                self.chat.say(t("human.playbook_empty"))
            return

        if lower in ("list trace", "list traces", "lt"):
            trace_dir = self.notebook.path / "trace"
            if not trace_dir.exists():
                self.chat.say(t("human.trace_empty"))
                return
            files = sorted(trace_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not files:
                self.chat.say(t("human.trace_empty"))
                return
            lines = []
            for f in files[:20]:
                size_kb = f.stat().st_size / 1024
                mtime = f.stat().st_mtime
                from datetime import datetime
                ts = datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")
                lines.append(t("human.trace_item", name=f.name, size=size_kb, ts=ts))
            header = t("human.trace_header")
            if len(files) > 20:
                header = t("human.trace_header_limited", shown=20, total=len(files))
            self.chat.say(f"{header}：\n" + "\n".join(lines))
            return

        if lower in ("list incidents", "li"):
            active = self.notebook.list_dir("incidents/active")
            archive = self.notebook.list_dir("incidents/archive")
            msg_parts = []
            if active:
                msg_parts.append(t("human.incidents_active", items="\n".join(f"   • {f}" for f in active)))
            else:
                msg_parts.append(t("human.incidents_no_active"))
            if archive:
                recent = archive[-5:]
                msg_parts.append(t("human.incidents_archive", items="\n".join(f"   • {f}" for f in recent)))
            self.chat.say("\n".join(msg_parts))
            return

        if lower.startswith("show "):
            # show <文件名>：显示一个 Notebook 文件
            target = msg[5:].strip()
            content = self._find_and_read(target)
            if content:
                # 限长，避免刷屏
                limit = self.ctx_limits.show_file_preview_chars
                trunc = t("human.file_truncated") if len(content) > limit else ""
                preview = content[:limit] + trunc
                self.chat.say(t("human.file_preview", target=target, preview=preview))
            else:
                self.chat.say(t("human.file_not_found", target=target), "warning")
            return

        # ═══ 协作排查模式 ═══

        if lower in ("collab", "协作", "collaborate"):
            self._enter_collab_mode()
            return

        # ═══ 通用对话 / 任务委派（让 LLM 处理） ═══

        self._handle_free_chat(msg)

    # ═══════════════════════════════════════════
    #  自由对话（带上下文）
    # ═══════════════════════════════════════════

    def _build_conversation_context(self) -> str:
        """构建对话上下文块，供自由对话和协作模式使用"""
        parts = []

        # 当前 Incident 内容
        if self.current_incident:
            content = self.notebook.read_incident(self.current_incident)
            if content:
                # 截断避免超 token
                limit = self.ctx_limits.conversation_incident_chars
                if len(content) > limit:
                    content = content[:limit] + t("pipeline.truncated_marker")
                parts.append(f"## 当前 Incident 记录\n{content}")

        # 最近对话历史
        recent = self.notebook.get_recent_conversation(limit=20)
        if recent:
            parts.append(f"## 最近对话记录\n{recent}")

        # 当前问题摘要
        if self.current_issue:
            parts.append(f"## 当前正在关注的问题\n{self.current_issue}")

        return "\n\n".join(parts) if parts else t("human.no_context")

    def _handle_free_chat(self, msg: str):
        """处理自由对话 / 任务委派 —— 带完整上下文，不可被中断

        关键设计：
        1. 注入当前 incident、最近对话、当前问题等上下文，避免 LLM 说"没有上下文"
        2. 维护 _free_chat_history 多轮对话上下文，支持连续追问
        3. allow_interrupt=False，因为这本身就是在处理人类输入，
           新输入会进 inbox 在下一轮 _drain_human_messages 处理
        """
        self.chat.log(t("human.thinking"))

        # 记录人类输入到内存历史和 notebook
        self._free_chat_history.append({"role": "human", "content": msg})
        self.notebook.log_conversation("Human", msg)

        # 构建上下文：固定上下文（incident/问题等）+ 内存中的对话历史
        context = self._build_conversation_context()
        max_rounds = getattr(self.ctx_limits, 'max_free_chat_history_rounds', 10)
        recent = self._free_chat_history[-max_rounds:]
        history_text = ""
        for entry in recent:
            label = t("pipeline.role_human") if entry["role"] == "human" else t("pipeline.role_agent")
            history_text += f"\n**{label}**: {entry['content']}\n"

        # 涉及代码话题时注入项目地图
        project_map_section = ""
        if self._is_code_related(msg):
            _map = self._load_agents_md_section(keywords=msg.split()[:10])
            if _map:
                project_map_section = f"\n## {t('pipeline.free_chat_section_project_map')}\n{_map}\n"

        prompt = f"""{t("pipeline.free_chat_prompt")}

## {t("pipeline.free_chat_section_state")}
- 工作模式: {self.mode}
- 只读模式: {self.readonly}
- 暂停: {self.paused}
- 活跃 Incident: {self.current_incident or '无'}

## {t("pipeline.free_chat_section_context")}
{context}
{project_map_section}

## {t("pipeline.free_chat_section_history")}
{history_text}

## {t("pipeline.free_chat_section_human_msg")}
{msg}

{t("pipeline.free_chat_question_format")}
"""

        try:
            response = self._ask_llm(prompt, allow_interrupt=False)
        except LLMDegraded:
            raise  # 降级异常仍需冒泡到主循环处理
        except Exception as e:
            self.chat.say(t("human.llm_error", error=e), "warning")
            return

        commands = self._extract_commands(response, allow_fallback=False)

        if commands:
            # 清除本次消息触发的中断标志，避免自己的输入导致命令被跳过
            # 只有在命令执行期间有 *新的* 人类输入才应触发中断
            self.chat.clear_interrupt()

            max_rounds = 20
            all_round_results = []  # 每轮的命令结果摘要
            round_stats = []       # 每轮的命令计数
            final_reply = ""

            for round_num in range(1, max_rounds + 1):
                if round_num == 1:
                    self.chat.say(t("human.will_execute", count=len(commands)))
                else:
                    self.chat.say(t("human.continue_round", round=round_num))

                cmd_results = []
                for cmd in commands[:self.limits.config.max_chat_commands]:
                    if self.chat.is_interrupted():
                        self.chat.say(t("human.new_interrupt"), "info")
                        return

                    self.chat.cmd_log(cmd)
                    result = self._run_cmd(cmd, timeout=20)
                    cmd_results.append(str(result))

                # 本轮结果摘要
                round_summary = "\n".join(
                    f"$ {cmd}\n{result}"
                    for cmd, result in zip(commands[:len(cmd_results)], cmd_results)
                )
                all_round_results.append(round_summary)
                round_stats.append({"cmd_count": len(cmd_results)})

                # 滚动窗口：只保留最近 10 条，之前的压缩
                if len(all_round_results) > 10:
                    display_results = (
                        [t("human.truncated_omitted")]
                        + all_round_results[-10:]
                    )
                else:
                    display_results = all_round_results

                results_summary = "\n".join(display_results)

                followup = f"""刚才的问题是：{msg}

## 上下文
{context}

## {t("pipeline.free_chat_section_history")}
{history_text}

## {t("pipeline.free_chat_section_exec_history")}
{results_summary}

{t("pipeline.free_chat_followup")}"""

                try:
                    followup_response = self._ask_llm(followup, allow_interrupt=False)
                except LLMDegraded:
                    raise
                except Exception as e:
                    self.chat.say(t("human.llm_error", error=e), "warning")
                    return

                # 检查 LLM 是否还要继续执行命令
                commands = self._extract_commands(followup_response, allow_fallback=False)
                if not commands:
                    # 没有更多命令，提取最终结论
                    text = re.sub(r"```(?:text)?\s*\n?(.*?)\n?```", r"\1", followup_response, flags=re.DOTALL).strip()
                    final_reply = text or followup_response
                    break

            if not final_reply:
                _text = re.sub(r"```(?:text)?\s*\n?(.*?)\n?```", r"\1", followup_response, flags=re.DOTALL).strip()
                final_reply = t("human.max_rounds_done", rounds=max_rounds, conclusion=_text or followup_response)

            # 记录 Agent 回复到内存历史和 notebook
            total_cmds = sum(r.get("cmd_count", 0) for r in round_stats)
            agent_record = t("pipeline.agent_record", rounds=len(all_round_results), cmds=total_cmds, conclusion=final_reply)
            self._free_chat_history.append({"role": "agent", "content": agent_record})
            self.notebook.log_conversation("Agent", agent_record)

            self.chat.say(final_reply)
        else:
            text = re.sub(r"```(?:text)?\s*\n?(.*?)\n?```", r"\1", response, flags=re.DOTALL).strip()
            reply = text or response

            # 记录纯文本回复到内存历史和 notebook
            self._free_chat_history.append({"role": "agent", "content": reply})
            self.notebook.log_conversation("Agent", reply)

            self.chat.say(reply)

    # ═══════════════════════════════════════════
    #  协作排查模式
    # ═══════════════════════════════════════════

    # 只读/信息收集类命令前缀白名单 —— CONTINUE 意图时直接执行不问人
    _SAFE_COMMAND_PREFIXES = (
        "cat", "tail", "head", "grep", "egrep", "fgrep", "awk", "sed -n",
        "less", "more", "ls", "find", "stat", "file", "wc",
        "ps", "top", "htop", "pgrep", "lsof",
        "df", "du", "free", "uptime", "vmstat", "iostat", "mpstat",
        "journalctl", "dmesg", "last", "who", "w",
        "kubectl get", "kubectl describe", "kubectl logs", "kubectl top",
        "docker ps", "docker logs", "docker inspect", "docker stats",
        "netstat", "ss", "ip addr", "ip route", "iptables -L", "iptables -S",
        "curl", "wget -q", "dig", "nslookup", "ping", "traceroute", "mtr",
        "mysql -e", "psql -c", "redis-cli info", "redis-cli get",
        "systemctl status", "systemctl is-active", "systemctl list-units",
        "date", "hostname", "uname", "env", "printenv", "id",
    )

    # 连续自主执行轮次上限 —— 从 limits.yaml 读取，防止 LLM 跑飞
    _MAX_AUTO_ROUNDS_FALLBACK = 30

    def _is_safe_command(self, cmd: str) -> bool:
        """检查命令是否属于只读/信息收集类，可以不经人类确认直接执行"""
        stripped = cmd.strip()
        return any(stripped.startswith(p) for p in self._SAFE_COMMAND_PREFIXES)

    def _parse_collab_intent(self, response: str) -> tuple:
        """解析 LLM 回复末尾的意图标记。

        Returns:
            (intent, clean_text) — intent 为 "CONTINUE" / "CONFIRM" / "WAIT"
        """
        for tag in ("CONTINUE", "CONFIRM", "WAIT"):
            pattern = rf"\[{tag}\]\s*$"
            if re.search(pattern, response):
                clean = re.sub(pattern, "", response).strip()
                return tag, clean
        # 没有标记 → 保守处理，等人类输入
        return "WAIT", response

    def _run_collab_commands(self, commands: list) -> str:
        """批量执行命令并返回格式化结果"""
        cmd_results = []
        for cmd in commands[:self.limits.config.max_chat_commands]:
            self.chat.cmd_log(cmd)
            result = self._run_cmd(cmd, timeout=30)
            cmd_results.append(f"$ {cmd}\n{result}")
        return "\n".join(cmd_results)

    def _enter_collab_mode(self):
        """进入协作排查模式 —— 人类和 Agent 一起定位问题

        核心改进（相对旧版严格轮转）：
        1. Agent 自主推进只读/信息收集操作，不每步都问人
        2. 只在关键决策点（写操作、重启、方向不确定）暂停等人确认
        3. LLM 通过 [CONTINUE]/[CONFIRM]/[WAIT] 意图标记控制流程
        4. 安全阀：连续自主执行最多 _MAX_AUTO_ROUNDS 轮后强制暂停
        5. 人类随时可以插话，Agent 会在下一轮看到
        """
        self.chat.say(
            t("human.collab_enter"),
            "info",
        )

        # 初始化对话历史，注入当前上下文
        base_context = self._build_conversation_context()
        collab_history = []  # list of {"role": str, "content": str}

        system = self._build_system_prompt()
        system += (
            "\n\n" + t("pipeline.collab_system_rules") +
            f"\n\n## {t('pipeline.free_chat_section_context')}\n{base_context}"
        )

        waiting_for_human = True  # 初始状态等人描述问题
        auto_rounds = 0  # 连续自主执行轮次计数

        while self._running:
            # ─── 等待人类输入（仅在需要时阻塞）───
            if waiting_for_human:
                human_input = self.chat.ask_question(
                    t("human.collab_prompt"),
                    timeout=1800,  # 30 分钟超时
                )

                if human_input is None:
                    self.chat.say(t("human.collab_timeout"), "info")
                    break

                if human_input.strip().lower() in tuple(t("pipeline.free_chat_exit_keywords")):
                    self.chat.say(t("human.collab_exit"), "success")
                    break

                # 控制指令仍然可以在协作中使用
                hl = human_input.strip().lower()
                if hl in ("status", "pause", "resume", "stop", "freeze", "unfreeze"):
                    self._handle_human_message(human_input)
                    continue

                collab_history.append({"role": "human", "content": human_input})
                auto_rounds = 0  # 人类说话了，重置计数

            else:
                # ─── Agent 自主推进时，非阻塞检查人类是否插话 ───
                pending_msg = self.chat.check_inbox()
                if pending_msg is not None:
                    pl = pending_msg.strip().lower()
                    if pl in tuple(t("pipeline.free_chat_exit_keywords")):
                        self.chat.say(t("human.collab_exit"), "success")
                        break
                    if pl in ("status", "pause", "resume", "stop", "freeze", "unfreeze"):
                        self._handle_human_message(pending_msg)
                        continue
                    # 人类插话，纳入上下文
                    collab_history.append({"role": "human", "content": pending_msg})
                    auto_rounds = 0

            # ─── 构建多轮 prompt ───
            history_text = ""
            recent_rounds = collab_history[-self.limits.config.max_collab_history_rounds:]
            for entry in recent_rounds:
                role_label = t("pipeline.role_human") if entry["role"] == "human" else t("pipeline.role_agent")
                history_text += f"\n**{role_label}**: {entry['content']}\n"

            prompt = t("pipeline.collab_continue_prompt", history=history_text)

            # ─── LLM 调用 ───
            try:
                response = self.llm.ask(
                    prompt, system=system, max_tokens=4096,
                    interrupt_check=None,  # 协作模式不可中断
                )
            except LLMDegraded:
                self.chat.say(t("human.llm_unavailable"), "critical")
                break
            except Exception as e:
                self.chat.say(t("human.llm_error", error=e), "warning")
                waiting_for_human = True
                continue

            # ─── 解析意图标记 ───
            intent, clean_response = self._parse_collab_intent(response)
            commands = self._extract_commands(clean_response, allow_fallback=False)

            # 展示文本部分（去掉命令块）
            text_part = re.sub(
                r"```(?:commands|bash|shell|sh)?\s*\n.*?```", "", clean_response,
                flags=re.DOTALL,
            ).strip()
            if text_part:
                self.chat.say(text_part)

            # ─── 处理命令 ───
            if commands:
                all_safe = all(self._is_safe_command(c) for c in commands)

                if intent == "CONTINUE" and all_safe:
                    # 只读命令 + CONTINUE → 直接执行，不问人
                    result_text = self._run_collab_commands(commands)
                    self.chat.say(t("human.exec_result", result=result_text))
                    collab_history.append({
                        "role": "agent",
                        "content": t("pipeline.collab_result_append", text=text_part, result=result_text),
                    })
                    waiting_for_human = False

                elif intent == "CONTINUE" and not all_safe:
                    # LLM 说 CONTINUE 但命令不安全 → 自动升级为 CONFIRM
                    logger.info("collab: CONTINUE 含非安全命令，升级为 CONFIRM")
                    cmd_list = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(commands))
                    approved = self.chat.request_approval(
                        t("human.collab_unsafe_upgrade", cmd_list=cmd_list)
                    )
                    if approved:
                        self.chat.clear_interrupt()  # 批准后清除残留中断标志，避免命令被误杀
                        result_text = self._run_collab_commands(commands)
                        self.chat.say(t("human.exec_result", result=result_text))
                        collab_history.append({
                            "role": "agent",
                            "content": t("pipeline.collab_result_append", text=text_part, result=result_text),
                        })
                        waiting_for_human = False
                    else:
                        collab_history.append({
                            "role": "agent",
                            "content": f"{text_part}\n" + t("human.cmd_rejected"),
                        })
                        waiting_for_human = True

                else:
                    # CONFIRM / WAIT → 请求批准
                    cmd_list = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(commands))
                    approved = self.chat.request_approval(
                        t("human.collab_confirm_cmds", cmd_list=cmd_list)
                    )
                    if approved:
                        self.chat.clear_interrupt()  # 批准后清除残留中断标志，避免命令被误杀
                        result_text = self._run_collab_commands(commands)
                        self.chat.say(t("human.exec_result", result=result_text))
                        collab_history.append({
                            "role": "agent",
                            "content": t("pipeline.collab_result_append", text=text_part, result=result_text),
                        })
                        # CONFIRM 后继续推进；WAIT 则等人
                        waiting_for_human = (intent == "WAIT")
                    else:
                        collab_history.append({
                            "role": "agent",
                            "content": f"{text_part}\n" + t("human.cmd_rejected"),
                        })
                        waiting_for_human = True

            else:
                # 无命令的纯分析
                collab_history.append({
                    "role": "agent",
                    "content": text_part or clean_response,
                })
                if intent == "CONTINUE":
                    waiting_for_human = False
                else:  # CONFIRM / WAIT
                    waiting_for_human = True

            # ─── 安全阀：连续自主轮次上限 ───
            if not waiting_for_human:
                auto_rounds += 1
                max_rounds = getattr(self.limits.config, 'max_collab_auto_rounds', self._MAX_AUTO_ROUNDS_FALLBACK)
                if auto_rounds >= max_rounds:
                    self.chat.say(
                        t("human.collab_auto_limit", rounds=auto_rounds),
                        "info",
                    )
                    waiting_for_human = True
                    auto_rounds = 0

    def _show_help(self):
        """显示帮助"""
        self.chat.say(t("human.help_text"))

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
            cooldown_line = t("human.status_cooldown", remaining=s['cooldown_remaining'])
        frozen_line = ""
        if self.emergency.frozen:
            frozen_line = t("human.status_frozen")

        # Smart Notebook 附加状态
        smart_line = ""
        if hasattr(self.notebook, "get_smart_stats"):
            try:
                ss = self.notebook.get_smart_stats()
                smart_line = (
                    t("human.status_smart_title")
                    + "\n"
                    + t("human.status_smart_links", count=ss.get('linker', {}).get('total_links', 0))
                    + "\n"
                    + t("human.status_smart_fp", count=ss.get('fp_suppressed', 0))
                    + "\n"
                    + t("human.status_smart_insights", count=ss.get('durability_insights', 0))
                )
            except Exception:
                pass
        if hasattr(self.notebook, "evaluate_trust"):
            try:
                tr = self.notebook.evaluate_trust()
                if tr:
                    smart_line += "\n" + t("human.status_smart_trust", level=tr.get('level', '?'))
            except Exception:
                pass

        self.chat.say(
            t("human.status_title") + "\n"
            + t("human.status_mode", mode=self.mode) + "\n"
            + t("human.status_target", name=self.current_target.name, type=self.current_target.mode) + "\n"
            + t("human.status_targets", targets=target_list) + "\n"
            + (t("human.status_paused_yes") if self.paused else t("human.status_paused_no")) + "\n"
            + (t("human.status_readonly_yes") if self.readonly else t("human.status_readonly_no")) + "\n"
            + t("human.status_active", count=len(active_incidents)) + "\n"
            + t("human.status_archived", count=len(archived)) + "\n"
            + t("human.status_playbooks", count=len(playbooks)) + "\n"
            + t("human.status_quota", used=s['actions_last_hour'], max=s['max_actions_per_hour']) + "\n"
            + t("human.status_issue", issue=self.current_issue or t("prompt.no_incident"))
            + cooldown_line + frozen_line + smart_line,
            "info",
        )

    def _show_scorecard(self):
        """展示最近的成长记分卡"""
        content = self.notebook.read("growth/scorecard.md")
        if content:
            self.chat.say(content[:2000], "info")
        else:
            self.chat.say(
                t("human.scorecard_empty"),
                "info",
            )

    def _show_trust_level(self):
        """展示当前信任层级"""
        if hasattr(self.notebook, "evaluate_trust"):
            try:
                result = self.notebook.evaluate_trust()
                if result:
                    limits = result.get("limits", {})
                    lines = [t("human.trust_level", level=result['level'])]
                    if isinstance(limits, dict):
                        for k, v in limits.items():
                            lines.append(t("human.trust_limit", key=k, value=v))
                    self.chat.say("\n".join(lines), "info")
                    return
            except Exception:
                pass
        self.chat.say(t("human.trust_need_smart"), "info")

    def _run_self_repair(self, description: str):
        """触发一次自修复会话。

        运行目录和 selfdev 工作区必须物理分离,由 OPS_AGENT_SELFDEV_PATH
        环境变量指定。未配置则拒绝执行。
        """
        # 紧急冻结检查
        emergency_stop = Path(self.notebook.path) / "EMERGENCY_STOP_SELF_MODIFY"
        if emergency_stop.exists():
            self.chat.say(
                t("human.selffix_frozen", reason=emergency_stop.read_text()),
                "critical"
            )
            return

        selfdev_path = os.environ.get("OPS_AGENT_SELFDEV_PATH", "")
        if not selfdev_path:
            self.chat.say(
                t("human.selffix_no_path"),
                "warning"
            )
            return

        if self.patch_loop is None:
            self.chat.say(
                t("human.selffix_no_loop"), "warning"
            )
            return

        try:
            from src.repair.self_repair import SelfRepairSession
        except Exception as e:
            logger.exception("SelfRepairSession 导入失败")
            self.chat.say(t("human.selffix_load_failed", error=e), "warning")
            return

        session = SelfRepairSession(
            agent=self,
            repo_path=selfdev_path,
        )
        result = session.run(description)

        if result.success:
            # 成功路径会触发 3 秒后重启,这里只打一条
            self.chat.say(
                t("human.selffix_success", reason=result.reason, branch=result.branch, pre_tag=result.pre_tag),
                "success"
            )
        else:
            self.chat.say(t("human.selffix_incomplete", reason=result.reason), "warning")
