"""
HumanChannel — Agent 与人类的对话通道（交互式版本）

核心设计：
1. 输入区固定在屏幕底部，Agent 输出从上方滚出，互不干扰
2. 后台监听 stdin，人类可随时打字，输入字符不会被 Agent 输出冲掉
3. 优先级：人类指令 > Agent 自主行为
4. 中断机制：人类可随时打断 Agent 当前任务

底层用 prompt_toolkit 实现固定输入框；如果未安装，自动降级为 readline 模式。
"""

import sys
import os
import queue
import threading
import logging
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger("ops-agent.chat")

# ── 终端状态保存/恢复（防止退出后 echo 丢失）──
_saved_termios = None
try:
    import termios
    if sys.stdin.isatty():
        _saved_termios = termios.tcgetattr(sys.stdin.fileno())
except Exception:
    pass


def _restore_terminal():
    """恢复终端到启动时的状态（确保 echo 等标志正常）"""
    global _saved_termios
    if _saved_termios is not None:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, _saved_termios)
        except Exception:
            pass
    # 兜底：尝试用 stty sane 恢复
    try:
        if sys.stdin.isatty():
            os.system("stty sane")
    except Exception:
        pass


import atexit
atexit.register(_restore_terminal)

# ── 尝试导入 prompt_toolkit（推荐模式）──
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style
    HAS_PTK = True
except ImportError:
    HAS_PTK = False


# ── ANSI 颜色 ──
class Color:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


# ── 紧急度对应的图标和颜色 ──
URGENCY_STYLE = {
    "info":     ("💬", Color.CYAN),
    "success":  ("✅", Color.GREEN),
    "warning":  ("⚠️ ", Color.YELLOW),
    "critical": ("🚨", Color.RED),
    "question": ("❓", Color.MAGENTA),
    "action":   ("🔧", Color.BLUE),
    "observe":  ("🔍", Color.GRAY),
}


