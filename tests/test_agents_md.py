"""测试 AGENTS.md 自动生成与加载的核心函数"""

import os
import sys
import importlib.util
import tempfile
import pytest

# ── 手动加载 agents_md 模块，绕过包导入链 ──

class MockRepo:
    """模拟 SourceRepo"""
    def __init__(self, name, path, language="python", repo_url=""):
        self.name = name
        self.path = path
        self.language = language
        self.repo_url = repo_url


class MockLimits:
    agents_md_chars = 8000


# Mock 依赖
sys.modules.setdefault("src.infra.targets", type(sys)("mock_targets"))
sys.modules["src.infra.targets"].SourceRepo = MockRepo
sys.modules.setdefault("src.context_limits", type(sys)("mock_ctx"))
sys.modules["src.context_limits"].get_context_limits = lambda: MockLimits()

_spec = importlib.util.spec_from_file_location(
    "agents_md",
    os.path.join(os.path.dirname(__file__), "..", "src", "agent", "agents_md.py"),
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ═══════════════════════════════════════════
#  模块级函数测试
# ═══════════════════════════════════════════

class TestListSourceTree:
    def test_returns_entries(self):
        """用项目自身 src/ 目录测试"""
        src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
        tree = mod._list_source_tree(src_dir, "python")
        assert len(tree) > 0
        for rel, lc in tree:
            assert isinstance(rel, str)
            assert isinstance(lc, int)

    def test_nonexistent_dir(self):
        assert mod._list_source_tree("/nonexistent/path", "python") == []

    def test_respects_language(self):
        """python 语言只关注 .py 和通用配置文件"""
        src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
        tree = mod._list_source_tree(src_dir, "python")
        for rel, _ in tree:
            ext = os.path.splitext(rel)[1].lower()
            # 应该是 .py 或通用配置类
            allowed = {".py", ".md", ".yaml", ".yml", ".toml", ".json", ".xml", ".conf", ".cfg"}
            assert ext in allowed, f"Unexpected extension {ext} for {rel}"


class TestSampleKeyFiles:
    def test_finds_readme_and_entry(self):
        """用项目根目录测试"""
        root = os.path.join(os.path.dirname(__file__), "..")
        repo = MockRepo("test", root, "python")
        samples = mod._sample_key_files(repo)
        assert any("README" in k for k in samples)

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            repo = MockRepo("empty", d, "python")
            samples = mod._sample_key_files(repo)
            assert isinstance(samples, dict)


class TestReadDependencyFile:
    def test_finds_requirements(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        repo = MockRepo("test", root, "python")
        deps = mod._read_dependency_file(repo)
        assert "requirements.txt" in deps

    def test_no_deps(self):
        with tempfile.TemporaryDirectory() as d:
            repo = MockRepo("empty", d, "go")
            deps = mod._read_dependency_file(repo)
            assert "无" in deps


class TestCollectRepoContext:
    def test_complete_context(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        repo = MockRepo("ops-agent", root, "python", "https://github.com/test")
        ctx = mod._collect_repo_context(repo)
        assert ctx["name"] == "ops-agent"
        assert ctx["language"] == "python"
        assert len(ctx["tree"]) > 0
        assert len(ctx["key_files"]) > 0


class TestBuildGenerationPrompt:
    def test_prompt_structure(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        repo = MockRepo("test", root, "python", "https://github.com/test")
        ctx = mod._collect_repo_context(repo)
        prompt = mod._build_generation_prompt(repo, ctx)
        assert "项目概述" in prompt
        assert "目录结构" in prompt
        assert "核心模块" in prompt
        assert "问题定位指南" in prompt
        assert "test" in prompt  # repo name


class TestSplitSections:
    def test_basic_split(self):
        text = "# Title\n\nIntro\n\n## Section A\nContent A\n\n## Section B\nContent B"
        sections = mod._split_sections(text)
        assert len(sections) == 3
        assert sections[0][0] == "Title"
        assert sections[1][0] == "Section A"
        assert sections[2][0] == "Section B"

    def test_no_sections(self):
        text = "Just plain text\nNo headings"
        sections = mod._split_sections(text)
        assert len(sections) == 1
        assert sections[0][0] == ""

    def test_empty_text(self):
        sections = mod._split_sections("")
        assert len(sections) == 1  # one empty section

    def test_chinese_headings(self):
        text = "# 项目名\n\n## 目录结构\n内容\n\n## 核心模块\n模块描述"
        sections = mod._split_sections(text)
        assert len(sections) == 3
        assert sections[1][0] == "目录结构"
        assert sections[2][0] == "核心模块"


class TestIsCodeRelated:
    def setup_method(self):
        self.mixin = mod.AgentsMdMixin()

        class MockTarget:
            source_repos = [{"name": "test"}]

        self.mixin.current_target = MockTarget()

    def test_code_keywords(self):
        assert self.mixin._is_code_related("这个模块的代码有bug")
        assert self.mixin._is_code_related("help me debug this error")
        assert self.mixin._is_code_related("项目的架构是什么样的")
        assert self.mixin._is_code_related("检查一下日志文件")

    def test_non_code_topics(self):
        assert not self.mixin._is_code_related("今天天气怎么样")
        assert not self.mixin._is_code_related("帮我重启服务")
        assert not self.mixin._is_code_related("磁盘空间还够吗")

    def test_no_source_repos(self):
        """没有 source_repos 时始终返回 False"""
        class EmptyTarget:
            source_repos = []

        self.mixin.current_target = EmptyTarget()
        assert not self.mixin._is_code_related("代码有bug")

    def test_no_target(self):
        self.mixin.current_target = None
        assert not self.mixin._is_code_related("代码有bug")


class TestLoadAgentsMdSection:
    """测试智能裁剪加载"""

    def setup_method(self):
        self.mixin = mod.AgentsMdMixin()
        self.tmpdir = tempfile.mkdtemp()

        # 写一个测试用的 AGENTS.md
        self.agents_md = """# Test Project

A test project for unit testing.

## 目录结构
src/ — 源码
tests/ — 测试

## 核心模块
Module A 负责数据处理。
Module B 负责 API 接口。
Module A 调用 Module B 的服务。

## 配置与环境
DATABASE_URL 环境变量
REDIS_HOST 配置

## 问题定位指南
日志检查从 /var/log/app.log 开始。
数据库问题看 Module A。
API 问题看 Module B。
"""
        with open(os.path.join(self.tmpdir, "AGENTS.md"), "w") as f:
            f.write(self.agents_md)

        class MockTarget:
            def get_source_repos(self):
                return [MockRepo("test", self.path)]

        target = MockTarget()
        target.path = self.tmpdir
        target.source_repos = [{"name": "test"}]
        self.mixin.current_target = target

        class _Limits:
            agents_md_chars = 8000

        self.mixin.ctx_limits = _Limits()

    def test_full_load(self):
        content = self.mixin._load_agents_md("test")
        assert "# Test Project" in content
        assert "核心模块" in content

    def test_keyword_filtering(self):
        content = self.mixin._load_agents_md_section("test", ["模块", "Module"])
        assert "核心模块" in content
        # 第一段（项目概述）始终包含
        assert "Test Project" in content

    def test_no_keywords_falls_back(self):
        content = self.mixin._load_agents_md_section("test", None)
        assert "Test Project" in content

    def test_no_match_returns_full(self):
        content = self.mixin._load_agents_md_section("test", ["zzzznonexistent"])
        # 无命中时返回全文截断版
        assert "Test Project" in content

    def test_nonexistent_repo(self):
        content = self.mixin._load_agents_md("nonexistent")
        assert content == ""

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
