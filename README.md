# OpsAgent — 数字运维员工

> 一个实时在岗、会成长、在人类监督下工作的 AI 运维 Agent。
>
> 它不是监控系统,不是日志管道,是一个**会用 Shell、会记笔记、会跟你商量、会自己修代码的数字同事**。

**当前版本: v2.0** — 6 个 Sprint + 状态机重构完成,493 项测试覆盖.

完整能力:多目标管理 · 自主诊断修复 · 源码 bug 自动修复 · PR 自动合并与生产观察 · 崩溃自恢复 · LLM 降级 · 审计/Metrics/IM 通知/日报.

详见 [USER_GUIDE.md](./USER_GUIDE.md) 获取完整使用说明.

---

## 工作原理

```
┌──────────────┐         SSH / docker / kubectl         ┌──────────────────┐
│   OpsAgent   │ ──────────────────────────────────────►│   目标系统        │
│  (运维工作站) │ ◄──────────────────────────────────────│  (你的服务器)     │
└──────┬───────┘                                        └──────────────────┘
       │
       ├── 观察    (tail / grep / dmesg / systemctl ...)
       ├── 判断    (LLM: 正常还是异常?)
       ├── 诊断    (LLM: 根因是什么?  ← 自动定位异常源码)
       ├── 修复    (重启 / 改配置 / 生成补丁 → 本地编译测试 → push → PR → 自动合并)
       ├── 验证    (生产观察 5 分钟 → 异常复发 → 自动 revert)
       └── 复盘    (写笔记、更新 Playbook、IM 通知、日报)
```

整个循环是**自主**的,但所有 L2+ 动作都受爆炸半径限制约束,任何不确定都会主动升级人类.

---

## 5 大能力一览

```
核心能力
├── 多目标支持           (SSH / Docker / K8s / Local)
├── 实时对话式交互       (CLI, 任何时候可中断)
├── 自主异常检测与修复   (observe → diagnose → act → verify → reflect)
├── 源码 Bug 修复       (定位 → 生成补丁 → 本地验证 → PR → 生产观察 → 复发自动 revert)
└── 完整爆炸半径限制     (频率 / 并发 / 冷却 / token / auto-merge)

可靠性
├── 崩溃自恢复          (state.json + recover_state)
├── LLM 降级            (重试 + 自动 readonly + 自动恢复)
├── Notebook 完整性     (git fsck + 远端备份)
└── Watchdog + systemd  (ops-agent.service + scripts/watchdog.sh)

可观测性
├── 审计日志            (append-only JSONL,日滚动)
├── Prometheus metrics  (/metrics 端点)
├── IM 通知             (Slack / 钉钉 / 飞书 + 通知策略)
└── 每日健康报告        (LLM 总结 + 模板回退)
```

---

## 快速开始

### 1. 安装

```bash
git clone <repo-url> && cd ops-agent
pip install -r requirements.txt
```

依赖:`anthropic` `openai` `prompt_toolkit` `pyyaml` 四个,其余全部 stdlib.

**安装后可直接使用 `ops-agent` 命令:**

```bash
# 方式一: pip 可编辑安装(推荐，装完就有 ops-agent 命令)
pip install -e .

# 方式二: 无需安装，用脚本
./scripts/ops-agent --help
```

### 2. 一键配置（推荐）

```bash
ops-agent init
```

交互式引导,自动生成所有配置文件:

```
🚀 Welcome to ops-agent setup!

? LLM Provider (anthropic): anthropic
? API Key: sk-ant-****
? Target name (my-ssh): web-prod
? Target type (ssh): ssh
? SSH address (user@host): ubuntu@10.0.0.10
? SSH key path (optional): ~/.ssh/id_rsa
? Configure a source repo? [y/N]: n
? Notification type (none): none
✅ notebook/config/targets.yaml
✅ notebook/config/limits.yaml
✅ notebook/config/permissions.md
🎉 Setup complete!
```

**Docker 用户**用环境变量模式:

```bash
docker run -it --rm \
  -e OPS_LLM_PROVIDER=anthropic \
  -e OPS_LLM_API_KEY=sk-ant-... \
  -e OPS_TARGET_TYPE=ssh \
  -e OPS_TARGET_NAME=web-prod \
  -e OPS_TARGET_HOST=ubuntu@10.0.0.10 \
  -e OPS_TARGET_KEY_FILE=/root/.ssh/id_rsa \
  -v $(pwd)/notebook:/app/notebook \
  slobcoder/ops-agent init --from-env
```

### 3. 手动配置（可选）

如果不想用 `init`,也可以手动设置:

