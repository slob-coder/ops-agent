"""测试 _parse_block_lines 和 _extract_commands 对多行命令的处理"""

import pytest
from src.agent.parsers import ParsersMixin


class TestParseBlockLines:
    """直接测试静态方法 _parse_block_lines"""

    def test_simple_commands(self):
        block = "ls -la\ndf -h\nfree -m"
        result = ParsersMixin._parse_block_lines(block)
        assert result == ["ls -la", "df -h", "free -m"]

    def test_skip_comments_and_empty(self):
        block = "# this is a comment\nls -la\n\n# another comment\ndf -h\n"
        result = ParsersMixin._parse_block_lines(block)
        assert result == ["ls -la", "df -h"]

    def test_heredoc_single_quotes(self):
        block = "cat > /tmp/test.md << 'EOF'\nline 1\nline 2\nEOF"
        result = ParsersMixin._parse_block_lines(block)
        assert len(result) == 1
        assert "cat > /tmp/test.md << 'EOF'" in result[0]
        assert "line 1" in result[0]
        assert "line 2" in result[0]
        assert result[0].endswith("EOF")

    def test_heredoc_double_quotes(self):
        block = 'cat > /tmp/test.sh << "SCRIPT_END"\n#!/bin/bash\necho hello\nSCRIPT_END'
        result = ParsersMixin._parse_block_lines(block)
        assert len(result) == 1
        assert "SCRIPT_END" in result[0]
        assert "echo hello" in result[0]

    def test_heredoc_no_quotes(self):
        block = "cat > /tmp/test.txt << END\nfoo\nbar\nEND"
        result = ParsersMixin._parse_block_lines(block)
        assert len(result) == 1
        assert result[0].count("\n") == 3  # 4 lines joined by 3 newlines

    def test_heredoc_dash_variant(self):
        """<<- 变体（允许 tab 缩进的结束符）"""
        block = "cat > /tmp/test.txt <<- MARKER\n\tindented content\n\tMARKER"
        result = ParsersMixin._parse_block_lines(block)
        assert len(result) == 1
        assert "MARKER" in result[0]

    def test_heredoc_with_many_lines(self):
        """模拟 LLM 生成长文件的场景 — 触发 bug 的典型情况"""
        lines = [
            "cat > /opt/vol/ops-agent/notebook/playbook/agents-md-design.md << 'DESIGN_EOF'",
            "# AGENTS.md 设计方案",
            "",
            "## 1. 目标",
            "定义 Agent 的行为规范...",
            "",
            "## 2. 结构",
            "- Identity",
            "- Rules",
            "- Memory",
            "",
            "## 3. 实现细节",
            "每个 Agent 启动时读取 AGENTS.md...",
            "DESIGN_EOF",
        ]
        block = "\n".join(lines)
        result = ParsersMixin._parse_block_lines(block)
        assert len(result) == 1, f"Expected 1 command, got {len(result)}: {result}"
        assert result[0].startswith("cat > /opt/vol/ops-agent")
        assert result[0].endswith("DESIGN_EOF")
        # 所有内容行都在这一条命令内
        assert "# AGENTS.md 设计方案" in result[0]
        assert "## 3. 实现细节" in result[0]

    def test_heredoc_followed_by_normal_command(self):
        """heredoc 后面还有普通命令"""
        block = (
            "cat > /tmp/config.yaml << 'CFG'\nkey: value\nCFG\n"
            "systemctl restart myservice"
        )
        result = ParsersMixin._parse_block_lines(block)
        assert len(result) == 2
        assert "key: value" in result[0]
        assert result[1] == "systemctl restart myservice"

    def test_backslash_continuation(self):
        block = "docker run \\\n  --name test \\\n  -p 8080:80 \\\n  nginx:latest"
        result = ParsersMixin._parse_block_lines(block)
        assert len(result) == 1
        assert "docker run" in result[0]
        assert "nginx:latest" in result[0]

    def test_backslash_then_normal(self):
        block = "curl -X POST \\\n  http://localhost/api\necho done"
        result = ParsersMixin._parse_block_lines(block)
        assert len(result) == 2
        assert "curl" in result[0]
        assert "http://localhost/api" in result[0]
        assert result[1] == "echo done"

    def test_mixed_heredoc_backslash_normal(self):
        """混合场景"""
        block = (
            "echo 'step 1'\n"
            "cat > /tmp/f.txt << 'END'\nhello\nworld\nEND\n"
            "docker run \\\n  --rm \\\n  alpine echo hi\n"
            "echo 'done'"
        )
        result = ParsersMixin._parse_block_lines(block)
        assert len(result) == 4
        assert result[0] == "echo 'step 1'"
        assert "hello" in result[1] and "world" in result[1]
        assert "docker run" in result[2] and "alpine echo hi" in result[2]
        assert result[3] == "echo 'done'"

    def test_empty_block(self):
        assert ParsersMixin._parse_block_lines("") == []
        assert ParsersMixin._parse_block_lines("  \n\n  ") == []

    def test_only_comments(self):
        assert ParsersMixin._parse_block_lines("# comment\n# another") == []


class TestExtractCommandsIntegration:
    """通过 _extract_commands 测试完整流程（需要一个 mixin 实例）"""

    def setup_method(self):
        self.parser = ParsersMixin()

    def test_heredoc_in_commands_block(self):
        text = (
            "我来帮你创建这个文件：\n\n"
            "```commands\n"
            "cat > /tmp/design.md << 'EOF'\n"
            "# Design Doc\n"
            "\n"
            "## Overview\n"
            "This is the design.\n"
            "EOF\n"
            "```\n"
        )
        cmds = self.parser._extract_commands(text, allow_fallback=False)
        assert len(cmds) == 1
        assert cmds[0].startswith("cat > /tmp/design.md")
        assert "# Design Doc" in cmds[0]

    def test_heredoc_not_extracted_from_bash_block_when_no_fallback(self):
        """allow_fallback=False 时不从 bash 块提取"""
        text = "```bash\ncat > /tmp/f << 'E'\nhello\nE\n```"
        cmds = self.parser._extract_commands(text, allow_fallback=False)
        assert cmds == []

    def test_heredoc_extracted_from_bash_block_with_fallback(self):
        """allow_fallback=True 时从 bash 块提取"""
        text = "```bash\ncat > /tmp/f << 'E'\nhello\nE\n```"
        cmds = self.parser._extract_commands(text, allow_fallback=True)
        assert len(cmds) == 1
        assert "hello" in cmds[0]

    def test_backslash_in_commands_block(self):
        text = (
            "```commands\n"
            "curl -X POST \\\n"
            "  -H 'Content-Type: application/json' \\\n"
            "  -d '{\"key\":\"val\"}' \\\n"
            "  http://localhost:8080/api\n"
            "```"
        )
        cmds = self.parser._extract_commands(text)
        assert len(cmds) == 1
        assert "curl" in cmds[0]
        assert "http://localhost:8080/api" in cmds[0]
