# OpsAgent — 数字运维员工

一个实时在岗、会成长、在人类监督下工作的 AI 运维 Agent。

它不是监控系统，不是日志管道，是一个**会用 Shell、会记笔记、会跟你商量的数字同事**。

**当前版本: Sprint 1**:支持多目标(SSH / Docker / K8s)、爆炸半径限制、紧急停止开关。
查看 [examples/docker-compose-demo](examples/docker-compose-demo/) 了解快速演示。

## 工作原理

```
┌──────────────┐         SSH / kubectl / API         ┌──────────────────┐
│   OpsAgent   │ ──────────────────────────────────► │   目标系统        │
│   (运维工作站) │ ◄────────────────────────────────── │   (你的服务器)    │
└──────┬───────┘                                     └──────────────────┘
       │
       ├── 观察（tail/grep/dmesg/systemctl...）
       ├── 判断（LLM: 正常还是异常？）
       ├── 诊断（LLM: 根因是什么？）
       ├── 修复（重启/改配置/提 PR）
       ├── 验证（修好了吗？）
       └── 复盘（写笔记、更新 Playbook）
```

## 快速开始

### 1. 安装

```bash
git clone <repo-url> && cd ops-agent
pip install -r requirements.txt
```

### 2. 配置 LLM

```bash
# Anthropic（默认）
export OPS_LLM_API_KEY="sk-ant-..."

# 或 OpenAI
export OPS_LLM_PROVIDER=openai
export OPS_LLM_API_KEY="sk-..."
export OPS_LLM_MODEL=gpt-4o

# 或本地模型（兼容 OpenAI API 的）
export OPS_LLM_PROVIDER=openai
export OPS_LLM_BASE_URL=http://localhost:11434/v1
export OPS_LLM_MODEL=llama3
export OPS_LLM_API_KEY=dummy
```

### 3. 启动

```bash
# 监控本机
python main.py

# 监控远程服务器
python main.py --target user@192.168.1.100

# 只看不动（只读模式）
python main.py --target user@192.168.1.100 --readonly

# 调试模式
python main.py --debug
```

### 4. 和 Agent 对话

Agent 启动后，直接在终端输入消息：

```
> status                     # 查看 Agent 状态
> 最近 nginx 有没有报错？     # 问问题，Agent 会去查
> readonly on                # 切换只读模式
> readonly off               # 恢复正常模式
> stop                       # 停止当前调查
> quit                       # 退出
```

### 5. Docker 部署

```bash
docker build -t ops-agent .

# 监控本机
docker run -it \
  -e OPS_LLM_API_KEY=sk-ant-... \
  -v $(pwd)/notebook:/data/notebook \
  ops-agent

# 监控远程（需要 SSH 密钥）
docker run -it \
  -e OPS_LLM_API_KEY=sk-ant-... \
  -v $(pwd)/notebook:/data/notebook \
  -v ~/.ssh:/root/.ssh:ro \
  ops-agent --target user@192.168.1.100
```

## 项目结构

```
ops-agent/
├── main.py              # Agent 主循环（大脑）
├── llm.py               # LLM 调用抽象层
├── notebook.py          # Notebook 读写（记忆）
├── tools.py             # 命令执行工具箱（双手）
├── trust.py             # 信任度引擎（授权判断）
├── chat.py              # 人机交互通道（嘴巴和耳朵）
├── prompts/             # 6 个核心 Prompt 模板
│   ├── observe.md       # "现在该看什么"
│   ├── assess.md        # "这些输出正常吗"
│   ├── diagnose.md      # "根因是什么"
│   ├── plan.md          # "怎么修"
│   ├── verify.md        # "修好了吗"
│   └── reflect.md       # "这次学到了什么"
├── notebook/            # Agent 的笔记本（git 仓库）
│   ├── config/
│   │   ├── permissions.md
│   │   ├── watchlist.md
│   │   └── contacts.md
│   └── playbook/
│       ├── nginx-502.md
│       ├── oom-killer.md
│       └── disk-full.md
├── requirements.txt
├── Dockerfile
└── README.md
```

## 核心概念

### Notebook（笔记本）
Agent 的记忆，就是一个 git 仓库，里面全是 markdown。你可以直接打开编辑，Agent 下次循环就会读到。

### Playbook（操作手册）
`notebook/playbook/` 目录下的 markdown 文件，描述"遇到 X 问题怎么办"。新增修复能力 = 往这个目录里扔一个 markdown。

### Incident（事件）
Agent 发现并处理的每一次异常，全过程记录在 `notebook/incidents/` 下。

### Trust Level（信任等级）
- **L0 只读**：观察命令，直接执行
- **L1 写笔记**：修改 Notebook，直接执行
- **L2 服务操作**：重启/改配置，按 permissions.md 规则
- **L3 代码修改**：提 PR，需要人类批准
- **L4 破坏性**：永远禁止

## 扩展

### 添加新 Playbook

往 `notebook/playbook/` 里创建一个 markdown 文件即可，格式自由，建议包含：
- 什么时候用我（触发条件）
- 先查什么（诊断命令）
- 怎么修（修复步骤）
- 验证标准（怎么确认修好了）

### 修改授权规则

编辑 `notebook/config/permissions.md`，用自然语言描述规则。

### 对接 Slack/钉钉

继承 `chat.py` 中的 `HumanChannel` 类，实现对应的消息收发即可。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPS_LLM_PROVIDER` | `anthropic` | LLM 提供商（anthropic / openai） |
| `OPS_LLM_MODEL` | `claude-sonnet-4-20250514` | 模型名称 |
| `OPS_LLM_API_KEY` | （无） | API Key |
| `OPS_LLM_BASE_URL` | （无） | 自定义 API 地址（用于本地模型） |

## License

MIT
