# 完整代码修复流程(本地验证 + PR + 生产观察)

## 什么时候用我
- code-fix-local 已成功产出 VerifiedPatch(本地编译 + 测试通过)
- 该 SourceRepo 配置了 git host 类型(targets.yaml `source_repos[].git_host: github|gitlab`)
- 当前限流允许:今天自动合并次数未达上限

## 流程
1. **push 分支** — `git push -u origin fix/agent/...`
2. **创建 PR** — 用 `templates/pr-body.md` 填充 incident 上下文
3. **再次检查 CI** — 如果 PR CI 已经失败,**不合并**,降级为"等人类 review"
4. **自动合并** — `gh pr merge --squash --delete-branch`
5. **等待部署信号** — DeployWatcher,根据 `deploy_signal.type` 选择 http / file / command / fixed_wait
6. **生产观察 5 分钟** — ProductionWatcher 用 ParsedTrace.signature() 检测原异常是否复发
   - 无复发 → 关闭 Incident ✅
   - 有复发 → RevertGenerator 自动 revert 并合并 → 升级人类 ⚠️

## 安全约束
- limits.yaml `max_auto_merges_per_day` 是硬上限(默认 5),超过直接降级"等人类"
- 所有 git host 操作通过 `gh` / `glab` CLI 执行,凭据由 user 在工作站上配置
- 复发检测**只**比较 ParsedTrace.signature() 的精确匹配,不做模糊匹配,避免误报
- revert 失败 → 立即升级人类,不再做第二次自动 revert
- 任何步骤异常 → 当前 incident 标记为"自动流程中断",降级走人工

## 成功标准
- PR 已 merge,sha 已记录
- 部署信号确认到位
- 5 分钟观察期内无复发
- Incident 关闭,reflect 写入 lessons

## 失败降级
- push/PR 失败 → 走 code-fix-local 的"等待 Sprint 4"流程,人类介入
- 合并被分支保护规则拒绝 → 降级为"已创建 PR,等人类 review"
- 部署信号超时 → 不合并 revert,只升级人类("我合并了但不知道部署没")
- 复发检测到 → 自动 revert + 升级 + 当天禁用自动修复
