"""Sprint 6 tests — audit, notifier, reporter, metrics

Run: python test_sprint6.py
All IO uses tempfile / stub HTTP / stub LLM. No real network.
"""
import os
import sys
import json
import time
import shutil
import tempfile
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.reliability.audit import AuditLog
from src.infra.notifier import (
    NotifierConfig, NoOpNotifier, SlackNotifier, DingTalkNotifier,
    FeishuNotifier, PolicyNotifier, make_notifier,
)
from src.reporter import DailyReporter
from src.reliability.health import HealthServer

PASS = 0
FAIL = 0


def test(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


# ──────────────────────────────────────
# 1. AuditLog
# ──────────────────────────────────────
print("\n[audit]")
tmp = tempfile.mkdtemp()
try:
    log = AuditLog(tmp)
    test("record: 第一条 ok",
         log.record("incident_opened", target="web", severity=7))
    test("record: 第二条 ok",
         log.record("action_executed", target="web", kind="restart"))
    test("record: 不可序列化字段降级为 str",
         log.record("test", weird=object()))

    today = log.read_day()
    test("read_day: 3 条", len(today) == 3)
    test("read_day: 第一条 type", today[0]["type"] == "incident_opened")
    test("read_day: 第一条带 timestamp", "timestamp" in today[0])
    test("read_day: 字段保留", today[0].get("severity") == 7)
    test("read_day: weird 字段被字符串化", isinstance(today[2].get("weird"), str))

    counts = log.count_by_type()
    test("count: incident_opened=1", counts.get("incident_opened") == 1)
    test("count: action_executed=1", counts.get("action_executed") == 1)
    test("count: test=1", counts.get("test") == 1)

    dates = log.list_dates()
    test("list_dates: 至少 1 天", len(dates) >= 1)
    test("list_dates: 今天在内", AuditLog._today_str() in dates)

    # 不存在日期
    test("read_day: 不存在日期返回空",
         log.read_day("1999-01-01") == [])

    # 损坏行容错
    today_file = os.path.join(tmp, AuditLog._today_str() + ".jsonl")
    with open(today_file, "a") as f:
        f.write("not json\n")
        f.write('{"type":"recovered","timestamp":"x"}\n')
    again = log.read_day()
    test("损坏行: 跳过坏行,保留好行", len(again) == 4)
    test("损坏行: 恢复后的事件可见",
         any(e.get("type") == "recovered" for e in again))
finally:
    shutil.rmtree(tmp)


# ──────────────────────────────────────
# 2. NotifierConfig
# ──────────────────────────────────────
print("\n[notifier:config]")

# 默认值
cfg = NotifierConfig()
test("default: type=none", cfg.type == "none")
test("default: notify_on 含 incident_opened",
     "incident_opened" in cfg.notify_on)

# from_yaml — 不存在文件
cfg = NotifierConfig.from_yaml("/nonexistent/x.yaml")
test("yaml: 不存在 → 默认", cfg.type == "none")

# from_yaml — 真实加载
tmp = tempfile.mkdtemp()
try:
    p = os.path.join(tmp, "n.yaml")
    with open(p, "w") as f:
        f.write("""\
type: slack
webhook_url: https://hooks.example/abc
notify_on:
  - incident_opened
  - pr_merged
quiet_hours:
  start: "22:00"
  end: "08:00"
  except_urgency:
    - critical
""")
    cfg = NotifierConfig.from_yaml(p)
    test("yaml: type", cfg.type == "slack")
    test("yaml: webhook_url", cfg.webhook_url == "https://hooks.example/abc")
    test("yaml: notify_on 长度", len(cfg.notify_on) == 2)
    test("yaml: quiet start", cfg.quiet_hours_start == "22:00")
    test("yaml: quiet end", cfg.quiet_hours_end == "08:00")

    # 环境变量覆盖
    os.environ["OPS_NOTIFIER_WEBHOOK_URL"] = "https://override/x"
    cfg2 = NotifierConfig.from_yaml(p)
    test("yaml: 环境变量覆盖", cfg2.webhook_url == "https://override/x")
    del os.environ["OPS_NOTIFIER_WEBHOOK_URL"]
finally:
    shutil.rmtree(tmp)

# in_quiet_hours
cfg = NotifierConfig(quiet_hours_start="22:00", quiet_hours_end="08:00")
test("quiet: 23:00 在内", cfg.in_quiet_hours(datetime(2026, 1, 1, 23, 0)))
test("quiet: 06:00 在内", cfg.in_quiet_hours(datetime(2026, 1, 1, 6, 0)))
test("quiet: 12:00 不在内", not cfg.in_quiet_hours(datetime(2026, 1, 1, 12, 0)))
test("quiet: 08:00 边界外", not cfg.in_quiet_hours(datetime(2026, 1, 1, 8, 0)))
# 同日时段
cfg = NotifierConfig(quiet_hours_start="13:00", quiet_hours_end="14:00")
test("quiet: 同日 13:30 在内",
     cfg.in_quiet_hours(datetime(2026, 1, 1, 13, 30)))
test("quiet: 同日 12:00 不在内",
     not cfg.in_quiet_hours(datetime(2026, 1, 1, 12, 0)))
# 空配置 → 永远不静音
cfg = NotifierConfig()
test("quiet: 空配置永不静音",
     not cfg.in_quiet_hours(datetime(2026, 1, 1, 23, 0)))


# ──────────────────────────────────────
# 3. NoOpNotifier + 各通道 stub HTTP
# ──────────────────────────────────────
print("\n[notifier:channels]")
n = NoOpNotifier()
test("noop: send ok", n.send("t", "c"))
test("noop: 调用记录 1 条", len(n.calls) == 1)
test("noop: test_send", n.test_send())

# stub HTTP capture
calls = []
def stub_http(url, payload):
    calls.append((url, payload))
    return True

slack = SlackNotifier("https://hooks.test", http_fn=stub_http)
ok = slack.send("title", "content", "warning")
test("slack: send ok", ok)
test("slack: payload 含 attachments",
     "attachments" in calls[-1][1])
test("slack: color = 黄色 warning",
     calls[-1][1]["attachments"][0]["color"] == "#ff9900")

calls.clear()
ding = DingTalkNotifier("https://oapi.test", http_fn=stub_http)
ding.send("t", "c", "critical")
test("ding: msgtype=markdown", calls[-1][1].get("msgtype") == "markdown")
test("ding: critical prefix",
     "🚨" in calls[-1][1]["markdown"]["title"])

calls.clear()
fs = FeishuNotifier("https://feishu.test", http_fn=stub_http)
fs.send("t", "c")
test("feishu: msg_type=text", calls[-1][1].get("msg_type") == "text")
test("feishu: content.text 含 OpsAgent",
     "OpsAgent" in calls[-1][1]["content"]["text"])

# HTTP 失败不抛
def http_fail(url, payload):
    raise RuntimeError("network down")
slack = SlackNotifier("https://x", http_fn=http_fail)
test("slack: HTTP 失败返回 False", not slack.send("t", "c"))

# 空 webhook
slack = SlackNotifier("", http_fn=stub_http)
test("slack: 空 webhook → False", not slack.send("t", "c"))

# make_notifier 工厂
test("factory: slack",
     isinstance(make_notifier(NotifierConfig(type="slack")), SlackNotifier))
test("factory: dingtalk",
     isinstance(make_notifier(NotifierConfig(type="dingtalk")), DingTalkNotifier))
test("factory: feishu",
     isinstance(make_notifier(NotifierConfig(type="feishu")), FeishuNotifier))
test("factory: none → noop",
     isinstance(make_notifier(NotifierConfig(type="none")), NoOpNotifier))
try:
    make_notifier(NotifierConfig(type="weird"))
    test("factory: 未知抛异常", False)
except ValueError:
    test("factory: 未知抛异常", True)


# ──────────────────────────────────────
# 4. PolicyNotifier
# ──────────────────────────────────────
print("\n[notifier:policy]")
n = NoOpNotifier()
cfg = NotifierConfig(notify_on=["incident_opened", "pr_merged"])
pn = PolicyNotifier(n, cfg)

test("policy: 在白名单 → 发送",
     pn.maybe_notify("incident_opened", "t", "c", "info"))
test("policy: 调用记录 1 条", len(n.calls) == 1)

test("policy: 不在白名单 → 拦截",
     not pn.maybe_notify("foo", "t", "c", "info"))
test("policy: 调用仍 1 条", len(n.calls) == 1)
test("policy: dropped 记录", len(pn.dropped) == 1)

# quiet_hours info 被过滤,critical 通过
cfg = NotifierConfig(
    notify_on=["x"], quiet_hours_start="22:00", quiet_hours_end="08:00",
    quiet_except_urgency=["critical"],
)
n = NoOpNotifier()
pn = PolicyNotifier(n, cfg)
quiet_time = datetime(2026, 1, 1, 23, 0)
test("policy: quiet info 拦截",
     not pn.maybe_notify("x", "t", "c", "info", now=quiet_time))
test("policy: quiet critical 通过",
     pn.maybe_notify("x", "t", "c", "critical", now=quiet_time))


# ──────────────────────────────────────
# 5. DailyReporter
# ──────────────────────────────────────
print("\n[reporter]")
tmp = tempfile.mkdtemp()
try:
    audit = AuditLog(tmp)
    today = AuditLog._today_str()
    audit.record("incident_opened", target="web")
    audit.record("incident_closed", target="web")
    audit.record("action_executed", target="web", kind="restart")

    # 1. 回退报告(无 LLM)
    reporter = DailyReporter(audit, llm=None, notifier=None)
    report = reporter.generate(date_str=today)
    test("reporter: 回退报告非空", bool(report))
    test("reporter: 回退含日期", today in report)
    test("reporter: 回退含统计",
         "incident_opened" in report and "action_executed" in report)
    test("reporter: 回退含'回退模板'", "回退模板" in report)

    # 2. LLM 报告
    class FakeLLM:
        def __init__(self): self.calls = []
        def ask(self, prompt, **kw):
            self.calls.append(prompt)
            return "- 处理 1 个 incident\n- 重启 1 次\n- token 成本可控"
    fake = FakeLLM()
    reporter = DailyReporter(audit, llm=fake, notifier=None)
    report = reporter.generate(date_str=today)
    test("reporter: LLM 调用过", len(fake.calls) == 1)
    test("reporter: LLM 报告含 LLM 输出", "处理 1 个 incident" in report)
    test("reporter: prompt 含统计", "incident_opened" in fake.calls[0])

    # 3. LLM 失败 → 回退
    class BrokenLLM:
        def ask(self, *a, **k): raise RuntimeError("api dead")
    reporter = DailyReporter(audit, llm=BrokenLLM(), notifier=None)
    report = reporter.generate(date_str=today)
    test("reporter: LLM 异常 → 回退", "回退模板" in report)

    # 4. should_send_today / mark_sent
    reporter = DailyReporter(audit, llm=None, notifier=None,
                             marker_dir=os.path.join(tmp, "markers"))
    test("reporter: 初始 should_send=True", reporter.should_send_today())
    reporter.mark_sent()
    test("reporter: mark 后 should_send=False",
         not reporter.should_send_today())

    # 5. send_report_for via PolicyNotifier
    n = NoOpNotifier()
    cfg = NotifierConfig(notify_on=["daily_report"])
    pn = PolicyNotifier(n, cfg)
    reporter = DailyReporter(audit, llm=None, notifier=pn,
                             marker_dir=os.path.join(tmp, "markers2"))
    test("reporter: send_report_for=True", reporter.send_report_for(date_str=today))
    test("reporter: notifier 收到 1 条", len(n.calls) == 1)
    test("reporter: 标记已发", not reporter.should_send_today())
finally:
    shutil.rmtree(tmp)


# ──────────────────────────────────────
# 6. HealthServer /metrics 端点
# ──────────────────────────────────────
print("\n[health:metrics]")
import socket

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p

snap_calls = [0]
def snap():
    snap_calls[0] += 1
    return {"status": "ok"}

def metrics():
    return ("# HELP test_metric example\n"
            "# TYPE test_metric counter\n"
            "test_metric 42\n")

server = HealthServer(snapshot_fn=snap, metrics_fn=metrics)
port = free_port()
test("health: 启动", server.start(host="127.0.0.1", port=port))
try:
    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2)
    body = resp.read().decode()
    test("metrics: 200", resp.status == 200)
    test("metrics: 含 test_metric", "test_metric 42" in body)
    test("metrics: 含 HELP", "# HELP" in body)
    ct = resp.headers.get("Content-Type", "")
    test("metrics: text/plain", "text/plain" in ct)

    # /healthz 仍然可用
    resp2 = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2)
    test("metrics: /healthz 仍工作", resp2.status == 200)
