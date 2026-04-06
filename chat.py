"""
HumanChannel — Agent 与人类的沟通通道
MVP 实现 CLI 模式，未来可扩展 Slack/钉钉/Web。
"""

import sys
import queue
import threading
import logging
from datetime import datetime

logger = logging.getLogger("ops-agent.chat")


class HumanChannel:
    """和人类沟通的通道"""

    def __init__(self, notebook, mode: str = "cli"):
        self.notebook = notebook
        self.mode = mode
        self.inbox = queue.Queue()
        self._running = False

        if mode == "cli":
            self._start_cli_listener()

    def _start_cli_listener(self):
        """后台线程监听 stdin"""
        self._running = True

        def listener():
            while self._running:
                try:
                    if sys.stdin.readable():
                        line = sys.stdin.readline()
                        if line.strip():
                            self.inbox.put(line.strip())
                except (EOFError, OSError):
                    break

        t = threading.Thread(target=listener, daemon=True)
        t.start()

    def check_inbox(self) -> str | None:
        """非阻塞检查有没有人类消息"""
        try:
            msg = self.inbox.get_nowait()
            self.notebook.log_conversation("Human", msg)
            return msg
        except queue.Empty:
            return None

    def wait_for_response(self, timeout: int = 300) -> str | None:
        """阻塞等待人类回复（用于请求批准等场景）"""
        try:
            msg = self.inbox.get(timeout=timeout)
            self.notebook.log_conversation("Human", msg)
            return msg
        except queue.Empty:
            return None

    def say(self, message: str, urgency: str = "info"):
        """Agent 说话（输出到人类）"""
        prefix_map = {
            "info":     "💬",
            "success":  "✅",
            "warning":  "⚠️ ",
            "critical": "🚨",
            "question": "❓",
            "action":   "🔧",
        }
        prefix = prefix_map.get(urgency, "💬")
        ts = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{ts}] {prefix} {message}"

        if self.mode == "cli":
            print(formatted, flush=True)

        self.notebook.log_conversation("Agent", message)

    def notify(self, message: str, urgency: str = "info"):
        """Agent 主动通知"""
        self.say(message, urgency)

    def request_approval(self, action_description: str) -> bool:
        """请求人类批准一个行动，返回是否批准"""
        self.say(
            f"我打算执行以下操作：\n"
            f"   {action_description}\n"
            f"   请输入 'y' 批准 / 'n' 否决 / 其他内容作为指示：",
            urgency="warning",
        )
        response = self.wait_for_response(timeout=600)

        if response is None:
            self.say("等待超时，取消操作。", "warning")
            return False

        response_lower = response.strip().lower()
        if response_lower in ("y", "yes", "approve", "ok", "确认", "批准"):
            self.say("收到批准，开始执行。", "success")
            return True
        elif response_lower in ("n", "no", "deny", "cancel", "拒绝", "否决"):
            self.say("操作已取消。", "info")
            return False
        else:
            self.say(f"收到你的指示：{response}，暂不执行原操作。", "info")
            return False

    def escalate(self, summary: str, details: str = ""):
        """升级给人类"""
        self.say(
            f"遇到超出我能力的问题，需要你的帮助：\n"
            f"   {summary}\n"
            + (f"   详情：{details}" if details else ""),
            urgency="critical",
        )

    def ask_question(self, question: str) -> str | None:
        """Agent 提问，等待人类回答"""
        self.say(question, urgency="question")
        return self.wait_for_response(timeout=600)

    def stop(self):
        self._running = False
