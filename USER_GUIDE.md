# OpsAgent 使用手册

> 一个实时在岗、会成长、在人类监督下工作的数字运维员工

---

## 目录

1. [它是什么](#1-它是什么)
2. [安装](#2-安装)
3. [配置 LLM](#3-配置-llm)
4. [配置目标系统](#4-配置目标系统)
5. [启动 Agent](#5-启动-agent)
6. [和 Agent 对话](#6-和-agent-对话)
7. [理解 Agent 的行为](#7-理解-agent-的行为)
8. [Notebook 笔记本](#8-notebook-笔记本)
9. [扩展 Agent 能力](#9-扩展-agent-能力)
10. [授权与安全](#10-授权与安全)
11. [常见问题排查](#11-常见问题排查)
12. [常用场景示例](#12-常用场景示例)
13. [环境变量参考](#13-环境变量参考)
14. [命令行参数参考](#14-命令行参数参考)

---

## 1. 它是什么

OpsAgent 不是监控系统，不是日志聚合平台，也不是告警工具。

**它是一个数字员工**——一个 7×24 在线、会用 Shell、会记笔记、会跟你商量事情的运维工程师。

它的工作方式和人类运维一样：
- 用终端命令观察系统（`tail`、`systemctl`、`dmesg`...）
- 看到异常就思考是什么原因
- 根据经验决定怎么修
- 改完之后验证效果
- 把这次经历写进笔记本，下次遇到类似问题就更熟练

它部署在**业务系统之外**——通常是你的运维工作站或堡垒机——通过 SSH/kubectl 远程操作目标。**对业务零侵入，不需要在被监控的机器上装任何东西**。

---

## 2. 安装

### 2.1 环境要求

- Python 3.10+
- Git
- 可选：`sshpass`（仅在使用 SSH 密码认证时需要）
- 可选：Docker（如果用容器方式部署）

### 2.2 源码安装

```bash
# 解压发布包
tar -xzf ops-agent.tar.gz
cd ops-agent

# 安装 Python 依赖
pip install -r requirements.txt
```

`requirements.txt` 只包含两个 LLM SDK：

```
anthropic>=0.40.0
openai>=1.50.0
```

如果你只用其中一个，可以只装一个。

### 2.3 安装 sshpass（可选）

只有当你需要用密码登录远程服务器时才需要：

```bash
# macOS
brew install hudochenkov/sshpass/sshpass

# Ubuntu / Debian
sudo apt install sshpass

# CentOS / RHEL
sudo yum install sshpass
```

如果使用 SSH 密钥认证（推荐），不需要安装 sshpass。

### 2.4 Docker 安装

```bash
docker build -t ops-agent .
```

---

## 3. 配置 LLM

OpsAgent 支持三种 LLM 提供商。通过环境变量切换：

### 3.1 Anthropic Claude（默认）

```bash
export OPS_LLM_PROVIDER=anthropic
export OPS_LLM_API_KEY='sk-ant-xxxxxxxxxxxx'
# 可选：指定模型，默认 claude-sonnet-4-20250514
export OPS_LLM_MODEL=claude-sonnet-4-20250514
```

支持的模型示例：
- `claude-opus-4-5` — 最强模型
- `claude-sonnet-4-20250514` — 平衡型（推荐）
- `claude-haiku-4-5-20251001` — 速度优先

### 3.2 OpenAI

```bash
export OPS_LLM_PROVIDER=openai
export OPS_LLM_API_KEY='sk-xxxxxxxxxxxx'
export OPS_LLM_MODEL=gpt-4o   # 默认
```

如果使用 OpenAI 兼容的代理或本地模型（如 Ollama）：

```bash
export OPS_LLM_PROVIDER=openai
export OPS_LLM_BASE_URL='http://localhost:11434/v1'
export OPS_LLM_MODEL=llama3
export OPS_LLM_API_KEY=dummy   # 本地模型也要随便填一个
```

### 3.3 智谱 GLM

```bash
export OPS_LLM_PROVIDER=zhipu
export OPS_LLM_API_KEY='你的智谱API Key'
# 可选：默认 glm-4-plus
export OPS_LLM_MODEL=glm-4-plus
```

支持的模型：
- `glm-4-plus` — 最强（默认）
- `glm-4` — 平衡
- `glm-4-air` — 性价比
- `glm-4-flash` — 速度优先
- `glm-4-long` — 长上下文

---

## 4. 配置目标系统

OpsAgent 可以监控本机、也可以远程监控其他机器。

### 4.1 监控本机

什么都不用配，直接启动即可：

```bash
python main.py
```

### 4.2 远程监控（SSH 密钥）

**推荐方式**，最安全：

```bash
# 先确保你能用密钥登录目标机器
ssh -i ~/.ssh/id_rsa user@192.168.1.100

# 然后启动 Agent
python main.py --target user@192.168.1.100 --key ~/.ssh/id_rsa
```

### 4.3 远程监控（SSH 密码）

需要先安装 `sshpass`，然后有三种方式提供密码：

**方式 A：环境变量（推荐）**

```bash
export OPS_SSH_PASSWORD='你的密码'
python main.py --target user@192.168.1.100 --password
```

**方式 B：交互输入**

不设环境变量，加 `--password` 参数后，启动时会提示输入密码：

```bash
python main.py --target user@192.168.1.100 --password
# SSH password for user@192.168.1.100: ****
```

**方式 C：默认密钥（推荐 SSH 密钥代替密码）**

如果可以用密钥就用密钥。

### 4.4 自定义 SSH 端口

```bash
python main.py --target user@192.168.1.100 --port 2222 --key ~/.ssh/id_rsa
```

---

## 5. 启动 Agent

### 5.1 最简单的启动

```bash
export OPS_LLM_API_KEY='你的 API Key'
python main.py
```

第一次启动时，Agent 会自动**入职探索**——它会跑一系列命令认识这台机器，然后生成 `notebook/system-map.md` 和 `notebook/config/watchlist.md`。

之后会进入巡检循环。

### 5.2 只读模式（推荐第一次使用）

第一次部署到生产环境时，强烈建议先用只读模式跑几天：

```bash
python main.py --readonly
```

只读模式下：
- Agent 仍然会观察、判断、诊断、记录 Incident
- 但**不会执行任何修改操作**（重启、改配置、提 PR 都不会做）
- 你可以观察 Agent 的判断是否靠谱，再决定是否放权

### 5.3 调试模式

启动时加 `--debug` 可以看到详细的 LLM 调用日志：

```bash
python main.py --debug
```

### 5.4 Docker 启动

```bash
# 监控本机
docker run -it \
  -e OPS_LLM_API_KEY='你的 Key' \
  -v $(pwd)/notebook:/data/notebook \
  ops-agent

# 监控远程
docker run -it \
  -e OPS_LLM_API_KEY='你的 Key' \
  -v $(pwd)/notebook:/data/notebook \
  -v ~/.ssh:/root/.ssh:ro \
  ops-agent --target user@192.168.1.100 --key /root/.ssh/id_rsa
```

注意 `-v $(pwd)/notebook:/data/notebook` 把笔记本挂载到宿主机，这样容器重启后 Agent 的记忆不会丢。

---

## 6. 和 Agent 对话

启动后终端是交互式的——你可以**随时输入消息和 Agent 对话**。

### 6.1 内置命令

| 命令 | 作用 |
|---|---|
| `status` | 查看 Agent 当前状态 |
| `readonly on` | 切换到只读模式 |
| `readonly off` | 退出只读模式 |
| `stop` | 停止当前调查，回到巡检 |
| `quit` / `exit` / `bye` | 退出 Agent |

### 6.2 自然语言对话

直接用自然语言问任何问题：

```
> 最近有没有 OOM 事件？
[14:32:15] 💬 我查一下...让我看看 dmesg 和 journal。
[14:32:20] 💬 过去 24 小时没有发现 OOM 事件。最近一次 OOM 是在
            2026-04-03 14:23，被杀的是 backend 进程。详见
            incidents/archive/2026-04-03-1423-oom-backend.md。

> nginx 的连接数现在多少？
[14:33:02] 💬 当前 nginx 共有 142 个活跃连接（其中 118 个 ESTABLISHED）。
            状态正常，最近 10 分钟内没有异常波动。

> 帮我把 nginx 重启一下
[14:34:10] ⚠️  我打算执行以下操作：
              动作：systemctl restart nginx
              理由：用户请求
              回滚：如果重启失败，systemctl start nginx
            请输入 'y' 批准 / 'n' 否决：
> y
[14:34:25] ✅ nginx 重启成功，验证通过。已记录到 conversations/。
```

### 6.3 Agent 主动通知你

Agent 会在以下时机主动找你说话：

- 发现异常时
- 即将执行重要操作前（请求批准）
- 操作执行成功/失败后
- 遇到自己搞不定的问题（升级给人类）

```
[14:23:05] ⚠️  发现异常（严重度 8/10）：nginx error.log 出现大量 502
[14:23:12] 💬 正在调查...让我检查 backend 服务状态。
[14:23:20] 🚨 backend 服务 inactive (dead)，疑似 OOM 被杀。
              我打算执行：systemctl restart backend
              这是 L2 操作，按授权规则需要你批准。
              请输入 'y' 批准 / 'n' 否决：
```

---

## 7. 理解 Agent 的行为

### 7.1 三种工作模式

| 模式 | 何时进入 | 行为 |
|---|---|---|
| **巡检 (patrol)** | 默认状态 | 每 60 秒扫一次系统，轻量观察 |
| **调查 (investigate)** | 发现疑似异常 | 每 5 秒高频观察，深度收集信息 |
| **应急 (incident)** | 确认问题需要处置 | 每 2 秒密集监控，执行修复 |

模式切换由 Agent 自主决定。你可以用 `status` 命令查看当前模式。

### 7.2 一次完整事件的处理流程

```
1. 巡检发现异常
   ↓
2. 进入调查模式，创建 Incident 笔记
   ↓
3. 检索相关 Playbook 和历史 Incident
   ↓
4. 形成诊断假设（说明把握有多大）
   ↓
5. 把握 < 70% → 升级给人类
   把握 ≥ 70% → 制定修复方案
   ↓
6. 信任度检查（依据 permissions.md）
   ↓
7. allow → 直接执行
   notify → 通知后执行
   ask    → 等你批准
   deny   → 拒绝
   ↓
8. 执行修复
   ↓
9. 验证效果
   ↓
10. 复盘 → 更新 Playbook → 归档 Incident
```

### 7.3 Agent 的"成长"

Agent 处理过的每一次事件都会沉淀经验：

- **更新 Playbook**：在已有 Playbook 末尾追加历史记录
- **创建新 Playbook**：从全新场景中提炼操作手册
- **写 Lesson**：从多个相似事件中蒸馏教训
- **修正 system-map**：发现新服务/新依赖时更新拓扑

你可以打开 `notebook/playbook/` 查看 Agent 学到了什么。

---

## 8. Notebook 笔记本

Notebook 是 Agent 的"大脑"——一个 git 仓库，里面全是 markdown。

### 8.1 目录结构

```
notebook/
├── README.md              ← Agent 的自我介绍 + 当前状态
├── system-map.md          ← Agent 画的系统拓扑
├── config/
│   ├── permissions.md     ← 授权规则（你可以改）
│   ├── watchlist.md       ← 观察源配置（你可以改）
│   └── contacts.md        ← 联络人信息（你应该填）
├── playbook/              ← 操作手册（Agent 和你都可以写）
│   ├── nginx-502.md
│   ├── oom-killer.md
│   └── disk-full.md
├── incidents/
│   ├── active/            ← 正在处理的事件
│   └── archive/           ← 已关闭的事件
├── lessons/               ← 蒸馏出来的经验教训
├── conversations/         ← 和你的对话历史
└── questions/             ← Agent 想问但还没答案的问题
```

### 8.2 人类可以直接编辑

**Notebook 是人和 Agent 共用的笔记。** 你可以：

- 用 IDE 直接打开 markdown 编辑
- Agent 在下一轮循环就会读到你的修改
- 修改不需要重启 Agent

**修改 Notebook 的常见场景**：

| 场景 | 改哪个文件 |
|---|---|
| 我想加一个新的修复 SOP | 往 `playbook/` 加一个 `.md` 文件 |
| 我想让 Agent 多检查某个日志 | 编辑 `config/watchlist.md` |
| 我想收紧权限，重启服务必须批准 | 编辑 `config/permissions.md` |
| 告诉 Agent 业务联系人是谁 | 编辑 `config/contacts.md` |
| 修正 Agent 误解的系统拓扑 | 编辑 `system-map.md` |

### 8.3 git 历史

Notebook 是个 git 仓库，所有 Agent 的修改都有 commit message。你可以：

```bash
cd notebook
git log --oneline       # 看 Agent 都做了什么
git diff HEAD~5 HEAD    # 看最近 5 次改了什么
git revert <commit>     # 回滚某次修改
```

### 8.4 跨机器同步

Notebook 可以 push 到远端 git 仓库实现"经验共享"：

```bash
cd notebook
git remote add origin git@github.com:yourcompany/ops-notebook.git
git push -u origin main

# 在另一台机器上
git clone git@github.com:yourcompany/ops-notebook.git
python main.py --notebook ./ops-notebook
```

---

## 9. 扩展 Agent 能力

### 9.1 添加新 Playbook

**最简单的扩展方式**——往 `notebook/playbook/` 里扔一个 markdown 文件即可，不需要重启 Agent。

格式自由，建议包含四个部分：

```markdown
# MySQL 慢查询飙升

## 什么时候用我
- MySQL 慢查询日志数量突然增加
- 应用响应延迟报警
- mysql slow_queries 指标异常

## 先查什么
1. 当前活跃连接：`mysql -e 'SHOW PROCESSLIST'`
2. 慢查询日志：`tail -100 /var/log/mysql/slow.log`
3. InnoDB 状态：`mysql -e 'SHOW ENGINE INNODB STATUS\G'`
4. 系统负载：`top -bn1 | head`

## 怎么修
- 短期：识别 long-running query 并 kill：`mysql -e 'KILL <id>'`
- 中期：增加索引（需要走 PR 流程）
- 长期：拆库分表（需要升级给人类）

## 验证标准
- 慢查询率回落到正常水平
- 应用响应延迟恢复
- 持续观察 10 分钟稳定

## 历史记录
（Agent 自动追加）
```

写完保存即可。Agent 在下次遇到匹配场景时会自动找到并使用。

### 9.2 添加新观察源

编辑 `notebook/config/watchlist.md`，用自然语言描述：

```markdown
## 自定义观察源
- 每 60 秒：`tail -n 30 /opt/myapp/logs/error.log` — 关注 ERROR 级别日志
- 每 300 秒：`curl -s http://localhost:8080/health` — 业务健康检查
- 每 60 秒：`redis-cli INFO clients` — Redis 连接数
```

Agent 会读到这些自然语言描述，自己生成对应的命令并执行。

### 9.3 修改 Agent 的行为准则

`README.md` 是 Agent 的"工作准则"，每次 LLM 调用都会读到。你可以编辑它来改变 Agent 的行事风格：

```markdown
# 我是谁
我是 prod-cluster-01 的运维员工。

# 工作准则
- 凌晨 0:00 - 6:00 是免打扰时段，非紧急情况不要叫醒人类
- 涉及支付服务（payment-*）的任何操作都必须人类批准
- 优先级：业务可用性 > 数据安全 > 性能 > 日志噪声
- 每周一早上 9 点写一份周报到 lessons/weekly-report.md
```

### 9.4 适配新系统类型

如果你要监控的目标是非 Linux 系统（Windows、嵌入式设备等），需要：

1. 在 `tools.py` 中增加对应的命令封装方法
2. 在 `prompts/system.md` 中补充该系统的可用命令清单
3. 写 2-3 个示例 Playbook 让 Agent 知道怎么处理常见问题

---

## 10. 授权与安全

### 10.1 信任等级

OpsAgent 的所有动作分为 5 个等级：

| 等级 | 说明 | 默认行为 |
|---|---|---|
| **L0 只读** | tail/grep/ps/df 等观察命令 | 直接执行 |
| **L1 写笔记** | 修改 Notebook 内容 | 直接执行 |
| **L2 服务操作** | 重启/改配置 | 通知后执行（核心服务需批准）|
| **L3 代码修改** | 提 PR | 必须人类批准 |
| **L4 破坏性** | rm -rf / DROP DATABASE 等 | **永远禁止** |

### 10.2 编辑 permissions.md 自定义授权

`notebook/config/permissions.md` 是用**自然语言**写的授权规则。Agent 每次执行操作前都会读这个文件，让 LLM 判断当前操作是否允许。

示例规则：

```markdown
# 授权规则

## 默认策略
- 只读操作：直接执行
- 重启非核心服务：通知人类后直接执行
- 重启核心服务（mysql, redis, nginx）：必须人类批准
- 修改配置文件：通知人类后执行，必须先备份原文件
- 修改代码/提交 PR：必须人类批准

## 核心服务列表
mysql, redis, nginx, gateway, payment-service

## 时间窗口
- 工作日 9:00-18:00：可自主重启非核心服务
- 其他时间：所有 L2 操作都要批准

## 紧急情况
如果业务完全不可用且每分钟损失 > 1万元，可不等批准直接执行 L2，
但必须事后在 Incident 中详细记录。
```

**这是真正的自然语言**——你怎么写都行，LLM 会理解。

### 10.3 黑名单（永远不能改）

以下命令在代码里硬编码为黑名单，Agent **永远不会执行**，即使你在 permissions.md 里允许也不行：

- `rm -rf /` 和 `rm -rf /*`
- `mkfs` / `mke2fs` 等格式化命令
- `dd if=... of=/dev/sd...` 等写磁盘设备
- `DROP DATABASE` / `DROP TABLE` / `TRUNCATE TABLE`
- `shutdown` / `reboot` / `poweroff` / `halt`
- Fork 炸弹
- `format c:` 等 Windows 格式化
- `chmod -R / `/`chown -R /`

注意：合法的运维操作不会被误伤，例如 `rm -rf /tmp/cache`、`docker ps --format`、`iptables -j DROP` 都能正常执行。

### 10.4 紧急停手

如果发现 Agent 行为异常，立即在终端输入：

```
> readonly on
```

Agent 会立即切换到只读模式，不再执行任何修改操作。

或者直接 Ctrl+C 退出。

---

## 11. 常见问题排查

### 11.1 启动时报错 `Command blocked (matches '...')`

**原因**：黑名单误判。理论上不应该再发生（已修复）。如果还遇到：

1. 把完整的报错命令贴出来
2. 检查 `tools.py` 的 `BLACKLIST_PATTERNS`
3. 临时绕过：把命令换个写法

### 11.2 启动时报错 `pip install anthropic` / `pip install openai`

**原因**：缺少 LLM SDK。

```bash
pip install anthropic openai
```

### 11.3 SSH 密码登录失败 `sshpass: command not found`

**原因**：没装 sshpass。

```bash
# macOS
brew install hudochenkov/sshpass/sshpass

# Ubuntu
sudo apt install sshpass
```

或者改用 SSH 密钥认证：

```bash
ssh-keygen -t ed25519
ssh-copy-id user@target-host
python main.py --target user@target-host --key ~/.ssh/id_ed25519
```

### 11.4 Agent 不响应人类输入

**可能原因**：

1. Agent 正在等 LLM 回复——稍等几秒
2. Agent 进入了应急模式正在密集处理——输入 `stop` 中断
3. stdin 被其他进程占用——用 Docker 时记得加 `-it`

### 11.5 Agent 频繁误判正常状态为异常

**原因**：LLM 对你的系统不熟。

**解决**：

1. 检查 `system-map.md` 是否准确，手动修正
2. 在 `README.md` 里加一段"什么是正常的"说明
3. 在 `playbook/` 里加 `false-positives.md` 列出常见误报模式

### 11.6 LLM 调用超时或 API 报错

**临时解决**：

- 检查网络：`curl -v https://api.anthropic.com`
- 检查 API Key 余额
- 切换到其他 Provider 试试

**长期方案**：使用本地部署的 LLM（通过 OpenAI 兼容接口）。

### 11.7 Notebook 越来越大怎么办

Agent 会自动归档已关闭的 Incident 到 `incidents/archive/`。如果太多了：

```bash
cd notebook/incidents/archive
# 把 30 天前的归档压缩
find . -name "*.md" -mtime +30 | tar czf old-incidents.tar.gz -T -
find . -name "*.md" -mtime +30 -delete
git add -A && git commit -m "Archive old incidents"
```

---

## 12. 常用场景示例

### 12.1 场景：第一次部署到测试环境

```bash
# 1. 用只读模式跑 24 小时观察 Agent 的判断
export OPS_LLM_API_KEY='...'
python main.py --target user@test-server --key ~/.ssh/id_rsa --readonly

# 2. 观察 notebook/incidents/active/ 看 Agent 创建了哪些事件
ls notebook/incidents/active/

# 3. 看 Agent 的诊断是否准确
cat notebook/incidents/active/*.md

# 4. 觉得靠谱了，关闭只读模式
> readonly off
```

### 12.2 场景：监控生产服务器（保守配置）

编辑 `notebook/config/permissions.md`：

```markdown
## 严格策略（生产环境）
- 所有 L2 操作（重启/改配置）都必须人类批准
- 工作日 9:00-18:00 可批准 L2 操作
- 其他时间只允许 L1 及以下
- L3 操作走 PR 流程，需要 code review
```

启动：

```bash
python main.py --target user@prod-server --key ~/.ssh/prod_key
```

### 12.3 场景：监控 K8s 集群

OpsAgent 跑在能访问 K8s 的运维工作站上：

```bash
# 确保 kubectl 配置可用
kubectl get nodes

# 启动 Agent（local 模式即可，因为 kubectl 命令在本地执行）
python main.py
```

Agent 入职探索时会自动发现 K8s 并把 `kubectl get pods` 等命令加入观察列表。

### 12.4 场景：让 Agent 帮你写一份系统报告

```
> 给我一份这台服务器最近一周的运行报告

[Agent 会自动:
 1. 翻 incidents/archive 看一周内的事件
 2. 跑命令收集当前指标
 3. 综合写一份 markdown 报告
 4. 保存到 lessons/weekly-report-2026-04-08.md]
```

### 12.5 场景：手动添加一个 Playbook

```bash
# 直接在 notebook 目录里创建文件
cat > notebook/playbook/redis-memory-full.md << 'EOF'
# Redis 内存满

## 什么时候用我
- redis-cli INFO memory 显示 used_memory_rss 接近 maxmemory
- 应用报 OOM command not allowed when used memory > maxmemory

## 先查什么
1. `redis-cli INFO memory` 查内存详情
2. `redis-cli INFO clients` 查连接数
3. `redis-cli --bigkeys` 找大 key
4. `redis-cli MEMORY STATS` 看内存分布

## 怎么修
- 临时：清理过期 key `redis-cli --scan --pattern "*tmp*" | xargs redis-cli DEL`
- 中期：调整 maxmemory-policy 为 allkeys-lru
- 长期：扩容 Redis 实例或拆分

## 验证标准
- used_memory < maxmemory * 0.8
- 应用读写恢复正常
EOF
```

写完即生效，无需重启 Agent。

---

## 13. 环境变量参考

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPS_LLM_PROVIDER` | `anthropic` | LLM 提供商：`anthropic` / `openai` / `zhipu` |
| `OPS_LLM_MODEL` | 各 Provider 默认 | 模型名称 |
| `OPS_LLM_API_KEY` | （无） | LLM API Key |
| `OPS_LLM_BASE_URL` | （无） | 自定义 API 地址（用于本地模型或代理） |
| `OPS_SSH_PASSWORD` | （无） | SSH 密码（推荐用环境变量传） |

各 Provider 的默认模型：

| Provider | 默认模型 |
|---|---|
| anthropic | `claude-sonnet-4-20250514` |
| openai | `gpt-4o` |
| zhipu | `glm-4-plus` |

---

## 14. 命令行参数参考

```
python main.py [OPTIONS]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--notebook PATH` | `./notebook` | Notebook 目录路径 |
| `--target USER@HOST` | （空，本机）| SSH 目标系统 |
| `--port N` | `22` | SSH 端口 |
| `--key PATH` | （空）| SSH 私钥路径 |
| `--password` | 关闭 | 使用密码认证（需 sshpass） |
| `--readonly` | 关闭 | 只读模式，不执行任何修改 |
| `--debug` | 关闭 | 调试日志（显示 LLM 调用详情） |

### 完整示例

```bash
# 本机监控
python main.py

# 远程监控（密钥）
python main.py --target ops@10.0.0.1 --key ~/.ssh/id_rsa

# 远程监控（密码 + 自定义端口）
export OPS_SSH_PASSWORD='xxx'
python main.py --target ops@10.0.0.1 --port 2222 --password

# 只读 + 自定义 Notebook 路径 + 调试
python main.py \
  --notebook /var/lib/ops-agent/notebook \
  --target ops@prod.example.com \
  --key /etc/ops-agent/prod.key \
  --readonly \
  --debug
```

---

## 附录 A：Agent 输出图标含义

| 图标 | 含义 |
|---|---|
| 💬 | Agent 的常规消息 |
| ✅ | 操作成功 |
| ⚠️ | 警告 / 需要注意 |
| 🚨 | 紧急 / 升级给人类 |
| ❓ | Agent 在向你提问 |
| 🔧 | Agent 正在执行操作 |

## 附录 B：项目文件清单

```
ops-agent/
├── main.py              # Agent 主循环
├── llm.py               # LLM 调用抽象（支持 anthropic/openai/zhipu）
├── notebook.py          # Notebook 读写
├── tools.py             # 命令执行工具箱
├── trust.py             # 授权引擎
├── chat.py              # 人机交互通道
├── prompts/
│   ├── system.md        # System prompt（Agent 的自我认知）
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
├── test_basic.py        # 基础功能测试
├── test_blacklist.py    # 黑名单回归测试
├── requirements.txt
├── Dockerfile
└── README.md
```

---

## 反馈与贡献

如果遇到问题或有改进建议，欢迎提 issue。最有价值的贡献是**分享你的 Playbook**——你写的每一个 Playbook 都可能帮助别人的系统更稳定。