finally:
    server.stop()

# 没配 metrics_fn → 404
server2 = HealthServer(snapshot_fn=snap)
port = free_port()
server2.start(host="127.0.0.1", port=port)
try:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2)
        test("metrics: 未配置 → 404", False)
    except urllib.error.HTTPError as e:
        test("metrics: 未配置 → 404", e.code == 404)
finally:
    server2.stop()


# ──────────────────────────────────────
# 7. main wiring: _emit_audit / _emit_notify / metrics rendering
# ──────────────────────────────────────
print("\n[main:wiring]")
import main as ops_main

class _Stub(ops_main.OpsAgent):
    def __init__(self, tmp):
        from infra.notebook import Notebook
        from safety.limits import LimitsEngine, LimitsConfig
        self._tmp = tmp
        self.notebook = Notebook(tmp)
        self.toolboxes = {}
        self.targets = []
        self.current_target = type("T", (), {"name": "stub-tgt"})()
        self.tools = None
        self.mode = self.PATROL
        self.readonly = False
        self.paused = False
        self.current_incident = "incident-x.md"
        self.current_issue = ""
        self._last_error_text = ""
        self.limits = LimitsEngine(LimitsConfig())
        self.start_time = time.time()
        self.last_loop_time = 0.0
        self.llm_degraded = False
        self.pending_queue = None
        self.health_server = None
        # Sprint 6 components
        from reliability.audit import AuditLog
        from infra.notifier import NotifierConfig, NoOpNotifier, PolicyNotifier
        self.audit = AuditLog(os.path.join(tmp, "audit"))
        self._noop_n = NoOpNotifier()
        self.notifier = PolicyNotifier(
            self._noop_n,
            NotifierConfig(notify_on=["pr_merged", "incident_opened"]),
        )
        self.reporter = None
        self._counter_actions = {}
        self._counter_incidents = {}

