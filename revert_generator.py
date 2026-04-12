"""
revert_generator — 当生产观察期检测到复发,自动 revert 已合并的补丁

流程:
  1. checkout base 分支(通常 main)
  2. git pull(拿到最新代码,包括我们刚 merge 的 commit)
  3. 创建新分支 revert/agent/<原分支名>
  4. git revert --no-edit <commit_sha>
  5. push + create_pr + merge_pr (走 GitHostClient,和正向补丁同一套)

完全不依赖 LLM:revert 是机械操作,git revert 自动生成 commit 信息。
失败也算完成 — revert 失败时返回 success=False,Sprint 4 中会升级人类。
"""

from __future__ import annotations

import time
import logging
import subprocess
from dataclasses import dataclass

from git_host import GitHostClient, PR

logger = logging.getLogger("ops-agent.revert_generator")


@dataclass
class RevertResult:
    success: bool
    revert_branch: str = ""
    pr: PR | None = None
    merged: bool = False
    error: str = ""
    stage: str = ""           # checkout / pull / branch / revert / push / pr / merge


class RevertGenerator:
    """生成并合并 revert PR"""

    def __init__(self, host: GitHostClient, run=None):
        self.host = host
        self._run = run or self._default_run

    def revert_and_merge(self, repo_path: str, commit_sha: str,
                         original_branch: str, base_branch: str = "main",
                         failure_reason: str = "") -> RevertResult:
        if not commit_sha:
            return RevertResult(success=False, stage="branch",
                                error="commit_sha is empty")

        revert_branch = self._make_revert_branch(original_branch)

        # 1. checkout base
        rc, out = self._run(["git", "checkout", base_branch], cwd=repo_path)
        if rc != 0:
            return RevertResult(success=False, stage="checkout",
                                error=f"checkout {base_branch} failed: {out}")

        # 2. pull(失败不中止,本地可能没有 remote)
        self._run(["git", "pull", "--ff-only"], cwd=repo_path)

        # 3. 创建 revert 分支
        rc, out = self._run(["git", "checkout", "-b", revert_branch], cwd=repo_path)
        if rc != 0:
            return RevertResult(success=False, stage="branch",
                                error=f"branch failed: {out}")

        # 4. revert
        rc, out = self._run(
            ["git", "-c", "user.email=ops-agent@local",
             "-c", "user.name=OpsAgent",
             "revert", "--no-edit", commit_sha],
            cwd=repo_path,
        )
        if rc != 0:
            # 清理:切回 base 并删除 revert 分支
            self._run(["git", "revert", "--abort"], cwd=repo_path)
            self._run(["git", "checkout", base_branch], cwd=repo_path)
            self._run(["git", "branch", "-D", revert_branch], cwd=repo_path)
            return RevertResult(success=False, stage="revert",
                                error=f"git revert failed: {out}",
                                revert_branch=revert_branch)

        # 5. push
        ok, push_out = self.host.push_branch(repo_path, revert_branch)
        if not ok:
            # 本地 revert 已成功,但推送失败 — 仍然返回部分成功信息
            return RevertResult(success=False, stage="push",
                                revert_branch=revert_branch,
                                error=f"push failed: {push_out}")

        # 6. 创建 PR
        title = f"revert: auto-revert {commit_sha[:8]} (recurrence detected)"
        body = self._build_revert_body(commit_sha, original_branch, failure_reason)
        pr_result = self.host.create_pr(
            repo_path=repo_path, branch=revert_branch, base=base_branch,
            title=title, body=body,
        )
        if not pr_result.success:
            return RevertResult(success=False, stage="pr",
                                revert_branch=revert_branch,
                                error=f"create_pr failed: {pr_result.error}")

        # 7. 立即合并
        ok, merge_out = self.host.merge_pr(repo_path, pr_result.pr.number)
        if not ok:
            return RevertResult(success=False, stage="merge",
                                revert_branch=revert_branch,
                                pr=pr_result.pr,
                                error=f"merge failed: {merge_out}")

        return RevertResult(
            success=True, stage="merge",
            revert_branch=revert_branch,
            pr=pr_result.pr,
            merged=True,
        )

    @staticmethod
    def _make_revert_branch(original_branch: str) -> str:
        ts = time.strftime("%Y%m%d-%H%M%S")
        slug = (original_branch or "unknown").replace("/", "-")[:40]
        return f"revert/agent/{slug}-{ts}"

    @staticmethod
    def _build_revert_body(commit_sha: str, original_branch: str,
                           failure_reason: str) -> str:
        return (
            "## 🤖 Auto-revert by OpsAgent\n\n"
            f"原 commit: `{commit_sha}`\n"
            f"原分支: `{original_branch}`\n\n"
            "### 触发原因\n"
            f"{failure_reason or '生产观察期检测到原异常复发'}\n\n"
            "### 后续\n"
            "- 已自动合并 revert\n"
            "- OpsAgent 已升级给人类\n"
            "- 自动修复功能可能已被熔断,需人工评估\n"
        )

    @staticmethod
    def _default_run(cmd, cwd: str, timeout: int = 120) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            )
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, f"timeout"
        except FileNotFoundError as e:
            return 127, str(e)