class HumanChannel:
    """和人类沟通的通道"""

    def __init__(self, notebook, mode: str = "auto"):
        self.notebook = notebook
        self.inbox: queue.Queue = queue.Queue()
        self._running = True
        self._output_lock = threading.Lock()

        # ── 中断标志（被人类打断时设为 True）──
        self.interrupted = threading.Event()

        # ── 等待批准状态 ──
        self._waiting_approval = False
        self._approval_queue: queue.Queue = queue.Queue()

        # ── 模式：interactive (prompt_toolkit) / readline (退化) ──
        if mode == "auto":
            mode = "interactive" if (HAS_PTK and sys.stdin.isatty()) else "readline"
        self.mode = mode

        if self.mode == "interactive":
            self._init_interactive()
        else:
            self._init_readline()

    # ═══════════════════════════════════════════
    #  交互模式（prompt_toolkit）
    # ═══════════════════════════════════════════

    def _init_interactive(self):
        """初始化 prompt_toolkit 模式：底部固定输入框"""
        self._session = PromptSession(
            "  > ",
            style=Style.from_dict({"prompt": "ansicyan bold"}),
        )
        self._listener_thread = threading.Thread(
            target=self._interactive_listener, daemon=True
        )
        self._listener_thread.start()
        logger.info("Chat mode: interactive (prompt_toolkit)")

    def _interactive_listener(self):
        """后台线程：用 prompt_toolkit 持续读取输入"""
        while self._running:
            try:
                # patch_stdout 让 print 输出从输入框上方滚出
                with patch_stdout(raw=True):
                    line = self._session.prompt()
                if not line:
                    continue
                line = line.strip()
                if not line:
                    continue

                # 路由消息：批准等待中 → 走 approval 通道
                if self._waiting_approval:
                    self._approval_queue.put(line)
                else:
                    self.inbox.put(line)
                    # 触发中断标志，让 Agent 知道有新输入要处理
                    self.interrupted.set()
            except (EOFError, KeyboardInterrupt):
                self._running = False
                self.inbox.put("quit")
                self.interrupted.set()
                break
            except Exception as e:
                logger.error(f"Listener error: {e}")
                time.sleep(0.5)

    # ═══════════════════════════════════════════
    #  Readline 模式（退化）
    # ═══════════════════════════════════════════

    def _init_readline(self):
        """退化模式：基础 stdin 监听（不支持固定输入框）"""
        self._listener_thread = threading.Thread(
            target=self._readline_listener, daemon=True
        )
        self._listener_thread.start()
        logger.info("Chat mode: readline (basic, install prompt_toolkit for better UX)")

    def _readline_listener(self):
        while self._running:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                if self._waiting_approval:
                    self._approval_queue.put(line)
                else:
                    self.inbox.put(line)
                    self.interrupted.set()
            except (EOFError, OSError):
                break

    # ═══════════════════════════════════════════
    #  输出（线程安全）
    # ═══════════════════════════════════════════

    def say(self, message: str, urgency: str = "info"):
        """Agent 说话（高调，会显示给人类看）"""
        icon, color = URGENCY_STYLE.get(urgency, ("💬", Color.CYAN))
        ts = datetime.now().strftime("%H:%M:%S")

        # 多行消息缩进对齐
        lines = message.split("\n")
        first = lines[0]
        rest_lines = ["           " + l for l in lines[1:]]

        formatted = f"{Color.GRAY}[{ts}]{Color.RESET} {icon} {color}{first}{Color.RESET}"
        if rest_lines:
            formatted += "\n" + "\n".join(rest_lines)

        with self._output_lock:
            print(formatted, flush=True)

        self.notebook.log_conversation("Agent", message)

    def log(self, message: str, urgency: str = "observe"):
        """低调日志（巡检过程等内部状态，不存入对话记录）"""
        icon, color = URGENCY_STYLE.get(urgency, ("·", Color.GRAY))
        ts = datetime.now().strftime("%H:%M:%S")
        formatted = f"{Color.GRAY}[{ts}] {icon} {message}{Color.RESET}"
        with self._output_lock:
            print(formatted, flush=True)

    def notify(self, message: str, urgency: str = "info"):
        """主动通知（同 say）"""
        self.say(message, urgency)

    # ═══════════════════════════════════════════
    #  输入（非阻塞）
    # ═══════════════════════════════════════════

    def check_inbox(self) -> Optional[str]:
        """非阻塞检查有没有人类消息"""
        try:
            msg = self.inbox.get_nowait()
            self.notebook.log_conversation("Human", msg)
            return msg
        except queue.Empty:
            return None

    def has_pending(self) -> bool:
        """是否有待处理的人类消息（不消费）"""
        return not self.inbox.empty()

    def clear_interrupt(self):
        """清除中断标志（Agent 处理完中断后调用）"""
        self.interrupted.clear()

    def is_interrupted(self) -> bool:
        """检查是否被人类中断"""
        return self.interrupted.is_set()

    # ═══════════════════════════════════════════
    #  请求批准 / 提问（阻塞）
    # ═══════════════════════════════════════════

    def request_approval(self, action_description: str) -> bool:
        """请求人类批准一个行动"""
        self.say(
            f"我打算执行以下操作：\n{action_description}\n"
            f"   ▸ 输入 'y' 批准 / 'n' 否决 / 其他作为指示",
            urgency="warning",
        )

        self._waiting_approval = True
        try:
            response = self._approval_queue.get(timeout=600)
        except queue.Empty:
            self.say("等待超时，取消操作。", "warning")
            return False
        finally:
            self._waiting_approval = False

        self.notebook.log_conversation("Human", response)
        response_lower = response.strip().lower()

        if response_lower in ("y", "yes", "approve", "ok", "确认", "批准", "同意"):
            self.say("收到批准，开始执行。", "success")
            return True
        elif response_lower in ("n", "no", "deny", "cancel", "拒绝", "否决", "不"):
            self.say("操作已取消。", "info")
            return False
        else:
            # 其他内容当作新指令推回 inbox
            self.say(f"收到你的指示：{response}，原操作暂不执行。", "info")
            self.inbox.put(response)
            self.interrupted.set()
            return False

    def ask_question(self, question: str, timeout: int = 600) -> Optional[str]:
        """Agent 主动提问，阻塞等待回答"""
        self.say(question, urgency="question")

        self._waiting_approval = True
        try:
            response = self._approval_queue.get(timeout=timeout)
            self.notebook.log_conversation("Human", response)
            return response
        except queue.Empty:
            return None
        finally:
            self._waiting_approval = False

    def escalate(self, summary: str, details: str = ""):
        """升级给人类"""
        msg = f"遇到超出我能力的问题，需要你的帮助：\n{summary}"
        if details:
            msg += f"\n详情：{details}"
        self.say(msg, urgency="critical")

    # ═══════════════════════════════════════════
    #  生命周期
    # ═══════════════════════════════════════════

    def stop(self):
        self._running = False
        # 恢复终端状态（prompt_toolkit 可能把 echo 关掉了）
        _restore_terminal()

    def banner(self, agent_name: str = "OpsAgent"):
        """启动横幅"""
        mode_hint = "" if self.mode == "interactive" else f"  {Color.YELLOW}(基础模式，建议 pip install prompt_toolkit){Color.RESET}"
        with self._output_lock:
            print(f"""
{Color.CYAN}╭───────────────────────────────────────────────────────╮
│  {Color.BOLD}{agent_name}{Color.RESET}{Color.CYAN} — 数字运维员工已上岗                   │
│                                                       │
│  随时打字和我对话。常用指令：                            │
│    {Color.YELLOW}status{Color.CYAN}    查看我的状态                              │
│    {Color.YELLOW}pause{Color.CYAN}     暂停我的自主行动                           │
│    {Color.YELLOW}resume{Color.CYAN}    恢复自主行动                              │
│    {Color.YELLOW}stop{Color.CYAN}      停止当前调查回到巡检                       │
│    {Color.YELLOW}quit{Color.CYAN}      让我下班                                 │
╰───────────────────────────────────────────────────────╯{Color.RESET}{mode_hint}
""", flush=True)