```bash
# LLM — 通过环境变量
export OPS_LLM_API_KEY="sk-ant-..."

# 目标 — 命令行参数
ops-agent --target user@192.168.1.100

# 或手写 notebook/config/targets.yaml（参考 targets.yaml.example）
```

### 4. 启动

```bash
# 监控本机（无需配置）
ops-agent

# 多目标（用 init 生成的配置）
ops-agent --notebook ./notebook

# 只读模式（只观察不动手）
ops-agent --readonly

# Docker
docker build -t ops-agent .
docker run -it -e OPS_LLM_API_KEY=sk-ant-... \
  -v $(pwd)/notebook:/data/notebook ops-agent
```

### 5. 和 Agent 对话

```
> status                       # 查看 Agent 状态
> 最近 nginx 有没有报错?         # 自然语言提问
> readonly on / readonly off    # 切换只读模式
> stop                          # 停止当前调查
> pause / resume                # 暂停/恢复巡检
> quit                          # 退出
```

完整命令列表见 [USER_GUIDE.md](./USER_GUIDE.md).

### 6. 健康检查

```bash
# Agent 启动后默认在 127.0.0.1:9876 暴露 HTTP 端点
curl localhost:9876/healthz   # JSON 状态快照
curl localhost:9876/metrics   # Prometheus 格式 metrics
```

---

## 项目结构

```
ops-agent/
├── main.py                       # 入口,解析参数,启动 OpsAgent
├── src/
│   ├── init.py                    # ops-agent init 交互式配置引导
│   ├── core.py                   # OpsAgent 类,主循环 + 状态机
│   ├── context_limits.py         # 上下文窗口限制配置
│   ├── reporter.py               # 每日健康报告
│   │
│   ├── agent/                    # 思考层 — Mixin
│   │   ├── pipeline.py           # OODA 流水线(observe/assess/diagnose/plan/execute/verify/reflect)
│   │   ├── parsers.py            # JSON 解析、命令提取、targeted observe
│   │   ├── prompt_engine.py      # Prompt 模板加载/填充
│   │   ├── human.py              # 人类消息处理、自由对话、协作模式
│   │   ├── metrics.py            # Prometheus metrics mixin
│   │   └── pr_workflow.py        # PR 创建/合并/观察 mixin
│   │
│   ├── infra/                    # 感知层 + 行动层
│   │   ├── tools.py              # 命令执行(SSH/Docker/K8s/Local)
│   │   ├── targets.py            # 多目标 + SourceRepo 配置
│   │   ├── chat.py               # 终端交互(prompt_toolkit)
│   │   ├── llm.py                # LLM 抽象层(含 RetryingLLM 降级)
│   │   ├── notebook.py           # Notebook 读写 + git 完整性
│   │   ├── deploy_watcher.py     # 部署信号监听
│   │   ├── production_watcher.py # 复发检测
│   │   ├── notifier.py           # IM 通知(Slack/钉钉/飞书)
│   │   └── git_host.py           # GitHub/GitLab CLI 抽象
│   │
│   ├── safety/                   # 安全与约束
│   │   ├── trust.py              # 信任度引擎 + ActionPlan
│   │   ├── safety.py             # 紧急停止开关 + 命令黑名单
│   │   ├── limits.py             # 爆炸半径限制
│   │   ├── patch_generator.py    # LLM 补丁生成
│   │   ├── patch_applier.py      # git 应用 + 编译 + 测试
│   │   ├── patch_loop.py         # 重试循环(最多 3 次)
│   │   └── revert_generator.py   # 自动 revert
│   │
│   ├── repair/                   # 自修复与源码定位
│   │   ├── self_repair.py        # 自修复系统
│   │   ├── self_context.py       # 自修复上下文收集
│   │   ├── source_locator.py     # 异常 → 源码反向定位
│   │   └── stack_parser.py       # 多语言 traceback 解析
│   │
│   └── reliability/              # 可靠性基础
│       ├── state.py              # 崩溃恢复状态持久化
│       ├── pending_events.py     # 待处理事件队列
│       ├── health.py             # 健康检查端点 + /metrics
│       └── audit.py              # append-only 审计日志
│
├── prompts/                      # 7 个核心 prompt 模板
├── templates/pr-body.md          # PR 描述模板
├── notebook/                     # Agent 的笔记本(git 仓库)
│   ├── config/
│   │   ├── targets.yaml
│   │   ├── permissions.md
│   │   ├── limits.yaml
│   │   └── notifier.yaml.example
│   ├── playbook/
│   ├── incidents/
│   ├── lessons/
│   └── audit/
├── tests/                        # 10 个测试文件
├── ops-agent.service             # systemd unit
├── scripts/
│   ├── watchdog.sh               # 外部健康看门狗
│   └── install.sh                # 一键安装
└── README.md / USER_GUIDE.md
```

