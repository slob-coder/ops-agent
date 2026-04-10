"""
Notebook — Agent 的记忆
一个 git 仓库，里面全是 markdown，Agent 自己读自己写，人类也能读写。
"""

import subprocess
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("ops-agent.notebook")


class Notebook:
    """Agent 的笔记本 —— 文件系统 + git 的薄封装"""

    def __init__(self, path: str, remote_url: str = ""):
        self.path = Path(path).resolve()
        self.remote_url = remote_url
        self._ensure_init()

    def _ensure_init(self):
        """确保 Notebook 目录和 git 仓库存在"""
        dirs = [
            "config", "playbook", "incidents/active", "incidents/archive",
            "lessons", "conversations", "questions",
        ]
        for d in dirs:
            (self.path / d).mkdir(parents=True, exist_ok=True)

        if not (self.path / ".git").exists():
            self._git("init")
            logger.info(f"Initialized notebook at {self.path}")

    def _git(self, *args) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.path)] + list(args),
            capture_output=True, text=True,
        )
        return result.stdout.strip()

    # ── 读 ──

    def read(self, relative_path: str) -> str:
        """读一个文件，不存在返回空字符串"""
        fp = self.path / relative_path
        if fp.exists():
            return fp.read_text(encoding="utf-8")
        return ""

    def exists(self, relative_path: str) -> bool:
        return (self.path / relative_path).exists()

    def list_dir(self, relative_path: str) -> list[str]:
        """列出目录下的文件名"""
        dp = self.path / relative_path
        if dp.exists() and dp.is_dir():
            return sorted([f.name for f in dp.iterdir() if f.is_file()])
        return []

    def search(self, keyword: str) -> list[str]:
        """grep 搜索整个 Notebook，返回匹配文件的相对路径"""
        result = subprocess.run(
            ["grep", "-rl", "--include=*.md", keyword, str(self.path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return []
        hits = []
        for line in result.stdout.strip().split("\n"):
            if line:
                try:
                    hits.append(str(Path(line).relative_to(self.path)))
                except ValueError:
                    hits.append(line)
        return hits

    def find_relevant(self, context: str, top_k: int = 5) -> list[str]:
        """根据上下文找相关的 Notebook 文件（关键词搜索版本）"""
        # 简单实现：从 context 里拆词搜索
        words = [w for w in context.split() if len(w) > 3][:10]
        all_hits = []
        for word in words:
            all_hits.extend(self.search(word))
        # 按出现频率排序
        from collections import Counter
        ranked = Counter(all_hits).most_common(top_k)
        return [path for path, _ in ranked]

    def read_playbooks_summary(self) -> str:
        """读取所有 Playbook 的第一行（标题）作为摘要"""
        summaries = []
        for name in self.list_dir("playbook"):
            content = self.read(f"playbook/{name}")
            first_line = content.split("\n")[0] if content else name
            summaries.append(f"- {name}: {first_line}")
        return "\n".join(summaries) if summaries else "（暂无 Playbook）"

    # ── 写 ──

    def write(self, relative_path: str, content: str):
        """写一个文件（覆盖）"""
        fp = self.path / relative_path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        logger.debug(f"Wrote {relative_path}")

    def append(self, relative_path: str, content: str):
        """追加内容"""
        fp = self.path / relative_path
        fp.parent.mkdir(parents=True, exist_ok=True)
        with open(fp, "a", encoding="utf-8") as f:
            f.write("\n" + content)

    def commit(self, message: str):
        """git add + commit"""
        self._git("add", "-A")
        result = subprocess.run(
            ["git", "-C", str(self.path), "commit", "-m", message, "--allow-empty"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            logger.info(f"Committed: {message}")
        return result.stdout.strip()

    # ── Incident 管理 ──

    def create_incident(self, title: str) -> str:
        """创建新 Incident，返回文件名"""
        ts = datetime.now().strftime("%Y-%m-%d-%H%M")
        slug = title.lower().replace(" ", "-")[:40]
        filename = f"{ts}-{slug}.md"
        self.write(
            f"incidents/active/{filename}",
            f"# Incident: {title} @ {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n## 时间线\n",
        )
        self.commit(f"Create incident: {title}")
        return filename

    def append_to_incident(self, filename: str, content: str):
        """向活跃 Incident 追加内容"""
        self.append(f"incidents/active/{filename}", content)

    def close_incident(self, filename: str, summary: str):
        """关闭并归档 Incident"""
        self.append(f"incidents/active/{filename}", f"\n## 关闭总结\n{summary}")
        src = self.path / "incidents/active" / filename
        dst = self.path / "incidents/archive" / filename
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        self.commit(f"Close incident: {filename}")

    # ── 对话记录 ──

    def log_conversation(self, role: str, message: str):
        """记录一条对话"""
        today = datetime.now().strftime("%Y-%m-%d")
        ts = datetime.now().strftime("%H:%M:%S")
        self.append(
            f"conversations/{today}.md",
            f"**[{ts}] {role}**: {message}",
        )

    # ── Sprint 5: 完整性校验与远端备份 ──

    def verify_integrity(self) -> tuple[bool, str]:
        """校验 git 仓库完整性。返回 (是否健康, 错误描述)"""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.path), "fsck", "--no-progress"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                return False, (result.stderr or result.stdout or "fsck failed").strip()
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "git fsck timeout"
        except FileNotFoundError:
            return False, "git not found"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def push_to_remote(self) -> tuple[bool, str]:
        """推送笔记到远端。失败只记日志,不抛异常。"""
        if not self.remote_url:
            return False, "no remote configured"
        try:
            # 确保 remote 已配置
            existing = subprocess.run(
                ["git", "-C", str(self.path), "remote"],
                capture_output=True, text=True,
            ).stdout
            if "origin" not in existing.split():
                subprocess.run(
                    ["git", "-C", str(self.path), "remote", "add", "origin",
                     self.remote_url],
                    capture_output=True, text=True, check=False,
                )
            result = subprocess.run(
                ["git", "-C", str(self.path), "push", "-u", "origin", "HEAD"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                return False, (result.stderr or "").strip()
            return True, "ok"
        except subprocess.TimeoutExpired:
            return False, "push timeout"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def restore_from_remote(self) -> tuple[bool, str]:
        """笔记损坏时尝试从远端拉取覆盖恢复。"""
        if not self.remote_url:
            return False, "no remote configured"
        try:
            # 移走损坏目录(只移走 .git,文件保留作为参考)
            broken = self.path / ".git.broken"
            if (self.path / ".git").exists():
                if broken.exists():
                    import shutil
                    shutil.rmtree(broken)
                (self.path / ".git").rename(broken)
            # 重新 init + fetch + reset
            subprocess.run(["git", "-C", str(self.path), "init", "-q"], check=True)
            subprocess.run(
                ["git", "-C", str(self.path), "remote", "add", "origin",
                 self.remote_url],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(self.path), "fetch", "origin"],
                capture_output=True, text=True, timeout=120, check=True,
            )
            subprocess.run(
                ["git", "-C", str(self.path), "reset", "--hard",
                 "origin/HEAD"],
                capture_output=True, text=True, check=True,
            )
            return True, "restored"
        except subprocess.TimeoutExpired:
            return False, "fetch timeout"
        except subprocess.CalledProcessError as e:
            return False, f"git failed: {e}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
