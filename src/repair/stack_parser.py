"""
stack_parser — 多语言 stack trace / traceback 解析器

把一段包含异常栈的文本解析成统一的 StackFrame 列表。
目前支持:Python / Java / Go / Node.js。

设计原则:
- 宽容:解析失败返回空列表而不是抛异常
- 启发式:优先识别最明确的语言特征,再回退到通用正则
- 无依赖:只用 re 和标准库

供 source_locator 使用。
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("ops-agent.stack_parser")


@dataclass
class StackFrame:
    """统一的栈帧表示"""
    file: str              # 原始文本里的路径(可能是容器内路径)
    line: int              # 行号
    function: str = ""     # 函数/方法名
    module: str = ""       # 模块/类名
    language: str = ""     # python / java / go / node


@dataclass
class ParsedTrace:
    """一次解析的完整结果"""
    language: str = ""
    exception_type: str = ""    # e.g. NullPointerException, AttributeError
    exception_message: str = ""
    frames: list[StackFrame] = field(default_factory=list)

    def signature(self) -> str:
        """生成一个异常指纹,用于 Sprint 4 的复发检测"""
        top = self.frames[0] if self.frames else None
        if top:
            return f"{self.language}:{self.exception_type}:{top.file}:{top.line}"
        return f"{self.language}:{self.exception_type}"


class StackTraceParser:
    """多语言 stack trace 解析器。

    用法:
        parser = StackTraceParser()
        result = parser.parse(log_text)
        for frame in result.frames:
            print(frame.file, frame.line)
    """

    # ---------- 入口 ----------

    def parse(self, text: str) -> ParsedTrace:
        """自动识别语言并解析。解析失败返回空 ParsedTrace。"""
        if not text:
            return ParsedTrace()

        try:
            if self._looks_like_python(text):
                return self._parse_python(text)
            if self._looks_like_java(text):
                return self._parse_java(text)
            if self._looks_like_go(text):
                return self._parse_go(text)
            if self._looks_like_node(text):
                return self._parse_node(text)
        except Exception as e:
            logger.debug(f"stack parse failed: {e}")

        return ParsedTrace()

    def extract_and_parse(self, text: str) -> ParsedTrace:
        """从一段可能包含很多噪音的日志里尽力抽取并解析。"""
        # 先直接试
        result = self.parse(text)
        if result.frames:
            return result
        # 尝试找 traceback 开始标志
        markers = [
            "Traceback (most recent call last)",
            "Exception in thread",
            "panic:",
            "TypeError:",
            "ReferenceError:",
        ]
        for m in markers:
            idx = text.find(m)
            if idx >= 0:
                chunk = text[idx:idx + 8000]
                result = self.parse(chunk)
                if result.frames:
                    return result
        return ParsedTrace()

    # ---------- 语言识别 ----------

    @staticmethod
    def _looks_like_python(text: str) -> bool:
        return "Traceback (most recent call last)" in text or \
               bool(re.search(r'File "[^"]+", line \d+', text))

    @staticmethod
    def _looks_like_java(text: str) -> bool:
        return bool(re.search(r"\bat [\w.$]+\([^)]*\.java:\d+\)", text)) or \
               "Exception in thread" in text

    @staticmethod
    def _looks_like_go(text: str) -> bool:
        return "goroutine " in text and ("panic:" in text or ".go:" in text)

    @staticmethod
    def _looks_like_node(text: str) -> bool:
        # Node 形如: at foo (/app/x.js:12:5)
        return bool(re.search(r"\bat \S.*\([^)]+\.(js|mjs|ts|cjs):\d+:\d+\)", text)) or \
               bool(re.search(r"\bat [^\s(]+\.(js|mjs|ts|cjs):\d+:\d+", text))

    # ---------- Python ----------

    _PY_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
    _PY_EXC = re.compile(r"^([A-Za-z_][\w.]*Error|[A-Za-z_][\w.]*Exception|[A-Za-z_][\w.]*Warning)(?::\s*(.*))?$")

    def _parse_python(self, text: str) -> ParsedTrace:
        frames = []
        for m in self._PY_FRAME.finditer(text):
            frames.append(StackFrame(
                file=m.group(1),
                line=int(m.group(2)),
                function=m.group(3),
                language="python",
            ))
        # Python traceback 是从旧到新,最顶层(出错点)在最后
        # 反转一下让 frames[0] 是出错位置
        frames.reverse()

        exc_type, exc_msg = "", ""
        # 最后一行非空行通常是 "ExceptionType: message"
        for line in reversed(text.strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            m = self._PY_EXC.match(line)
            if m:
                exc_type = m.group(1)
                exc_msg = (m.group(2) or "").strip()
            break

        return ParsedTrace(
            language="python",
            exception_type=exc_type,
            exception_message=exc_msg,
            frames=frames,
        )

    # ---------- Java ----------

    _JAVA_FRAME = re.compile(r"\bat\s+([\w.$]+)\.([\w$<>]+)\(([\w$.]+):(\d+)\)")
    _JAVA_FRAME_NATIVE = re.compile(r"\bat\s+([\w.$]+)\.([\w$<>]+)\(Native Method\)")
    _JAVA_EXC_LINE = re.compile(
        r"(?:Exception in thread \"[^\"]*\"\s+)?"
        r"([\w.$]*(?:Exception|Error|Throwable))(?::\s*(.*))?"
    )

    def _parse_java(self, text: str) -> ParsedTrace:
        frames = []
        for line in text.splitlines():
            m = self._JAVA_FRAME.search(line)
            if m:
                class_full, method, filename, lineno = m.groups()
                # 把 class 名拆成 module(包.类) + function(方法)
                frames.append(StackFrame(
                    file=filename,       # 原始只有文件名(Foo.java),locator 要按后缀匹配
                    line=int(lineno),
                    function=method,
                    module=class_full,
                    language="java",
                ))

        exc_type, exc_msg = "", ""
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("at "):
                continue
            m = self._JAVA_EXC_LINE.match(s)
            if m:
                exc_type = m.group(1).split(".")[-1]
                exc_msg = (m.group(2) or "").strip()
                break

        return ParsedTrace(
            language="java",
            exception_type=exc_type,
            exception_message=exc_msg,
            frames=frames,
        )

    # ---------- Go ----------

    # Go panic 格式:
    # panic: runtime error: ...
    # goroutine 1 [running]:
    # main.handleRequest(0xc0001020e0)
    #         /app/main.go:42 +0x1a
    _GO_FRAME_FUNC = re.compile(r"^([\w./\-]+(?:\.[\w.$]+)+)\(")
    _GO_FRAME_FILE = re.compile(r"^\s*([/\w.\-]+\.go):(\d+)")

    def _parse_go(self, text: str) -> ParsedTrace:
        frames = []
        lines = text.splitlines()
        i = 0
        current_func = ""
        while i < len(lines):
            line = lines[i]
            # 函数行
            m = self._GO_FRAME_FUNC.match(line.strip())
            if m:
                current_func = m.group(1)
            # 文件行
            m2 = self._GO_FRAME_FILE.match(line)
            if m2 and current_func:
                frames.append(StackFrame(
                    file=m2.group(1),
                    line=int(m2.group(2)),
                    function=current_func.split(".")[-1],
                    module=".".join(current_func.split(".")[:-1]),
                    language="go",
                ))
                current_func = ""
            i += 1

        exc_type, exc_msg = "panic", ""
        for line in lines:
            s = line.strip()
            if s.startswith("panic:"):
                exc_msg = s[len("panic:"):].strip()
                break

        return ParsedTrace(
            language="go",
            exception_type=exc_type,
            exception_message=exc_msg,
            frames=frames,
        )

    # ---------- Node.js ----------

    # 两种形式:
    #   at funcName (/app/x.js:12:5)
    #   at /app/x.js:12:5         (匿名)
    _NODE_FRAME_NAMED = re.compile(
        r"\bat\s+([^\s(]+)\s+\(([^)]+\.(?:js|mjs|ts|cjs)):(\d+):(\d+)\)"
    )
    _NODE_FRAME_ANON = re.compile(
        r"\bat\s+([^\s(]+\.(?:js|mjs|ts|cjs)):(\d+):(\d+)"
    )
    _NODE_EXC = re.compile(
        r"^([A-Za-z_]\w*(?:Error|Exception))(?::\s*(.*))?$"
    )

    def _parse_node(self, text: str) -> ParsedTrace:
        frames: list[StackFrame] = []
        seen = set()

        for m in self._NODE_FRAME_NAMED.finditer(text):
            func, filename, lineno, _col = m.groups()
            key = (filename, int(lineno), func)
            if key in seen:
                continue
            seen.add(key)
            frames.append(StackFrame(
                file=filename,
                line=int(lineno),
                function=func,
                language="node",
            ))

        for m in self._NODE_FRAME_ANON.finditer(text):
            filename, lineno, _col = m.groups()
            key = (filename, int(lineno), "")
            # 避免与 named 重复
            if any(f.file == filename and f.line == int(lineno) for f in frames):
                continue
            if key in seen:
                continue
            seen.add(key)
            frames.append(StackFrame(
                file=filename,
                line=int(lineno),
                function="<anonymous>",
                language="node",
            ))

        exc_type, exc_msg = "", ""
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("at "):
                continue
            m = self._NODE_EXC.match(s)
            if m:
                exc_type = m.group(1)
                exc_msg = (m.group(2) or "").strip()
                break

        return ParsedTrace(
            language="node",
            exception_type=exc_type,
            exception_message=exc_msg,
            frames=frames,
        )
