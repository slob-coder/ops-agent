# 本地代码修复

## 什么时候用我
- 上一步 `source-locate` 已成功定位到本地源码
- 诊断结论的 `type` 字段为 `code_bug`(在源码层面就能修复的 bug,不是配置/资源/外部依赖问题)
- 该源码仓库在 targets.yaml 里配置了 `path` 和 `build_cmd`(测试可选)

## 流程(由 Agent 自动执行,不需要逐步输出命令)
1. **生成补丁** — PatchGenerator 调用 LLM,基于 diagnosis + SourceLocation 生成 unified diff
2. **应用** — PatchApplier 创建 `fix/agent/<incident-id>-<时间>` 分支,git apply,git commit
3. **编译** — 跑 SourceRepo.build_cmd(超时 5 分钟)
4. **测试** — 跑 SourceRepo.test_cmd(超时 10 分钟,可选)
5. **失败 → 自动重试** — 上次的 diff 和错误回灌给 LLM,最多 3 次
6. **三次都失败 → 升级人类**,本地工作区已彻底回滚

## 安全约束
- 永远不会把改动 push 到远端(Sprint 4 才会做)
- 永远不会修改测试文件 — PatchApplier 检测到 patch 只动测试就直接拒绝
- 任何阶段失败都会 `git reset --hard` + 删除新分支 + `git stash pop`
- 改动只发生在本地 clone 上,生产服务零影响

## 成功标准
- VerifiedPatch.result.success == True
- Incident 中记录补丁说明、本地分支名、commit sha,以及"等待 Sprint 4 推送"的状态

## 失败降级
- LLM 反复输出不可解析的 diff:三次后升级人类
- build_cmd 反复失败:升级人类,告诉他诊断假设可能错了
- test_cmd 反复失败:升级人类,可能需要更新测试或诊断错根因
- 工作区残留:不应该发生;如果发生,人类需手动 `git stash list` 检查
