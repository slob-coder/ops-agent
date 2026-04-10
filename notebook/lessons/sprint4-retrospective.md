# Sprint 4 回顾 — PR 工作流与生产观察

## 已交付
- `git_host.py` — `GitHostClient` 抽象 + `GitHubClient`(gh CLI)+ `GitLabClient`(glab CLI)+ `NoopGitHost`(测试/dry-run)+ `make_client` 工厂
- `deploy_watcher.py` — 4 种信号源:`http` / `file` / `command` / `fixed_wait`,所有 IO 全部可注入
- `production_watcher.py` — 基于 `ParsedTrace.signature()` 的精确复发检测,4 种 outcome:OK / FAILED_RECURRENCE / OBSERVE_ERROR / NO_BASELINE
- `revert_generator.py` — 自动 `git revert` + push + create PR + merge,所有阶段失败有精确 stage 标记
- `templates/pr-body.md` — PR 描述模板
- `notebook/playbook/code-fix-full.md` — Agent 检索可见的完整流程 playbook
- `targets.py::SourceRepo` — 新增 `git_host` / `base_branch` / `deploy_signal` 字段(默认值兼容 Sprint 1-3)
- `limits.py` — 新增 `max_auto_merges_per_day`(默认 5)+ `check_auto_merge()` / `record_auto_merge()`
- `main.py::_run_pr_workflow` — 完整 push → PR → CI 检查 → merge → 部署观察 → 复发检测 → revert 串联
- `main.py::_run_auto_revert` — 复发自动 revert + 升级人类
- `main.py::_build_pr_body` / `_make_git_host` / `_make_observe_fn` / `_note` — 注入点 + 辅助
- `test_sprint4.py` — 74 项测试

## 测试总数
319 / 319 通过 (Sprint 3 末 245 + 本 sprint 74). 零回归.

## 设计决策
1. **CLI 工具优先,SDK 次之** — `gh` / `glab` 已是开发者标配,直接用 CLI 避开 PyGithub / python-gitlab 依赖,凭据复用用户已有的登录态
2. **所有外部 IO 可注入** — git_host 的 `run`、deploy_watcher 的 `sleep_fn/now_fn/http_fn/run_fn`、main 的 `_make_git_host/_make_observe_fn` — 让 74 项测试全部毫秒级跑完,无需真 git/网络/CI
3. **NoopGitHost 是一等公民** — 不只是测试用,生产环境配 `git_host: noop` 可以禁用自动 PR 同时保留本地补丁验证(渐进式启用)
4. **复发检测严格匹配 signature** — 不做模糊匹配,避免误报。signature 由 Sprint 2 留下的 `ParsedTrace.signature()` 提供,Sprint 4 零额外工作
5. **PR 合并前再次查 CI** — `get_pr_status` 返回 `ci_passing=False` 时直接降级为"等人类 review",绝不在 CI 红的时候硬合并
6. **revert 也算自动合并次数** — `_run_auto_revert` 调用 `record_auto_merge()`,防止恶性 revert 循环耗光配额(双向计入更安全)
7. **错误边界清晰** — 8 种降级路径都明确写出 chat.escalate / chat.say(warning) + _note 写入 incident,人类能从笔记上完整重建发生了什么
8. **复发后立即停手** — Sprint 4 没做"revert 后再尝试新补丁"的循环,而是直接升级人类。roadmap 留在 Sprint 5+ 的"硬熔断"思路,本 sprint 已隐式实现:revert 后不会再生成补丁(主循环只有 incident 触发,不会自动重试同一个)

## 完整闭环达成
```
Sprint 2: traceback → SourceLocation
Sprint 3: SourceLocation → VerifiedPatch (本地 git 分支 + commit_sha)
Sprint 4: VerifiedPatch → push → PR → merge → 部署观察 → 复发检测 → 关闭/revert
```
你在 Sprint 1 之前提的核心需求 "PR 自动合并但必须保证修改有效" — 已经齐了:
- 本地编译 + 测试通过(Sprint 3)
- PR CI 通过(Sprint 4 二次检查)
- 部署信号到位(DeployWatcher)
- 5 分钟生产观察无复发(ProductionWatcher)
- 任一环节失败 → 自动 revert + 升级

## 留给 Sprint 5 的钩子
- `_make_observe_fn` 默认从 `repo.log_path` 读取,Sprint 5 可以接通统一的 observe 服务
- `limits.record_auto_merge` 已记录到 deque,Sprint 5 的崩溃恢复需要把这个状态持久化
- `_last_error_text` 是 in-memory 的,Sprint 5 崩溃恢复要把它落到 state.json
- DeployWatcher 的 http 默认实现走 stdlib `urllib`,Sprint 5 加 audit 时可以在这一层加 hook
