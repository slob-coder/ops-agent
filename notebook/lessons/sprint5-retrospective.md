# Sprint 5 回顾 — 可靠性与崩溃恢复

## 已交付
- `state.py` — `AgentState` dataclass + 原子写(tmp + os.replace + fsync)+ 版本号丢弃 + 未知字段过滤
- `pending_events.py` — 文件 JSONL 队列,FIFO + 去重 + 大小上限 + raw 截断 + 损坏行容错
- `health.py` — stdlib `http.server` 后台线程,只读快照,默认 127.0.0.1,优雅 stop
- `llm.py::LLMDegraded` + `llm.py::RetryingLLM` — 包装任何 .ask() 兼容对象,指数退避,degraded 标志,状态变化回调,LLMInterrupted 透传不计失败
- `notebook.py` — `verify_integrity` (git fsck) / `push_to_remote` / `restore_from_remote`,`__init__` 加 `remote_url=""` 默认值兼容旧调用
- `main.py` 集成:
  - `_build_state_snapshot` / `save_state` / `recover_state` / `health_snapshot` / `start_health_server` / `stop_health_server`
  - `run()` 在 onboard 之前先 `recover_state` + `start_health_server`
  - 主循环每轮 `save_state`
  - `LLMDegraded` 异常处理:flip readonly + escalate + sleep 5min 后重试
- `ops-agent.service` systemd unit + `scripts/watchdog.sh` + `scripts/install.sh`
- `test_sprint5.py` — 79 项测试

## 测试总数
398 / 398 通过 (Sprint 4 末 319 + 本 sprint 79). 零回归.

## 设计决策
1. **state 版本号 = 1,旧版本直接丢弃** — 不做迁移,简单粗暴可靠;字段加减安全(未知字段会被过滤,但版本不一致时整个状态丢弃)
2. **state 写入原子化的三件套** — 写 tmp / fsync / os.replace,即使在 fsync 后崩溃也只会出现"旧文件"或"新文件",绝不会半文件
3. **HealthServer 用 stdlib 而非 Flask** — 零依赖,12KB 实现,后台 daemon 线程不阻塞 stop
4. **HealthServer 默认监听 127.0.0.1** — 不暴露公网,Sprint 6 metrics 同此原则
5. **RetryingLLM 是包装器而非替换** — 保留原 `LLMClient` 不动,所有现有测试零修改通过;生产用 `RetryingLLM(LLMClient())` 包一层即可
6. **LLMInterrupted 透传** — 人类中断不算 LLM 故障,不消耗重试预算,不污染 degraded 状态
7. **degraded 状态自动恢复** — 一次成功调用就自动 reset,无需显式 heal API
8. **PendingEventQueue 单消费者假设** — Agent 进程是唯一消费者,不需要 fcntl/dbm,append + rewrite 已经够用
9. **Notebook integrity 启动只查一次** — `git fsck` 慢,roadmap §5.5 明确说不要定期跑
10. **recover_state 只恢复软状态** — 模式 / readonly / paused / 当前 incident 引用 / error_text baseline / merge 时间戳;**不重放未完成动作**(roadmap §5.3.2:任何执行中的动作崩溃时被中断,重启后让人类决定)

## 留给 Sprint 6 的钩子
- `health_snapshot` 已有完整字段,Sprint 6 的 `/metrics` 端点直接复用
- `audit.py` 可以挂到 `_save_state` / `start_health_server` / `LLMDegraded` 入口,事件类型一目了然
- IM 通知最自然的接入点是 chat.escalate / chat.say(critical) — Sprint 6 的 Notifier 可以做成 chat 的旁路 hook
- Token 计数已在 limits 里,reporter 直接读 `limits.status()` 即可

## 已知约束
- 状态文件不在 git 里(默认不 commit),崩溃恢复依赖磁盘可读 — 与 watchdog 互补够用
- 单进程消费者:多副本部署需要重新设计 PendingEventQueue
- LLM 降级期间 sleep 5min 是阻塞式 — 可被 KeyboardInterrupt 打断,但不会响应 chat 命令(Sprint 5 接受这个限制)
