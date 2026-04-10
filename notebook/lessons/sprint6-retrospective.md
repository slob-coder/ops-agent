# Sprint 6 回顾 — 可观测性与 IM 集成

## 已交付
- `audit.py` — append-only JSONL 审计日志,daily-rotated,容错读取(损坏行跳过),`record` / `read_day` / `count_by_type` / `list_dates`
- `notifier.py`:
  - `NotifierConfig` + yaml 加载 + 环境变量 webhook 覆盖 + `in_quiet_hours()` 跨日时段判定
  - `NoOpNotifier` / `SlackNotifier` / `DingTalkNotifier` / `FeishuNotifier`(共享 `_HTTPNotifier` 基类)
  - `make_notifier` 工厂 + `PolicyNotifier` 策略包装(notify_on 白名单 + quiet hours)
- `reporter.py::DailyReporter` — LLM 总结 + 失败回退到模板化纯统计 + `should_send_today` / `mark_sent` 防重
- `health.py` 扩展 — 加 `metrics_fn` 参数和 `/metrics` 路由,Prometheus 0.0.4 格式
- `notebook/config/notifier.yaml.example`
- `main.py` 集成:
  - `__init__` 创建 audit / notifier / reporter / 计数器
  - `_emit_audit` 自动塞 target / incident,顺手更新内存计数器
  - `_emit_notify` 走 PolicyNotifier
  - `render_prometheus_metrics` 手写 Prometheus 文本(零依赖)
  - `start_health_server` 自动挂 metrics 端点
  - `maybe_send_daily_report` 主循环可选调用
- `test_sprint6.py` — 95 项测试

## 测试总数
493 / 493 通过 (Sprint 5 末 398 + 本 sprint 95). 零回归.

## 设计决策
1. **审计日志在 git 之外** — `audit/` 目录不进 notebook 的 git commit(roadmap §6.5),append-only 不可篡改,避免事后无意覆盖
2. **不可序列化字段降级为字符串** — `audit.record(weird=object())` 不会崩溃,自动 `str()` 转换,人类事后能看到原始信息
3. **stdlib urllib 而非 requests** — 一致的零依赖原则,Slack/钉钉/飞书 webhook 都是简单 POST JSON
4. **环境变量 > yaml 文件** — `OPS_NOTIFIER_WEBHOOK_URL` 覆盖 yaml,生产环境放凭据更安全(yaml 留模板)
5. **PolicyNotifier 是包装器,不是基类** — 任何 `Notifier` 实现都可以套上策略,不强制继承
6. **quiet_hours 跨日逻辑** — `22:00 → 08:00` 明确支持(start > end 视为跨日),避免常见运维 bug
7. **DailyReporter 永不失败** — LLM 异常 → 回退模板,模板基于纯审计统计,即使 LLM API key 全错也能收到日报
8. **Reporter 防重靠 marker 文件** — 一天发一份,`marker_dir/sent-YYYY-MM-DD` 单个空文件即标记,跨进程持久
9. **手写 Prometheus 文本** — 不引 prometheus_client,一个 `render_prometheus_metrics` 方法 50 行搞定;Sprint 5 留下的 `health_snapshot` + 内存计数器 + `limits.status()` 三处源数据合成
10. **counter 是内存累加,不是审计回放** — 既能秒级更新,又不依赖 audit 文件读取性能

## 完整能力清单(5 个 sprint 累计)
```
核心能力
├── 多目标支持        (SSH/Docker/K8s/Local)              [Sprint 1]
├── 实时对话式交互     (CLI, 可中断)                        [Sprint 1]
├── 自主异常检测与修复 (observe→diagnose→act→verify→reflect) [Sprint 1]
├── 源码 Bug 修复     (定位→生成→本地验证→PR→生产观察)      [Sprint 2-4]
├── 完整爆炸半径限制   (频率/并发/冷却/token/auto-merge)      [Sprint 1+4]
└── 紧急停止开关       (文件/信号/CLI)                      [Sprint 1]

可靠性
├── 崩溃自恢复         (state.json + recover_state)        [Sprint 5]
├── 状态持久化         (原子写 + 版本号)                    [Sprint 5]
├── LLM 降级           (RetryingLLM + degraded 状态)        [Sprint 5]
├── Notebook 完整性    (verify_integrity / 远端备份)        [Sprint 5]
└── Watchdog + systemd (ops-agent.service)                  [Sprint 5]

可观测性
├── 审计日志          (append-only JSONL,日滚动)           [Sprint 6]
├── Prometheus metrics (/metrics 端点)                     [Sprint 6]
├── IM 通知           (Slack/钉钉/飞书 + 策略)              [Sprint 6]
├── 每日健康报告       (LLM + 模板回退)                     [Sprint 6]
└── Token 成本日报     (audit + limits 联动)                [Sprint 6]

知识沉淀
├── Notebook (git + markdown)                              [Sprint 1]
├── Playbook 扩展(source-locate / code-fix-local / -full)  [Sprint 2-4]
├── Incident 归档                                          [Sprint 1]
└── Lessons 蒸馏                                           [Sprint 1+]
```

## 测试覆盖

| Sprint | 模块 | 测试数 | 累计 |
|---|---|---|---|
| 0 (基线) | basic + blacklist | 85 | 85 |
| 1 | 多目标 / 爆炸半径 / 紧急停止 | 53 | 138 |
| 2 | stack_parser / source_locator | 51 | 189 |
| 3 | patch generation / applier / loop | 56 | 245 |
| 4 | git_host / deploy / production / revert | 74 | 319 |
| 5 | state / pending / health / RetryingLLM | 79 | 398 |
| 6 | audit / notifier / reporter / metrics | 95 | **493** |

每个 sprint 测试数都增长,从未减少;每个 sprint 都零回归.

## 完成状态
roadmap §0 中规划的 5 个 sprint **全部完成**.OpsAgent 现在是一个:
- 能管多目标(SSH/Docker/K8s)
- 能自主诊断和修复(包括代码 bug)
- 能保证修改有效后才合并 PR
- 能崩溃自恢复 + LLM 抖动不崩
- 能让人类完整观察(audit/metrics/IM/日报)

的可信赖生产组件.

## 给后续开发的建议
1. **真实环境演练** — 单元测试已经足够多,但真实场景(真 git host / 真 K8s / 真 LLM)的端到端演练是下一步必须做的
2. **playbook 扩展** — 把团队的实际故障 case 沉淀成 playbook,Agent 越用越聪明
3. **多副本部署** — 当前是单进程假设,如果要 HA 部署,`PendingEventQueue` / `state.json` / `LimitsEngine` 都要重新设计
4. **审计日志中心化** — 可以把 `audit/*.jsonl` 通过 vector / fluent-bit 推到 SIEM,做集中分析
5. **告警分级** — 当前 `critical` / `warning` / `info` 三级够用,但如果要对接 PagerDuty 等可以加 `pager` 级
