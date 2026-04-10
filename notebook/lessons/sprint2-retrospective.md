# Sprint 2 回顾 — 源码地图与异常反向定位

## 已交付
- `stack_parser.py` — Python / Java / Go / Node 四语言 stack trace 解析器,带 ParsedTrace.signature() 给 Sprint 4 复发检测复用
- `source_locator.py` — 路径前缀映射 + 路径后缀打分 + 文件名兜底,带函数提取 (Python 缩进 / 大括号语言匹配)
- `targets.py::SourceRepo` 数据类 + `Target.get_source_repos()`,完全向后兼容(底层仍是 list[dict])
- `main.py::_locate_source_from_text` 与 `_diagnose` 集成,所有失败路径都降级为"找不到"不影响主流程
- `prompts/diagnose.md` 新增 `{source_locations}` 变量
- `notebook/playbook/source-locate.md` Agent 检索可见的 playbook
- `test_sprint2.py` 51 项测试(roadmap 验收线 ≥30)

## 测试总数
189 / 189 通过(Sprint 1 末 138 + 本 sprint 51)

## 设计决策
1. **保持 source_repos 底层为 list[dict]**:`targets.py` 不破坏 Sprint 1 任何字段类型,新增 `get_source_repos()` 提供类型化视图。Sprint 1 测试零修改通过。
2. **解析失败 = 空结果而非异常**:`_locate_source_from_text` 把所有 import / parse / locate 错误都吞掉,诊断流程绝不被新模块拖累。
3. **匹配打分而非首匹配**:`utils.py` 在多个目录重名时按"路径后缀重合段数"打分,远比单纯文件名匹配靠谱。
4. **函数提取尽力而为**:Python 走缩进 + def 锚点;C 系列走大括号配对。失败也不影响 SourceLocation 其他字段。
5. **language 字段做软过滤**:repo 声明 language=python 时不会被 java frame 误命中,但允许 node↔js↔ts 互认。

## 留给 Sprint 3 的钩子
- `ParsedTrace.signature()` 已实现,Sprint 4 的 ProductionWatcher 直接调用
- `SourceLocation.local_file` 是绝对路径,Sprint 3 PatchApplier 直接拿来 git apply
- diagnose prompt 中的 `{source_locations}` 已经把代码摆给 LLM,Sprint 3 只需让 LLM 在结论里加 `type: code_bug` 字段
