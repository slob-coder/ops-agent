#!/usr/bin/env python3
"""Sprint 1 新模块测试 - limits / safety / targets / 多目标 ToolBox"""

import os
import sys
import time
import shutil
import tempfile

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
#  Limits 测试
# ═══════════════════════════════════════════
print("\n=== Limits 测试 ===")
from src.safety.limits import LimitsConfig, LimitsEngine

# 默认配置
cfg = LimitsConfig()
test("默认 enabled", cfg.enabled)
test("默认 max_actions_per_hour=20", cfg.max_actions_per_hour == 20)

# 引擎基础
le = LimitsEngine(LimitsConfig(max_actions_per_hour=3,
                                max_restarts_per_service_per_hour=2,
                                max_concurrent_incidents=2,
                                cooldown_after_failure_seconds=60))

ok, _ = le.check_action("restart", "nginx")
test("初始允许动作", ok)

# 全局动作配额
le.record_action("restart", "svc-a")
le.record_action("restart", "svc-b")
le.record_action("restart", "svc-c")
ok, reason = le.check_action("restart", "svc-d")
test("全局动作上限触发", not ok and "每小时" in reason)

# 单服务限制
le2 = LimitsEngine(LimitsConfig(max_restarts_per_service_per_hour=2,
                                  max_actions_per_hour=100))
le2.record_action("restart", "nginx")
le2.record_action("restart", "nginx")
ok, reason = le2.check_action("restart", "nginx")
test("单服务重启上限", not ok and "nginx" in reason)
ok, _ = le2.check_action("restart", "mysql")
test("其他服务不受影响", ok)

# 并发 Incident
le3 = LimitsEngine(LimitsConfig(max_concurrent_incidents=2, max_actions_per_hour=100))
le3.record_incident_start()
le3.record_incident_start()
ok, reason = le3.check_action("restart", "x")
test("并发 Incident 上限", not ok and "并发" in reason)
le3.record_incident_end()
ok, _ = le3.check_action("restart", "x")
test("Incident 结束后恢复", ok)

# 失败冷却
le4 = LimitsEngine(LimitsConfig(cooldown_after_failure_seconds=10, max_actions_per_hour=100))
le4.record_failure()
ok, reason = le4.check_action("restart", "x")
test("失败后进入冷却", not ok and "冷却" in reason)

# Token 预算
le5 = LimitsEngine(LimitsConfig(llm_tokens_per_hour=1000))
le5.record_tokens(800)
ok, _ = le5.check_llm_budget(100)
test("Token 预算未超", ok)
ok, reason = le5.check_llm_budget(300)
test("Token 预算超出", not ok)

# 禁用引擎
le6 = LimitsEngine(LimitsConfig(enabled=False, max_actions_per_hour=1))
le6.record_action("x", "y")
le6.record_action("x", "y")
ok, _ = le6.check_action("x", "y")
test("禁用时不限制", ok)

# 状态查询
status = le5.status()
test("status 包含 enabled", "enabled" in status)
test("status 包含 token 字段", "tokens_last_hour" in status)


# ═══════════════════════════════════════════
#  Safety / EmergencyStop 测试
# ═══════════════════════════════════════════
print("\n=== Safety 测试 ===")
from src.safety.safety import EmergencyStop

test_dir = tempfile.mkdtemp(prefix="ops_safety_test_")
es = EmergencyStop(test_dir)

frozen, reason = es.check()
test("初始未冻结", not frozen)

# 文件触发
with open(os.path.join(test_dir, "EMERGENCY_STOP"), "w") as f:
    f.write("测试触发")
frozen, reason = es.check()
test("文件触发冻结", frozen and "测试触发" in reason)

# 文件删除自动解冻
os.remove(os.path.join(test_dir, "EMERGENCY_STOP"))
frozen, reason = es.check()
test("文件删除自动解冻", not frozen)

# 主动触发
es.trigger("代码触发")
test("trigger() 后冻结", es.frozen)
test("写入了 STOP 文件", os.path.exists(os.path.join(test_dir, "EMERGENCY_STOP")))

# clear
es.clear()
test("clear() 后解冻", not es.frozen)
test("STOP 文件被删", not os.path.exists(os.path.join(test_dir, "EMERGENCY_STOP")))

shutil.rmtree(test_dir)


# ═══════════════════════════════════════════
#  Targets 配置加载测试
# ═══════════════════════════════════════════
print("\n=== Targets 加载测试 ===")
from src.infra.targets import load_targets, render_targets_summary, Target

test_dir = tempfile.mkdtemp(prefix="ops_targets_test_")
yaml_path = os.path.join(test_dir, "targets.yaml")
with open(yaml_path, "w") as f:
    f.write("""
targets:
  - name: web-prod
    type: ssh
    description: 测试服务器
    criticality: high
    host: ubuntu@10.0.0.1
    key_file: ~/.ssh/id_rsa
    tags: [linux, web]

  - name: local-docker
    type: docker
    description: 本地 docker
    compose_file: ./docker-compose.yaml

  - name: prod-k8s
    type: k8s
    namespace: default
    context: prod-cluster
    source_repos:
      - name: api
        path: /tmp/api
        language: go
        build_cmd: go build
""")

