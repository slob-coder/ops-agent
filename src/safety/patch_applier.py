"""
patch_applier — 在本地源码 clone 上应用补丁并验证

完整流程(apply_and_verify):
  1. _ensure_clean_workspace        — 工作区脏 → stash 起来
  2. 创建 fix/agent/<id> 分支
  3. git apply patch
  4. git add . && git commit
  5. 跑 build_cmd(超时 5 分钟)        — 编译/语法检查
  6. 跑 test_cmd(超时 10 分钟)        — 单元测试
  7. 任何一步失败 → rollback 到原始状态
  8. 返回 VerificationResult,带每一步的输出

设计原则:
- 所有失败都回滚得彻底:`git reset --hard` + `git checkout 原分支` + 删新分支
- 测试和编译输出截断为最后 5000 字符,避免重试 prompt 爆炸
- repo.test_cmd 为空时跳过测试阶段(只验证编译)
- 全部使用 subprocess + 命令行 git,无 GitPython 依赖
"""

from __future__ import annotations

import os
import time
import shlex
import logging
import subprocess
from dataclasses import dataclass

from src.safety.patch_generator import Patch
from src.context_limits import get_context_limits

logger = logging.getLogger("ops-agent.patch_applier")


# 各阶段超时(秒)
BUILD_TIMEOUT = 300
TEST_TIMEOUT = 600


@dataclass
class VerificationResult:
    """补丁应用 + 验证的完整结果"""
    success: bool
    stage: str                       # applied / built / tested / failed-at-{apply,build,test,commit}
    branch_name: str = ""            # 成功时新分支名
    commit_sha: str = ""             # 成功时 commit sha
    apply_output: str = ""
    build_output: str = ""
    test_output: str = ""
    error_message: str = ""

    def short_summary(self) -> str:
        if self.success:
            return f"✓ {self.stage} on {self.branch_name} ({self.commit_sha[:8]})"
        return f"✗ {self.stage}: {self.error_message}"


