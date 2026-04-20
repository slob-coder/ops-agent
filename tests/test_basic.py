#!/usr/bin/env python3
"""
基础功能测试 — 验证 Notebook、Tools、Trust 的核心逻辑
不需要 LLM API Key 即可运行
"""

import os
import sys
import shutil
import tempfile

# 测试计数
passed = 0
failed = 0


def test(name, condition):
    global passed, failed
    if condition:
        print(f"  ✓ {name}")
        passed += 1
    else:
        print(f"  ✗ {name}")
        failed += 1


# ═══════════════════════════════════════════
#  Notebook 测试
# ═══════════════════════════════════════════
print("\n=== Notebook 测试 ===")
from infra.notebook import Notebook

test_dir = tempfile.mkdtemp(prefix="ops_agent_test_")
nb = Notebook(test_dir)

# 基本读写
nb.write("test.md", "hello world")
test("写入文件", nb.read("test.md") == "hello world")
test("文件存在", nb.exists("test.md"))
test("文件不存在", not nb.exists("nonexistent.md"))

# 追加
nb.append("test.md", "line 2")
test("追加内容", "line 2" in nb.read("test.md"))

# 目录结构
test("playbook 目录存在", os.path.isdir(os.path.join(test_dir, "playbook")))
test("config 目录存在", os.path.isdir(os.path.join(test_dir, "config")))
test("incidents 目录存在", os.path.isdir(os.path.join(test_dir, "incidents/active")))

# 列目录
nb.write("playbook/a.md", "aaa")
nb.write("playbook/b.md", "bbb")
files = nb.list_dir("playbook")
test("列目录", "a.md" in files and "b.md" in files)

# 搜索
hits = nb.search("hello")
test("grep 搜索", any("test.md" in h for h in hits))

# git
nb.commit("test commit")
test("git commit", os.path.isdir(os.path.join(test_dir, ".git")))

# Incident 管理
incident = nb.create_incident("test error")
test("创建 Incident", incident.endswith(".md"))
test("Incident 在 active", incident in nb.list_dir("incidents/active"))

nb.append_to_incident(incident, "- 14:00 发现问题")
content = nb.read(f"incidents/active/{incident}")
test("追加 Incident 内容", "发现问题" in content)

nb.close_incident(incident, "已解决")
test("归档 Incident", incident in nb.list_dir("incidents/archive"))
test("active 已清空", incident not in nb.list_dir("incidents/active"))

# Playbook 摘要
nb.write("playbook/nginx-502.md", "# Nginx 502 Bad Gateway\n详情...")
summary = nb.read_playbooks_summary()
test("Playbook 摘要", "nginx-502.md" in summary)

# 对话记录
nb.log_conversation("Human", "hello")
nb.log_conversation("Agent", "hi there")
today_files = nb.list_dir("conversations")
test("对话记录", len(today_files) > 0)

# 清理
shutil.rmtree(test_dir)

# ═══════════════════════════════════════════
#  Tools 测试
# ═══════════════════════════════════════════
print("\n=== Tools 测试 ===")
from infra.tools import ToolBox, TargetConfig, CommandResult

tb = ToolBox(TargetConfig.local())

# 基础命令
r = tb.run("echo hello")
test("执行 echo", r.success and "hello" in r.stdout)

r = tb.uptime()
test("uptime", r.success)

r = tb.free()
test("free -h", r.success)

r = tb.disk()
test("df -h", r.success)

r = tb.ps_aux()
test("ps aux", r.success)

# 超时
r = tb.run("sleep 10", timeout=1)
test("超时处理", not r.success)

# 黑名单
try:
    tb.run("rm -rf /")
    test("黑名单拦截", False)
except PermissionError:
    test("黑名单拦截", True)

try:
    tb.run("DROP DATABASE production")
    test("SQL 黑名单", False)
except PermissionError:
    test("SQL 黑名单", True)

# CommandResult
cr = CommandResult("test", "out", "", 0, 0.1)
test("CommandResult.success", cr.success)
test("CommandResult.output", cr.output == "out")

