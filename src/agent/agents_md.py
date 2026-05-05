"""
AgentsMd — AGENTS.md 自动生成与按需加载

在 onboard 阶段为每个 source_repo 生成 AGENTS.md（项目地图），
在诊断/规划/自由对话等阶段按需加载，为 LLM 提供全局项目视野。
"""

import os
import re
import logging
from pathlib import Path
from typing import Optional

from src.infra.targets import SourceRepo
from src.context_limits import get_context_limits
from src.i18n import t

logger = logging.getLogger("ops-agent.agents_md")

# 入口文件候选（按语言）
_ENTRY_CANDIDATES = {
    "python": ["main.py", "app.py", "manage.py", "wsgi.py", "__main__.py"],
    "java": ["Main.java", "Application.java", "App.java"],
    "go": ["main.go", "cmd/main.go"],
    "node": ["index.js", "index.ts", "app.js", "app.ts", "server.js", "server.ts"],
    "rust": ["main.rs", "lib.rs"],
    "cpp": ["main.cpp", "main.cc", "main.c"],
}

# 配置/依赖文件候选
_CONFIG_FILES = [
    "Dockerfile", "docker-compose.yaml", "docker-compose.yml",
    "Makefile", "CMakeLists.txt",
]

_DEPENDENCY_FILES = [
    "requirements.txt", "setup.py", "pyproject.toml",
    "package.json",
    "go.mod",
    "pom.xml", "build.gradle",
    "Cargo.toml",
    "Gemfile",
]

# 源码树扫描时忽略的目录
_IGNORE_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "node_modules", "dist",
    "build", ".pytest_cache", ".tox", ".mypy_cache", "target",
    ".next", ".nuxt", "vendor", "third_party",
}

# 源码文件扩展名（按语言分组）
_SOURCE_EXTENSIONS = {
    "python": {".py"},
    "java": {".java"},
    "go": {".go"},
    "node": {".js", ".ts", ".jsx", ".tsx"},
    "rust": {".rs"},
    "cpp": {".cpp", ".cc", ".c", ".h", ".hpp"},
}

# 通用关注的扩展名
_COMMON_EXTENSIONS = {".md", ".yaml", ".yml", ".toml", ".json", ".xml", ".conf", ".cfg"}


