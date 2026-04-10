# 源码异常定位

## 什么时候用我
诊断中的异常日志里包含 stack trace 或 traceback,比如:
- Python `Traceback (most recent call last)`
- Java `Exception in thread ... at com.example.Foo(Foo.java:42)`
- Go `panic: ... goroutine ... main.go:42`
- Node.js `TypeError: ... at foo (/app/x.js:12:5)`

## 步骤
1. Agent 在 diagnose 阶段会自动抽取异常文本,调用 stack_parser 解析出 StackFrame。
2. 基于 targets.yaml 中配置的 source_repos,调用 source_locator 把每个 frame 映射到本地源码文件。
3. 读出目标行及前后 30 行代码,并尝试提取完整函数定义。
4. 定位结果会通过 `{source_locations}` 变量注入到 diagnose prompt。
5. 在 Incident 记录中明确写出:**"异常在 <文件>:<行号>,相关代码:<片段>"**,而不是只记录原始 stack trace。
6. 基于代码内容进一步诊断根因(例如 "第 51 行没有 null 检查,调用者传入 user_id 不存在时返回 None")。

## 成功标准
- Incident 记录中包含"文件:行号 + 代码片段"
- 根因分析明确引用了具体代码行,而不是只说"代码有 bug"
- 如果定位失败,记录"未能定位源码"并说明原因(例如 source_repos 未配置)

## 失败降级
- source_repos 未配置:继续用原始 stack trace 推理,但在结论中标注"缺少源码上下文"
- 路径前缀映射不对:在 Incident 中记录"找到了栈帧但映射到本地失败",建议人类修正 targets.yaml
- 文件重名歧义:locator 已用路径后缀打分,选了最佳匹配;如果诊断结果不合理,人类可复查