class PatchApplier:
    """补丁应用器 + 本地验证

    依赖:工作站上有 git 命令。
    """

    def __init__(self, run=None):
        """run: 可选的命令执行函数,签名 run(cmd, cwd, timeout) -> (rc, stdout)
        默认用内部 _run。
        """
        self._run = run or self._default_run

    # ────────────────────────────────────────
    # 主流程
    # ────────────────────────────────────────

    def apply_and_verify(self, patch: Patch, repo, incident_id: str = "") -> VerificationResult:
        """完整流程。任何阶段失败都会自动回滚到原始状态。"""
        if not patch or not patch.is_valid():
            return VerificationResult(
                success=False, stage="failed-at-apply",
                error_message="patch is invalid (missing diff or @@)",
            )

        repo_path = repo.path
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            return VerificationResult(
                success=False, stage="failed-at-apply",
                error_message=f"not a git repo: {repo_path}",
            )

        # 防止 LLM 改测试作弊
        if patch.touches_only_tests():
            return VerificationResult(
                success=False, stage="failed-at-apply",
                error_message="patch only touches test files (cheating guard)",
            )

        # 记录原始状态以便回滚
        orig_branch = self._current_branch(repo_path)
        stashed = self._stash_if_dirty(repo_path)
        new_branch = self._make_branch_name(incident_id)

        try:
            # ── 1. 创建并切换到新分支 ──
            rc, out = self._run(["git", "checkout", "-b", new_branch], cwd=repo_path)
            if rc != 0:
                return self._fail("failed-at-apply", f"checkout -b failed: {out}",
                                  apply_output=out)

            # ── 2. apply patch ──
            apply_rc, apply_out = self._git_apply(repo_path, patch.diff)
            if apply_rc != 0:
                self._rollback(repo_path, orig_branch, new_branch, stashed)
                return VerificationResult(
                    success=False, stage="failed-at-apply",
                    apply_output=self._truncate(apply_out),
                    error_message=f"git apply failed (rc={apply_rc}): {self._truncate(apply_out)}",
                )

            # ── 3. commit ──
            self._run(["git", "add", "-A"], cwd=repo_path)
            commit_msg = f"fix(agent): {patch.description or 'auto-generated patch'}"
            rc, out = self._run(
                ["git", "-c", "user.email=ops-agent@local",
                 "-c", "user.name=OpsAgent",
                 "commit", "-m", commit_msg],
                cwd=repo_path,
            )
            if rc != 0:
                self._rollback(repo_path, orig_branch, new_branch, stashed)
                return VerificationResult(
                    success=False, stage="failed-at-commit",
                    apply_output=self._truncate(apply_out + "\n" + out),
                    error_message=f"git commit failed: {out}",
                )
            commit_sha = self._head_sha(repo_path)

            # ── 4. build ──
            build_output = ""
            if getattr(repo, "build_cmd", ""):
                rc, build_output = self._run_shell(repo.build_cmd, cwd=repo_path,
                                                   timeout=BUILD_TIMEOUT)
                if rc != 0:
                    self._rollback(repo_path, orig_branch, new_branch, stashed)
                    return VerificationResult(
                        success=False, stage="failed-at-build",
                        apply_output=self._truncate(apply_out),
                        build_output=self._truncate(build_output),
                        error_message=f"build failed (rc={rc})",
                    )

            # ── 5. test ──
            test_output = ""
            if getattr(repo, "test_cmd", ""):
                rc, test_output = self._run_shell(repo.test_cmd, cwd=repo_path,
                                                  timeout=TEST_TIMEOUT)
                if rc != 0:
                    self._rollback(repo_path, orig_branch, new_branch, stashed)
                    return VerificationResult(
                        success=False, stage="failed-at-test",
                        apply_output=self._truncate(apply_out),
                        build_output=self._truncate(build_output),
                        test_output=self._truncate(test_output),
                        error_message=f"tests failed (rc={rc})",
                    )

            # 全部通过 — 不回滚,留下分支供 Sprint 4 push
            return VerificationResult(
                success=True,
                stage="tested" if getattr(repo, "test_cmd", "") else "built",
                branch_name=new_branch,
                commit_sha=commit_sha,
                apply_output=self._truncate(apply_out),
                build_output=self._truncate(build_output),
                test_output=self._truncate(test_output),
            )

        except Exception as e:
            logger.exception("unexpected error in apply_and_verify")
            try:
                self._rollback(repo_path, orig_branch, new_branch, stashed)
            except Exception:
                pass
            return VerificationResult(
                success=False, stage="failed-at-apply",
                error_message=f"unexpected: {type(e).__name__}: {e}",
            )

    def rollback(self, repo, branch_name: str = "") -> bool:
        """主动回滚:由上层在不再需要分支时调用(目前 Sprint 3 不直接用)"""
        try:
            self._rollback(repo.path, "", branch_name, False)
            return True
        except Exception:
            return False

    # ────────────────────────────────────────
    # 内部辅助
    # ────────────────────────────────────────

    def _rollback(self, repo_path: str, orig_branch: str,
                  new_branch: str, stashed: bool):
        """彻底回到原始状态:reset 当前分支、切回原分支、删除新分支、pop stash"""
        # reset 工作区(不论当前在哪)
        self._run(["git", "reset", "--hard", "HEAD"], cwd=repo_path)
        self._run(["git", "clean", "-fd"], cwd=repo_path)
        if orig_branch:
            self._run(["git", "checkout", orig_branch], cwd=repo_path)
        if new_branch:
            self._run(["git", "branch", "-D", new_branch], cwd=repo_path)
        if stashed:
            self._run(["git", "stash", "pop"], cwd=repo_path)

    def _git_apply(self, repo_path: str, diff: str) -> tuple[int, str]:
        """通过 stdin 把 diff 喂给 git apply"""
        # 预检: diff 中引用的文件是否存在于仓库中
        missing = self._check_missing_files(repo_path, diff)
        if missing:
            # 尝试模糊匹配修复路径
            diff = self._fuzzy_fix_paths(diff, repo_path)
            missing = self._check_missing_files(repo_path, diff)
        if missing:
            logger.warning(f"patch_applier: missing files in repo: {missing}")
            return 1, f"files not found in repo: {', '.join(missing)}"
        logger.info(f"patch_applier: applying diff ({len(diff)} chars):\n{diff[:3000]}")
        try:
            proc = subprocess.run(
                ["git", "apply", "--3way", "--whitespace=nowarn", "-"],
                cwd=repo_path,
                input=diff,
                capture_output=True,
                text=True,
                timeout=60,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode, out
        except subprocess.TimeoutExpired:
            return 124, "git apply timeout"
        except Exception as e:
            return 1, f"git apply error: {e}"

    @staticmethod
    def _check_missing_files(repo_path: str, diff: str) -> list[str]:
        """检查 diff 中 +++ 行引用的文件是否在仓库中存在"""
        missing = []
        for line in diff.splitlines():
            if not line.startswith("+++ "):
                continue
            p = line[4:].strip()
            if p.startswith("b/"):
                p = p[2:]
            elif p.startswith("a/"):
                p = p[2:]
            if p and p != "/dev/null":
                full = os.path.join(repo_path, p)
                if not os.path.isfile(full):
                    missing.append(p)
        return missing

    @staticmethod
    def _fuzzy_fix_paths(diff: str, repo_path: str) -> str:
        """尝试修复 diff 中不存在的文件路径，通过 basename 模糊匹配

        LLM 经常编造路径（如 internal/lifecycle.py → app/services/lifecycle.py），
        但文件名通常是对的。用 basename 在仓库中搜索正确路径。
        """
        import subprocess as sp
        lines = diff.splitlines()
        fixed = []
        changed = False
        for line in lines:
            if not (line.startswith("--- a/") or line.startswith("+++ b/")):
                fixed.append(line)
                continue
            prefix = line[:6]  # "--- a/" or "+++ b/"
            p = line[6:].strip()
            if p == "/dev/null":
                fixed.append(line)
                continue
            full = os.path.join(repo_path, p)
            if os.path.isfile(full):
                fixed.append(line)
                continue
            # basename 搜索
            basename = os.path.basename(p)
            try:
                result = sp.run(
                    ["find", repo_path, "-name", basename, "-type", "f",
                     "-not", "-path", "*/.git/*", "-not", "-path", "*/__pycache__/*",
                     "-not", "-path", "*/node_modules/*"],
                    capture_output=True, text=True, timeout=10,
                    cwd=repo_path,
                )
                candidates = [c for c in result.stdout.strip().splitlines() if c]
                if len(candidates) == 1:
                    # 唯一匹配 → 替换
                    new_rel = os.path.relpath(candidates[0], repo_path)
                    fixed.append(f"{prefix}{new_rel}")
                    logger.info(f"patch_applier: fuzzy fix path: {p} → {new_rel}")
                    changed = True
                elif len(candidates) > 1:
                    # 多个匹配 → 选最相似的
                    best = min(candidates, key=lambda c: len(c))
                    new_rel = os.path.relpath(best, repo_path)
                    fixed.append(f"{prefix}{new_rel}")
                    logger.info(f"patch_applier: fuzzy fix path (best of {len(candidates)}): {p} → {new_rel}")
                    changed = True
                else:
                    fixed.append(line)
            except Exception:
                fixed.append(line)

        return "\n".join(fixed) if changed else diff

    def _current_branch(self, repo_path: str) -> str:
        rc, out = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
        return out.strip() if rc == 0 else ""

    def _head_sha(self, repo_path: str) -> str:
        rc, out = self._run(["git", "rev-parse", "HEAD"], cwd=repo_path)
        return out.strip() if rc == 0 else ""

    def _stash_if_dirty(self, repo_path: str) -> bool:
        rc, out = self._run(["git", "status", "--porcelain"], cwd=repo_path)
        if rc != 0 or not out.strip():
            return False
        rc, _ = self._run(
            ["git", "stash", "push", "-u", "-m", "ops-agent-pre-patch"],
            cwd=repo_path,
        )
        return rc == 0

    @staticmethod
    def _make_branch_name(incident_id: str) -> str:
        ts = time.strftime("%Y%m%d-%H%M%S")
        slug = (incident_id or "incident")[:24].replace("/", "-")
        return f"fix/agent/{slug}-{ts}"

    @staticmethod
    def _truncate(text: str) -> str:
        if not text:
            return ""
        limit = get_context_limits().patch_output_truncate_chars
        if len(text) <= limit:
            return text
        head = limit // 4
        tail = limit - head
        return text[:head] + "\n... [truncated] ...\n" + text[-tail:]

    @staticmethod
    def _fail(stage: str, msg: str, **kwargs) -> VerificationResult:
        return VerificationResult(success=False, stage=stage,
                                  error_message=msg, **kwargs)

    # 默认命令执行(用于 git 等结构化命令)
    @staticmethod
    def _default_run(cmd, cwd: str, timeout: int = 60) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
            )
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, f"timeout after {timeout}s"
        except FileNotFoundError as e:
            return 127, str(e)

    @staticmethod
    def _run_shell(cmd: str, cwd: str, timeout: int) -> tuple[int, str]:
        """跑 shell 字符串(build_cmd / test_cmd 是用户配的字符串)"""
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=cwd, capture_output=True,
                text=True, timeout=timeout,
            )
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as e:
            return 124, f"timeout after {timeout}s\n{e.stdout or ''}\n{e.stderr or ''}"
        except Exception as e:
            return 1, f"shell error: {e}"