class AgentsMdMixin:
    """AGENTS.md 自动生成与加载 Mixin"""

    # ═══════════════════════════════════════════
    #  生成
    # ═══════════════════════════════════════════

    def _check_and_generate_agents_md(self):
        """onboard 时对所有 source_repos 检查并生成 AGENTS.md。

        对每个 target 的每个 source_repo:
        - 有 AGENTS.md → 跳过
        - 无 AGENTS.md → 扫描 + LLM 生成
        """
        for target in self.targets:
            repos = target.get_source_repos()
            if not repos:
                continue
            for repo in repos:
                if not repo.path or not os.path.isdir(repo.path):
                    logger.debug(f"repo {repo.name}: path not accessible, skip AGENTS.md")
                    continue
                agents_md_path = os.path.join(repo.path, "AGENTS.md")
                if os.path.exists(agents_md_path):
                    logger.info(f"AGENTS.md already exists for {repo.name}, skipping")
                    continue
                try:
                    self._generate_agents_md(repo)
                except Exception as e:
                    logger.warning(f"Failed to generate AGENTS.md for {repo.name}: {e}")
                    self.chat.say(t("agents_md.generate_failed", name=repo.name, error=e), "warning")

    def _generate_agents_md(self, repo: SourceRepo):
        """扫描源码结构，让 LLM 生成 AGENTS.md"""
        self.chat.progress(t("agents_md.generate_start", name=repo.name))

        # 1. 收集源码结构信息
        context = _collect_repo_context(repo)

        # 2. 构建 prompt
        prompt = _build_generation_prompt(repo, context)

        # 3. LLM 生成
        content = self._ask_llm(prompt, max_tokens=4096)

        # 4. 写入文件
        agents_md_path = os.path.join(repo.path, "AGENTS.md")
        with open(agents_md_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(content)

        logger.info(f"Generated AGENTS.md for {repo.name} at {agents_md_path}")
        self.chat.say(t("agents_md.generate_done", name=repo.name), "success")

    # ═══════════════════════════════════════════
    #  加载
    # ═══════════════════════════════════════════

    def _load_agents_md(self, repo_name: str = None) -> str:
        """加载指定 repo 的 AGENTS.md，截断到上下文限制。

        repo_name 为 None 时，返回第一个有 AGENTS.md 的 repo 的内容。
        """
        if not self.current_target:
            return ""
        repos = self.current_target.get_source_repos()
        for repo in repos:
            if repo_name and repo.name != repo_name:
                continue
            if not repo.path:
                continue
            agents_md_path = os.path.join(repo.path, "AGENTS.md")
            if not os.path.exists(agents_md_path):
                continue
            try:
                content = Path(agents_md_path).read_text(encoding="utf-8")
            except Exception as e:
                logger.debug(f"Failed to read AGENTS.md for {repo.name}: {e}")
                continue
            limit = getattr(self.ctx_limits, "agents_md_chars", 8000)
            if len(content) > limit:
                content = content[:limit] + "\n" + t("agents_md.truncated")
            return content
        return ""

    def _load_agents_md_section(self, repo_name: str = None,
                                keywords: list = None) -> str:
        """智能裁剪：只加载 AGENTS.md 中与 keywords 相关的段落。

        按 ## 标题分段，对每段计算关键词命中数，返回命中最多的段落。
        如果没有命中或没有关键词，回退到 _load_agents_md（全文截断）。
        """
        if not keywords:
            return self._load_agents_md(repo_name)

        full_content = self._load_agents_md_full(repo_name)
        if not full_content:
            return ""

        # 按 ## 标题分段
        sections = _split_sections(full_content)
        if not sections:
            return self._load_agents_md(repo_name)

        # 关键词匹配评分
        keywords_lower = [k.lower() for k in keywords if k]
        scored = []
        for heading, body in sections:
            text_lower = (heading + " " + body).lower()
            score = sum(1 for kw in keywords_lower if kw in text_lower)
            if score > 0:
                scored.append((score, heading, body))

        if not scored:
            # 无命中，返回全文截断版
            return self._load_agents_md(repo_name)

        # 按分数降序
        scored.sort(key=lambda x: -x[0])

        # 拼接：项目概述（第一段）+ 命中段落，直到达到字符限制
        limit = getattr(self.ctx_limits, "agents_md_chars", 8000)
        parts = []
        used = 0

        # 始终包含第一段（项目概述）
        first_heading, first_body = sections[0]
        first_section = f"## {first_heading}\n{first_body}" if first_heading else first_body
        parts.append(first_section)
        used += len(first_section)

        for score, heading, body in scored:
            section_text = f"## {heading}\n{body}"
            if heading == first_heading:
                continue  # 已包含
            if used + len(section_text) > limit:
                break
            parts.append(section_text)
            used += len(section_text)

        return "\n\n".join(parts)

    def _load_agents_md_full(self, repo_name: str = None) -> str:
        """加载完整 AGENTS.md，不截断（内部用）。"""
        if not self.current_target:
            return ""
        repos = self.current_target.get_source_repos()
        for repo in repos:
            if repo_name and repo.name != repo_name:
                continue
            if not repo.path:
                continue
            agents_md_path = os.path.join(repo.path, "AGENTS.md")
            if not os.path.exists(agents_md_path):
                continue
            try:
                return Path(agents_md_path).read_text(encoding="utf-8")
            except Exception:
                continue
        return ""

    def _is_code_related(self, msg: str) -> bool:
        """判断消息是否涉及代码/项目话题，决定是否注入 AGENTS.md"""
        if not self.current_target or not self.current_target.source_repos:
            return False
        indicators = [
            "代码", "源码", "source", "code", "bug", "错误", "异常",
            "模块", "module", "函数", "function", "类", "class",
            "接口", "api", "架构", "architecture", "结构", "structure",
            "定位", "排查", "调试", "debug", "修复", "fix",
            "依赖", "dependency", "配置", "config",
            "文件", "目录", "路径", "path", "file",
            "项目", "project", "仓库", "repo",
            "日志", "log", "栈", "stack", "trace",
        ]
        msg_lower = msg.lower()
        return any(ind in msg_lower for ind in indicators)


# ═══════════════════════════════════════════
#  内部工具函数（模块级，不依赖 self）
# ═══════════════════════════════════════════

def _collect_repo_context(repo: SourceRepo) -> dict:
    """收集仓库的结构化信息，供生成 AGENTS.md。"""
    context = {
        "name": repo.name,
        "language": repo.language,
        "path": repo.path,
        "repo_url": repo.repo_url,
        "tree": [],
        "key_files": {},
        "deps": "",
    }

    if not os.path.isdir(repo.path):
        return context

    # 1. 源码树
    context["tree"] = _list_source_tree(repo.path, repo.language)

    # 2. 关键文件采样
    context["key_files"] = _sample_key_files(repo)

    # 3. 依赖信息
    context["deps"] = _read_dependency_file(repo)

    return context


def _list_source_tree(repo_path: str, language: str = "") -> list:
    """列出仓库源文件清单 [(rel_path, line_count), ...]"""
    if not os.path.isdir(repo_path):
        return []

    # 确定关注的扩展名
    extensions = set(_COMMON_EXTENSIONS)
    if language and language.lower() in _SOURCE_EXTENSIONS:
        extensions |= _SOURCE_EXTENSIONS[language.lower()]
    else:
        # 语言不明确，包含所有源码扩展名
        for exts in _SOURCE_EXTENSIONS.values():
            extensions |= exts

    result = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS and not d.startswith(".")]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in extensions:
                continue
            abs_path = os.path.join(root, fn)
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                    line_count = sum(1 for _ in f)
            except Exception:
                line_count = 0
            rel = os.path.relpath(abs_path, repo_path)
            result.append((rel, line_count))

    result.sort(key=lambda x: x[0])
    return result


