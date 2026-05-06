"""
HumanChannel — Agent 与人类的对话通道（多后端版本）

核心设计：
1. 多后端并行：Console / Feishu / ... 共享 inbox / approval_queue / interrupted
2. 任意后端收到的输入都进入统一 inbox，agent 主循环无感知
3. say / log 等输出广播到所有后端
4. 批准请求广播，任一后端先回复即生效

底层 Console 后端用 prompt_toolkit 实现固定输入框；如果未安装，自动降级为 readline 模式。
"""

import sys
import os
import queue
import threading
import logging
import time
from datetime import datetime
from typing import Optional, List

from src.i18n import t
from src.infra.channel_backend import ChannelBackend

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
    "observe":  ("·", Color.GRAY),
}

# ── 分隔线 ──
_SEPARATOR = f"{Color.DIM}{'─' * 50}{Color.RESET}"


def _raw_print(*args, **kwargs):
    """绕过 patch_stdout，直接写原始 stdout，保留 ANSI 颜色且不干扰 prompt_toolkit"""
    kwargs.setdefault("flush", True)
    print(*args, file=sys.__stdout__, **kwargs)


class ConsoleBackend(ChannelBackend):
    """终端交互后端 — 原有 chat.py 的输入/输出逻辑"""

    def __init__(self, mode: str = "auto"):
        self._running = True
        self._output_lock = threading.Lock()
        self._waiting_approval = False
        self._inbox: queue.Queue = queue.Queue()
        self._approval_queue: queue.Queue = queue.Queue()
        self._interrupted: threading.Event = threading.Event()

        if mode == "auto":
            mode = "interactive" if (HAS_PTK and sys.stdin.isatty()) else "readline"
        self.mode = mode

    def start(self, inbox: queue.Queue, approval_queue: queue.Queue,
              interrupted: threading.Event) -> None:
        self._inbox = inbox
        self._approval_queue = approval_queue
        self._interrupted = interrupted

        if self.mode == "interactive":
            self._session = PromptSession(
                "  > ",
                style=Style.from_dict({"prompt": "ansicyan bold"}),
            )
            self._listener_thread = threading.Thread(
                target=self._interactive_listener, daemon=True
            )
        else:
            self._listener_thread = threading.Thread(
                target=self._readline_listener, daemon=True
            )
        self._listener_thread.start()
        logger.info(f"ConsoleBackend started (mode={self.mode})")

    def _interactive_listener(self):
        while self._running:
            try:
                with patch_stdout(raw=True):
                    line = self._session.prompt()
                if not line:
                    continue
                line = line.strip()
                if not line:
                    continue
                with self._output_lock:
                    ts = datetime.now().strftime("%H:%M:%S")
                    _raw_print(
                        f"{Color.GRAY}[{ts}]{Color.RESET} "
                        f"{Color.BOLD}{Color.YELLOW}▶ {line}{Color.RESET}",
                        flush=True,
                    )
                    _raw_print(_SEPARATOR, flush=True)
                if self._waiting_approval:
                    self._approval_queue.put(("console", line))
                else:
                    self._inbox.put(line)
                    self._interrupted.set()
            except (EOFError, KeyboardInterrupt):
                self._running = False
                self._inbox.put("quit")
                self._interrupted.set()
                break
            except Exception as e:
                logger.error(f"Listener error: {e}")
                time.sleep(0.5)

    def _readline_listener(self):
        while self._running:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                ts = datetime.now().strftime("%H:%M:%S")
                _raw_print(
                    f"{Color.GRAY}[{ts}]{Color.RESET} "
                    f"{Color.BOLD}{Color.YELLOW}▶ {line}{Color.RESET}",
                    flush=True,
                )
                _raw_print(_SEPARATOR, flush=True)
                if self._waiting_approval:
                    self._approval_queue.put(("console", line))
                else:
                    self._inbox.put(line)
                    self._interrupted.set()
            except (EOFError, OSError):
                break

    def send(self, message: str, urgency: str = "info") -> None:
        icon, color = URGENCY_STYLE.get(urgency, ("💬", Color.CYAN))
        ts = datetime.now().strftime("%H:%M:%S")
        lines = message.split("\n")
        first = lines[0]
        formatted = f"{Color.GRAY}[{ts}]{Color.RESET} {icon} {color}{first}{Color.RESET}"
        if len(lines) > 1:
            formatted += "\n" + "\n".join(lines[1:])
        with self._output_lock:
            _raw_print(formatted, flush=True)
            _raw_print(_SEPARATOR, flush=True)

    def send_log(self, message: str, urgency: str = "observe") -> None:
        icon, color = URGENCY_STYLE.get(urgency, ("·", Color.GRAY))
        ts = datetime.now().strftime("%H:%M:%S")
        formatted = f"{Color.GRAY}[{ts}] {icon} {message}{Color.RESET}"
        with self._output_lock:
            _raw_print(formatted, flush=True)

    def send_cmd_log(self, cmd: str) -> None:
        with self._output_lock:
            _raw_print(f"{Color.DIM}           │ {cmd}{Color.RESET}", flush=True)

    def request_approval(self, action_description: str) -> None:
        self.send(
            t("chat.approval_prompt", action=action_description),
            urgency="warning",
        )

    @property
    def waiting_approval(self):
        return self._waiting_approval

    @waiting_approval.setter
    def waiting_approval(self, value: bool):
        self._waiting_approval = value

    def stop(self) -> None:
        self._running = False
        _restore_terminal()