cr2 = CommandResult("test", "", "error msg", 1, 0.1)
test("CommandResult.failed", not cr2.success)
test("CommandResult.stderr", "error" in cr2.output)

# ═══════════════════════════════════════════
#  Trust 测试
# ═══════════════════════════════════════════
print("\n=== Trust 测试 ===")
from safety.trust import TrustEngine, ActionPlan, ALLOW, NOTIFY_THEN_DO, ASK, DENY

# 测试默认策略（不调 LLM）
test_dir2 = tempfile.mkdtemp(prefix="ops_agent_test_trust_")
nb2 = Notebook(test_dir2)
te = TrustEngine(nb2, None)

plan_l0 = ActionPlan("tail -f log", "观察", "无", "看到日志", 0, "无")
test("L0 默认 allow", te._default_check(plan_l0) == ALLOW)

plan_l2 = ActionPlan("restart nginx", "修复", "stop", "恢复", 2, "curl")
test("L2 默认 notify", te._default_check(plan_l2) == NOTIFY_THEN_DO)

plan_l3 = ActionPlan("git push", "提交补丁", "revert", "PR创建", 3, "CI")
test("L3 默认 ask", te._default_check(plan_l3) == ASK)

plan_l4 = ActionPlan("rm -rf", "清理", "无", "空间释放", 4, "df")
test("L4 默认 deny", te._default_check(plan_l4) == DENY)

# ActionPlan markdown 输出
md = plan_l2.to_markdown()
test("ActionPlan 输出", "restart nginx" in md and "L2" in md)

shutil.rmtree(test_dir2)

# ═══════════════════════════════════════════
#  Main 解析测试
# ═══════════════════════════════════════════
print("\n=== 解析逻辑测试 ===")
from main import OpsAgent

test_dir3 = tempfile.mkdtemp(prefix="ops_agent_test_main_")
# 创建一个不启动循环的 Agent 实例来测试解析方法
agent = OpsAgent.__new__(OpsAgent)
agent._prompts = {}

# 命令提取
cmds = agent._extract_commands("""
检查以下内容：
```commands
tail -n 50 /var/log/nginx/error.log
systemctl status backend
free -h
```
""")
test("提取 commands 块", len(cmds) == 3 and "tail" in cmds[0])

cmds2 = agent._extract_commands("""
STEP 1: `systemctl restart nginx`
STEP 2: `curl http://localhost`
""")
test("提取 STEP 格式", len(cmds2) == 2)

# Assessment 解析
assessment = agent._parse_assessment("""
STATUS: ABNORMAL
SEVERITY: 8
SUMMARY: nginx error.log 出现大量 502
DETAILS: 发现 connect() failed 错误
NEXT_STEP: 检查 backend 服务
""")
test("解析 assessment status", assessment["status"] == "ABNORMAL")
test("解析 assessment severity", assessment["severity"] == 8)
test("解析 assessment summary", "502" in assessment["summary"])

# Normal assessment
normal = agent._parse_assessment("STATUS: NORMAL\nSEVERITY: 0\nSUMMARY: 一切正常")
test("解析 normal status", normal["status"] == "NORMAL")

# Diagnosis 解析
diag = agent._parse_diagnosis("""
### 1. 现象（Facts）
nginx error.log 出现 connect() failed

### 2. 假设（Hypothesis）
backend 服务挂了

### 3. 把握（Confidence）
85%

### 4. 缺失信息（Gaps）
无

### 5. 是否需要人类（Escalate）
NO
""")
test("解析 diagnosis hypothesis", "backend" in diag["hypothesis"])
test("解析 diagnosis confidence", diag["confidence"] == 85)
test("解析 diagnosis escalate", diag["escalate"] == "NO")

shutil.rmtree(test_dir3)

# ═══════════════════════════════════════════
#  结果
# ═══════════════════════════════════════════
print(f"\n{'=' * 40}")
print(f"  通过: {passed}    失败: {failed}")
print(f"{'=' * 40}")

sys.exit(0 if failed == 0 else 1)