def _sample_key_files(repo: SourceRepo) -> dict:
    """采样关键文件内容，返回 {filename: content_preview}"""
    samples = {}
    repo_path = repo.path

    # README
    for name in ["README.md", "readme.md", "README.rst", "README"]:
        p = os.path.join(repo_path, name)
        if os.path.isfile(p):
            samples["README"] = _read_truncated(p, 2000)
            break

    # 入口文件
    language = (repo.language or "").lower()
    candidates = _ENTRY_CANDIDATES.get(language, [])
    # 也尝试通用候选
    if language not in _ENTRY_CANDIDATES:
        for lang_candidates in _ENTRY_CANDIDATES.values():
            candidates.extend(lang_candidates)
    for name in candidates:
        p = os.path.join(repo_path, name)
        if os.path.isfile(p):
            samples[f"entry:{name}"] = _read_truncated(p, 3000)
            break
        # 也在 src/ cmd/ 等常见子目录下找
        for subdir in ["src", "cmd", "app", "lib"]:
            p2 = os.path.join(repo_path, subdir, name)
            if os.path.isfile(p2):
                samples[f"entry:{subdir}/{name}"] = _read_truncated(p2, 3000)
                break

    # 配置文件
    for name in _CONFIG_FILES:
        p = os.path.join(repo_path, name)
        if os.path.isfile(p):
            samples[f"config:{name}"] = _read_truncated(p, 1500)

    return samples


def _read_dependency_file(repo: SourceRepo) -> str:
    """读取依赖声明文件"""
    for name in _DEPENDENCY_FILES:
        p = os.path.join(repo.path, name)
        if os.path.isfile(p):
            return f"### {name}\n```\n{_read_truncated(p, 2000)}\n```"
    return t("agents_md.no_deps")


def _read_truncated(path: str, max_chars: int) -> str:
    """读取文件并截断"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(max_chars + 100)
        if len(content) > max_chars:
            content = content[:max_chars] + "\n" + t("agents_md.truncated")
        return content
    except Exception as e:
        return t("agents_md.read_failed", error=e)


def _split_sections(text: str) -> list:
    """按 ## 标题拆分 markdown，返回 [(heading, body), ...]

    第一段如果没有 ## 标题，heading 为空字符串。
    """
    lines = text.split("\n")
    sections = []
    current_heading = ""
    current_body = []

    for line in lines:
        if line.startswith("## "):
            # 保存前一段
            if current_heading or current_body:
                sections.append((current_heading, "\n".join(current_body).strip()))
            current_heading = line[3:].strip()
            current_body = []
        elif line.startswith("# ") and not sections:
            # 顶级标题，作为第一段
            current_heading = line[2:].strip()
            current_body = []
        else:
            current_body.append(line)

    # 最后一段
    if current_heading or current_body:
        sections.append((current_heading, "\n".join(current_body).strip()))

    return sections


def _build_generation_prompt(repo: SourceRepo, context: dict) -> str:
    """构建生成 AGENTS.md 的 prompt"""

    # 格式化源码树
    tree_text = ""
    tree = context.get("tree", [])
    # 限制条目数
    max_entries = 150
    for i, (rel, lines) in enumerate(tree):
        if i >= max_entries:
            tree_text += f"\n... ({len(tree) - max_entries} more files omitted)\n"
            break
        tree_text += f"{lines:>6}  {rel}\n"

    # 格式化关键文件
    key_files_text = ""
    for label, content in context.get("key_files", {}).items():
        key_files_text += f"\n### {label}\n```\n{content}\n```\n"

    deps_text = context.get("deps", t("reporter.fallback_none"))

    # 加载 prompt 模板
    from src.i18n import get_lang
    prompts_root = Path(__file__).parent.parent.parent / "prompts"
    lang = get_lang()
    prompt_path = prompts_root / lang / "agents_md.md"
    fallback_path = prompts_root / "zh" / "agents_md.md"
    template = None
    for p in [prompt_path, fallback_path]:
        if p.exists():
            template = p.read_text(encoding="utf-8")
            break

    if template is None:
        # 内联兜底
        template = (
            "Generate AGENTS.md for project {repo_name}.\n\n"
            "## Source Structure\n```\n{tree_text}\n```\n\n"
            "## Key Files\n{key_files_text}\n\n## Dependencies\n{deps_text}\n"
        )

    return template.format(
        repo_name=repo.name,
        repo_language=repo.language or t("reporter.fallback_none"),
        repo_url=repo.repo_url or t("reporter.fallback_none"),
        tree_text=tree_text,
        key_files_text=key_files_text,
        deps_text=deps_text,
    )
