"""
PR 工作流 Mixin — Sprint 4: push → PR → merge → 部署观察 → revert
"""

import os
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("ops-agent")


class PRWorkflowMixin:
    """Git 推送、PR 创建/合并、部署观察、自动回滚"""

    def _make_git_host(self, repo):
        """Sprint 4: 工厂方法,根据 repo.git_host 创建 client。

        测试可以 monkey-patch 这个方法注入 NoopGitHost。
        """
        from infra.git_host import make_client
        return make_client(getattr(repo, "git_host", "") or "noop")

    def _make_observe_fn(self, repo):
        """Sprint 4: 给 ProductionWatcher 用的 observe_fn

        默认从 repo.log_path 抓最后 200 行;失败返回空串。
        测试可以 monkey-patch 这个方法。
        """
        log_path = getattr(repo, "log_path", "")
        if not log_path:
            return lambda: ""
        def _obs():
            try:
                result = self._run_cmd(f"tail -n 200 {log_path}", timeout=10)
                return getattr(result, "stdout", "") or str(result)
            except Exception:
                return ""
        return _obs

    def _run_pr_workflow(self, verified, repo) -> None:
        """Sprint 4: VerifiedPatch → push → PR → merge → 部署观察 → (可选 revert)

        所有失败都降级,绝不抛异常。状态全部写入当前 Incident 笔记。
        """
        if not getattr(repo, "git_host", ""):
            self._note("Sprint 4 流程跳过: repo 未配置 git_host")
            return
        if not self.deploy_watcher or not self.prod_watcher:
            self._note("Sprint 4 流程跳过: watchers 未初始化")
            return

        # 1. 限流检查
        ok, reason = self.limits.check_auto_merge()
        if not ok:
            self._note(f"Sprint 4 流程跳过: {reason}")
            self.chat.say(f"⛔ 自动合并被限流拒绝: {reason}", "warning")
            return

        host = self._make_git_host(repo)
        repo_path = repo.path
        branch = verified.result.branch_name
        commit_sha = verified.result.commit_sha

        # 2. push
        self.chat.say(f"推送分支 {branch} 到远端...", "info")
        ok, push_out = host.push_branch(repo_path, branch)
        if not ok:
            self._note(f"push 失败,降级等人类: {push_out}")
            self.chat.say(f"🚨 git push 失败，需要人类检查。\n详情：{push_out}", "critical")
            return

        # 3. 创建 PR
        title = f"fix(agent): {verified.patch.description[:60] or 'auto patch'}"
        body = self._build_pr_body(verified, repo, commit_sha)
        pr_result = host.create_pr(repo_path, branch, repo.base_branch or "main",
                                   title, body)
        if not pr_result.success:
            self._note(f"创建 PR 失败: {pr_result.error}")
            self.chat.say(f"🚨 create_pr 失败，需要人类检查。\n详情：{pr_result.error}", "critical")
            return
        pr = pr_result.pr
        self.chat.say(f"✓ PR 已创建: {pr.url}", "success")
        self._note(f"PR 已创建: #{pr.number} {pr.url}")

        # 4. 再次检查 PR 状态(CI)
        status = host.get_pr_status(repo_path, pr.number)
        if not status.ci_passing:
            self._note(f"PR CI 未通过/进行中,降级等人类: state={status.state}")
            self.chat.say("PR CI 未就绪,不自动合并,等人类决定", "warning")
            return

        # 5. 合并
        ok, merge_out = host.merge_pr(repo_path, pr.number)
        if not ok:
            self._note(f"merge 失败(可能被分支保护): {merge_out}")
            self.chat.say("merge 被拒绝(可能是分支保护),已留 PR 等人类 review", "warning")
            return
        self.limits.record_auto_merge()
        self.chat.say(f"✓ PR #{pr.number} 已自动合并", "success")
        self._note(f"PR #{pr.number} 已自动合并")

        # 6. 等待部署信号
        signal = getattr(repo, "deploy_signal", {}) or {}
        if signal:
            self.chat.say(f"等待部署信号 ({signal.get('type', '?')})...", "info")
            dstatus = self.deploy_watcher.wait_for_deploy(signal, commit_sha)
            if not dstatus.deployed:
                self._note(f"部署信号超时: {dstatus.error}")
                self.chat.say(f"🚨 部署信号超时，需要人类检查。\n详情：{dstatus.error}", "critical")
                return
            self._note(f"部署确认: {dstatus.detail}")

        # 7. 生产观察期
        observe_fn = self._make_observe_fn(repo)
        self.chat.say("进入生产观察期...", "info")
        wresult = self.prod_watcher.watch(
            original_error_text=self._last_error_text or "",
            observe_fn=observe_fn,
            duration=300, interval=30,
        )

        if wresult.success:
            self._note(f"生产观察通过: {wresult.detail}")
            self.chat.say("✓ 生产观察期通过,Incident 关闭", "success")
            try:
                self._close_incident("自动修复成功并通过生产观察")
            except Exception:
                pass
            return

        # 8. 复发或观察失败 → revert
        from infra.production_watcher import WatchOutcome
        if wresult.outcome == WatchOutcome.FAILED_RECURRENCE:
            self.chat.say("⚠ 检测到原异常复发,启动自动 revert", "critical")
            self._run_auto_revert(repo, host, commit_sha, branch,
                                  failure_reason=wresult.detail)
        elif wresult.outcome == WatchOutcome.NO_BASELINE:
            self._note("观察期无 baseline,无法判断复发,降级等人类")
            self.chat.say("无法做复发检测(无 baseline),已合并但需人类确认", "warning")
        else:
            self._note(f"观察期异常: {wresult.detail}")
            self.chat.say(f"🚨 生产观察期异常，需要人类检查。\n详情：{wresult.detail}", "critical")

    def _run_auto_revert(self, repo, host, commit_sha: str,
                         original_branch: str, failure_reason: str) -> None:
        """Sprint 4: 自动 revert 已合并的补丁 + 升级人类"""
        try:
            from safety.revert_generator import RevertGenerator
            rg = RevertGenerator(host)
            result = rg.revert_and_merge(
                repo_path=repo.path, commit_sha=commit_sha,
                original_branch=original_branch,
                base_branch=repo.base_branch or "main",
                failure_reason=failure_reason,
            )
        except Exception as e:
            logger.exception("revert crashed")
            self._note(f"revert 异常: {e}")
            self.chat.say(f"🚨 revert 异常，需要人类检查。\n详情：{e}", "critical")
            return

        if result.success:
            self._note(
                f"已自动 revert: 分支 {result.revert_branch}, "
                f"PR #{result.pr.number if result.pr else '?'}"
            )
            self.limits.record_auto_merge()  # revert 也算一次
            self.chat.say(
                f"🚨 已自动 revert 失败的补丁\n"
                f"原 commit: {commit_sha[:8]}\n原因: {failure_reason}\n"
                f"revert PR: {result.pr.url if result.pr else 'N/A'}\n"
                "请人工评估根因并决定是否再次尝试修复。",
                "critical",
            )
        else:
            self._note(f"revert 失败 stage={result.stage}: {result.error}")
            self.chat.say(
                f"🚨 ⚠️ 自动 revert 也失败了，需要人工立即介入！\n"
                f"原 commit: {commit_sha}\n失败阶段: {result.stage}\n错误: {result.error}",
                "critical",
            )

    def _build_pr_body(self, verified, repo, commit_sha: str) -> str:
        """Sprint 4: 渲染 PR 描述(基于 templates/pr-body.md)"""
        try:
            tmpl_path = Path(__file__).parent / "templates" / "pr-body.md"
            tmpl = tmpl_path.read_text(encoding="utf-8")
        except Exception:
            tmpl = ("## OpsAgent auto patch\n\n"
                    "{patch_description}\n\nCommit: {commit_sha}\n")
        replacements = {
            "{incident_id}": str(self.current_incident or "?"),
            "{target_name}": getattr(self.current_target, "name", "?"),
            "{severity}": "auto",
            "{root_cause}": "(see incident notebook)",
            "{patch_description}": verified.patch.description or "(no description)",
            "{build_cmd}": getattr(repo, "build_cmd", "") or "n/a",
            "{test_cmd}": getattr(repo, "test_cmd", "") or "n/a",
            "{test_status}": "✅" if getattr(repo, "test_cmd", "") else "⚪ (跳过)",
            "{attempts}": str(verified.attempts),
            "{files_changed}": "\n".join(f"- `{f}`" for f in verified.patch.files_changed),
            "{commit_sha}": commit_sha,
            "{timestamp}": datetime.utcnow().isoformat() + "Z",
        }
        for k, v in replacements.items():
            tmpl = tmpl.replace(k, v)
        return tmpl
