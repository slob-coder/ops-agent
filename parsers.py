"""
LLM 输出解析 & 辅助方法 Mixin
"""

import re
import hashlib
import logging
from typing import Optional

logger = logging.getLogger("ops-agent")


class ParsersMixin:
    """LLM 输出解析、命令提取、指纹计算等工具方法"""

    def _extract_commands(self, text: str) -> list:
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
            "confidence": 60,
            "gaps": "",
            "escalate": "NO",
            "type": "unknown",
        }

        valid_types = {"code_bug", "runtime", "config", "resource", "external", "unknown"}

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
            elif "类型" in lower or section.lstrip().lower().startswith("type"):
                # 抓第一个出现的合法关键词
                for vt in valid_types:
                    if re.search(rf"\b{vt}\b", section):
                        result["type"] = vt
                        break

        return result

    def _parse_plan(self, response: str) -> Optional["ActionPlan"]:
        """解析 plan 的输出为 ActionPlan"""
        from trust import ActionPlan

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
        """生成异常指纹用于静默去重。

        同一目标、同一症状归一化后应得到相同的指纹。
        为了对小幅抖动鲁棒(比如错误里带时间戳/行号),我们只取
        summary 中的字母数字字符,截断后与 target_name 拼接。
        """
        normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", summary or "")[:120]
        raw = f"{target_name}::{normalized}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]

    def _clear_issue_fingerprint(self, target_name: str, summary: str):
        """修复验证通过后清除指纹,允许同类问题下次复发时立刻再次触发。"""
        fp = self._issue_fingerprint(target_name, summary)
        self._issue_fingerprints.pop(fp, None)

    def _quick_observe(self) -> str:
        """快速观察当前状态（用于修复前后对比）"""
        commands = [
            "systemctl --failed --no-pager",
            "free -h",
            "df -h",
        ]
        outputs = []
        for cmd in commands:
            result = self._run_cmd(cmd, timeout=10)
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
