# OpsAgent 使用场景指南

**[English](./SCENARIOS.en.md)** | 中文

> 版本：v2.3.0

---

## 目录

1. [发现问题（只读巡检模式）](#1-发现问题只读巡检模式)
2. [简单问题自修复](#2-简单问题自修复)
3. [复杂问题定位（协作模式）](#3-复杂问题定位协作模式)
4. [大项目问题定位](#4-大项目问题定位)
5. [让 OpsAgent 更智能（Notebook 扩展）](#5-让-opsagent-更智能notebook-扩展)
6. [多目标监控](#6-多目标监控)
7. [通知与升级](#7-通知与升级)
8. [紧急情况处理](#8-紧急情况处理)

---

## 1. 发现问题（只读巡检模式）

### 场景描述

你刚部署 OpsAgent，还不确定它能做什么、会做什么。你希望它先观察系统，发现异常并通知你，但**绝不自动执行任何修复操作**。

### 适用情况

- 初次部署，想了解系统能力范围
- 对生产环境持谨慎态度，不允许自动操作
- 只需要监控告警能力，修复由人工处理

### 配置步骤

启动时加 `--readonly` 标志：

```bash
ops-agent --readonly
```

Agent 将以只读模式运行：所有巡检、观测、诊断正常工作，但不会执行任何写操作（重启、改配置、改代码等）。

### 运行中切换

无需重启，在交互界面直接输入命令即可切换：

```
readonly on    # 切换为只读模式
readonly off   # 切换为可写模式，允许自动修复
```

### 操作示例

```bash
# 以只读模式启动
ops-agent --readonly --notebook /path/to/notebook

# 运行中查看当前状态
status
# 输出包含: 只读模式: 是

# 确认能力后，切换为可写
readonly off
```

### 注意事项

- 只读模式下，Agent 仍会消耗 LLM token 进行诊断分析
- 检测到异常后 Agent 只会通知，不会尝试修复；如果希望 Agent 能处理简单问题，需关闭只读模式
- `readonly on/off` 切换是即时的，不需要重启 Agent

---

## 2. 简单问题自修复

### 场景描述

系统出现常见故障（如服务 OOM、配置错误、代码 bug），你希望 Agent 自动定位问题、生成补丁、提交 PR、验证修复，全程无需人工介入。

### 适用情况

- 已部署 Agent 并关闭只读模式
- 目标系统有对应的源代码仓库可访问
- 问题相对明确，Agent 可独立定位和修复

### 配置步骤

**核心：在 `notebook/config/targets.yaml` 中配置 `source_repos`**

```yaml
targets:
  - name: web-prod
    type: ssh
    host: ubuntu@10.0.0.10
    key_file: ~/.ssh/id_rsa

    source_repos:
      - name: backend
        path: /opt/sources/backend           # Agent 工作站上的源码 clone 路径
        repo_url: git@github.com:mycompany/backend.git
        branch: main
        language: java
        build_cmd: mvn clean compile          # 编译验证
        test_cmd: mvn test                    # 测试验证
        git_host: github                      # PR 平台：github | gitlab | noop | ""
        base_branch: main                     # PR 目标分支
        deploy_cmd: systemctl restart backend # 部署命令
        runtime_service: backend              # 对应的运行时服务名
        log_path: /opt/backend/logs/app.log
        # 路径前缀映射（处理容器内外路径差异）
        path_prefix_runtime: /app             # 容器内路径前缀
        path_prefix_local: ""                 # 本地 clone 中的相对前缀
```

**GitHub PR 认证配置（如需自动 PR）：**

```bash
# 安装 gh CLI
brew install gh    # macOS
# 或 sudo apt install gh   # Ubuntu

# 认证（需要 repo + pull_request 权限的 Personal Access Token）
gh auth login --with-token <<< "ghp_xxxxxxxxxxxxxxxxxxxx"

# 验证
gh auth status
```

### 自修复完整流程

Agent 检测到异常后，自动执行以下流程：

```
observe → diagnose → plan → patch → PR → deploy → verify
```

1. **observe**：采集系统指标、日志、进程状态
2. **diagnose**：LLM 分析根因，定位到源码位置（`source_repos` 提供代码映射）
3. **plan**：生成修复方案和验证步骤
4. **patch**：在本地源码 clone 上生成补丁并应用
5. **PR**：提交代码变更，创建 Pull Request（如配置了 `git_host`）
6. **deploy**：执行 `deploy_cmd` 部署补丁
7. **verify**：验证修复效果（即时验证 + 连续观察）

**补丁重试机制**：`patch_loop` 最多尝试 `max_patch_attempts` 次（默认 3 次，配置于 `notebook/config/limits.yaml`）。每次重试会根据上次失败信息调整补丁。

### 自动 Merge 与生产观察

- PR 创建后，如果验证通过，Agent 可自动合并（受 `max_auto_merges_per_day` 限制，默认每天最多 5 次）
- 部署后进入**连续观察期**（`watch_required_consecutive` 默认 2 次连续通过，间隔 `watch_default_interval` 默认 60 秒）

### 操作示例

```bash
# 正常启动（非只读模式）
ops-agent --notebook /path/to/notebook

# Agent 检测到异常后自动进入修复流程
# 也可以手动触发自修复：
self-fix backend 服务启动后内存持续增长，疑似 OOM

# 查看修复状态
status

# 查看 Incident 记录
show incidents/active/
```

### 注意事项

- `source_repos.path` 是 Agent 工作站上的路径，**不是**目标服务器上的路径
- 如果目标运行在 Docker/K8s 中，需要配置 `path_prefix_runtime` / `path_prefix_local` 映射容器内外路径
- `build_cmd` 和 `test_cmd` 是必要的——补丁应用后会先编译和测试，通过后才进入部署
- 当前默认走 **push + deploy 快速路径**，不经过 PR 审批；如需 PR 严格模式，需额外配置

---

## 3. 复杂问题定位（协作模式）

### 场景描述

系统出现复杂问题，Agent 在自动模式下多轮诊断仍无法定位根因。你需要与 Agent 一起排查，提供人类经验和领域知识。

### 适用情况

- 自动诊断多轮后仍无明确结论
- 根因涉及多个系统/服务的交互
- 需要人类经验判断（如业务逻辑、历史上下文）
- 问题表象和根因距离较远，需要方向性引导

### 关键说明：自动模式的上下文压缩

⚠️ **这是切换到协作模式最核心的原因。**

在自动诊断模式下，每一轮 `diagnose` 和 `plan` 的上下文会被 `context_limits` 机制压缩：

- `diagnosis_json_chars`（默认 700 字符）：诊断结论传入 plan 时被截断
- `prev_summary_chars`（默认 1000 字符）：上轮观测摘要被压缩
- `max_observations_chars`（默认 8000 字符）：所有观测数据传入 LLM 时被截断

这些压缩对于简单问题足够，但**复杂问题的关键线索可能在压缩中丢失**，导致 Agent 在多轮诊断中"遗忘"早期发现，陷入死循环。

**协作模式不会压缩上下文**——完整对话历史始终保留在 `collab_history` 中，每轮 LLM 调用都能看到之前所有分析。代价是 token 消耗显著更大。

### 如何进入协作模式

在交互界面输入：

```
collab
```

或中文：

```
协作
```

### 协作模式下的交互方式

协作模式采用**智能轮转**机制：

1. **Agent 自主推进只读操作**：信息收集类命令（`cat`、`grep`、`ps`、`kubectl logs` 等 60+ 种）Agent 会自动执行，不每步都问人
2. **关键决策暂停等人确认**：写操作、重启、方向不确定时，Agent 会 `[CONFIRM]` 等待人类批准
3. **人类随时插话**：即使 Agent 在自主推进，你也可以随时输入新信息或指令
4. **意图标记控制流程**：Agent 通过 `[CONTINUE]`（继续自主推进）、`[CONFIRM]`（等人确认）、`[WAIT]`（等人输入）三种标记控制节奏

```
# 协作模式交互示例
[你] collab
[Agent] 进入协作排查模式。请描述问题或让我分析当前 Incident。
[你] 数据库连接池频繁超时，怀疑和上周的配置变更有关
[Agent] 让我检查数据库连接配置和最近的变更记录...
[Agent] $ cat /etc/app/db.yml
[Agent] $ journalctl -u backend --since "7 days ago" | grep -i "config"
[Agent] [CONTINUE] 我看到连接池 max_size 从 50 改为 10，继续排查影响...
[你] 等一下，那个改动是有意的，先看慢查询日志
[Agent] 收到，切换方向查看慢查询日志...
[Agent] $ cat /var/log/mysql/slow.log | tail -100
[Agent] [CONFIRM] 发现 3 条耗时 >10s 的查询，建议 kill 这些连接并添加索引，是否执行？
[你] 先加索引，kill 连接稍后再说
[Agent] 好的，只执行索引添加...
```

### 何时应该切换到协作模式

| 信号 | 说明 |
|------|------|
| 多轮诊断仍无结论 | `max_diagnose_rounds` 耗尽仍 COLLECT_MORE |
| 根因涉及多系统交互 | 单系统观测无法解释的异常 |
| 需要人类经验判断 | 业务逻辑、历史背景、运维经验 |
| 诊断上下文被截断 | 日志中看到"truncated"标记，关键信息可能丢失 |
| Agent 反复尝试同一修复 | 陷入循环，需要人类引导方向 |

### 注意事项

- 协作模式 token 消耗远大于自动模式，注意 `llm_tokens_per_hour` 预算
- 连续自主执行上限为 `max_collab_auto_rounds`（默认 30 轮），超过后强制暂停等人确认
- 退出协作模式：输入 `exit`、`quit`、`退出` 等关键词

---

## 4. 大项目问题定位

### 场景描述

你的项目规模大、服务多、流量高，默认的参数限制不足以支撑 Agent 有效工作。需要调大各项限制。

### 适用情况

- 同时运行的服务 > 5 个
- 日志量大，默认上下文窗口不够
- 需要同时处理多个 Incident
- 诊断需要多轮深度分析

### 配置调整

编辑 `notebook/config/limits.yaml`：

```yaml
# ── 提高操作频率上限 ──
# 默认 20/h，大项目建议 50-80
max_actions_per_hour: 60

# ── 允许同时处理更多 Incident ──
# 默认 2，大项目建议 5-8
max_concurrent_incidents: 5

# ── 调大 token 预算 ──
# 默认 200k/h，大项目建议 500k-1M
llm_tokens_per_hour: 500000
llm_tokens_per_day: 3000000

# ── 增加诊断轮次 ──
# 默认 4（limits.py 代码默认），yaml 示例中为 25
# 复杂问题建议 8-12
max_diagnose_rounds: 10

# ── 增加总轮次上限 ──
# 默认 40，大项目可调到 60-80
max_total_rounds: 60

# ── 增加修复尝试次数 ──
# 默认 2（代码默认），yaml 示例中为 3
max_fix_attempts: 4

# ── 补丁生成重试次数 ──
max_patch_attempts: 5
```

编辑 `notebook/config/context_limits.yaml`：

```yaml
# ── 放大上下文窗口（使用 128k+ 大上下文模型时）──

# 传入 LLM 诊断 prompt 的观测数据最大字符数
# 默认 8000，大项目建议 16000-32000
max_observations_chars: 16000

# 诊断结论传入 plan 的最大字符数
# 默认 700，复杂问题建议 1500-2000
diagnosis_json_chars: 1500

# 上轮观测摘要最大字符数
# 默认 1000，多轮诊断时建议 2000-3000
prev_summary_chars: 2000

# 源码上下文 trace 最大字符数
# 默认 2000，大代码库建议 4000-6000
source_context_trace_chars: 4000

# 历史 Incident 内容最大字符数
# 默认 1000，建议 2000-3000
incident_history_chars: 2000

# Playbook 内容最大字符数
# 默认 1500，经验丰富的项目建议 3000
playbook_content_chars: 3000
```

### 多目标轮询间隔调整

当监控多个目标时，可以通过调整巡检间隔减少资源消耗或提高响应速度。轮询逻辑由主循环控制，每个目标依次巡检。如果目标较多，可以在 `targets.yaml` 中调整目标的 `criticality` 来影响巡检优先级：

```yaml
targets:
  - name: core-api
    criticality: critical    # 高优先级，更频繁巡检
  - name: monitoring
    criticality: low         # 低优先级，减少巡检频率
```

### 操作示例

```bash
# 修改配置后，Agent 自动重新加载，无需重启
# 查看当前限制状态
limits
# 输出：
# 每小时动作: 12/60
# 活跃 Incident: 3/5
# Token 预算: 120000/500000
```

### 注意事项

- 调大限制意味着**更大的爆炸半径**——确保你有信心在 Agent 出错时能快速回滚
- `max_concurrent_incidents` 过高可能导致 Agent 处理不过来，反而降低单 Incident 处理质量
- `max_observations_chars` 调大时注意 token 预算是否足够
- 修改配置后无需重启 Agent，下一次主循环自动重新加载

---

## 5. 让 OpsAgent 更智能（Notebook 扩展）

### 场景描述

内置的 Basic Notebook 只提供文件存储和简单检索，Agent 的"记忆"有限。你希望 Agent 具备知识图谱、误报过滤、成长学习等高级能力。

### 适用情况

- 长期运行，积累了大量 Incident 和 Playbook
- 误报率高，需要智能过滤
- 希望 Agent 从历史经验中学习和成长

### 内置 Basic Notebook 的局限

- **无知识图谱**：Incident 之间没有关联，无法发现跨服务的根因模式
- **无智能感知**：不能根据上下文主动推荐相关 Playbook 或历史经验
- **无成长引擎**：不会从成功/失败中学习，每次都从头开始
- **无信任评估**：无法根据历史表现调整操作权限

### 安装扩展 Notebook

推荐安装 `smart-notebook` 扩展：

```bash
# 在 notebook 目录下安装扩展
cd /path/to/notebook
pip install ops-agent-smart-notebook

# 在 notebook/config/notebook.yaml 中配置
```

编辑 `notebook/config/notebook.yaml`：

```yaml
# 启用 Smart Notebook 扩展
type: smart

# 如果不配置，默认使用 basic
# type: basic
```

### 扩展带来的能力

| 能力 | 说明 | 对应功能 |
|------|------|----------|
| **知识图谱** | 自动关联 Incident、Playbook、服务之间的关系 | Linker 引擎 |
| **智能感知** | 根据上下文推荐相关历史经验和 Playbook | 感知引擎 |
| **成长引擎** | 从修复成功/失败中学习，持续改进 | Scorecard + Trust 评估 |
| **误报过滤** | 记录和管理误报模式，避免重复处理 | FP Tracker |
| **信任评估** | 根据历史表现自动调整操作权限 | Trust Level |

### 扩展后的交互命令

```
# 查看成长记分卡
scorecard

# 查看当前信任层级
trust

# 标记误报
fp <模式描述>
# 例如: fp 内存使用率 90% 是正常业务高峰表现

# 查看智能统计（status 命令会自动显示）
status
```

### 详细文档

完整的 Notebook 扩展指南请参考：[docs/notebook-extension.md](./docs/notebook-extension.md)

### 注意事项

- Smart Notebook token 消耗比 Basic Notebook 更高（知识图谱查询和关联分析需要额外 LLM 调用）
- 从 Basic 迁移到 Smart 是无缝的，已有数据会自动索引
- 如需回退到 Basic，将 `notebook.yaml` 中 `type` 改回 `basic` 即可

---

## 6. 多目标监控

### 场景描述

你需要同时监控多台服务器、Docker 主机或 K8s 集群，Agent 在它们之间轮询巡检。

### 适用情况

- 管理多个环境（开发、预发、生产）
- 同时监控不同类型的目标（SSH、Docker、K8s）
- 需要针对不同目标采用不同的巡检策略

### 配置步骤

编辑 `notebook/config/targets.yaml`：

```yaml
targets:
  # 远程 SSH 服务器
  - name: web-prod
    type: ssh
    description: "生产 web 服务器"
    criticality: high
    host: ubuntu@10.0.0.10
    key_file: ~/.ssh/id_rsa

  # 本地 Docker
  - name: local-docker
    type: docker
    description: "本地 docker-compose 项目"
    criticality: normal
    docker_host: ""
    compose_file: ./docker-compose.yaml

  # K8s 集群
  - name: prod-k8s
    type: k8s
    description: "生产 K8s 集群"
    criticality: critical
    kubeconfig: ~/.kube/config
    context: prod-cluster
    namespace: default
```

### 目标切换

在交互界面中使用 `switch` 命令：

```
# 列出所有目标
targets

# 切换到指定目标
switch web-prod

# 切换后，后续操作聚焦在该目标上
```

### 不同目标的巡检策略

Agent 采用轮询方式依次巡检所有目标。通过 `criticality` 影响巡检优先级：

- `critical`：最高优先级，出现问题立即通知
- `high`：高优先级，快速响应
- `normal`：正常巡检频率
- `low`：低优先级，降低巡检频率

### 注意事项

- 所有目标共享同一个 Notebook 和 LLM 实例
- 同时监控多个目标时注意 `max_concurrent_incidents` 是否足够
- SSH 目标需要确保网络连通和认证配置正确
- K8s 目标需要有效的 kubeconfig

---

## 7. 通知与升级

### 场景描述

你希望 Agent 在关键事件发生时主动通知你，并在需要人类介入时自动升级。

### 适用情况

- 不想一直盯着 Agent 控制台
- 需要在手机上收到告警
- 希望在 Agent 无法处理时自动通知人类介入

### 配置步骤

复制并编辑通知配置：

```bash
cp notebook/config/notifier.yaml.example notebook/config/notifier.yaml
```

编辑 `notebook/config/notifier.yaml`：

```yaml
# 通知类型：slack | dingtalk | feishu | feishu_app | none
type: feishu_app

# Webhook URL（slack/dingtalk/feishu 使用）
# 也可通过 OPS_NOTIFIER_WEBHOOK_URL 环境变量设置
webhook_url: ""

# 飞书自建应用机器人配置
feishu_app:
  app_id: "cli_xxx"
  app_secret: "xxx"
  chat_id: "oc_xxx"
  # 启用双向交互
  interactive:
    enabled: true
    callback_port: 9877
    encrypt_key: ""
    verification_token: ""

# 触发通知的事件类型
notify_on:
  - incident_opened       # 新 Incident 创建
  - incident_closed       # Incident 关闭
  - pr_merged             # PR 已合并
  - revert_triggered      # 触发回滚
  - critical_failure      # 严重故障
  - llm_degraded          # LLM 降级
  - daily_report          # 每日报告

# 免打扰时段（critical 不受影响）
quiet_hours:
  start: "22:00"
  end: "08:00"
  except_urgency:
    - critical
```

### 通知策略

| 事件 | 默认通知 | 说明 |
|------|----------|------|
| `incident_opened` | ✅ | 新问题被发现 |
| `incident_closed` | ✅ | 问题已解决 |
| `pr_merged` | ✅ | 自动修复的 PR 已合并 |
| `revert_triggered` | ✅ | 修复失败，自动回滚 |
| `critical_failure` | ✅ | 严重故障，需人类介入 |
| `llm_degraded` | ✅ | LLM 服务降级 |
| `daily_report` | ✅ | 每日巡检摘要 |

### ESCALATE 时的人类介入

当 Agent 遇到以下情况时会自动升级（ESCALATE）：

- 超过操作频率限制
- 超过并发 Incident 上限
- 冷却期内再次触发
- 修复连续失败
- 遇到无法识别的异常

升级时 Agent 会：

1. 通过配置的通知渠道发送告警
2. 进入等待状态，不再尝试自动修复
3. 等待人类确认或提供指导

### 飞书双向交互

配置 `interactive.enabled: true` 后，飞书成为双向交互通道：

- **接收通知**：Agent 推送消息到飞书群
- **发送指令**：在飞书群中直接回复 Agent，如 `readonly on`、`status` 等
- **审批操作**：Agent 请求确认时，可在飞书中回复"同意"或"拒绝"

⚠️ 飞书交互需要公网可达的回调端口（`callback_port`）。

### 注意事项

- Webhook URL 等敏感信息建议通过环境变量传入，不要明文写在配置文件中
- `quiet_hours` 对 `critical` 级别通知无效，确保紧急告警不被静默
- 钉钉和 Slack 使用 `webhook_url`，飞书推荐使用 `feishu_app` 模式以支持双向交互

---

## 8. 紧急情况处理

### 场景描述

Agent 执行了错误操作，或者系统出现紧急状况，你需要立即停止 Agent 的所有操作。

### 适用情况

- Agent 正在执行错误修复，需要立即制止
- 系统出现严重故障，不允许 Agent 继续操作
- Agent 行为异常，需要紧急暂停

### 三种紧急停止方式

#### 方式一：CLI `freeze` 命令

在交互界面输入：

```
freeze
```

效果：触发紧急停止，同时自动开启只读模式。Agent 停止所有操作，但进程不退出。

解除：

```
unfreeze
```

#### 方式二：文件标记

在 Notebook 目录下创建标记文件：

```bash
touch /path/to/notebook/EMERGENCY_STOP_SELF_MODIFY
```

效果：阻止 Agent 执行自修复操作（`self-fix` 命令会检查此文件并拒绝执行）。

解除：

```bash
rm /path/to/notebook/EMERGENCY_STOP_SELF_MODIFY
```

#### 方式三：信号

向 Agent 进程发送信号：

```bash
# 查找 Agent 进程
ps aux | grep ops-agent

# 发送 SIGUSR1 触发紧急停止
kill -USR1 <pid>

# 发送 SIGTERM 优雅退出
kill <pid>

# 紧急强制终止（最后手段）
kill -9 <pid>
```

### 紧急停止后的恢复

1. **评估影响**：检查 `notebook/incidents/active/` 下的记录，确认 Agent 做了什么
2. **解除停止**：使用 `unfreeze` 或删除标记文件
3. **恢复只读巡检**：先以只读模式恢复观察，确认系统正常后再开放修复能力

```
# 恢复步骤
unfreeze              # 解除紧急停止
status                # 查看当前状态
readonly on           # 先只读观察
# ... 确认系统正常后 ...
readonly off          # 恢复自动修复
```

### 误操作后的 revert 机制

Agent 的每次修复操作都有记录和回滚能力：

- **Git revert**：如果修复通过 PR 提交，可通过 `git revert` 回滚
- **配置回滚**：Agent 在修改配置前会备份原始文件（`.bak` 后缀）
- **部署回滚**：
  - Docker：`docker-compose down && docker-compose up -d --build` 使用原始镜像
  - K8s：`kubectl rollout undo deployment/<name>` 回滚到上一版本
  - Systemd：`systemctl restart <service>` 使用回滚后的配置

手动触发回滚：

```
# 查看 Incident 记录中的修复操作
show incidents/active/<incident-id>

# 如果有 pre_tag（修复前的 git tag），可以回滚到该版本
git checkout <pre_tag>
```

### 注意事项

- `freeze` 是最安全的紧急停止方式——它保留 Agent 进程和上下文，方便排查
- 文件标记方式适合无法访问交互界面的情况（如通过 SSH 远程操作）
- `kill -9` 是最后手段，可能导致数据不一致
- 每次自修复前 Agent 会创建 `pre_tag`（git tag），这是回滚的锚点
