"""
patch_generator — 基于诊断结果和源码定位生成补丁

由 LLM 生成 unified diff 格式的补丁,本模块负责构造 prompt、解析输出。
完全不接触 git / 文件系统;应用与验证的责任在 patch_applier 里。

关键约束:
- LLM 输出必须包含三段:修改说明 / 修改的文件 / Diff
- diff 必须是标准 unified diff,可以被 `git apply` 接受
- 解析失败返回 None,而不是抛异常(让上层重试)
"""

from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("ops-agent.patch_generator")


@dataclass
class Patch:
    """一个待应用的补丁"""
    repo_name: str
    repo_path: str                          # 仓库本地路径
    diff: str                               # unified diff 文本
    description: str = ""                   # 人类可读的修改说明
    files_changed: list[str] = field(default_factory=list)  # 仓库相对路径列表

    def is_valid(self) -> bool:
        if not self.diff.strip():
            return False
        # 至少有一个 hunk header
        if "@@" not in self.diff:
            return False
        # 至少有一个 +++ 目标文件
        if "+++ " not in self.diff:
            return False
        return True

    def touches_only_tests(self) -> bool:
        """补丁是否只改测试文件 — 防止 LLM 改测试让结果作弊"""
        if not self.files_changed:
            return False
        return all(self._is_test_path(f) for f in self.files_changed)

    @staticmethod
    def _is_test_path(path: str) -> bool:
        p = path.lower()
        if "/test" in p or p.startswith("test"):
            return True
        if "/tests/" in p or "/__tests__/" in p:
            return True
        base = os.path.basename(p)
        if base.startswith("test_") or base.endswith("_test.py") \
                or base.endswith(".test.js") or base.endswith(".spec.js") \
                or base.endswith("test.go"):
            return True
        return False