tmp = tempfile.mkdtemp()
try:
    stub = _Stub(tmp)

    # _emit_audit 写入审计日志 + 自动塞 target/incident
    stub._emit_audit("incident_opened", severity=8)
    today_events = stub.audit.read_day()
    test("wiring: audit 写入 1 条", len(today_events) == 1)
    test("wiring: 自动 target", today_events[0].get("target") == "stub-tgt")
    test("wiring: 自动 incident", today_events[0].get("incident") == "incident-x.md")
    test("wiring: 自定义字段", today_events[0].get("severity") == 8)

    # action 计数器
    stub._emit_audit("action_executed", kind="restart")
    stub._emit_audit("action_executed", kind="restart")
    stub._emit_audit("action_executed", kind="patch")
    test("wiring: 重启计数 2",
         stub._counter_actions.get(("stub-tgt", "restart")) == 2)
    test("wiring: 补丁计数 1",
         stub._counter_actions.get(("stub-tgt", "patch")) == 1)
    test("wiring: incident_opened 计数",
         stub._counter_incidents.get(("stub-tgt", "opened")) == 1)

    # _emit_notify 走 PolicyNotifier
    stub._emit_notify("pr_merged", "PR 合并", "OK", "info")
    test("wiring: notify 在白名单 → 发送", len(stub._noop_n.calls) == 1)
    stub._emit_notify("foo_bar", "x", "y")
    test("wiring: notify 不在白名单 → 拦截",
         len(stub._noop_n.calls) == 1)

    # render_prometheus_metrics
    metrics_text = stub.render_prometheus_metrics()
    test("metrics: 含 uptime",
         "ops_agent_uptime_seconds" in metrics_text)
    test("metrics: 含 mode label",
         'ops_agent_mode{mode="patrol"}' in metrics_text)
    test("metrics: 含 actions counter (restart)",
         'ops_agent_actions_total{target="stub-tgt",kind="restart"} 2' in metrics_text)
    test("metrics: 含 actions counter (patch)",
         'ops_agent_actions_total{target="stub-tgt",kind="patch"} 1' in metrics_text)
    test("metrics: 含 incidents counter",
         'ops_agent_incidents_total{target="stub-tgt",status="opened"} 1' in metrics_text)
    test("metrics: 含 llm_degraded",
         "ops_agent_llm_degraded" in metrics_text)
    test("metrics: HELP/TYPE 注释",
         "# HELP" in metrics_text and "# TYPE" in metrics_text)

    # llm_degraded 切换
    stub.llm_degraded = True
    metrics2 = stub.render_prometheus_metrics()
    test("metrics: degraded=1",
         "ops_agent_llm_degraded 1" in metrics2)

    # audit 模块缺失时静默
    stub.audit = None
    stub._emit_audit("test_event")  # 不抛
    test("wiring: audit=None 静默", True)

    # notifier 模块缺失时静默
    stub.notifier = None
    test("wiring: notifier=None 返回 False",
         not stub._emit_notify("pr_merged", "t", "c"))
finally:
    shutil.rmtree(tmp)


print()
print("=" * 40)
print(f"  通过: {PASS}    失败: {FAIL}")
print("=" * 40)
sys.exit(0 if FAIL == 0 else 1)
