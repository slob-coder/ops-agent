"""
人类交互 Mixin — 指令处理、自由对话、协作排查
"""

import os
import re
import time
import logging
from pathlib import Path
from datetime import datetime

from src.infra.llm import LLMDegraded

logger = logging.getLogger("ops-agent")


class HumanInteractionMixin:
    """处理所有人类指令、自由对话、协作排查模式"""

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

        if lower in ("new", "clear chat", "清除对话"):
            self._free_chat_history.clear()
            self.chat.say("已清除自由对话上下文。", "info")
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

        # ═══ 自修复命令 ═══
        if lower.startswith("self-fix") or lower.startswith("selffix"):
            # 提取描述部分
            parts = msg.split(None, 1)
            description = parts[1].strip() if len(parts) > 1 else ""
            if not description:
                self.chat.say(
                    "用法: self-fix <问题描述>\n"
                    "例: self-fix 巡检异常路径 return 后没 sleep,反复开 incident",
                    "info"
                )
                return
            self._run_self_repair(description)
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
                limit = self.ctx_limits.show_file_preview_chars
                preview = content[:limit] + ("\n...(已截断)" if len(content) > limit else "")
                self.chat.say(f"{target}:\n{preview}")
            else:
                self.chat.say(f"找不到 {target}", "warning")
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
                    content = content[:limit] + "\n...(已截断)"
                parts.append(f"## 当前 Incident 记录\n{content}")

        # 最近对话历史
        recent = self.notebook.get_recent_conversation(limit=20)
        if recent:
            parts.append(f"## 最近对话记录\n{recent}")

        # 当前问题摘要
        if self.current_issue:
            parts.append(f"## 当前正在关注的问题\n{self.current_issue}")

        return "\n\n".join(parts) if parts else "（无历史上下文）"

    def _handle_free_chat(self, msg: str):
        """处理自由对话 / 任务委派 —— 带完整上下文，不可被中断

        关键设计：
        1. 注入当前 incident、最近对话、当前问题等上下文，避免 LLM 说"没有上下文"
        2. 维护 _free_chat_history 多轮对话上下文，支持连续追问
        3. allow_interrupt=False，因为这本身就是在处理人类输入，
           新输入会进 inbox 在下一轮 _drain_human_messages 处理
        """
        self.chat.log("正在思考你的指令...")

        # 记录人类输入到内存历史和 notebook
        self._free_chat_history.append({"role": "human", "content": msg})
        self.notebook.log_conversation("Human", msg)

        # 构建上下文：固定上下文（incident/问题等）+ 内存中的对话历史
        context = self._build_conversation_context()
        max_rounds = getattr(self.ctx_limits, 'max_free_chat_history_rounds', 10)
        recent = self._free_chat_history[-max_rounds:]
        history_text = ""
        for entry in recent:
            label = "人类" if entry["role"] == "human" else "Agent"
            history_text += f"\n**{label}**: {entry['content']}\n"

        prompt = f"""人类同事给你发了一条消息。判断这是一个问题（要回答）还是一个任务（要执行）。

## 当前状态
- 工作模式: {self.mode}
- 只读模式: {self.readonly}
- 暂停: {self.paused}
- 活跃 Incident: {self.current_incident or '无'}

## 上下文
{context}

## 对话历史
{history_text}

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
- 结合上面的上下文来理解人类的问题
- 只输出真正需要执行的命令
- 如果是修改类操作（L2+），先说明你打算做什么，等批准

判断任务类型：
- 如果是一次性查询或简单操作（查状态、看日志、单条命令）→ 正常输出 commands
- 如果需要多步自主执行（排查问题、修复故障、需要观察-判断-再行动）→ 在 commands 块第一行写 AUTONOMOUS，我会自主执行完成后汇报
"""

        try:
            response = self._ask_llm(prompt, allow_interrupt=False)
        except LLMDegraded:
            raise  # 降级异常仍需冒泡到主循环处理
        except Exception as e:
            self.chat.say(f"LLM 调用出错: {e}", "warning")
            return

        commands = self._extract_commands(response)

        if commands:
            # 多步任务自动识别：如果消息语义是排查/分析类任务，
            # 则转入自主执行模式，而非一步步等人类确认
            if not self._is_simple_query(msg):
                self._enter_autonomous_task(msg, commands)
                return

            # 清除本次消息触发的中断标志，避免自己的输入导致命令被跳过
            # 只有在命令执行期间有 *新的* 人类输入才应触发中断
            self.chat.clear_interrupt()
            self.chat.say(f"我打算执行 {len(commands)} 条命令来回答你...")
            cmd_results = []
            for cmd in commands[:self.limits.config.max_chat_commands]:
                if self.chat.is_interrupted():
                    self.chat.say("收到新指令，停止当前任务。", "info")
                    return
                self.chat.log(f"执行: {cmd}")
                result = self._run_cmd(cmd, timeout=20)
                cmd_results.append(str(result))

            # 构建命令结果摘要
            results_summary = "\n".join(
                f"$ {cmd}\n{result}"
                for cmd, result in zip(commands[:len(cmd_results)], cmd_results)
            )

            followup = f"""刚才的问题是：{msg}

## 上下文
{context}

## 对话历史
{history_text}

执行了以下命令，结果如下：
{results_summary}

请基于上下文和命令结果，简洁地回答人类的问题。直接给出结论，不要重复命令输出。"""
            try:
                final = self._ask_llm(followup, allow_interrupt=False)
            except LLMDegraded:
                raise
            except Exception as e:
                self.chat.say(f"LLM 调用出错: {e}", "warning")
                return

            # 记录 Agent 回复（含命令+结果+最终回答）到内存历史和 notebook
            agent_record = f"执行命令: {', '.join(commands[:len(cmd_results)])}\n结论: {final}"
            self._free_chat_history.append({"role": "agent", "content": agent_record})
            self.notebook.log_conversation("Agent", agent_record)

            self.chat.say(final)
        else:
            text = re.sub(r"```(?:text)?\s*\n?(.*?)\n?```", r"\1", response, flags=re.DOTALL).strip()
            reply = text or response

            # 记录纯文本回复到内存历史和 notebook
            self._free_chat_history.append({"role": "agent", "content": reply})
            self.notebook.log_conversation("Agent", reply)

            self.chat.say(reply)

    def _enter_autonomous_task(self, task_description: str, initial_commands: list = None):
        """进入自主任务模式 — 多步执行完成后汇报，复用 incident 机制"""
        self.chat.say(f"收到，开始自主执行: {task_description}", "info")

        # 如果已有活跃 incident，不重复创建
        if not self.current_incident:
            self.current_incident = self.notebook.create_incident(task_description)
            self.chat._trace_file = self.current_incident
            self.mode = self.INVESTIGATE

        # 如果有初始命令，先执行
        if initial_commands:
            for cmd in initial_commands[:self.limits.config.max_chat_commands]:
                self.chat.log(f"执行: {cmd}")
                self._run_cmd(cmd, timeout=20)

        # 回到主循环，incident_loop 会自动接管后续的 observe → diagnose → ... → reflect
        self.notebook.log_conversation("Agent", f"开始自主任务: {task_description}")

    def _is_simple_query(self, msg: str) -> bool:
        """判断是否是简单查询（查状态、看信息），不需要多步自主执行。
        默认不是简单查询——带命令输出的消息多半需要多步分析。"""
        lower = msg.lower()
        # 明确的简单查询关键词
        simple_keywords = (
            "status", "版本", "version", "whoami", "hostname", "uptime",
        )
        # 明确的排查/分析关键词 → 一定不是简单查询
        complex_keywords = (
            "分析", "排查", "为什么", "什么原因", "怎么回事", "诊断",
            "调查", "排查", "修复", "解决", "analyzer", "diagnos",
            "investigat", "troubleshoot", "fix", "resolve", "debug",
        )
        if any(kw in lower for kw in complex_keywords):
            return False
        # 短消息且不含命令/日志 = 可能是简单问题
        if len(msg) < 20 and "$" not in msg and "HTTP" not in msg:
            return True
        if any(kw in lower for kw in simple_keywords) and len(msg) < 40:
            return True
        # 包含日志、报错等 = 需要分析
        return False

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
            self.chat.log(f"执行: {cmd}")
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
            "进入协作排查模式 🤝\n"
            "我会主动推进排查，只在关键决策点请你确认。\n"
            "你随时可以插话补充信息或改变方向。\n"
            "输入 done 或 结束 退出协作模式。",
            "info",
        )

        # 初始化对话历史，注入当前上下文
        base_context = self._build_conversation_context()
        collab_history = []  # list of {"role": str, "content": str}

        system = self._build_system_prompt()
        system += (
            "\n\n## 协作排查模式规则\n"
            "你正在和人类同事一起排查问题。你应该**主动推进排查**，不要每一步都问人类的想法。\n\n"
            "### 输出格式\n"
            "每次回复末尾必须附带一个意图标记（放在最后一行，独占一行）：\n\n"
            "- `[CONTINUE]` — 你打算继续推进，不需要人类输入。\n"
            "  适用于：读日志、查状态、收集信息、执行只读命令、分析中间结果\n\n"
            "- `[CONFIRM]` — 你需要人类确认才能继续。\n"
            "  适用于：执行写操作、重启服务、修改配置、方向性决策、你不确定的判断\n\n"
            "- `[WAIT]` — 你已完成当前分析，等待人类提供新信息或新方向。\n"
            "  适用于：排查到死胡同、需要人类提供业务背景、已给出结论等待反馈\n\n"
            "### 行为原则\n"
            "1. **大胆推进只读操作** —— 查看日志、检查进程、查看配置等不需要确认\n"
            "2. **连续执行有关联的步骤** —— 比如查日志→发现错误→查相关服务状态→分析，一气呵成\n"
            "3. **只在关键决策点停下** —— 要重启？要改配置？不确定方向？才用 [CONFIRM]\n"
            "4. **每次回复保持简洁** —— 不要长篇大论解释你要做什么，直接做\n"
            "5. **如果需要执行命令，用 ```commands``` 格式输出**\n\n"
            f"## 当前上下文\n{base_context}"
        )

        waiting_for_human = True  # 初始状态等人描述问题
        auto_rounds = 0  # 连续自主执行轮次计数

        while self._running:
            # ─── 等待人类输入（仅在需要时阻塞）───
            if waiting_for_human:
                human_input = self.chat.ask_question(
                    "请输入你的想法（输入 done 退出协作）：",
                    timeout=1800,  # 30 分钟超时
                )

                if human_input is None:
                    self.chat.say("协作超时，退出协作模式。", "info")
                    break

                if human_input.strip().lower() in ("done", "结束", "exit", "quit"):
                    self.chat.say("退出协作排查模式，回到正常工作。", "success")
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
                    if pl in ("done", "结束", "exit", "quit"):
                        self.chat.say("退出协作排查模式，回到正常工作。", "success")
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
                role_label = "人类" if entry["role"] == "human" else "Agent"
                history_text += f"\n**{role_label}**: {entry['content']}\n"

            prompt = f"""## 协作排查对话历史
{history_text}

请继续排查。如果需要执行命令，用 ```commands``` 格式输出。
回复末尾附上意图标记：[CONTINUE] / [CONFIRM] / [WAIT]"""

            # ─── LLM 调用 ───
            try:
                response = self.llm.ask(
                    prompt, system=system, max_tokens=4096,
                    interrupt_check=None,  # 协作模式不可中断
                )
            except LLMDegraded:
                self.chat.say("LLM 不可用，退出协作模式。", "critical")
                break
            except Exception as e:
                self.chat.say(f"LLM 调用出错: {e}", "warning")
                waiting_for_human = True
                continue

            # ─── 解析意图标记 ───
            intent, clean_response = self._parse_collab_intent(response)
            commands = self._extract_commands(clean_response)

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
                    self.chat.say(f"执行结果：\n{result_text}")
                    collab_history.append({
                        "role": "agent",
                        "content": f"{text_part}\n\n执行结果：\n{result_text}",
                    })
                    waiting_for_human = False

                elif intent == "CONTINUE" and not all_safe:
                    # LLM 说 CONTINUE 但命令不安全 → 自动升级为 CONFIRM
                    logger.info("collab: CONTINUE 含非安全命令，升级为 CONFIRM")
                    cmd_list = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(commands))
                    approved = self.chat.request_approval(
                        f"执行以下命令（含写操作，需确认）：\n{cmd_list}"
                    )
                    if approved:
                        result_text = self._run_collab_commands(commands)
                        self.chat.say(f"执行结果：\n{result_text}")
                        collab_history.append({
                            "role": "agent",
                            "content": f"{text_part}\n\n执行结果：\n{result_text}",
                        })
                        waiting_for_human = False
                    else:
                        collab_history.append({
                            "role": "agent",
                            "content": f"{text_part}\n（命令被人类否决）",
                        })
                        waiting_for_human = True

                else:
                    # CONFIRM / WAIT → 请求批准
                    cmd_list = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(commands))
                    approved = self.chat.request_approval(
                        f"执行以下命令：\n{cmd_list}"
                    )
                    if approved:
                        result_text = self._run_collab_commands(commands)
                        self.chat.say(f"执行结果：\n{result_text}")
                        collab_history.append({
                            "role": "agent",
                            "content": f"{text_part}\n\n执行结果：\n{result_text}",
                        })
                        # CONFIRM 后继续推进；WAIT 则等人
                        waiting_for_human = (intent == "WAIT")
                    else:
                        collab_history.append({
                            "role": "agent",
                            "content": f"{text_part}\n（命令被人类否决）",
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
                        f"已连续自主执行 {auto_rounds} 步，暂停等待你的确认。",
                        "info",
                    )
                    waiting_for_human = True
                    auto_rounds = 0

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
            "   collab (协作)  进入协作排查模式(人+Agent 一起定位问题)\n"
            "   self-fix <描述> 触发一次自修复会话(修改 ops-agent 自己)\n"
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

    def _run_self_repair(self, description: str):
        """触发一次自修复会话。

        运行目录和 selfdev 工作区必须物理分离,由 OPS_AGENT_SELFDEV_PATH
        环境变量指定。未配置则拒绝执行。
        """
        # 紧急冻结检查
        emergency_stop = Path(self.notebook.path) / "EMERGENCY_STOP_SELF_MODIFY"
        if emergency_stop.exists():
            self.chat.say(
                f"🚨 自修复已被冻结(EMERGENCY_STOP_SELF_MODIFY 存在)。\n"
                f"原因:\n{emergency_stop.read_text()}\n"
                f"人类确认后删除该文件可恢复。",
                "critical"
            )
            return

        selfdev_path = os.environ.get("OPS_AGENT_SELFDEV_PATH", "")
        if not selfdev_path:
            self.chat.say(
                "自修复未配置。请设置环境变量 OPS_AGENT_SELFDEV_PATH "
                "指向一个独立的 ops-agent git 工作区(不能与运行目录相同)。",
                "warning"
            )
            return

        if self.patch_loop is None:
            self.chat.say(
                "PatchLoop 未初始化,无法自修复。请检查启动日志。", "warning"
            )
            return

        try:
            from src.repair.self_repair import SelfRepairSession
        except Exception as e:
            logger.exception("SelfRepairSession 导入失败")
            self.chat.say(f"自修复模块加载失败: {e}", "warning")
            return

        session = SelfRepairSession(
            agent=self,
            repo_path=selfdev_path,
        )
        result = session.run(description)

        if result.success:
            # 成功路径会触发 3 秒后重启,这里只打一条
            self.chat.say(
                f"自修复成功: {result.reason}\n"
                f"分支: {result.branch}\n"
                f"pre-tag: {result.pre_tag}",
                "success"
            )
        else:
            self.chat.say(f"自修复未完成: {result.reason}", "warning")
