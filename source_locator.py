"""
source_locator — 从 StackFrame 反向定位到本地源码文件

给定一组 StackFrame(来自 stack_parser)和可用的 SourceRepo 列表,
找到每个 frame 对应的本地源文件,读出上下文代码。

定位策略(按优先级):
  1) 路径前缀映射(path_prefix_runtime → path_prefix_local)
  2) 完整路径后缀匹配 — 找重合最多的文件
  3) 文件名匹配 — 在仓库里递归搜同名文件

保持宽容:所有错误都降级为"找不到",不抛异常。
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field

from stack_parser import StackFrame
from targets import SourceRepo

logger = logging.getLogger("ops-agent.source_locator")

# 单个源文件允许读取的最大体积(超过通常是生成代码,跳过防止上下文爆炸)
MAX_SOURCE_FILE_BYTES = 500 * 1024

# 目标行上下的默认上下文行数
DEFAULT_CONTEXT_LINES = 30

# 搜索源码时忽略的目录
_IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    "venv", ".venv", "env", ".env", "target", "build", "dist",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
}


@dataclass
class SourceLocation:
    """一个 frame 定位到本地源码的结果"""
    frame: StackFrame
    local_file: str                # 本地绝对路径
    repo_name: str                 # 所属仓库别名
    context_before: str = ""       # 目标行之前的代码
    target_line: str = ""          # 目标行本身
    context_after: str = ""        # 目标行之后的代码
    function_definition: str = ""  # 包含目标行的完整函数(尽力而为)
    start_line: int = 1            # context_before 起始行号(1-based)

    def render(self, max_chars: int = 2000) -> str:
        """渲染成人类/LLM 友好的文本块"""
        lines = []
        lines.append(f"### {self.repo_name}:{os.path.relpath(self.local_file, start='/')}"
                     f":{self.frame.line}")
        if self.frame.function:
            lines.append(f"函数: {self.frame.function}")
        lines.append("```")
        before = self.context_before.rstrip("\n")
        if before:
            for i, l in enumerate(before.splitlines()):
                lines.append(f"{self.start_line + i:5d}  {l}")
        lines.append(f"{self.frame.line:5d}> {self.target_line.rstrip()}")
        after = self.context_after.rstrip("\n")
        if after:
            for i, l in enumerate(after.splitlines()):
                lines.append(f"{self.frame.line + 1 + i:5d}  {l}")
        lines.append("```")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated)"
        return text


@dataclass
class LocateResult:
    """一次完整定位的结果"""
    locations: list[SourceLocation] = field(default_factory=list)
    unresolved: list[StackFrame] = field(default_factory=list)

    def render(self) -> str:
        """生成给 diagnose prompt 的源码上下文文本"""
        if not self.locations:
            return "（未能将异常栈映射到本地源码）"
        parts = []
        for loc in self.locations:
            parts.append(loc.render())
        if self.unresolved:
            unresolved_str = ", ".join(
                f"{f.file}:{f.line}" for f in self.unresolved[:5]
            )
            parts.append(f"\n(另有 {len(self.unresolved)} 个栈帧未能定位: {unresolved_str})")
        return "\n\n".join(parts)


class SourceLocator:
    """源码定位器。用法:

        locator = SourceLocator(target.get_source_repos())
        result = locator.locate(frames)
        print(result.render())
    """

    def __init__(self, repos: list[SourceRepo],
                 context_lines: int = DEFAULT_CONTEXT_LINES,
                 max_frames: int = 10):
        self.repos = [r for r in repos if r.path and os.path.isdir(r.path)]
        self.context_lines = context_lines
        self.max_frames = max_frames
        # 文件名索引缓存: {repo_name: {basename: [abs_path, ...]}}
        self._file_index: dict[str, dict[str, list[str]]] = {}

    # ---------- 公共 API ----------

    def locate(self, frames: list[StackFrame]) -> LocateResult:
        """对每个 frame 尝试定位。"""
        result = LocateResult()
        if not self.repos:
            result.unresolved = list(frames[: self.max_frames])
            return result

        for frame in frames[: self.max_frames]:
            loc = self._locate_single(frame)
            if loc:
                result.locations.append(loc)
            else:
                result.unresolved.append(frame)
        return result

    # ---------- 单帧定位 ----------

    def _locate_single(self, frame: StackFrame) -> SourceLocation | None:
        if not frame.file or frame.line <= 0:
            return None

        # 策略 1: 路径前缀映射
        for repo in self.repos:
            if self._lang_mismatch(repo, frame):
                continue
            if repo.path_prefix_runtime and frame.file.startswith(repo.path_prefix_runtime):
                relative = frame.file[len(repo.path_prefix_runtime):].lstrip("/\\")
                local = os.path.join(
                    repo.path,
                    repo.path_prefix_local.lstrip("/\\") if repo.path_prefix_local else "",
                    relative,
                )
                local = os.path.normpath(local)
                if os.path.isfile(local):
                    return self._build_location(frame, local, repo.name)

        # 策略 2 + 3: 按文件名/路径后缀匹配
        filename = os.path.basename(frame.file.replace("\\", "/"))
        if not filename:
            return None

        best: tuple[int, str, str] | None = None  # (score, abs_path, repo_name)
        for repo in self.repos:
            if self._lang_mismatch(repo, frame):
                continue
            candidates = self._find_in_repo(repo, filename)
            for cand in candidates:
                score = self._score_match(frame.file, cand, repo.path)
                if best is None or score > best[0]:
                    best = (score, cand, repo.name)

        if best and best[0] > 0:
            return self._build_location(frame, best[1], best[2])

        return None

    # ---------- 辅助 ----------

    @staticmethod
    def _lang_mismatch(repo: SourceRepo, frame: StackFrame) -> bool:
        """如果 repo 声明了语言且和 frame 语言冲突,则跳过。"""
        if not repo.language or not frame.language:
            return False
        rl = repo.language.lower()
        fl = frame.language.lower()
        # node / js 互认
        if fl == "node" and rl in ("node", "js", "javascript", "typescript", "ts"):
            return False
        return rl != fl

    def _find_in_repo(self, repo: SourceRepo, filename: str) -> list[str]:
        """返回 repo 中所有同名文件的绝对路径,带缓存。"""
        index = self._file_index.get(repo.name)
        if index is None:
            index = self._build_index(repo.path)
            self._file_index[repo.name] = index
        return index.get(filename, [])

    @staticmethod
    def _build_index(repo_path: str) -> dict[str, list[str]]:
        """遍历 repo 构建 basename → [abs_path] 索引。"""
        index: dict[str, list[str]] = {}
        try:
            for dirpath, dirnames, filenames in os.walk(repo_path):
                # 原地过滤忽略目录
                dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
                for fn in filenames:
                    index.setdefault(fn, []).append(os.path.join(dirpath, fn))
        except OSError as e:
            logger.debug(f"index build failed for {repo_path}: {e}")
        return index

    @staticmethod
    def _score_match(original_path: str, candidate_abs: str, repo_path: str) -> int:
        """基于路径后缀重合度打分。越多后缀段重合得分越高。

        "有后缀重合 ≥ 1 段" 是基础门槛。纯同名文件(完全无路径信息)给 1 分。
        """
        orig = original_path.replace("\\", "/").strip("/")
        cand = os.path.relpath(candidate_abs, repo_path).replace("\\", "/").strip("/")
        orig_parts = [p for p in orig.split("/") if p]
        cand_parts = [p for p in cand.split("/") if p]
        if not orig_parts or not cand_parts:
            return 0
        # 从后往前比
        score = 0
        for a, b in zip(reversed(orig_parts), reversed(cand_parts)):
            if a == b:
                score += 1
            else:
                break
        return score if score > 0 else 1  # 同名兜底

    def _build_location(self, frame: StackFrame, local_file: str,
                        repo_name: str) -> SourceLocation | None:
        """读取目标行前后上下文,组装成 SourceLocation。"""
        try:
            size = os.path.getsize(local_file)
            if size > MAX_SOURCE_FILE_BYTES:
                logger.debug(f"skipping oversized file: {local_file} ({size} bytes)")
                return None
            with open(local_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except (OSError, UnicodeError) as e:
            logger.debug(f"read failed for {local_file}: {e}")
            return None

        if frame.line < 1 or frame.line > len(lines):
            return None

        n = self.context_lines
        start = max(1, frame.line - n)           # 1-based
        end = min(len(lines), frame.line + n)    # inclusive
        before = "".join(lines[start - 1: frame.line - 1])
        target = lines[frame.line - 1]
        after = "".join(lines[frame.line: end])
        func_def = self._extract_function(lines, frame.line, frame.language or "")

        return SourceLocation(
            frame=frame,
            local_file=local_file,
            repo_name=repo_name,
            context_before=before,
            target_line=target,
            context_after=after,
            function_definition=func_def,
            start_line=start,
        )

    @staticmethod
    def _extract_function(lines: list[str], target_line: int, language: str) -> str:
        """尽力提取包含 target_line 的函数定义。找不到返回空串。

        - Python: 向前找 def/async def,以缩进为锚,向下到缩进回退
        - Java/Go/Node/C 系: 向前找第一个含 '{' 的函数签名行,向下做大括号配对
        """
        if target_line < 1 or target_line > len(lines):
            return ""

        lang = language.lower()
        if lang == "python":
            return SourceLocator._extract_python_function(lines, target_line)
        # 其余都走大括号策略
        return SourceLocator._extract_brace_function(lines, target_line)

    @staticmethod
    def _extract_python_function(lines: list[str], target_line: int) -> str:
        import re as _re
        def_re = _re.compile(r"^(\s*)(async\s+def|def)\s+\w+")

        # 向前找最近的 def
        start = -1
        def_indent = -1
        for i in range(target_line - 1, -1, -1):
            m = def_re.match(lines[i])
            if m:
                indent = len(m.group(1))
                # 不要跨越到外层函数:第一次看到的 def 就是最近的
                start = i
                def_indent = indent
                break
        if start < 0:
            return ""

        # 向下找缩进回到 def_indent 或更少的非空行
        end = len(lines)
        for j in range(start + 1, len(lines)):
            line = lines[j]
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= def_indent:
                end = j
                break
        snippet = "".join(lines[start:end])
        return snippet[:4000]

    @staticmethod
    def _extract_brace_function(lines: list[str], target_line: int) -> str:
        # 向前找最近的含 "{" 的签名行(不跨越另一个 "}")
        start = -1
        depth = 0
        for i in range(target_line - 1, -1, -1):
            line = lines[i]
            depth += line.count("}") - line.count("{")
            if depth < 0 and "{" in line:
                start = i
                break
        if start < 0:
            # 退回到简单策略:向前最多 20 行
            start = max(0, target_line - 20)

        # 向下做大括号匹配找结束
        depth = 0
        seen_open = False
        end = len(lines)
        for j in range(start, len(lines)):
            line = lines[j]
            depth += line.count("{")
            if line.count("{") > 0:
                seen_open = True
            depth -= line.count("}")
            if seen_open and depth <= 0:
                end = j + 1
                break
        snippet = "".join(lines[start:end])
        return snippet[:4000]
