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

有两种使用方式，选一个适合你的：

---

### 方式一：本地安装（推荐）

适合：长期运行、监控真实服务器、生产部署

**Step 1 — 一键安装**

```bash
curl -fsSL https://raw.githubusercontent.com/slob-coder/ops-agent/main/scripts/install-quick.sh | bash
```

脚本自动完成：检查 Python ≥ 3.9 → 克隆到 `~/.ops-agent` → 创建独立 venv → 安装依赖 → 配置 `ops-agent` 命令。

> 安装后如果 `ops-agent` 命令未识别，开一个新终端，或执行 `export PATH="$HOME/.ops-agent/bin:$PATH"`。
>
> 自定义安装目录：`OPS_AGENT_HOME=/opt/ops-agent curl -fsSL ... | bash`

**Step 2 — 初始化配置**

```bash
ops-agent init
```

交互式引导，自动生成所有配置文件和 `.env`：

```
? LLM Provider (anthropic): anthropic
? API Key: sk-ant-****
? Target name: web-prod
? Target type (ssh): ssh
? SSH address (user@host): ubuntu@10.0.0.10
? SSH key path (optional, Enter to skip):
? Configure a source repo? [y/N]: n
? Notification type (none): none
✅ notebook/config/targets.yaml
✅ notebook/config/limits.yaml
✅ notebook/config/permissions.md
✅ notebook/.env
🎉 Setup complete!
```

LLM API Key 等凭据自动写入 `.env`，无需手动 export。

**Step 3 — 启动**

```bash
ops-agent                  # 用 init 生成的配置启动
ops-agent --readonly       # 只读模式（只观察不动手）
ops-agent check            # 校验配置是否完整
ops-agent check --test-llm # 校验 + 测试 LLM 连通性
```

**Step 4 — 和 Agent 对话**

```
> status                       # 查看 Agent 状态
> 最近 nginx 有没有报错?         # 自然语言提问
> readonly on / readonly off    # 切换只读模式
> stop                          # 停止当前调查
> pause / resume                # 暂停/恢复巡检
> quit                          # 退出
```

---

### 方式二：Docker

适合：不想装 Python、CI/CD 环境、快速体验

**快速体验（Demo 模式）**

只需一个 API key，零配置：

```bash
git clone https://github.com/slob-coder/ops-agent.git && cd ops-agent/docker
cp .env.example .env
# 编辑 .env，只需填一行: OPS_LLM_API_KEY=sk-ant-...
docker compose run --rm ops-agent demo
```

Demo 模式自动生成 mock 配置，监控容器自身。进入后可以自然语言提问、查看状态。

> Demo 监控的是容器内部，主要是巡检演示。要监控真实服务器，看下面「正式部署」。

**Docker 正式部署**

Step 1 — 克隆并配置：

```bash
git clone https://github.com/slob-coder/ops-agent.git && cd ops-agent/docker
cp .env.example .env
```

编辑 `.env`，填写你的配置。最少只需两行：

```env
OPS_LLM_API_KEY=sk-ant-...        # 必填：LLM API Key
OPS_TARGET_TYPE=local             # local=监控容器自身 | ssh=远程服务器
```

如果要监控 SSH 服务器：

```env
OPS_LLM_API_KEY=sk-ant-...
OPS_TARGET_TYPE=ssh
OPS_TARGET_HOST=ubuntu@10.0.0.10  # SSH 地址
OPS_TARGET_KEY_FILE=/root/.ssh/id_rsa  # 密钥（已自动挂载 ~/.ssh）
```

> `.env.example` 中有每个参数的详细说明和示例。

Step 2 — 初始化并校验：

```bash
docker compose run --rm ops-agent init --from-env   # 生成配置文件
docker compose run --rm ops-agent check --test-llm   # 校验 + 测试连通性
```

Step 3 — 启动：

