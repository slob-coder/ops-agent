"""
LLM 输出解析 & 辅助方法 Mixin
"""

import re
import json
import hashlib
import logging
from typing import Optional

logger = logging.getLogger("ops-agent")


class ParsersMixin:
    """LLM 输出解析、命令提取、指纹计算等工具方法"""

    # ─── 通用工具 ───

    def _extract_commands(self, text: str, allow_fallback: bool = True) -> list:
        """从 LLM 输出中提取命令列表

        allow_fallback=False 时只匹配 commands 块，不匹配 bash/shell/sh。
        用于 free_chat 等场景，避免 LLM 展示代码片段被误执行。

        支持多行命令结构：heredoc（<< DELIM）和反斜杠续行（\\）。
        """
        commands = []

        # 只匹配 ```commands ... ``` 块
        blocks = re.findall(r"```commands\s*\n(.*?)```", text, re.DOTALL)
        for block in blocks:
            commands.extend(self._parse_block_lines(block))

        # 如果没有 commands 块，再尝试匹配 bash/shell/sh 块（兼容旧 observe/assess prompt）
        if not commands and allow_fallback:
            blocks = re.findall(r"```(?:bash|shell|sh)\s*\n(.*?)```", text, re.DOTALL)
            for block in blocks:
                commands.extend(self._parse_block_lines(block))

        # 如果没有代码块，尝试匹配 STEP N: 格式
        if not commands:
            for match in re.finditer(r"STEP\s+\d+:\s*`?(.+?)`?\s*$", text, re.MULTILINE):
                commands.append(match.group(1).strip())

        return commands

    @staticmethod
    def _parse_block_lines(block: str) -> list:
        """解析命令块中的行，正确合并 heredoc 和反斜杠续行为单条命令。

        处理的多行结构：
        1. Heredoc:  cmd << 'DELIM' ... DELIM  （含 <<- 和引号变体）
        2. 反斜杠续行: cmd foo \\
                       bar baz
        """
        lines = block.split("\n")
        commands = []
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # 跳过空行和注释
            if not stripped or stripped.startswith("#"):
                i += 1
                continue

            # ── 检测 heredoc: <<[-] ['"]?DELIM['"]? ──
            heredoc_match = re.search(r"<<-?\s*['\"]?([A-Za-z_]\w*)['\"]?", stripped)
            if heredoc_match:
                delimiter = heredoc_match.group(1)
                merged = [line]
                i += 1
                while i < len(lines):
                    merged.append(lines[i])
                    # heredoc 结束符：行首（可能有 tab，<<- 情况）就是 delimiter
                    if lines[i].strip() == delimiter:
                        break
                    i += 1
                commands.append("\n".join(merged))
                i += 1
                continue

            # ── 检测反斜杠续行 ──
            if stripped.endswith("\\"):
                merged = [line]
                i += 1
                while i < len(lines):
                    merged.append(lines[i])
                    if not lines[i].strip().endswith("\\"):
                        break
                    i += 1
                commands.append("\n".join(merged))
                i += 1
                continue

            # ── 普通单行命令 ──
            commands.append(stripped)
            i += 1

        return commands

    @staticmethod
    def _strip_trailing_commas(text: str) -> str:
        """移除 JSON 文本中的尾随逗号（LLM 常见输出问题）。

        处理 ] 和 } 前的逗号，如 [1, 2,] → [1, 2]
        """
        # ,] → ]  ,} → }  （逗号后可选空白再跟闭括号）
        return re.sub(r',\s*([}\]])', r'\1', text)

    def _extract_json(self, response: str) -> Optional[dict]:
        """从 LLM 回复中提取 JSON 对象。

        优先匹配 ```json 代码块，fallback 到整个文本 json.loads，
        最后尝试最外层 { ... }。
        自动修复 LLM 常见的尾随逗号问题。
        """
        candidates = []

        # 尝试 ```json ... ``` 块
        match = re.search(r'```json\s*\n(.*?)```', response, re.DOTALL)
        if match:
            candidates.append(match.group(1).strip())

        # 整个文本
        candidates.append(response.strip())

        # 尝试找 { ... } 最外层
        match = re.search(r'\{.*\}', response, re.DOTALL)
        if match:
            candidates.append(match.group(0))

        # 逐个尝试解析，先原样，再修复尾随逗号
        for text in candidates:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
            try:
                return json.loads(self._strip_trailing_commas(text))
            except json.JSONDecodeError:
                pass

        return None

    # ─── assess 解析（不变，assess prompt 仍然是文本格式）───

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

    # ─── diagnose 解析（JSON）───

    def _parse_diagnosis(self, response: str) -> dict:
        """解析 diagnose 输出 — 要求 JSON 格式"""
        valid_types = {"code_bug", "runtime", "config", "resource", "external", "unknown"}
        valid_actions = {"FIX", "COLLECT_MORE", "MONITOR", "ESCALATE"}

        data = self._extract_json(response)
        if not data or "hypothesis" not in data:
            logger.error("diagnose 输出不是合法 JSON，使用默认值")
            return {
                "facts": response,
                "hypothesis": "JSON 解析失败，无法提取诊断",
                "confidence": 30,
                "type": "unknown",
                "next_action": "COLLECT_MORE",
                "gaps": [],
                "escalate": False,
            }

        # 规范化 gaps：确保是 list[dict]
        gaps = data.get("gaps", [])
        if not isinstance(gaps, list):
            gaps = []
        normalized_gaps = []
        for g in gaps:
            if isinstance(g, dict):
                normalized_gaps.append(g)
            elif isinstance(g, str):
                normalized_gaps.append({"description": g, "command": ""})

        next_action = data.get("next_action", "FIX")
        if next_action not in valid_actions:
            next_action = "FIX"

        dtype = data.get("type", "unknown")
        if dtype not in valid_types:
            dtype = "unknown"

        escalate = data.get("escalate", False)
        if isinstance(escalate, str):
            escalate = escalate.upper() in ("YES", "TRUE")

        return {
            "facts": data.get("facts", ""),
            "hypothesis": data.get("hypothesis", ""),
            "confidence": int(data.get("confidence", 60)),
            "type": dtype,
            "next_action": next_action,
            "gaps": normalized_gaps,
            "escalate": escalate,
        }

    # ─── plan 解析（JSON）───

    def _parse_plan(self, response: str) -> Optional["ActionPlan"]:
        """解析 plan 输出 — 要求 JSON 格式"""
        from src.safety.trust import ActionPlan

        data = self._extract_json(response)
        if not data:
            logger.error(f"plan 输出不是合法 JSON, response={response[:500]}")
            return None

        # COLLECT_MORE / ESCALATE 可以没有 steps 字段
        next_action = data.get("next_action", "READY")
        if "steps" not in data and next_action not in ("COLLECT_MORE", "ESCALATE"):
            logger.error(f"plan JSON 缺少 steps 字段, response={response[:500]}")
            return None

        steps = data.get("steps", [])
        if not isinstance(steps, list) or (not steps and next_action not in ("COLLECT_MORE", "ESCALATE")):
            return None

        # 规范化 steps
        normalized_steps = []
        for s in steps:
            if isinstance(s, dict) and s.get("command"):
                normalized_steps.append({
                    "command": s["command"],
                    "purpose": s.get("purpose", ""),
                    "wait_seconds": int(s.get("wait_seconds", 0)),
                })
            elif isinstance(s, str) and s.strip():
                normalized_steps.append({"command": s.strip(), "purpose": "", "wait_seconds": 0})

        # READY 时必须有可执行的 steps；COLLECT_MORE / ESCALATE 允许空
        next_action = data.get("next_action", "READY")
        if not normalized_steps and next_action not in ("COLLECT_MORE", "ESCALATE"):
            logger.warning(f"plan READY 但无有效 steps, 原始 steps={steps[:3]}, 降级为 COLLECT_MORE")
            # 降级：如果 gaps 有内容，转为 COLLECT_MORE；否则返回 None 重试
            if data.get("gaps"):
                next_action = "COLLECT_MORE"
            else:
                return None

        # 规范化 rollback_steps
        rollback = []
        for s in data.get("rollback_steps", []):
            if isinstance(s, dict) and s.get("command"):
                rollback.append({"command": s["command"], "purpose": s.get("purpose", "")})
            elif isinstance(s, str) and s.strip():
                rollback.append({"command": s.strip(), "purpose": ""})

        # 规范化 verify_steps
        verify = []
        for s in data.get("verify_steps", []):
            if isinstance(s, dict) and s.get("command"):
                step = {
                    "command": s["command"],
                    "expect": s.get("expect", ""),
                }
                # 验证策略字段（可选）
                if "delay_seconds" in s:
                    step["delay_seconds"] = int(s["delay_seconds"])
                if s.get("watch"):
                    step["watch"] = True
                    step["watch_duration"] = int(s.get("watch_duration", 300))
                    step["watch_interval"] = int(s.get("watch_interval", 60))
                    step["watch_converge"] = int(s.get("watch_converge", 2))
                verify.append(step)
            elif isinstance(s, str) and s.strip():
                verify.append({"command": s.strip(), "expect": ""})

        # 规范化 gaps
        gaps = data.get("gaps", [])
        if not isinstance(gaps, list):
            gaps = []
        normalized_gaps = []
        for g in gaps:
            if isinstance(g, dict):
                normalized_gaps.append(g)
            elif isinstance(g, str):
                normalized_gaps.append({"description": g, "command": ""})

        valid_plan_actions = {"READY", "COLLECT_MORE", "ESCALATE"}
        next_action = data.get("next_action", "READY")
        if next_action not in valid_plan_actions:
            next_action = "READY"

        return ActionPlan(
            steps=normalized_steps,
            rollback_steps=rollback,
            verify_steps=verify,
            expected=data.get("expected", "系统恢复正常"),
            trust_level=int(data.get("trust_level", 2)),
            reason=data.get("reason", ""),
            next_action=next_action,
            gaps=normalized_gaps,
        )

    # ─── targeted observe（替代 _quick_observe）───

    def _targeted_observe(self, plan=None) -> str:
        """基于 plan 的验证命令做针对性观察

        优先用 plan.verify_steps → fallback LLM 动态生成 → fallback 通用命令
        """
        # 优先：plan 中 LLM 给出的验证命令
        if plan and plan.verify_steps:
            outputs = []
            for step in plan.verify_steps[:self.limits.config.max_verify_steps]:
                cmd = step.get("command", "")
                if cmd:
                    result = self._run_cmd(cmd, timeout=15)
                    outputs.append(f"$ {cmd}\n{str(result)}")
            if outputs:
                return "\n\n".join(outputs)

        # fallback：让 LLM 基于修复上下文动态生成
        if plan and plan.action:
            generated = self._generate_verify_commands(plan)
            if generated:
                return generated

        # 最终 fallback：通用命令
        return self._quick_observe()

    def _generate_verify_commands(self, plan) -> str:
        """plan 中没有验证命令时，让 LLM 基于上下文生成"""
        action_desc = plan.action[:self.ctx_limits.verify_action_desc_chars] if plan else ""
        expected = plan.expected[:self.ctx_limits.verify_expected_chars] if plan else ""
        prompt = (
            f"刚刚执行了以下修复操作:\n\n"
            f"操作:\n{action_desc}\n"
            f"预期结果: {expected}\n"
            f"目标: {self.current_target.name} ({self.current_target.mode})\n\n"
            f"请生成验证命令来确认修复是否生效。\n"
            f"要求:\n"
            f"- 只输出只读检查命令（不修改任何东西）\n"
            f"- 命令要能直接判断修复是否成功\n"
            f"- 最多 4 条命令\n"
            f"- 放在 ```commands 代码块中\n"
        )
        try:
            response = self._ask_llm(prompt, max_tokens=400, phase="VERIFY_COMMANDS")
            cmds = self._extract_commands(response)[:self.limits.config.max_quick_observe_commands]
            if cmds:
                outputs = []
                for cmd in cmds:
                    result = self._run_cmd(cmd, timeout=15)
                    outputs.append(f"$ {cmd}\n{str(result)}")
                return "\n\n".join(outputs)
        except Exception:
            pass
        return ""

    def _quick_observe(self) -> str:
        """通用快速观察（最终 fallback）"""
        commands = [
            "systemctl --failed --no-pager",
            "free -h",
            "df -h",
        ]
        outputs = []
        for cmd in commands:
            result = self._run_cmd(cmd, timeout=10)
            outputs.append(f"$ {cmd}\n{str(result)}")
        return "\n\n".join(outputs)

    # ─── reflect 解析（不变）───

    def _apply_reflect_updates(self, reflect_response: str):
        """从复盘结果中应用 Playbook 更新"""
        # 解析 NEW_PLAYBOOK 指令（优先 code fence 格式，回退旧格式）
        new_pb = re.search(
            r"NEW_PLAYBOOK:\s*(\S+\.md)\s*\nCONTENT:\s*\n```\n(.*?)\n```",
            reflect_response,
            re.DOTALL,
        )
        if not new_pb:
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

        # 解析 UPDATE_PLAYBOOK 指令（优先 code fence 格式，回退旧格式）
        update_pb = re.search(
            r"UPDATE_PLAYBOOK:\s*(\S+\.md)\s*\nAPPEND_CONTENT:\s*\n```\n(.*?)\n```",
            reflect_response,
            re.DOTALL,
        )
        if not update_pb:
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

        # 解析 LESSON 指令
        lesson_match = re.search(r'LESSON:\s*(.+)', reflect_response)
        if lesson_match and self.current_incident:
            from datetime import datetime
            ts = datetime.now().strftime("%Y-%m-%d")
            lesson_text = lesson_match.group(1).strip()
            incident_title = self.notebook.read_incident(self.current_incident).split('\n')[0]
            self.notebook.write(
                f"lessons/{ts}-{self.current_incident[:10]}.md",
                f"# {incident_title.replace('Incident:', '教训:')}\n\n{lesson_text}\n"
            )
            self.chat.say(f"记录了经验教训", "success")

    # ─── 辅助方法（不变）───

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

    def _issue_fingerprint(self, target_name: str, summary: str) -> str:
        """生成异常指纹用于静默去重。"""
        normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", summary or "")[:120]
        raw = f"{target_name}::{normalized}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]

    def _clear_issue_fingerprint(self, target_name: str, summary: str):
        """修复验证通过后清除指纹"""
        fp = self._issue_fingerprint(target_name, summary)
        self._issue_fingerprints.pop(fp, None)

    def _recent_incidents_summary(self) -> str:
        """最近 Incident 摘要"""
        files = self.notebook.list_dir("incidents/archive")[-self.limits.config.max_recent_incidents:]
        if not files:
            return "（暂无历史 Incident）"
        summaries = []
        for f in files:
            content = self.notebook.read(f"incidents/archive/{f}")
            first_line = content.split("\n")[0] if content else f
            summaries.append(f"- {first_line}")
        return "\n".join(summaries)
