# Sprint 3 回顾 — 补丁生成与本地验证

## 已交付
- `patch_generator.py` — LLM 输出 → unified diff Patch 解析,带 cheat guard(只改测试拒绝)
- `patch_applier.py` — git 分支化应用 + build/test 验证 + 失败彻底回滚(reset/clean/branch -D/stash pop)
- `patch_loop.py` — 最多 3 次重试,失败时把 diff + 错误回灌给 LLM
- `prompts/diagnose.md` — 新增 §6 Type 字段 (code_bug/runtime/config/resource/external/unknown)
- `main.py::_parse_diagnosis` — 解析 type
- `main.py::_maybe_run_patch_loop` — diagnose 后条件触发(type=code_bug && 有源码定位 && 非 readonly && 有 build_cmd)
- `notebook/playbook/code-fix-local.md`
- `test_sprint3.py` — 56 项测试(roadmap 验收线 ≥25),用真实 git + FakeLLM stub

## 测试总数
245 / 245 通过 (Sprint 2 末 189 + 本 sprint 56). 零回归.

## 设计决策
1. **git 命令而非 GitPython** — 零依赖,直接 subprocess.run
2. **stash + branch + reset 三重回滚** — 确保任何阶段失败都不留脏工作区,测试反复验证 `git status --porcelain` 为空
3. **cheat guard** — `Patch.touches_only_tests()` 在 apply 前拦截"只改测试让结果作弊"的补丁
4. **PatchApplier 接受 run 钩子** — 默认 subprocess.run,但允许测试注入(实际用真 git 更直接,所以测试没用钩子,留作未来扩展)
5. **失败原因截断** — build/test 输出截到 5000 字符再回灌 LLM,防止重试 prompt 爆炸
6. **完全独立的失败路径** — generator parse 失败 / apply 失败 / build 失败 / test 失败 各自独立,失败信息粒度精确
7. **触发严格** — 必须同时满足 type=code_bug + 有 SourceLocation + 非 readonly + repo 配置了 build_cmd,任何一项缺失都安静跳过

## 留给 Sprint 4 的钩子
- VerifiedPatch.result.branch_name 已经是真实 git 分支,Sprint 4 直接 `git push origin <branch>` 即可
- VerifiedPatch.result.commit_sha 已记录,Sprint 4 的 deploy_signal 检查直接用
- Incident 笔记里已经写了"等待 Sprint 4 推送至远端 / 创建 PR" — 状态机状态明确
- ParsedTrace.signature() (Sprint 2 留的) + Sprint 3 的 commit_sha → Sprint 4 production_watcher 复发检测的两个核心字段都齐了

## 已知约束
- LLM 必须严格按模板输出三段(修改说明/修改的文件/Diff),否则 parse 返回 None 触发重试
- 测试文件守卫只看路径名,不识别"业务代码改动 + 测试一起改"的合理 case(roadmap 提到这是 Sprint 3 接受的限制)
- 没有沙箱:build_cmd / test_cmd 直接在工作站上跑,user 需要保证它们不会破坏环境(用 docker / venv 是用户责任)