```bash
docker compose up -d               # 后台启动
docker compose logs -f             # 查看日志
curl localhost:9876/healthz        # 健康检查
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
│   └── targets.example.yaml  # 目标配置模板（带注释）
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
├── docker/                        # Docker 部署
│   ├── compose.yaml               # Docker Compose 配置
│   └── .env.example               # 环境变量模板（带注释）
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

## Notebook 扩展性

ops-agent 的 Notebook 是可插拔的。内置 Basic Notebook（文件系统 + git），也支持安装第三方扩展 Notebook 来增强能力（知识图谱、智能感知、成长引擎等）。启动时自动检测扩展包，零配置。

**→ 完整文档：[docs/notebook-extension.md](./docs/notebook-extension.md)**（接口协议、自定义扩展开发、Docker/本地安装步骤、私有仓库、已有数据处理、验证方法）

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
| `OPS_NOTIFIER_WEBHOOK_URL` | (无) | 覆盖 notifier.yaml 中的 webhook,推荐用于生产 |
| `OPS_FEISHU_APP_ID` | | 飞书应用 App ID（feishu_app 模式） |
| `OPS_FEISHU_APP_SECRET` | | 飞书应用 App Secret（feishu_app 模式） |
| `OPS_FEISHU_CHAT_ID` | | 飞书群聊 chat_id（feishu_app 模式） |

### 飞书通知配置

OpsAgent 支持两种飞书通知方式：

**方式一：Webhook 机器人（简单，推荐入门）**

1. 在飞书群聊中添加「自定义机器人」，获取 Webhook URL
2. 运行 `ops-agent init`，通知类型选 `feishu`，填入 Webhook URL
3. 或手动创建 `notebook/config/notifier.yaml`：

```yaml
type: feishu
webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
```

> Webhook 机器人只能向群聊发消息，无法接收回复。

**方式二：自建应用机器人（功能完整，支持双向交互）**

1. 在[飞书开放平台](https://open.feishu.cn/app)创建企业自建应用
2. 添加「机器人」能力，获取 App ID 和 App Secret
3. 在应用的「权限管理」中开通：`im:message:send_as_bot`
4. 创建群聊，将机器人加入群，获取群聊的 `chat_id`（群设置 → 群名片 → 复制群链接，`chat_id` 在 URL 中）
5. 配置 `notebook/config/notifier.yaml`：

```yaml
type: feishu_app
feishu_app:
  app_id: "cli_xxxxxxxx"
  app_secret: "xxxxxxxxxxxxxxxx"
  chat_id: "oc_xxxxxxxx"
```

6. **安全建议**：`app_secret` 不要提交到 git，用环境变量覆盖：

```bash
export OPS_FEISHU_APP_ID="cli_xxxxxxxx"
export OPS_FEISHU_APP_SECRET="xxxxxxxxxxxxxxxx"
export OPS_FEISHU_CHAT_ID="oc_xxxxxxxx"
```

**启用飞书双向交互**（可选）：

Agent 可以接收飞书群聊中 @它的消息并回复：

```yaml
feishu_app:
  app_id: "cli_xxxxxxxx"
  app_secret: "xxxxxxxxxxxxxxxx"
  chat_id: "oc_xxxxxxxx"
  interactive:
    enabled: true
    callback_port: 9877      # 飞书事件回调端口（需公网可达）
    encrypt_key: ""          # 飞书开放平台 → 事件订阅 → 加密 key
    verification_token: ""   # 飞书开放平台 → 事件订阅 → 验证 token
```

在飞书开放平台配置事件订阅：
- 请求地址：`http://<你的服务器IP>:9877/feishu/event`
- 订阅事件：`im.message.receive_v1`（接收消息）

> 双向交互需要服务器有公网 IP，飞书回调才能到达。

---

## 文档导航

- **[docs/notebook-extension.md](./docs/notebook-extension.md)** — Notebook 扩展性：接口协议、自定义扩展开发、安装步骤
- **[USER_GUIDE.md](./USER_GUIDE.md)** — 完整使用指南,涵盖配置、操作、故障排查、运维实践
- **[notebook/lessons/](./notebook/lessons/)** — Sprint 回顾,记录设计决策与权衡
- **[examples/docker-compose-demo/](./examples/docker-compose-demo/)** — 端到端演示环境

## License

MIT