class HumanChannel:
    """和人类沟通的通道 — 多后端调度器"""

    def __init__(self, notebook, backends: Optional[List[ChannelBackend]] = None, mode: str = "auto"):
        self.notebook = notebook
        self.inbox: queue.Queue = queue.Queue()
        self._approval_queue: queue.Queue = queue.Queue()
        self._running = True
        self._output_lock = threading.Lock()

        # ── 中断标志（被人类打断时设为 True）──
        self.interrupted = threading.Event()

        # ── 等待批准状态 ──
        self._waiting_approval = False

        # ── 后端 ──
        if backends is not None:
            self.backends: List[ChannelBackend] = backends
        else:
            self.backends = [ConsoleBackend(mode=mode)]

        self._console_backend: Optional[ConsoleBackend] = None
        for b in self.backends:
            b.start(self.inbox, self._approval_queue, self.interrupted)
            # ConsoleBackend 需要知道 approval 状态
            if isinstance(b, ConsoleBackend):
                self._console_backend = b

    # ═══════════════════════════════════════════
    #  输出（广播到所有后端）
    # ═══════════════════════════════════════════

    def say(self, message: str, urgency: str = "info"):
        """Agent 说话（高调，会显示给人类看）"""
        for b in self.backends:
            try:
                b.send(message, urgency)
            except Exception as e:
                logger.warning(f"backend {type(b).__name__} send failed: {e}")
        self.notebook.log_conversation("Agent", message)

    def log(self, message: str, urgency: str = "observe"):
        """低调日志（巡检过程等内部状态，不存入对话记录）"""
        for b in self.backends:
            try:
                b.send_log(message, urgency)
            except Exception as e:
                logger.warning(f"backend {type(b).__name__} send_log failed: {e}")

    def cmd_log(self, cmd: str):
        """命令执行日志"""
        for b in self.backends:
            try:
                b.send_cmd_log(cmd)
            except Exception as e:
                logger.warning(f"backend {type(b).__name__} cmd_log failed: {e}")

    def llm_log(self, phase: str = ""):
        """LLM 交互提示"""
        ts = datetime.now().strftime("%H:%M:%S")
        label = f" {phase}" if phase else ""
        # 只在 console 显示，其他后端忽略
        if self._console_backend:
            with self._console_backend._output_lock:
                _raw_print(
                    f"{Color.DIM}[{ts}] ◈ LLM{label}{Color.RESET}",
                    flush=True,
                )

    def progress(self, message: str):
        """轻量进度提示"""
        ts = datetime.now().strftime("%H:%M:%S")
        formatted = f"{Color.GRAY}[{ts}] → {message}{Color.RESET}"
        # console only
        if self._console_backend:
            with self._console_backend._output_lock:
                _raw_print(formatted, flush=True)

    def trace(self, phase: str, content: str):
        """详细过程记录 — 只写文件，不上屏幕"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n### [{ts}] {phase}\n{content}\n"
        trace_dir = os.path.join(str(self.notebook.path), "trace")
        os.makedirs(trace_dir, exist_ok=True)
        filename = getattr(self, '_trace_file', 'patrol') + ".md"
        filepath = os.path.join(trace_dir, filename)
        try:
            with open(filepath, "a", encoding="utf-8", errors="replace") as f:
                f.write(entry)
        except OSError:
            pass

    def notify(self, message: str, urgency: str = "info"):
        """主动通知（同 say）"""
        self.say(message, urgency)

    # ═══════════════════════════════════════════
    #  输入（非阻塞）
    # ═══════════════════════════════════════════

    def check_inbox(self) -> Optional[str]:
        """非阻塞检查有没有人类消息"""
        try:
            item = self.inbox.get_nowait()
            # 兼容：(source, text) 或纯 text
            if isinstance(item, tuple):
                source, text = item
                self.notebook.log_conversation(f"Human({source})", text)
                return text
            else:
                self.notebook.log_conversation("Human", item)
                return item
        except queue.Empty:
            return None

    def has_pending(self) -> bool:
        return not self.inbox.empty()

    def clear_interrupt(self):
        self.interrupted.clear()

    def is_interrupted(self) -> bool:
        return self.interrupted.is_set()

    # ═══════════════════════════════════════════
    #  请求批准 / 提问（阻塞）
    # ═══════════════════════════════════════════

    def request_approval(self, action_description: str) -> bool:
        """请求人类批准一个行动 — 广播到所有后端，任一先回复即生效"""
        self._set_waiting_approval(True)

        for b in self.backends:
            try:
                b.request_approval(action_description)
            except Exception as e:
                logger.warning(f"backend {type(b).__name__} request_approval failed: {e}")

        try:
            response_item = self._approval_queue.get(timeout=600)
        except queue.Empty:
            self.say(t("chat.approval_timeout"), "warning")
            return False
        finally:
            self._set_waiting_approval(False)

        # 兼容：(source, text) 或纯 text
        if isinstance(response_item, tuple):
            source, response = response_item
        else:
            source, response = "unknown", response_item

        self.notebook.log_conversation(f"Human({source})", response)
        response_lower = response.strip().lower()

        if response_lower in ("y", "yes", "approve", "ok", "确认", "批准", "同意"):
            self.say(t("chat.approval_received", source=source), "success")
            return True
        elif response_lower in ("n", "no", "deny", "cancel", "拒绝", "否决", "不"):
            self.say(t("chat.rejection_received", source=source), "info")
            return False
        else:
            self.say(t("chat.instruction_received", source=source, response=response), "info")
            self.inbox.put(response)
            self.interrupted.set()
            return False

    def ask_question(self, question: str, timeout: int = 600) -> Optional[str]:
        """Agent 主动提问，阻塞等待回答"""
        self.say(question, urgency="question")

        self._set_waiting_approval(True)
        try:
            response_item = self._approval_queue.get(timeout=timeout)
            if isinstance(response_item, tuple):
                source, response = response_item
                self.notebook.log_conversation(f"Human({source})", response)
            else:
                response = response_item
                self.notebook.log_conversation("Human", response)
            return response
        except queue.Empty:
            return None
        finally:
            self._set_waiting_approval(False)

    def escalate(self, summary: str, details: str = ""):
        """升级给人类"""
        msg = t("chat.escalate_msg", summary=summary)
        if details:
            msg += t("chat.escalate_detail", details=details)
        self.say(msg, urgency="critical")

    def _set_waiting_approval(self, value: bool):
        """同步所有后端的 waiting_approval 状态"""
        self._waiting_approval = value
        for b in self.backends:
            try:
                if isinstance(b, ConsoleBackend):
                    b.waiting_approval = value
                else:
                    b.set_waiting_approval(value)
            except Exception:
                pass

    # ═══════════════════════════════════════════
    #  生命周期
    # ═══════════════════════════════════════════

    def stop(self):
        for b in self.backends:
            try:
                b.stop()
            except Exception:
                pass

    def banner(self, agent_name: str = "OpsAgent"):
        """启动横幅"""
        mode_hint = ""
        if self._console_backend and self._console_backend.mode != "interactive":
            mode_hint = f"  {Color.YELLOW}(basic mode, pip install prompt_toolkit recommended){Color.RESET}"

        backend_names = ", ".join(type(b).__name__ for b in self.backends)
        banner_title = t("chat.banner_title", agent_name=agent_name)
        banner_hint = t("chat.banner_hint")

        banner_channel = t("chat.banner_channel")
        banner_status = t("chat.banner_status")
        banner_pause = t("chat.banner_pause")
        banner_resume = t("chat.banner_resume")
        banner_stop = t("chat.banner_stop")
        banner_help = t("chat.banner_help")
        banner_quit = t("chat.banner_quit")

        # 动态计算显示宽度（兼容 CJK 双宽字符）
        def _display_width(s: str) -> int:
            import unicodedata
            w = 0
            for ch in s:
                eaw = unicodedata.east_asian_width(ch)
                w += 2 if eaw in ('W', 'F') else 1
            return w

        # 构建各行内容（不含颜色、边框），然后统一对齐
        commands = [
            ("status", banner_status),
            ("pause",  banner_pause),
            ("resume", banner_resume),
            ("stop",   banner_stop),
            ("help",   banner_help),
            ("quit",   banner_quit),
        ]

        # 计算所需宽度：取所有行中最长者
        inner_lines = [
            f"  {banner_title}",
            "",
            f"  {banner_channel}: {backend_names}",
            "",
            f"  {banner_hint}",
        ]
        for cmd, desc in commands:
            inner_lines.append(f"    {cmd}    {desc}")

        inner_width = max(_display_width(line) for line in inner_lines)
        box_width = inner_width + 2  # 左右 │ 各占 1

        def _pad(line: str) -> str:
            """右填充空格使显示宽度对齐"""
            pw = inner_width - _display_width(line)
            return line + " " * max(pw, 0)

        with self._console_backend._output_lock if self._console_backend else self._output_lock:
            border = "─" * box_width
            lines = []
            lines.append(f"╭{border}╮")
            lines.append(f"│{_pad(f'  {Color.BOLD}{banner_title}{Color.RESET}')}{Color.CYAN}│")
            lines.append(f"│{_pad('')}│")
            lines.append(f"│{_pad(f'  {banner_channel}: {Color.YELLOW}{backend_names}{Color.RESET}')}{Color.CYAN}│")
            lines.append(f"│{_pad('')}│")
            lines.append(f"│{_pad(f'  {banner_hint}')}│")
            for cmd, desc in commands:
                lines.append(f"│{_pad(f'    {Color.YELLOW}{cmd}{Color.CYAN}    {desc}')}│")
            lines.append(f"╰{border}╯")

            _raw_print(Color.CYAN + f"\n".join(lines) + Color.RESET + mode_hint, flush=True)