targets = load_targets(yaml_path)
test("加载 3 个目标", len(targets) == 3)
test("第一个目标名", targets[0].name == "web-prod")
test("ssh 类型", targets[0].type == "ssh")
test("docker 类型", targets[1].type == "docker")
test("k8s 类型", targets[2].type == "k8s")
test("source_repos 加载", len(targets[2].source_repos) == 1)
test("source_repos 内容", targets[2].source_repos[0]["language"] == "go")

# 不存在的文件
empty = load_targets("/nonexistent/path.yaml")
test("不存在的文件返回空", empty == [])

# 摘要渲染
summary = render_targets_summary(targets)
test("摘要包含目标名", "web-prod" in summary and "prod-k8s" in summary)

shutil.rmtree(test_dir)


# ═══════════════════════════════════════════
#  TargetConfig 多模式测试
# ═══════════════════════════════════════════
print("\n=== TargetConfig 多模式测试 ===")
from src.infra.tools import TargetConfig, ToolBox

# local
tc = TargetConfig.local()
test("local 模式", tc.mode == "local")

# ssh
tc = TargetConfig.ssh("ubuntu@1.2.3.4", port=2222, key_file="/k")
test("ssh 模式", tc.mode == "ssh" and tc.host == "ubuntu@1.2.3.4")
test("ssh port", tc.port == 2222)

# docker
tc = TargetConfig.docker("d1", compose_file="./compose.yaml")
test("docker 模式", tc.mode == "docker" and tc.compose_file == "./compose.yaml")

# k8s
tc = TargetConfig.k8s("k1", context="prod", namespace="ns1")
test("k8s 模式", tc.mode == "k8s" and tc.kubectl_context == "prod")
test("k8s namespace", tc.namespace == "ns1")

# 从 Target 对象转换
t = Target(
    name="my-srv",
    type="ssh",
    description="test",
    host="user@host",
    key_file="~/.ssh/key",
)
tc = TargetConfig.from_target(t)
test("from_target ssh", tc.mode == "ssh" and tc.host == "user@host")
test("from_target 展开 ~", "~" not in tc.key_file)

# 从环境变量读密码
os.environ["TEST_PWD"] = "secret123"
t2 = Target(name="srv2", type="ssh", host="x", password_env="TEST_PWD")
tc2 = TargetConfig.from_target(t2)
test("password_env 读取", tc2.password == "secret123")
del os.environ["TEST_PWD"]


# ═══════════════════════════════════════════
#  多目标 ToolBox 测试
# ═══════════════════════════════════════════
print("\n=== 多目标 ToolBox 测试 ===")

# local 模式
tb_local = ToolBox(TargetConfig.local())
r = tb_local.run("echo local-mode")
test("local 模式 run", r.success and "local-mode" in r.stdout)

# docker 模式(命令在本地执行)
tb_docker = ToolBox(TargetConfig.docker("test"))
r = tb_docker.run("echo docker-mode")
test("docker 模式 run(本地执行)", r.success and "docker-mode" in r.stdout)

# k8s 模式同理
tb_k8s = ToolBox(TargetConfig.k8s("test"))
r = tb_k8s.run("echo k8s-mode")
test("k8s 模式 run(本地执行)", r.success and "k8s-mode" in r.stdout)

# 黑名单仍然生效
try:
    tb_docker.run("rm -rf /")
    test("docker 模式黑名单", False)
except PermissionError:
    test("docker 模式黑名单", True)


# ═══════════════════════════════════════════
#  OpsAgent 内部工具方法测试
# ═══════════════════════════════════════════
print("\n=== OpsAgent 辅助方法测试 ===")
from main import OpsAgent

# 不启动循环,只测试解析方法
agent = OpsAgent.__new__(OpsAgent)

# _classify_action
test("识别 systemctl restart",
     agent._classify_action("systemctl restart nginx") == "restart")
test("识别 docker restart",
     agent._classify_action("docker restart my-container") == "restart")
test("识别 kubectl rollout restart",
     agent._classify_action("kubectl rollout restart deployment/api") == "restart")
test("识别 sed 编辑",
     agent._classify_action("sed -i 's/a/b/' /etc/nginx.conf") == "edit")
test("识别 git apply",
     agent._classify_action("git apply patch.diff") == "code")
test("识别其他",
     agent._classify_action("ls -la") == "other")

# _extract_service_name
test("提取 systemctl 服务名",
     agent._extract_service_name("systemctl restart nginx") == "nginx")
test("提取 docker 容器名",
     agent._extract_service_name("docker restart my-app") == "my-app")
test("提取 kubectl deployment 名",
     agent._extract_service_name("kubectl rollout restart deployment/api-gateway") == "api-gateway")
test("无法提取返回空",
     agent._extract_service_name("ls -la") == "")


# ═══════════════════════════════════════════
print(f"\n{'=' * 40}")
print(f"  通过: {passed}    失败: {failed}")
print(f"{'=' * 40}")
sys.exit(0 if failed == 0 else 1)
