"""
人类交互 Mixin — 指令处理、自由对话、协作排查
"""

import os
import re
import time
import logging
from pathlib import Path
from datetime import datetime

from llm import LLMDegraded

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
                preview = content[:2000] + ("\n...(已截断)" if len(content) > 2000 else "")
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
                if len(content) > 3000:
                    content = content[:3000] + "\n...(已截断)"
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
        2. allow_interrupt=False，因为这本身就是在处理人类输入，
           新输入会进 inbox 在下一轮 _drain_human_messages 处理
        """
        self.chat.log("正在思考你的指令...")

        context = self._build_conversation_context()

        prompt = f"""人类同事给你发了一条消息。判断这是一个问题（要回答）还是一个任务（要执行）。

## 当前状态
- 工作模式: {self.mode}
- 只读模式: {self.readonly}
- 暂停: {self.paused}
- 活跃 Incident: {self.current_incident or '无'}

## 上下文
{context}

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
            self.chat.say(f"我打算执行 {len(commands)} 条命令来回答你...")
            cmd_results = []
            for cmd in commands[:8]:
                if self.chat.is_interrupted():
                    self.chat.say("收到新指令，停止当前任务。", "info")
                    return
                self.chat.log(f"执行: {cmd}")
                result = self._run_cmd(cmd, timeout=20)
                cmd_results.append(str(result))

            followup = f"""刚才的问题是：{msg}

## 上下文
{context}

执行了以下命令，结果如下：
{chr(10).join(cmd_results)}

请基于上下文和命令结果，简洁地回答人类的问题。直接给出结论，不要重复命令输出。"""
            try:
                final = self._ask_llm(followup, allow_interrupt=False)
            except LLMDegraded:
                raise
            except Exception as e:
                self.chat.say(f"LLM 调用出错: {e}", "warning")
                return
            self.chat.say(final)
        else:
            text = re.sub(r"```(?:text)?\s*\n?(.*?)\n?```", r"\1", response, flags=re.DOTALL).strip()
            self.chat.say(text or response)

    # ═══════════════════════════════════════════
    #  协作排查模式
    # ═══════════════════════════════════════════

    def _enter_collab_mode(self):
        """进入协作排查模式 —— 人类和 Agent 一起定位问题

        与普通对话的区别：
        1. 维护多轮对话历史（不是每次都无状态）
        2. 自动加载当前 incident / 最近操作上下文
        3. 用 ask_question 阻塞等待，不触发 interrupted
        4. 输入 'done' / '结束' 退出协作模式
        """
        self.chat.say(
            "进入协作排查模式 🤝\n"
            "我会保持对话上下文，你可以和我一起排查问题。\n"
            "输入 done 或 结束 退出协作模式。",
            "info",
        )

        # 初始化对话历史，注入当前上下文
        base_context = self._build_conversation_context()
        collab_history = []  # list of {"role": str, "content": str}

        system = self._build_system_prompt()
        system += (
            "\n\n## 协作排查模式\n"
            "你正在和人类同事一起排查问题。保持技术讨论风格，"
            "主动提出排查思路和需要执行的命令。"
            "如果需要执行命令来验证假设，用 ```commands``` 格式输出。\n"
            f"\n## 当前上下文\n{base_context}"
        )

        while self._running:
            # 用 ask_question 阻塞等待人类输入（走 _approval_queue，不触发 interrupted）
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

            # 加入对话历史
            collab_history.append({"role": "human", "content": human_input})

            # 构建多轮 prompt
            history_text = ""
            # 保留最近 10 轮避免 token 爆炸
            recent_rounds = collab_history[-20:]
            for entry in recent_rounds:
                role_label = "人类" if entry["role"] == "human" else "Agent"
                history_text += f"\n**{role_label}**: {entry['content']}\n"

            prompt = f"""## 协作排查对话历史
{history_text}

请回应人类最新的消息。如果需要执行命令，用 ```commands``` 格式输出。
如果你有排查思路，主动分享。"""

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
                continue

            # 检查是否包含命令
            commands = self._extract_commands(response)

            if commands:
                # 先展示 LLM 的分析
                text_part = re.sub(
                    r"```commands\s*\n.*?```", "", response, flags=re.DOTALL
                ).strip()
                if text_part:
                    self.chat.say(text_part)

                # 请求批准执行
                cmd_list = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(commands))
                approved = self.chat.request_approval(
                    f"执行以下命令：\n{cmd_list}"
                )
                if approved:
                    cmd_results = []
                    for cmd in commands[:8]:
                        self.chat.log(f"执行: {cmd}")
                        result = self._run_cmd(cmd, timeout=30)
                        cmd_results.append(f"$ {cmd}\n{result}")
                    result_text = "\n".join(cmd_results)
                    self.chat.say(f"执行结果：\n{result_text}")
                    collab_history.append({
                        "role": "agent",
                        "content": f"{text_part}\n\n执行结果：\n{result_text}",
                    })
                else:
                    collab_history.append({
                        "role": "agent",
                        "content": f"{text_part}\n（命令被人类否决）",
                    })
            else:
                text = re.sub(
                    r"```(?:text)?\s*\n?(.*?)\n?```", r"\1",
                    response, flags=re.DOTALL,
                ).strip()
                self.chat.say(text or response)
                collab_history.append({"role": "agent", "content": text or response})

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
                f"原因:\n{emergency_stop.read_text()[:500]}\n"
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
            from self_repair import SelfRepairSession
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
