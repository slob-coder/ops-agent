# OpsAgent 使用指南

> 版本: v2.0  ·  适用范围: 6 个 Sprint + 状态机重构完成后的 OpsAgent
>
> 本指南覆盖从首次部署到生产运维的全流程.如果你只想快速跑起来,看 [README.md](./README.md) 的"快速开始"即可.

---

## 目录

1. [安装与依赖](#1-安装与依赖)
2. [配置](#2-配置)
   - 2.1 [LLM 配置](#21-llm-配置)
   - 2.2 [目标配置 targets.yaml](#22-目标配置-targetsyaml)
   - 2.3 [源码仓库 SourceRepo](#23-源码仓库-sourcerepo)
   - 2.4 [授权规则 permissions.md](#24-授权规则-permissionsmd)
   - 2.5 [爆炸半径 limits.yaml](#25-爆炸半径-limitsyaml)
   - 2.6 [IM 通知 notifier.yaml](#26-im-通知-notifieryaml)
3. [启动与部署](#3-启动与部署)
   - 3.1 [本地 / Docker / systemd](#31-本地--docker--systemd)
   - 3.2 [崩溃恢复](#32-崩溃恢复)
   - 3.3 [Watchdog 集成](#33-watchdog-集成)
4. [与 Agent 对话](#4-与-agent-对话)
5. [自主修复闭环](#5-自主修复闭环)
   - 5.1 [代码 bug 自动修复全流程](#51-代码-bug-自动修复全流程)
   - 5.2 [部署信号配置](#52-部署信号配置)
   - 5.3 [生产观察期与自动 revert](#53-生产观察期与自动-revert)
6. [可观测性](#6-可观测性)
   - 6.1 [健康检查与 Metrics](#61-健康检查与-metrics)
   - 6.2 [审计日志](#62-审计日志)
   - 6.3 [IM 通知与日报](#63-im-通知与日报)
7. [安全与紧急停止](#7-安全与紧急停止)
8. [扩展](#8-扩展)
   - 8.1 [新增 Playbook](#81-新增-playbook)
   - 8.2 [新增 Git Host / Notifier 通道](#82-新增-git-host--notifier-通道)
9. [故障排查](#9-故障排查)
10. [运维实践](#10-运维实践)

---

## 1. 安装与依赖

**系统要求:**
- Python ≥ 3.10
- git ≥ 2.20(本地仓库 + 补丁应用)
- 可选:`gh` CLI(GitHub PR 工作流)/ `glab` CLI(GitLab)
- 可选:`docker` / `kubectl`(对应目标类型)

**Python 依赖**(`requirements.txt`):

```
anthropic>=0.40.0
openai>=1.50.0
prompt_toolkit>=3.0.0
pyyaml>=6.0
```

只有这 4 个第三方包,其余 Sprint 2-6 全部用 stdlib(stack 解析、git 操作、HTTP 健康检查、IM 通知、Prometheus metrics 都是手写零依赖).

```bash
git clone <repo-url> && cd ops-agent
pip install -r requirements.txt
```

---

## 2. 配置

OpsAgent 的所有配置都集中在 `notebook/config/` 目录下.除了 LLM 凭据走环境变量,其他都是 yaml/markdown,git 可追踪.

### 2.0 一键配置（ops-agent init）

如果你是第一次使用,推荐用交互式引导自动生成所有配置:

```bash
python main.py init
```

引导流程:

```
🚀 Welcome to ops-agent setup!

━━━ LLM Configuration ━━━
? LLM Provider (anthropic): anthropic
? API Key: ****
? API Base URL (enter for default): 
? Model (claude-sonnet-4-20250514): 
? Test LLM connection now? [Y/n]: y
  Testing LLM connection...
  ✅ LLM connection OK

━━━ Target Configuration ━━━
? Target type (ssh): ssh
? Target name (my-ssh): web-prod
? Criticality (normal): high
? Description (optional): 生产 web 服务器
? SSH address (user@host): ubuntu@10.0.0.10
? SSH port (22): 
? SSH key path (optional): ~/.ssh/id_rsa
? Test SSH connection now? [Y/n]: y
  Testing SSH connection...
  ✅ SSH connection OK
? Configure a source repo for this target? [y/N]: y
? Repo name (app): backend
? Local clone path: /opt/sources/backend
? Git remote URL (optional): git@github.com:org/backend.git
? Language (python/java/go/node/rust/...): python
? Build command (optional): make build
? Test command (optional): pytest -x

━━━ Notification (optional) ━━━
? Notification type (none/slack/dingtalk/feishu/feishu_app): none

✅ notebook/config/targets.yaml
✅ notebook/config/limits.yaml
✅ notebook/config/permissions.md

━━━ Next Steps ━━━
  1. Review:  cat notebook/config/targets.yaml
  2. Start:   python main.py --notebook ./notebook

🎉 Setup complete!
```

**`init` 生成的文件:**

| 文件 | 说明 |
|---|---|
| `targets.yaml` | 根据你的输入生成,可之后手动编辑 |
| `limits.yaml` | 安全默认值,已有文件不会被覆盖 |
| `permissions.md` | 标准授权规则,已有文件不会被覆盖 |
| `notifier.yaml` | 仅在选择通知类型时生成 |

**Docker / CI 环境**用 `--from-env` 模式,从环境变量读取配置:

```bash
python main.py init --from-env
```

缺少必填环境变量时会报错退出.完整环境变量列表:

| 变量 | 必填 | 说明 |
|---|---|---|
| `OPS_LLM_PROVIDER` | | LLM 提供商(默认 anthropic) |
| `OPS_LLM_API_KEY` | ✓ | API Key |
| `OPS_LLM_BASE_URL` | | API 地址(默认随 provider) |
| `OPS_LLM_MODEL` | | 模型名(默认随 provider) |
| `OPS_TARGET_TYPE` | | 目标类型(默认 ssh) |
| `OPS_TARGET_NAME` | | 目标名称(默认 my-{type}) |
| `OPS_TARGET_HOST` | ssh 时必填 | SSH 地址(user@host) |
| `OPS_TARGET_PORT` | | SSH 端口(默认 22) |
| `OPS_TARGET_KEY_FILE` | | SSH 密钥路径 |
| `OPS_TARGET_PASSWORD_ENV` | | SSH 密码环境变量名 |
| `OPS_TARGET_CRITICALITY` | | 严重度(默认 normal) |
| `OPS_TARGET_DESCRIPTION` | | 目标描述 |
| `OPS_TARGET_COMPOSE_FILE` | docker | docker-compose.yaml 路径 |
| `OPS_TARGET_KUBECONFIG` | k8s | kubeconfig 路径 |
| `OPS_TARGET_CONTEXT` | k8s | kubectl context |
| `OPS_TARGET_NAMESPACE` | k8s | namespace(默认 default) |
| `OPS_REPO_NAME` | | 仓库名(有此变量则启用源码配置) |
| `OPS_REPO_PATH` | 启用仓库必填 | 本地 clone 路径 |
| `OPS_REPO_URL` | | Git 远端 URL |
| `OPS_REPO_LANGUAGE` | | 编程语言 |
| `OPS_REPO_BUILD_CMD` | | 编译命令 |
| `OPS_REPO_TEST_CMD` | | 测试命令 |
| `OPS_REPO_DEPLOY_CMD` | | 部署命令 |
| `OPS_REPO_GIT_HOST` | | Git 托管(github/gitlab) |
| `OPS_NOTIFIER_TYPE` | | 通知类型(默认 none) |
| `OPS_NOTIFIER_WEBHOOK_URL` | | Webhook URL |

**docker-compose 示例:**

```yaml
# docker-compose.yml
services:
  ops-agent-init:
    image: slobcoder/ops-agent
    command: init --from-env
    env_file: .env
    volumes:
      - ./notebook:/app/notebook
      - ~/.ssh:/root/.ssh:ro

  ops-agent:
    image: slobcoder/ops-agent
    env_file: .env
    volumes:
      - ./notebook:/app/notebook
      - ~/.ssh:/root/.ssh:ro
    ports:
      - "9876:9876"
    depends_on:
      ops-agent-init:
        condition: service_completed_successfully
```

```bash
# .env
OPS_LLM_PROVIDER=anthropic
OPS_LLM_API_KEY=sk-ant-xxx
OPS_TARGET_TYPE=ssh
OPS_TARGET_NAME=web-prod
OPS_TARGET_HOST=ubuntu@10.0.0.10
OPS_TARGET_KEY_FILE=/root/.ssh/id_rsa
OPS_NOTIFIER_TYPE=slack
OPS_NOTIFIER_WEBHOOK_URL=https://hooks.slack.com/services/XXX
```

```bash
# 一键启动（init → run）
docker compose up -d
```

以下各节是手动配置的详细说明.如果你已经用 `init` 生成了配置,可以跳过,之后需要微调时再参考.

### 2.1 LLM 配置

通过环境变量配置.支持三种 provider:

```bash
# Anthropic Claude(默认,推荐)
export OPS_LLM_PROVIDER=anthropic
export OPS_LLM_API_KEY="sk-ant-..."
export OPS_LLM_MODEL=claude-sonnet-4-20250514     # 可选,有默认

# OpenAI
export OPS_LLM_PROVIDER=openai
export OPS_LLM_API_KEY="sk-..."
export OPS_LLM_MODEL=gpt-4o

# 智谱 GLM
export OPS_LLM_PROVIDER=zhipu
export OPS_LLM_API_KEY="..."
export OPS_LLM_MODEL=glm-4-plus

# 任何 OpenAI-API 兼容的本地模型(Ollama / vLLM / LM Studio)
export OPS_LLM_PROVIDER=openai
export OPS_LLM_BASE_URL=http://localhost:11434/v1
export OPS_LLM_MODEL=llama3
export OPS_LLM_API_KEY=dummy
```

**LLM 失败处理**:
所有 LLM 调用默认包了一层 `RetryingLLM`,在 API 抖动时自动重试 3 次(指数退避 1s/2s/4s).连续失败 → 抛 `LLMDegraded` → Agent 自动切到 readonly + escalate 人类 + 每 5 分钟重试一次.恢复后自动切回正常.

### 2.2 目标配置 targets.yaml

`notebook/config/targets.yaml` 定义 Agent 管理的所有目标系统.

```yaml
targets:
  # ── 本地 ──
  - name: local-dev
    type: local
    description: 开发机本地巡检
    criticality: low
    tags: [dev]

  # ── SSH 远程 ──
  - name: web-prod-01
    type: ssh
    host: ops@web01.example.com
    port: 22
    key_file: ~/.ssh/id_ed25519
    description: 生产环境 web 节点
    criticality: critical
    tags: [prod, web, frontend]

  # ── Docker 主机 ──
  - name: dockerhost-staging
    type: docker
    docker_host: ""               # 空 = 本地 unix socket
    compose_file: /opt/staging/docker-compose.yml
    description: 测试环境 docker compose
    criticality: normal
    tags: [staging]

  # ── Kubernetes 集群 ──
  - name: k8s-prod
    type: k8s
    kubeconfig: ~/.kube/config
    context: prod-cluster
    namespace: backend
    criticality: critical
    tags: [prod, k8s]
```

每个 target 有独立的 `ToolBox`(命令执行器)但共享同一个 `Notebook` 和 `LLM`.

**`criticality`** 字段在 prompt 中告知 LLM 当前目标的重要性,影响 Agent 的谨慎程度.

**`tags`** 字段供 playbook 检索时过滤(例如某 playbook 只对 `[prod]` 标签的目标生效).

### 2.3 源码仓库 SourceRepo

如果你要启用源码 bug 自动修复(Sprint 2-4 的能力),在每个 target 下添加 `source_repos`:

```yaml
targets:
  - name: web-prod-01
    type: ssh
    host: ops@web01.example.com
    source_repos:
      - name: backend
        path: /opt/sources/backend            # Agent 工作站上的本地 clone
        repo_url: git@github.com:org/backend.git
        branch: main
        language: python                       # python / java / go / node / rust / cpp
        build_cmd: "python -m py_compile $(git ls-files '*.py')"
        test_cmd: "pytest -x --timeout=30"

        # 运行时实体到本地 clone 的路径前缀映射
        # 容器里是 /app/handlers/user.py,本地是 /opt/sources/backend/handlers/user.py
        path_prefix_runtime: /app
        path_prefix_local: ""

        # ── Sprint 4: PR 工作流 ──
        git_host: github                       # github | gitlab | noop
        base_branch: main
        deploy_signal:
          type: http
          url: http://web01.example.com/version
          expect_contains: "{commit_sha}"
          check_interval: 10
          timeout: 1800

        # 用于复发检测的日志路径(可选)
        log_path: /var/log/backend/error.log
```

**字段说明:**

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✓ | 仓库别名,日志和 PR 标题里用到 |
| `path` | ✓ | 本地 clone 的绝对路径(Agent 必须能读写) |
| `language` | 推荐 | 用于 stack_parser 和 source_locator 的软过滤 |
| `build_cmd` | 启用补丁修复必填 | 编译/语法检查命令,5 分钟超时 |
| `test_cmd` |  | 单元测试命令,10 分钟超时.空 = 跳过测试 |
| `path_prefix_runtime` |  | 容器内路径前缀,例如 `/app` |
| `path_prefix_local` |  | 本地 clone 内的相对前缀(通常空) |
| `git_host` | 启用自动 PR 必填 | `github` / `gitlab` / `noop`(只本地验证不推送) |
| `base_branch` |  | PR 目标分支,默认 `main` |
| `deploy_signal` |  | 见 [§5.2](#52-部署信号配置) |

**渐进启用建议**:
- 阶段 1:先只配 `path` + `language`,启用 Sprint 2 的源码定位(Agent 在 incident 笔记里写出"出错代码在 user.py:42")
- 阶段 2:加 `build_cmd` + `test_cmd`,启用 Sprint 3 的本地补丁验证(Agent 生成补丁但不 push)
- 阶段 3:加 `git_host: github` + `deploy_signal`,启用 Sprint 4 的完整自动 PR/合并/观察/revert 流程

### 2.4 授权规则 permissions.md

`notebook/config/permissions.md` 用自然语言告诉 Agent 哪些 L2+ 操作可以自动执行,哪些必须先问人类.示例:

```markdown
# 授权规则

## 自动批准(L2 自动)
- 重启 nginx / php-fpm / redis(每天最多 3 次)
- 清理 /tmp 和 /var/log 下超过 7 天的文件
- 重启 docker 容器(配合 limits.yaml 的 max_restarts_per_service_per_day)

## 必须问人类(L2 但需确认)
- 修改 /etc/ 下的任何配置文件(改之前必须 cp 备份)
- 扩缩容 K8s deployment
- 任何涉及数据库(mysql/postgres)的操作

## 永远禁止(L4)
- rm -rf /
- DROP TABLE / TRUNCATE
- 关机/重启服务器
- 修改 /etc/passwd / /etc/shadow
```

Agent 在 plan 阶段会读取这个文件,LLM 自己判断当前 action 落在哪一档.黑名单(L4)由 `src/safety/safety.py` 硬编码兜底,无论 LLM 怎么说都拦截.

### 2.5 爆炸半径 limits.yaml

`notebook/config/limits.yaml` 是物理护栏,LLM 无法绕过:

```yaml
enabled: true

# 动作频率
max_actions_per_hour: 20
max_actions_per_day: 100

# 单服务限制
max_restarts_per_service_per_day: 5
max_restarts_per_service_per_hour: 3

# 并发上限
max_concurrent_incidents: 2

# 失败冷却(修复失败后多久内禁止再尝试)
cooldown_after_failure_seconds: 600

# LLM 成本
llm_tokens_per_day: 1000000
llm_tokens_per_hour: 200000

# Sprint 4: 自动合并 PR 上限
max_auto_merges_per_day: 5
```

任何超限都强制升级给人类,Agent 不能用任何理由突破.

### 2.6 IM 通知 notifier.yaml

`notebook/config/notifier.yaml`(可参考 `notifier.yaml.example`):

```yaml
type: slack                # slack | dingtalk | feishu | none
webhook_url: https://hooks.slack.com/services/XXX/YYY/ZZZ

notify_on:
  - incident_opened
  - incident_closed
  - pr_merged
  - revert_triggered
  - critical_failure
  - llm_degraded
  - daily_report

quiet_hours:
  start: "22:00"
  end: "08:00"
  except_urgency:
    - critical
```

**安全实践**:`webhook_url` 推荐通过环境变量覆盖,避免凭据进 git:

```bash
export OPS_NOTIFIER_WEBHOOK_URL="https://hooks.slack.com/services/..."
```

`quiet_hours` 支持跨日(`22:00 → 08:00`)和同日(`13:00 → 14:00`)两种.`critical` 级通知不受免打扰约束.

---

## 3. 启动与部署

### 3.1 本地 / Docker / systemd

**本地直接跑**(开发或调试):

```bash
# 单目标快速启动(命令行参数)
python main.py --target user@host --notebook ./notebook

# 多目标(从 targets.yaml 读)
python main.py --notebook ./notebook

# 只读模式(只观察不动手)
python main.py --readonly

# 调试模式
python main.py --debug
```

**Docker**:

```bash
docker build -t ops-agent .
docker run -it \
  -e OPS_LLM_API_KEY=sk-ant-... \
  -e OPS_NOTIFIER_WEBHOOK_URL=https://... \
  -v $(pwd)/notebook:/data/notebook \
  -v ~/.ssh:/root/.ssh:ro \
  -p 9876:9876 \
  ops-agent
```

**systemd**(生产推荐):

```bash
# 一键安装(查看 scripts/install.sh 后再执行)
sudo bash scripts/install.sh

# 手动安装
sudo cp ops-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ops-agent

# 查看日志
journalctl -u ops-agent -f

# 重启
sudo systemctl restart ops-agent
```

`ops-agent.service` 配置了 `Restart=always` + `StartLimitBurst=5`,5 分钟内崩溃 5 次后会停止重启等待人类介入.

### 3.2 崩溃恢复

OpsAgent 每次主循环结束都会把状态 checkpoint 到 `notebook/state.json`(原子写).包括:

- 当前模式(patrol / investigate / incident)
- 当前 target / incident
- readonly / paused 标志
- 最后一次错误文本(用于复发检测 baseline)
- 自动合并 PR 时间戳(用于限流复位)

**重启时自动恢复**.如果检测到上次有未完成的 incident,Agent 会:

1. 在终端输出"⚠️ 检测到上次未完成的工作,已恢复状态"
2. **不重放未完成的动作**(重启时被中断的动作可能已部分执行,重放可能造成损害)
3. 让人类决定是继续还是放弃

state 文件版本号不匹配 / 损坏 / 不存在 → 直接全新启动,绝不会因状态损坏而崩溃.

### 3.3 Watchdog 集成

Agent 启动后默认在 `127.0.0.1:9876` 暴露健康端点.外部 watchdog 可以做主动探活:

```bash
# 简单 cron 探活
* * * * * /opt/ops-agent/scripts/watchdog.sh

# scripts/watchdog.sh 内置逻辑:
#   连续 3 次健康检查失败 → systemctl restart ops-agent
```

也可以接 K8s liveness probe:

```yaml
livenessProbe:
  httpGet:
    path: /healthz
    port: 9876
  initialDelaySeconds: 30
  periodSeconds: 30
  failureThreshold: 3
```

---

## 4. 与 Agent 对话

Agent 启动后,任何时候都可以在终端输入消息.Agent 在做任何事(包括 LLM 流式生成、运行命令)的过程中都能被打断,秒级响应.

### 内置命令

| 命令 | 说明 |
|---|---|
| `status` | 查看 Agent 当前状态(模式、目标、incident、限制配额) |
| `pause` / `resume` | 暂停/恢复自主巡检(Agent 仍响应你的指令) |
| `stop` | 停止当前调查/incident,回到巡检 |
| `readonly on` / `readonly off` | 切换只读模式 |
| `target <name>` | 切换当前激活的目标(如果配了多个) |
| `quit` / `exit` | 退出 Agent |

### 自然语言对话

```
> 最近一小时 nginx 有没有 5xx 错误?
> 帮我看一下 db-prod 的磁盘使用率
> 这个 incident 你觉得是什么原因?
> 不要重启 redis,我先看一下
```

Agent 把这些当成"人类的指令",会立刻执行(读日志、跑命令、回答问题),而不是当成普通对话.

### 紧急停止

任何时候按 `Ctrl-C` 或输入 `stop`,Agent 会立刻:
1. 中断正在进行的 LLM 流式生成
2. 中断正在跑的 SSH/Docker 命令
3. 在 incident 笔记里写一行"被人类中断"
4. 回到 patrol 模式等下一步指令

---

## 5. 自主修复闭环

### 5.1 代码 bug 自动修复全流程

完整流程(Sprint 2-4 的能力):

```
异常发生
  │
  ├── observe → assess → "这是异常"
  │
  ├── diagnose:LLM 分析 + 自动定位异常源码                    [Sprint 2]
  │     ↳ stack_parser 解析 Python/Java/Go/Node traceback
  │     ↳ source_locator 反向定位到本地 clone
  │     ↳ 提取目标行 ±30 行上下文 + 函数定义
  │     ↳ 注入到 diagnose prompt 的 {source_locations}
  │     ↳ LLM 输出诊断结论,带 type=code_bug
  │
  ├── 满足触发条件(type=code_bug + 有 source_repos + 非 readonly + 有 build_cmd)
  │     ↓
  ├── PatchLoop(最多 3 次重试)                               [Sprint 3]
  │     │
  │     ├── PatchGenerator: LLM 生成 unified diff
  │     ├── PatchApplier:
  │     │     1. git stash(脏工作区)
  │     │     2. git checkout -b fix/agent/<incident-id>
  │     │     3. git apply
  │     │     4. git commit
  │     │     5. build_cmd(超时 5min)
  │     │     6. test_cmd(超时 10min,可选)
  │     │     7. 任一失败 → reset --hard + clean -fd + branch -D + stash pop
  │     │
  │     └── 失败 → 把 diff 和错误信息回灌给 LLM 重试
  │
  ├── 三次都失败 → 升级人类,本地工作区彻底干净
  │
  └── 成功 → VerifiedPatch
        ↓
        Sprint 4 PR 工作流(如果配了 git_host)
        │
        ├── 限流检查(max_auto_merges_per_day)
        ├── git push -u origin <branch>
        ├── gh pr create
        ├── 再次检查 PR CI 状态(失败 → 降级等人类)
        ├── gh pr merge --squash --delete-branch
        ├── DeployWatcher 等待部署信号
        ├── ProductionWatcher 观察 5 分钟
        │     ↳ 用 ParsedTrace.signature() 精确匹配复发
        │
        ├── 无复发 → 关闭 incident,IM 通知 ✅
        └── 复发 → RevertGenerator 自动 git revert
              + 创建 revert PR + 立即合并
              + 升级人类 ⚠️
```

**安全约束**(每一层都独立兜底):

| 层 | 约束 |
|---|---|
| PatchGenerator | 只接受标准 unified diff,带 `@@` 和 `+++` 行,否则视为生成失败 |
| PatchApplier | 拒绝只修改测试文件的补丁(`Patch.touches_only_tests()` cheat guard) |
| PatchApplier | 任何阶段失败 = `git reset --hard` + `git clean -fd` + `git branch -D` + `git stash pop`,工作区绝对干净 |
| PatchLoop | 最多 3 次重试,失败次数都计入 limits 配额 |
| Sprint 4 | merge 前再次查 CI,CI 红 → 降级等人类 |
| Sprint 4 | `max_auto_merges_per_day=5` 默认上限 |
| Sprint 4 | 复发检测严格匹配 signature(语言:类型:文件:行号),不做模糊匹配避免误报 |
| Sprint 4 | 自动 revert 也算一次自动合并,防止恶性循环 |

### 5.2 部署信号配置

`deploy_signal` 告诉 Agent "怎么知道我的补丁已经部署到生产了".支持 4 种类型:

```yaml
# 1. HTTP 探活:GET 一个 URL,返回内容包含 commit_sha
deploy_signal:
  type: http
  url: http://prod.example.com/version
  expect_contains: "{commit_sha}"   # 自动替换为实际 commit
  check_interval: 10                 # 每 10 秒探一次
  timeout: 1800                      # 最多等 30 分钟

# 2. 文件信号:某文件存在且包含 commit_sha
deploy_signal:
  type: file
  path: /var/run/deploy/version.txt
  expect_contains: "{commit_sha}"

# 3. 命令信号:跑一个命令,exit 0 = 已部署
deploy_signal:
  type: command
  cmd: "kubectl get deploy backend -o jsonpath='{.metadata.annotations.commit}' | grep {commit_sha}"

# 4. 固定等待:简单等 N 秒(无 CD 环境)
deploy_signal:
  type: fixed_wait
  seconds: 60
```

不配置 `deploy_signal` → 默认假设已部署,跳过等待直接进入观察期.

### 5.3 生产观察期与自动 revert

部署后,Agent 进入 **5 分钟观察期**(可在代码层调整 `duration` 参数).每 30 秒调用一次 `observe_fn`(默认从 `repo.log_path` tail 200 行),用 `ParsedTrace.signature()` 检查是否出现和原异常**相同 signature** 的复发.

**4 种结局:**

| 结局 | 含义 | 后续 |
|---|---|---|
| `OK` | 观察期满,无复发 | ✅ 关闭 incident,IM 通知成功 |
| `FAILED_RECURRENCE` | 检测到原异常复发 | ⚠️ 启动 RevertGenerator,自动 revert + 升级 |
| `OBSERVE_ERROR` | observe_fn 连续 3 次失败 | 升级人类("我合并了但看不到生产") |
| `NO_BASELINE` | 无法从原 incident 提取 signature | 升级人类("不能做复发检测,需人工确认") |

**Revert 失败也有兜底**:RevertGenerator 自身失败 → 立即升级人类("revert 也失败了,需要人立即介入").

---

## 6. 可观测性

### 6.1 健康检查与 Metrics

Agent 启动后默认在 `127.0.0.1:9876` 暴露:

```bash
# 健康检查 — JSON
curl localhost:9876/healthz
# {
#   "status": "ok",                  # ok | degraded | error
#   "mode": "patrol",
#   "uptime": 12345.6,
#   "current_target": "web-prod-01",
#   "current_incident": "",
#   "active_incidents": 0,
#   "paused": false,
#   "readonly": false,
#   "last_loop_time": 1712345678.9,
#   "llm_degraded": false,
#   "pending_events": 0
# }

# Prometheus metrics
curl localhost:9876/metrics
```

Metrics 示例输出:

```
# HELP ops_agent_uptime_seconds Agent uptime
# TYPE ops_agent_uptime_seconds gauge
ops_agent_uptime_seconds 12345

# HELP ops_agent_mode Current mode
# TYPE ops_agent_mode gauge
ops_agent_mode{mode="patrol"} 1

# HELP ops_agent_actions_total Actions executed
# TYPE ops_agent_actions_total counter
ops_agent_actions_total{target="web-prod",kind="restart"} 12
ops_agent_actions_total{target="web-prod",kind="patch"} 5

# HELP ops_agent_incidents_total Incidents by status
# TYPE ops_agent_incidents_total counter
ops_agent_incidents_total{target="web-prod",status="opened"} 17
ops_agent_incidents_total{target="web-prod",status="closed"} 16

# HELP ops_agent_llm_degraded LLM degraded state
# TYPE ops_agent_llm_degraded gauge
ops_agent_llm_degraded 0

# HELP ops_agent_pending_events Pending events
# TYPE ops_agent_pending_events gauge
ops_agent_pending_events 0
```

接 Prometheus + Grafana 可视化:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: ops-agent
    static_configs:
      - targets: ['ops-agent-host:9876']
```

### 6.2 审计日志

每个关键事件都写入 `notebook/audit/YYYY-MM-DD.jsonl`(JSONL 格式,append-only,日滚动).

事件类型包括:

| 类型 | 字段 |
|---|---|
| `incident_opened` | target, severity, summary |
| `incident_closed` | target, resolution |
| `action_executed` | target, kind(restart/patch/...), command |
| `action_denied` | target, reason(trust/limits/safety) |
| `patch_generated` / `patch_applied` | repo, branch, commit_sha, attempts |
| `pr_created` / `pr_merged` / `revert_triggered` | repo, pr_number, url |
| `llm_call` | tokens, duration |
| `llm_degraded` / `llm_recovered` | failure_count |
| `human_override` | command, message |
| `daily_report_sent` | date |

`audit/` 目录默认**不**进 notebook 的 git commit(append-only 不可篡改).

读取审计:

```bash
# 看今天的事件
cat notebook/audit/$(date +%F).jsonl | jq

# 统计某天事件类型
cat notebook/audit/2026-04-10.jsonl | jq -r .type | sort | uniq -c
```

也可以在代码里:

```python
from src.reliability.audit import AuditLog
log = AuditLog("notebook/audit")
events = log.read_day("2026-04-10")
counts = log.count_by_type()
```

### 6.3 IM 通知与日报

**IM 通知**走 `notifier.yaml` 配置 + `PolicyNotifier` 策略包装.事件触发流程:

```
代码里调用 self._emit_notify("incident_opened", title, body, "warning")
   ↓
PolicyNotifier 检查 notify_on 白名单
   ↓
PolicyNotifier 检查 quiet_hours(critical 例外)
   ↓
SlackNotifier / DingTalkNotifier / FeishuNotifier 发 webhook
```

发送失败不阻塞主循环(只记 logger.warning).

**日报**:`reporter.py` 在主循环里检查 `should_send_today()`,如果今天还没发过就调用 LLM 总结昨天的审计日志生成 markdown 日报,通过 IM 推送.LLM 失败 → 自动回退到模板化纯统计日报,**永不失败**.

防重靠 `marker_dir/sent-YYYY-MM-DD` 空文件标记,跨进程持久.

---

## 7. 安全与紧急停止

### 信任分级

| Level | 描述 | 谁批准 |
|---|---|---|
| **L0** | 只读观察(tail/grep/cat/ps/df...) | 自动 |
| **L1** | 写笔记(notebook 内的 markdown) | 自动 |
| **L2** | 服务操作(重启/改配置) | permissions.md 规则 |
| **L3** | 代码改动(git apply/PR) | 限流 + 自动验证 |
| **L4** | 破坏性操作(`rm -rf /` / `DROP TABLE` / `mkfs` / ...) | **永远禁止**,黑名单硬编码 |

### 紧急停止三种触发方式

**1. 文件触发**:

```bash
# 创建文件 → Agent 下次循环检测到 → 立即只读
touch /var/run/ops-agent.stop

# 删除文件恢复
rm /var/run/ops-agent.stop
```

**2. 信号触发**:

```bash
# SIGUSR1 → 紧急停止
kill -USR1 $(pgrep -f ops-agent)
```

**3. CLI 触发**:

```
> readonly on
```

无论哪种触发,Agent 会:
1. 立刻把 `readonly = True`
2. 拒绝任何 L2+ 操作(L0/L1 仍可执行)
3. 在 IM 频道告警
4. 写入审计日志
5. 等待人类显式 `readonly off` 才恢复

### 黑名单

`safety.py` 维护一个硬编码的危险命令模式列表,Agent 输出的命令在执行前会被这个列表逐项匹配.无论 LLM 怎么"被说服",这一关都过不去:

- `rm -rf /` / `rm -rf /*` / `rm -rf ~`
- `mkfs.*` / `dd if=`
- `DROP DATABASE` / `DROP TABLE` / `TRUNCATE`
- `shutdown` / `reboot` / `halt` / `poweroff`
- `chown -R / *` / `chmod -R 777 /`
- ...完整列表见 `src/safety/safety.py`

---

## 8. 扩展

### 8.1 新增 Playbook

往 `notebook/playbook/` 扔一个 markdown 文件即可,Agent 下次循环就能用到.格式自由,建议结构:

```markdown
# nginx 502 Bad Gateway

## 什么时候用我
- nginx 错误日志出现 connect() failed (111: Connection refused)
- 上游服务返回 502

## 先查什么
- `systemctl status <upstream>` 看后端是否在跑
- `tail -n 100 /var/log/nginx/error.log` 看具体错误
- `ss -tlnp | grep <port>` 看端口是否监听

## 怎么修
1. 上游进程死了 → `systemctl restart <upstream>`
2. 上游连接池满了 → 检查 `/etc/nginx/nginx.conf` 中的 `keepalive`
3. 上游 OOM → 看 dmesg + 调整内存

## 验证标准
- `curl http://localhost/` 返回 200
- 错误日志连续 1 分钟无新增 502
```

Agent 在 `find_relevant` 阶段会通过关键词匹配找到这个 playbook 并塞进 prompt.

### 8.2 新增 Git Host / Notifier 通道

`git_host.py` 和 `notifier.py` 都是抽象基类 + 多个实现.加新通道只需:

**Git Host**(参考 `GitHubClient` / `GitLabClient`):

```python
class GiteaClient(GitHostClient):
    def push_branch(self, repo_path, branch): ...
    def create_pr(self, repo_path, branch, base, title, body): ...
    def merge_pr(self, repo_path, pr_number): ...
    def get_pr_status(self, repo_path, pr_number): ...

# 注册到工厂 (src/infra/git_host.py)
def make_client(host_type, run=None):
    ...
    if host_type == "gitea":
        return GiteaClient(run=run)
```

**Notifier**(参考 `SlackNotifier`):

```python
class TeamsNotifier(_HTTPNotifier):
    def send(self, title, content, urgency="info"):
        payload = {
            "text": f"{title}\n\n{content}",
            "themeColor": {"info": "0078D7", "warning": "FF9900",
                           "critical": "D13438"}.get(urgency),
        }
        return self._post(payload)

# 注册到工厂 (src/infra/notifier.py)
def make_notifier(config, http_fn=None):
    ...
    if t == "teams":
        return TeamsNotifier(config.webhook_url, http_fn=http_fn)
```

---

## 9. 故障排查

### Agent 启动失败

```
RuntimeError: pip install anthropic
```
→ 装依赖:`pip install -r requirements.txt`

```
ValueError: Unsupported provider: xxx
```
→ `OPS_LLM_PROVIDER` 只支持 `anthropic` / `openai` / `zhipu`

### Agent 不响应人类输入

- 看是不是处于 LLM 流式生成中(终端有"思考中..."提示)— 输入会立刻打断它
- 检查 `pause` 状态:`status` 命令看一下
- 极端情况下 `Ctrl-C` 退出后重启,状态会从 `state.json` 恢复

### 健康端点 401/connection refused

- 端口被占用?改 port:看 `start_health_server` 调用处
- 默认监听 `127.0.0.1`,远程访问不到 — 故意的安全措施

### 补丁应用失败

```
git apply failed (rc=1)
```

排查顺序:
1. 看 incident 笔记里的 `apply_output` 字段 — 通常是 LLM 生成的 diff context 不匹配
2. PatchLoop 会自动重试 3 次,看是不是都失败了
3. 失败时本地工作区**应该**已彻底回滚.如果没有,手动:
   ```bash
   cd /opt/sources/backend
   git status                    # 应该是 clean
   git branch | grep fix/agent    # 应该没有遗留分支
   git stash list                 # 看是否有遗留 stash
   ```

### LLM 进入 degraded 状态

```
🚨 LLM 调用持续失败,已切换到只读模式
```

- 检查 API key 是否失效:`echo $OPS_LLM_API_KEY`
- 检查网络:`curl https://api.anthropic.com`
- 检查余额/配额
- Agent 会每 5 分钟自动尝试恢复,恢复后 IM 频道会通知

### Notebook 损坏

```bash
# 手动校验
cd notebook && git fsck

# 如果配了远端
# Agent 会在启动时尝试自动从远端恢复
# 手动恢复:
mv .git .git.broken
git init
git remote add origin <remote-url>
git fetch origin
git reset --hard origin/HEAD
```

### IM 通知不发

1. 检查 `notifier.yaml` 的 `type` 和 `webhook_url`
2. 检查 `notify_on` 白名单是否包含触发事件
3. 检查 `quiet_hours` 是否在静音时段
4. 看日志:`grep notifier journalctl -u ops-agent`

---

## 10. 运维实践

### 渐进信任策略

不要一上来就让 Agent 自动跑所有事情.推荐的渐进路径:

| 阶段 | 持续时间 | 配置 |
|---|---|---|
| **观察期** | 1-2 周 | `--readonly`,只看 Agent 怎么诊断,不让它动手 |
| **辅助期** | 2-4 周 | 关闭 readonly,但 `permissions.md` 严格,大部分动作都升级人类 |
| **自治期 L2** | 持续 | 放开常规 L2(重启/清日志),关闭 Sprint 3-4 的代码自动修复(`source_repos` 不配 `git_host`) |
| **自治期 L3** | 持续 | 启用代码自动修复,但 `max_auto_merges_per_day=2` 起步,慢慢加 |

### Notebook 备份

`notebook/` 是 Agent 的全部记忆.推荐:

```bash
# 1. 配置远端(用于灾难恢复)
cd notebook
git remote add origin git@github.com:org/ops-agent-notebook.git
git push -u origin main

# 2. 在 main.py 启动参数里启用自动 push(待 cron 或人工触发)
# 3. 或者每天 cron 一次:
0 3 * * * cd /var/lib/ops-agent/notebook && git push origin main
```

### Lessons 蒸馏

每次 incident 处理完 Agent 都会写 `notebook/lessons/<incident-id>-reflection.md`.建议每周翻一遍,把通用的教训手动整理成 playbook.

### 监控 OpsAgent 自身

OpsAgent 帮你管系统,但谁来管 OpsAgent?推荐:

1. **Prometheus 抓 `/metrics`**,设告警:
   - `ops_agent_llm_degraded == 1` 持续 10min → critical
   - `ops_agent_uptime_seconds < 60` → 进程刚重启过,值得看
   - `rate(ops_agent_actions_total[5m]) > 10` → 异常高频动作
2. **Watchdog 探活 `/healthz`** → 失败重启
3. **审计日志推送 SIEM** — `vector` / `fluent-bit` 抓 `notebook/audit/*.jsonl`
4. **IM 通知双通道** — 普通通知到 Slack,critical 也发到 PagerDuty

### Token 成本控制

Agent 每次 LLM 调用都会记录 token,在 `limits.yaml` 里:

```yaml
llm_tokens_per_hour: 200000     # 触发限流后只允许 L0 操作
llm_tokens_per_day: 1000000     # 触发后强制升级所有动作
```

实际成本(以 Claude Sonnet 4 为例):
- 一次诊断循环 ≈ 5-10K input + 1-2K output ≈ $0.05-0.10
- 一次完整代码修复(含 3 次重试)≈ 30-50K tokens ≈ $0.30-0.50
- 一份日报 ≈ 5K tokens ≈ $0.02

### 多副本部署(谨慎)

**当前 v2.0 是单进程假设**,以下组件不是分布式安全的:

- `state.json` — 单文件,多进程会互相覆盖
- `pending_events.jsonl` — append-only 但 pop 时 rewrite 有竞态
- `LimitsEngine`(`src/safety/limits.py`) — 内存 deque,多副本独立计数
- `auto_merge_timestamps` — 同上

如果必须 HA 部署,要么:
1. **主备模式**:一主一备,备机 readonly,主机崩溃时手动切
2. **重新设计共享存储层**:把上述四项落到 redis / etcd

不推荐"双活"直接部署 v2.0.

---

## 附录: Sprint 历史

| Sprint | 内容 | 测试数 | 累计 |
|---|---|---|---|
| 0 | 基线(basic + blacklist) | 85 | 85 |
| 1 | 多目标 / 爆炸半径 / 紧急停止 | 53 | 138 |
| 2 | 源码地图 / 异常反向定位 | 51 | 189 |
| 3 | 补丁生成 / 本地验证 | 56 | 245 |
| 4 | PR 工作流 / 生产观察 / revert | 74 | 319 |
| 5 | 状态持久化 / 健康检查 / LLM 降级 | 79 | 398 |
| 6 | 审计 / IM 通知 / Metrics / 日报 | 95 | **493** |

每个 Sprint 的设计回顾在 `notebook/lessons/sprint{1..6}-retrospective.md`,值得读.