class PatchGenerator:
    """补丁生成器。

    用法:
        gen = PatchGenerator(llm_client)
        patch = gen.generate(diagnosis, locations, repo, retry_context="")
    """

    SYSTEM_PROMPT = (
        "You are a senior engineer fixing a production bug. "
        "You output ONLY a unified diff patch in the exact format requested. "
        "You make the minimal change that fixes the root cause. "
        "You NEVER modify test files unless explicitly asked. "
        "You NEVER add unrelated refactors."
    )

    def __init__(self, llm, prompt_template: str | None = None):
        self.llm = llm
        self._template = prompt_template

    # ---------- 公共 API ----------

    def generate(self, diagnosis: dict, locations: list,
                 repo, retry_context: str = "") -> Patch | None:
        """生成补丁

        diagnosis: _parse_diagnosis 的结果(dict)
        locations: list[SourceLocation] 来自 source_locator
        repo: SourceRepo
        retry_context: 上一次失败的反馈(空 = 首次)
        """
        prompt = self._build_prompt(diagnosis, locations, repo, retry_context)
        try:
            response = self.llm.ask(prompt, system=self.SYSTEM_PROMPT, max_tokens=2048)
        except Exception as e:
            logger.warning(f"LLM call failed during patch generation: {e}")
            return None
        if not response or not response.strip():
            logger.warning(f"patch_generator: LLM returned empty response (prompt_len={len(prompt)})")
        return self.parse_response(response, repo)

    # ---------- prompt 构造 ----------

    def _build_prompt(self, diagnosis, locations, repo, retry_context) -> str:
        if self._template:
            tmpl = self._template
        else:
            tmpl = self._default_template()

        loc_text = self._render_locations(locations)
        diag_text = self._render_diagnosis(diagnosis)
        retry_text = retry_context.strip() if retry_context else "(首次尝试)"
        file_contents = self._render_full_file_contents(locations, repo)

        return (tmpl
                .replace("{diagnosis}", diag_text)
                .replace("{source_locations}", loc_text)
                .replace("{file_contents}", file_contents)
                .replace("{repo_name}", repo.name if repo else "")
                .replace("{repo_language}", getattr(repo, "language", "") or "")
                .replace("{retry_context}", retry_text))

    @staticmethod
    def _default_template() -> str:
        return (
            "# 任务: 生成修复补丁\n\n"
            "你正在修复一个生产 bug。基于下面的诊断和源码,生成一个最小化的补丁。\n\n"
            "## 诊断\n{diagnosis}\n\n"
            "## 涉及的源码片段（定位参考）\n{source_locations}\n\n"
            "## 需要修改的文件完整内容\n"
            "以下是所有涉及文件的完整源码。**diff 中的行号、上下文行必须与这些文件内容完全一致。**\n"
            "{file_contents}\n\n"
            "## 仓库\n名称: {repo_name}, 语言: {repo_language}\n\n"
            "## 上次尝试反馈\n{retry_context}\n\n"
            "## 输出格式(严格遵循)\n\n"
            "## 修改说明\n"
            "<一段话:这个补丁做了什么、为什么能修这个 bug>\n\n"
            "## 修改的文件\n"
            "- <仓库相对路径>\n\n"
            "## Diff\n"
            "```diff\n"
            "--- a/<相对路径>\n"
            "+++ b/<相对路径>\n"
            "@@ -<old_line>,<old_count> +<new_line>,<new_count> @@\n"
            " <unchanged context>\n"
            "-<deleted line>\n"
            "+<added line>\n"
            " <unchanged context>\n"
            "```\n\n"
            "## 强约束\n"
            "- 只输出上面三段,不要加任何额外解释\n"
            "- diff 必须能被 `git apply` 直接接受\n"
            "- diff 中的文件路径**必须**与源码定位中给出的路径完全一致"
           "(即 ### 行中冒号后的仓库相对路径)，**绝对不要**编造或猜测路径\n"
            "- 只修改源码定位中出现的文件，不要修改未在源码定位中列出的文件\n"
            "- 不要修改任何测试文件(test_*.py / *_test.go / *.spec.js 等)\n"
            "- 改动尽量小,只针对根因\n"
        )

    @staticmethod
    def _render_locations(locations) -> str:
        if not locations:
            return "(无)"
        parts = []
        for loc in locations[:5]:  # 受 limits.yaml max_source_locations 控制,由调用方截断
            parts.append(loc.render() if hasattr(loc, "render") else str(loc))
        return "\n\n".join(parts)

    @staticmethod
    def _render_full_file_contents(locations, repo) -> str:
        """读取并渲染涉及文件的完整内容，供 LLM 生成精确 diff

        只有看到完整文件，LLM 才能写出正确的行号、上下文行和路径。
        去重：同一个文件只读一次。截断过长文件（>500 行）。
        """
        if not locations or not repo:
            return ""

        MAX_LINES = 500
        seen = set()
        parts = []

        for loc in locations[:5]:
            local_file = getattr(loc, "local_file", "")
            if not local_file or local_file in seen:
                continue
            seen.add(local_file)

            # 计算仓库相对路径
            repo_path = getattr(loc, "repo_path", "") or getattr(repo, "path", "")
            if repo_path:
                rel_path = os.path.relpath(local_file, repo_path)
            else:
                rel_path = os.path.basename(local_file)

            try:
                with open(local_file, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except (OSError, UnicodeError):
                continue

            if len(lines) > MAX_LINES:
                content = "".join(lines[:MAX_LINES])
                content += f"\n... (truncated, {len(lines)} total lines) ...\n"
            else:
                content = "".join(lines)

            parts.append(f"### 完整文件: {rel_path} ({len(lines)} lines)\n```\n{content}\n```")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _render_diagnosis(diagnosis) -> str:
        if isinstance(diagnosis, str):
            return diagnosis
        if not isinstance(diagnosis, dict):
            return str(diagnosis)
        out = []
        for k in ("type", "facts", "hypothesis", "confidence", "gaps"):
            v = diagnosis.get(k)
            if v:
                out.append(f"**{k}**: {v}")
        return "\n".join(out) or str(diagnosis)

    # ---------- 解析 LLM 输出 ----------

    _FILES_RE = re.compile(r"##\s*修改的文件\s*\n([\s\S]*?)(?:\n##|\Z)", re.MULTILINE)
    _DESC_RE = re.compile(r"##\s*修改说明\s*\n([\s\S]*?)(?:\n##|\Z)", re.MULTILINE)
    _DIFF_RE = re.compile(r"```(?:diff)?\s*\n([\s\S]*?)```", re.MULTILINE)

    def parse_response(self, response: str, repo) -> Patch | None:
        """解析 LLM 响应为 Patch。失败返回 None。"""
        if not response or not response.strip():
            return None

        # diff 是必需的,先抓
        raw_diff = self._extract_diff(response)
        if not raw_diff:
            logger.debug("no diff block in response")
            return None

        # 记录日志
        diff = raw_diff
        logger.debug(f"patch_generator: extracted diff ({len(diff)} chars): {diff[:500]}")

        description = ""
        m = self._DESC_RE.search(response)
        if m:
            description = m.group(1).strip()

        files_changed = self._extract_files_from_diff(diff)
        # 如果 diff 没解析出来,再退到说明文字
        if not files_changed:
            m = self._FILES_RE.search(response)
            if m:
                for line in m.group(1).splitlines():
                    line = line.strip().lstrip("-*").strip().strip("`")
                    if line:
                        files_changed.append(line)

        patch = Patch(
            repo_name=repo.name if repo else "",
            repo_path=repo.path if repo else "",
            diff=diff,
            description=description,
            files_changed=files_changed,
        )
        if not patch.is_valid():
            return None
        return patch

    def _extract_diff(self, response: str) -> str:
        # 收集所有 diff 代码块（LLM 可能输出多个文件各自的 diff）
        diff_blocks = []
        for m in self._DIFF_RE.finditer(response):
            block = m.group(1)
            if "@@" in block and ("---" in block or "+++" in block):
                diff_blocks.append(block.strip("\n"))
        if diff_blocks:
            return "\n".join(diff_blocks) + "\n"
        # 退路:全文里找 unified diff 段落
        if "@@" in response and "---" in response:
            # 抓从第一个 --- 开始到末尾
            start = response.find("--- ")
            if start >= 0:
                return response[start:].strip("\n") + "\n"
        return ""

    @staticmethod
    def _extract_files_from_diff(diff: str) -> list[str]:
        files = []
        for line in diff.splitlines():
            if line.startswith("+++ "):
                p = line[4:].strip()
                # 去掉 a/ b/ 前缀
                if p.startswith("b/"):
                    p = p[2:]
                elif p.startswith("a/"):
                    p = p[2:]
                if p and p != "/dev/null":
                    files.append(p)
        return files