---

## 核心概念

| 概念 | 说明 |
|---|---|
| **Notebook** | Agent 的记忆 = 一个 git 仓库,里面全是 markdown.你可以直接打开编辑,Agent 下次循环就会读到. |
| **Playbook** | `notebook/playbook/*.md`,描述"遇到 X 问题怎么办".新增修复能力 = 往这个目录扔一个 markdown. |
| **Incident** | Agent 发现并处理的每一次异常,全过程记录在 `notebook/incidents/`. |
| **Target** | 一个被管理的目标系统(SSH/Docker/K8s/Local).配置在 `notebook/config/targets.yaml`. |
| **SourceRepo** | 一个目标对应的本地源码 clone,用于异常反向定位和补丁生成. |
| **Trust Level** | L0 只读 / L1 写笔记 / L2 服务操作 / L3 代码改动 / L4 永远禁止 |
| **爆炸半径限制** | 频率 / 并发 / 冷却 / token / 自动合并 PR 次数,任何超限都强制升级人类. |
| **紧急停止** | 文件 / 信号 / CLI 三种方式触发,Agent 立即切换只读. |

---

## 测试

```bash
# 运行所有测试(无需配置 LLM,全部 stdlib stub)
cd tests
for t in test_basic test_blacklist test_sprint1 test_sprint2 \
         test_sprint3 test_sprint4 test_sprint5 test_sprint6; do
    python $t.py
done
```

测试统计:**493 项,100% 通过**.

| Sprint | 范围 | 测试数 |
|---|---|---|
| 0 (基线) | basic + 黑名单 | 85 |
| 1 | 多目标 / 爆炸半径 / 紧急停止 | 53 |
| 2 | stack 解析 / 源码定位 | 51 |
| 3 | 补丁生成 / 应用 / 验证 | 56 |
| 4 | git host / 部署 / 复发 / revert | 74 |
| 5 | 状态持久化 / 队列 / 健康 / RetryingLLM | 79 |
| 6 | 审计 / 通知 / 日报 / metrics | 95 |
| **合计** | | **493** |

---

## 环境变量

### LLM

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPS_LLM_PROVIDER` | `anthropic` | LLM 提供商(anthropic / openai / zhipu) |
| `OPS_LLM_MODEL` | `claude-sonnet-4-20250514` | 模型名称 |
| `OPS_LLM_API_KEY` | (无) | API Key |
| `OPS_LLM_BASE_URL` | (无) | 自定义 API 地址 |

### ops-agent init（--from-env 模式）

| 变量 | 必填 | 说明 |
|---|---|---|
| `OPS_TARGET_TYPE` | ✓ | 目标类型(ssh / docker / k8s / local) |
| `OPS_TARGET_NAME` | | 目标名称(默认 `my-{type}`) |
| `OPS_TARGET_HOST` | ssh 必填 | SSH 地址(user@host) |
| `OPS_TARGET_PORT` | | SSH 端口(默认 22) |
| `OPS_TARGET_KEY_FILE` | | SSH 密钥路径 |
| `OPS_TARGET_PASSWORD_ENV` | | SSH 密码环境变量名 |
| `OPS_TARGET_CRITICALITY` | | 严重度(low/normal/high/critical) |
| `OPS_TARGET_DESCRIPTION` | | 目标描述 |
| `OPS_REPO_NAME` | | 仓库名(有此变量则启用源码配置) |
| `OPS_REPO_PATH` | 启用仓库必填 | 本地 clone 路径 |
| `OPS_REPO_URL` | | Git 远端 URL |
| `OPS_REPO_LANGUAGE` | | 编程语言 |
| `OPS_REPO_BUILD_CMD` | | 编译命令 |
| `OPS_REPO_TEST_CMD` | | 测试命令 |
| `OPS_REPO_DEPLOY_CMD` | | 部署命令 |
| `OPS_REPO_GIT_HOST` | | Git 托管(github/gitlab) |
| `OPS_NOTIFIER_TYPE` | | 通知类型(none/slack/dingtalk/feishu/feishu_app) |
| `OPS_NOTIFIER_WEBHOOK_URL` | | 通知 Webhook URL |

### 其他

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPS_NOTIFIER_WEBHOOK_URL` | (无) | 覆盖 notifier.yaml 中的 webhook,推荐用于生产 |

---

## 文档导航

- **[USER_GUIDE.md](./USER_GUIDE.md)** — 完整使用指南,涵盖配置、操作、故障排查、运维实践
- **[notebook/lessons/](./notebook/lessons/)** — Sprint 回顾,记录设计决策与权衡
- **[examples/docker-compose-demo/](./examples/docker-compose-demo/)** — 端到端演示环境

## License

MIT
