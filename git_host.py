"""
git_host — 抽象 Git 托管平台(GitHub / GitLab / Gitea)的 PR 操作

通过命令行工具(`gh` / `glab`)执行,避免引入 PyGithub / python-gitlab 等 SDK。
所有方法都返回结构化结果,失败时把 stderr 放进 error 字段而不是抛异常。

测试时可注入 NoopGitHost 或自定义 _run。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger("ops-agent.git_host")


@dataclass
class PR:
    number: int
    url: str
    branch: str
    base: str = "main"
    sha: str = ""               # head commit sha
    state: str = "open"         # open / merged / closed


@dataclass
class PRStatus:
    state: str                  # open / merged / closed / unknown
    mergeable: bool = False
    ci_passing: bool = True     # 没 CI 时默认 True
    error: str = ""


@dataclass
class PRResult:
    success: bool
    pr: PR | None = None
    error: str = ""


class GitHostClient(ABC):
    """Git 托管平台抽象接口"""

    @abstractmethod
    def push_branch(self, repo_path: str, branch: str) -> tuple[bool, str]: ...

    @abstractmethod
    def create_pr(self, repo_path: str, branch: str, base: str,
                  title: str, body: str) -> PRResult: ...

    @abstractmethod
    def merge_pr(self, repo_path: str, pr_number: int) -> tuple[bool, str]: ...

    @abstractmethod
    def get_pr_status(self, repo_path: str, pr_number: int) -> PRStatus: ...


# ────────────────────────────────────────
# GitHub via gh CLI
# ────────────────────────────────────────

class GitHubClient(GitHostClient):
    """用 `gh` CLI 实现 GitHub 操作。

    要求工作站已 `gh auth login`。
    """

    def __init__(self, run=None):
        self._run = run or _default_run

    def push_branch(self, repo_path: str, branch: str) -> tuple[bool, str]:
        rc, out = self._run(["git", "push", "-u", "origin", branch], cwd=repo_path)
        return rc == 0, out

    def create_pr(self, repo_path: str, branch: str, base: str,
                  title: str, body: str) -> PRResult:
        rc, out = self._run(
            ["gh", "pr", "create",
             "--base", base,
             "--head", branch,
             "--title", title,
             "--body", body],
            cwd=repo_path,
        )
        if rc != 0:
            return PRResult(success=False, error=out)
        # gh pr create 输出是 PR URL
        url = out.strip().splitlines()[-1] if out.strip() else ""
        number = self._parse_pr_number(url)
        # 抓 head sha
        rc2, sha_out = self._run(
            ["git", "rev-parse", branch], cwd=repo_path,
        )
        sha = sha_out.strip() if rc2 == 0 else ""
        return PRResult(
            success=True,
            pr=PR(number=number, url=url, branch=branch, base=base, sha=sha),
        )

    def merge_pr(self, repo_path: str, pr_number: int) -> tuple[bool, str]:
        # --auto 让 gh 等待 CI 通过再合并;--squash 减少历史噪音
        rc, out = self._run(
            ["gh", "pr", "merge", str(pr_number),
             "--squash", "--delete-branch"],
            cwd=repo_path,
        )
        return rc == 0, out

    def get_pr_status(self, repo_path: str, pr_number: int) -> PRStatus:
        rc, out = self._run(
            ["gh", "pr", "view", str(pr_number),
             "--json", "state,mergeable,statusCheckRollup"],
            cwd=repo_path,
        )
        if rc != 0:
            return PRStatus(state="unknown", error=out)
        try:
            import json
            data = json.loads(out)
        except Exception as e:
            return PRStatus(state="unknown", error=f"json parse: {e}")
        state = (data.get("state") or "OPEN").lower()
        mergeable = data.get("mergeable") in (True, "MERGEABLE")
        # CI 通过 = 没有 FAILURE/ERROR 状态
        checks = data.get("statusCheckRollup") or []
        ci_passing = True
        for c in checks:
            concl = (c.get("conclusion") or "").upper()
            status = (c.get("status") or "").upper()
            if concl in ("FAILURE", "CANCELLED", "TIMED_OUT", "ERROR"):
                ci_passing = False
                break
            if status == "IN_PROGRESS":
                ci_passing = False  # 暂未通过
        return PRStatus(state=state, mergeable=mergeable, ci_passing=ci_passing)

    @staticmethod
    def _parse_pr_number(url: str) -> int:
        # https://github.com/owner/repo/pull/42
        try:
            return int(url.rstrip("/").split("/")[-1])
        except Exception:
            return 0


# ────────────────────────────────────────
# GitLab via glab CLI(精简实现,接口对齐)
# ────────────────────────────────────────

class GitLabClient(GitHostClient):
    def __init__(self, run=None):
        self._run = run or _default_run

    def push_branch(self, repo_path: str, branch: str) -> tuple[bool, str]:
        rc, out = self._run(["git", "push", "-u", "origin", branch], cwd=repo_path)
        return rc == 0, out

    def create_pr(self, repo_path, branch, base, title, body) -> PRResult:
        rc, out = self._run(
            ["glab", "mr", "create",
             "--target-branch", base,
             "--source-branch", branch,
             "--title", title,
             "--description", body,
             "--yes"],
            cwd=repo_path,
        )
        if rc != 0:
            return PRResult(success=False, error=out)
        url = ""
        for line in out.splitlines():
            if line.startswith("http"):
                url = line.strip()
        number = GitHubClient._parse_pr_number(url)
        rc2, sha = self._run(["git", "rev-parse", branch], cwd=repo_path)
        return PRResult(success=True, pr=PR(
            number=number, url=url, branch=branch, base=base,
            sha=sha.strip() if rc2 == 0 else "",
        ))

    def merge_pr(self, repo_path, pr_number) -> tuple[bool, str]:
        rc, out = self._run(
            ["glab", "mr", "merge", str(pr_number),
             "--squash", "--remove-source-branch", "--yes"],
            cwd=repo_path,
        )
        return rc == 0, out

    def get_pr_status(self, repo_path, pr_number) -> PRStatus:
        rc, out = self._run(
            ["glab", "mr", "view", str(pr_number), "-F", "json"],
            cwd=repo_path,
        )
        if rc != 0:
            return PRStatus(state="unknown", error=out)
        try:
            import json
            data = json.loads(out)
        except Exception as e:
            return PRStatus(state="unknown", error=str(e))
        state = (data.get("state") or "opened").lower()
        if state == "opened":
            state = "open"
        return PRStatus(
            state=state,
            mergeable=data.get("merge_status") == "can_be_merged",
            ci_passing=(data.get("pipeline") or {}).get("status") in (None, "success"),
        )


# ────────────────────────────────────────
# Noop / 测试用
# ────────────────────────────────────────

class NoopGitHost(GitHostClient):
    """什么都不做的实现,用于测试和 dry-run。

    所有调用都被记录在 self.calls 里,可被断言。
    """
    def __init__(self):
        self.calls = []
        self.next_pr_number = 1

    def push_branch(self, repo_path, branch):
        self.calls.append(("push", repo_path, branch))
        return True, ""

    def create_pr(self, repo_path, branch, base, title, body):
        self.calls.append(("create_pr", repo_path, branch, base, title, body))
        n = self.next_pr_number
        self.next_pr_number += 1
        return PRResult(success=True, pr=PR(
            number=n, url=f"https://example.invalid/pr/{n}",
            branch=branch, base=base, sha="deadbeef" * 5,
        ))

    def merge_pr(self, repo_path, pr_number):
        self.calls.append(("merge_pr", repo_path, pr_number))
        return True, "merged"

    def get_pr_status(self, repo_path, pr_number):
        self.calls.append(("get_pr_status", repo_path, pr_number))
        return PRStatus(state="open", mergeable=True, ci_passing=True)


def _default_run(cmd, cwd: str, timeout: int = 120) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return 127, f"command not found: {e}"


def make_client(host_type: str, run=None) -> GitHostClient:
    """工厂方法:从字符串创建 client"""
    t = (host_type or "").lower()
    if t in ("github", "gh"):
        return GitHubClient(run=run)
    if t in ("gitlab", "glab"):
        return GitLabClient(run=run)
    if t in ("noop", "none", "dryrun", ""):
        return NoopGitHost()
    raise ValueError(f"unknown git host: {host_type}")
