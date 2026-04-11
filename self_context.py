"""Self-context collector for the self-repair session.

把"关于 ops-agent 自己"的各种信息打包成一个结构化上下文,
交给 LLM 诊断自身 bug 用。所有字段都有长度上限,防止 prompt 爆炸。
"""
from __future__ import annotations

import os
import json
import subprocess
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("ops-agent.self_context")

# 单字段长度上限(字符)
MAX_LOG_CHARS = 8000
MAX_TREE_ENTRIES = 200
MAX_INCIDENT_CHARS = 3000
MAX_CONFIG_CHARS = 4000


@dataclass
class SelfContext:
    """自修复会话的上下文打包。

    字段:
        user_description  用户报告的问题原话
        source_tree       源码清单 [(relpath, linecount), ...]
        agent_state       agent 的运行时自述(dict,已脱敏)
        recent_log_tail   自身日志的尾部
        recent_incidents  最近 incidents 的摘要文本
        config_snapshot   配置文件的原文 (dict[filename -> content])
        git_head          当前 HEAD 的 sha 和分支
        repo_path         selfdev 工作区的路径
    """
    user_description: str = ""
    source_tree: list = field(default_factory=list)
    agent_state: dict = field(default_factory=dict)
    recent_log_tail: str = ""
    recent_incidents: str = ""
    config_snapshot: dict = field(default_factory=dict)
    git_head: dict = field(default_factory=dict)
    repo_path: str = ""

    @classmethod
    def collect(
        cls,
        repo_path: str,
        description: str,
        agent_state: dict,
        recent_log_tail: str = "",
        recent_incidents: str = "",
    ) -> "SelfContext":
        """收集自身上下文的主入口。"""
        return cls(
            user_description=description.strip(),
            source_tree=_list_source_tree(repo_path),
            agent_state=_sanitize_state(agent_state),
            recent_log_tail=_truncate(recent_log_tail, MAX_LOG_CHARS),
            recent_incidents=_truncate(recent_incidents, MAX_INCIDENT_CHARS),
            config_snapshot=_read_config_snapshot(repo_path),
            git_head=_git_head(repo_path),
            repo_path=repo_path,
        )

    def to_prompt(self) -> str:
        """序列化成给 LLM 的结构化文本(markdown + fenced blocks)"""
        parts: list[str] = []

        parts.append("## 用户报告的问题")
        parts.append(self.user_description or "(空)")
        parts.append("")

        parts.append("## 当前 Git HEAD")
        parts.append(f"- branch: `{self.git_head.get('branch', '?')}`")
        parts.append(f"- sha: `{self.git_head.get('sha', '?')}`")
        parts.append(f"- dirty: `{self.git_head.get('dirty', '?')}`")
        parts.append("")

        parts.append("## 源码清单(文件 / 行数)")
        parts.append("```")
        shown = 0
        for relpath, lineno in self.source_tree:
            if shown >= MAX_TREE_ENTRIES:
                parts.append(f"... (另外 {len(self.source_tree) - shown} 个文件省略)")
                break
            parts.append(f"{lineno:>6}  {relpath}")
            shown += 1
        parts.append("```")
        parts.append("")

        parts.append("## Agent 运行时状态")
        parts.append("```json")
        try:
            parts.append(json.dumps(self.agent_state, ensure_ascii=False,
                                    indent=2, default=str)[:3000])
        except Exception as e:
            parts.append(f"(无法序列化: {e})")
        parts.append("```")
        parts.append("")

        if self.config_snapshot:
            parts.append("## 配置快照")
            remaining = MAX_CONFIG_CHARS
            for name, content in self.config_snapshot.items():
                if remaining <= 0:
                    break
                slice_ = content[:remaining]
                parts.append(f"### {name}")
                parts.append("```")
                parts.append(slice_)
                parts.append("```")
                remaining -= len(slice_)
            parts.append("")

        if self.recent_log_tail:
            parts.append("## 自身日志尾部")
            parts.append("```")
            parts.append(self.recent_log_tail)
            parts.append("```")
            parts.append("")

        if self.recent_incidents:
            parts.append("## 最近 Incidents 摘要")
            parts.append(self.recent_incidents)
            parts.append("")

        return "\n".join(parts)


# ──────────────────────────────────────────────────────────
#  内部工具
# ──────────────────────────────────────────────────────────

def _truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    head = max_chars // 3
    tail = max_chars - head - 32
    return f"{text[:head]}\n... ({len(text) - head - tail} chars omitted) ...\n{text[-tail:]}"


def _list_source_tree(repo_path: str) -> list:
    """列出仓库里的 python 源文件和关键配置文件,不读内容只算行数。"""
    if not repo_path or not os.path.isdir(repo_path):
        return []
    result = []
    ignore_dirs = {".git", "__pycache__", "venv", ".venv", "notebook",
                   "node_modules", "dist", "build", ".pytest_cache"}
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
        for fn in files:
            if not (fn.endswith(".py") or fn.endswith(".md")
                    or fn.endswith(".yaml") or fn.endswith(".yml")):
                continue
            abs_path = os.path.join(root, fn)
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                    lineno = sum(1 for _ in f)
            except Exception:
                lineno = 0
            rel = os.path.relpath(abs_path, repo_path)
            result.append((rel, lineno))
    # 按路径排序,保证 LLM 看到稳定的顺序
    result.sort(key=lambda x: x[0])
    return result


def _sanitize_state(state: dict) -> dict:
    """从 agent.snapshot_state() 返回的 dict 里剔除敏感/无关字段"""
    if not isinstance(state, dict):
        return {}
    blocked = {"api_key", "token", "password", "secret", "credential"}
    out: dict = {}
    for k, v in state.items():
        key_lower = k.lower()
        if any(b in key_lower for b in blocked):
            out[k] = "<redacted>"
            continue
        # 简单降维:把 dataclass / 复杂对象转 str
        if isinstance(v, (str, int, float, bool, type(None), list, dict)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _read_config_snapshot(repo_path: str) -> dict:
    """读取关键配置文件的原文"""
    if not repo_path or not os.path.isdir(repo_path):
        return {}
    wanted = [
        "requirements.txt",
        "ops-agent.service",
        "Dockerfile",
    ]
    out: dict = {}
    for name in wanted:
        p = Path(repo_path) / name
        if p.exists() and p.is_file():
            try:
                out[name] = p.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                out[name] = f"(读取失败: {e})"
    return out


def _git_head(repo_path: str) -> dict:
    """获取当前 HEAD 信息,不抛异常"""
    if not repo_path or not os.path.isdir(os.path.join(repo_path, ".git")):
        return {"sha": "?", "branch": "?", "dirty": "?"}
    out = {"sha": "?", "branch": "?", "dirty": "?"}
    try:
        out["sha"] = _git(repo_path, "rev-parse", "HEAD").strip()[:12]
        out["branch"] = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD").strip()
        dirty = _git(repo_path, "status", "--porcelain").strip()
        out["dirty"] = "yes" if dirty else "no"
    except Exception as e:
        out["error"] = str(e)
    return out


def _git(repo_path: str, *args) -> str:
    result = subprocess.run(
        ["git"] + list(args),
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout
