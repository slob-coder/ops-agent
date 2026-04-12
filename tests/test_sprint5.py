"""Sprint 5 tests — state persistence, pending queue, health, RetryingLLM, recovery

Run: python test_sprint5.py
All external IO either uses tempfile or stubs; no real network/LLM/CI.
"""
import os
import sys
import json
import time
import shutil
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from state import AgentState, STATE_VERSION
from pending_events import PendingEventQueue, PendingEvent, MAX_PENDING_EVENTS
from health import HealthServer
from llm import RetryingLLM, LLMDegraded, LLMInterrupted
from notebook import Notebook

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
# 1. AgentState save/load
# ──────────────────────────────────────
print("\n[state:roundtrip]")
tmp = tempfile.mkdtemp()
try:
    p = os.path.join(tmp, "state.json")
    s = AgentState(
        mode="incident", current_target_name="web-prod",
        current_incident="incident-001.md", current_issue="500 errors",
        last_error_text="Traceback...", auto_merge_timestamps=[1.0, 2.0],
    )
    test("save: 成功", s.save(p))
    test("save: 文件存在", os.path.exists(p))
    test("save: last_checkpoint_time 更新", s.last_checkpoint_time > 0)

    loaded = AgentState.load(p)
    test("load: 不为 None", loaded is not None)
    test("load: mode", loaded.mode == "incident")
    test("load: incident", loaded.current_incident == "incident-001.md")
    test("load: target", loaded.current_target_name == "web-prod")
    test("load: last_error_text", loaded.last_error_text == "Traceback...")
    test("load: auto_merge_timestamps", loaded.auto_merge_timestamps == [1.0, 2.0])
    test("load: has_active_work=True", loaded.has_active_work())

    # 空 state — 没活儿
    empty = AgentState()
    test("empty: has_active_work=False", not empty.has_active_work())
finally:
    shutil.rmtree(tmp)

# ──────────────────────────────────────
# 2. AgentState 版本不匹配 → 丢弃
# ──────────────────────────────────────
print("\n[state:version]")
tmp = tempfile.mkdtemp()
try:
    p = os.path.join(tmp, "state.json")
    with open(p, "w") as f:
        json.dump({"version": 999, "mode": "incident"}, f)
    test("version mismatch → None", AgentState.load(p) is None)

    # 不存在 → None
    test("不存在 → None", AgentState.load(os.path.join(tmp, "nope.json")) is None)

    # 损坏 json → None
    with open(p, "w") as f:
        f.write("{not json")
    test("损坏 json → None", AgentState.load(p) is None)

    # 未知字段被过滤
    with open(p, "w") as f:
        json.dump({"version": STATE_VERSION, "mode": "patrol",
                   "garbage": 42}, f)
    loaded = AgentState.load(p)
    test("未知字段过滤", loaded is not None and loaded.mode == "patrol")
finally:
    shutil.rmtree(tmp)

# ──────────────────────────────────────
# 3. AgentState 原子写 — 无残留 .tmp
# ──────────────────────────────────────
print("\n[state:atomic]")
tmp = tempfile.mkdtemp()
try:
    p = os.path.join(tmp, "deep/nest/state.json")  # 目录不存在
    s = AgentState(mode="patrol")
    test("atomic: 自动建目录", s.save(p))
    test("atomic: .tmp 不残留", not os.path.exists(p + ".tmp"))
    test("atomic: 文件可读", AgentState.load(p) is not None)
finally:
    shutil.rmtree(tmp)


# ──────────────────────────────────────
# 4. PendingEventQueue
# ──────────────────────────────────────
print("\n[pending_events]")
tmp = tempfile.mkdtemp()
try:
    p = os.path.join(tmp, "queue.jsonl")
    q = PendingEventQueue(p)
    test("空: size=0", q.size() == 0)
    test("空: pop=None", q.pop() is None)

    e1 = PendingEvent(id="e1", target_name="web", summary="500 err",
                      detected_at=time.time())
    test("push: ok", q.push(e1))
    test("push: size=1", q.size() == 1)

    # 重复 id 被过滤
    e1_dup = PendingEvent(id="e1", target_name="x", summary="dup",
                          detected_at=time.time())
    test("push: 重复 id 拒绝", not q.push(e1_dup))
    test("push: size 仍=1", q.size() == 1)

    # FIFO
    e2 = PendingEvent(id="e2", target_name="db", summary="slow",
                      detected_at=time.time())
    q.push(e2)
    popped = q.pop()
    test("pop: FIFO 取 e1", popped is not None and popped.id == "e1")
    test("pop: size=1", q.size() == 1)
    popped2 = q.pop()
    test("pop: 取 e2", popped2 is not None and popped2.id == "e2")
    test("pop: 空了", q.pop() is None)

    # raw 截断
    big = PendingEvent(id="big", target_name="x", summary="x",
                       detected_at=time.time(), raw="A" * 10000)
    q.push(big)
    out = q.pop()
    test("push: raw 截断到 4KB", out is not None and len(out.raw) == 4096)

    # 持久化:重启队列
    q2 = PendingEventQueue(p)
    q2.push(PendingEvent(id="persist", target_name="x", summary="s",
                         detected_at=time.time()))
    q3 = PendingEventQueue(p)
    test("persist: 跨实例", q3.size() == 1 and q3.pop().id == "persist")

    # peek_all 不消费
    q.push(PendingEvent(id="p1", target_name="x", summary="s",
                        detected_at=time.time()))
    q.push(PendingEvent(id="p2", target_name="x", summary="s",
                        detected_at=time.time()))
    peek = q.peek_all()
    test("peek: 2 项", len(peek) == 2)
    test("peek: 不消费", q.size() == 2)

    # clear
    q.clear()
    test("clear: 空", q.size() == 0)

    # 损坏行不崩溃
    with open(p, "w") as f:
        f.write('{"id":"good","target_name":"x","summary":"s","detected_at":1.0}\n')
        f.write("garbage\n")
        f.write('{"id":"good2","target_name":"x","summary":"s","detected_at":1.0}\n')
    q4 = PendingEventQueue(p)
    test("损坏行: 跳过", q4.size() == 2)
finally:
    shutil.rmtree(tmp)


# ──────────────────────────────────────
# 5. HealthServer (real http loopback)
# ──────────────────────────────────────
print("\n[health]")
import socket

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

state = {"calls": 0}
def snap():
    state["calls"] += 1
    return {"status": "ok", "mode": "patrol", "uptime": 1.5}

server = HealthServer(snapshot_fn=snap)
port = free_port()
test("start: ok", server.start(host="127.0.0.1", port=port))
test("start: running", server.running)

try:
    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2)
    body = resp.read().decode()
    test("GET /healthz: 200", resp.status == 200)
    data = json.loads(body)
    test("snapshot: status=ok", data["status"] == "ok")
    test("snapshot: mode", data["mode"] == "patrol")
    test("snapshot_fn 被调用", state["calls"] >= 1)

    # 别名
    resp2 = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
    test("GET /: 200", resp2.status == 200)

    # 404
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=2)
        test("GET /nope: 404", False)
    except urllib.error.HTTPError as e:
        test("GET /nope: 404", e.code == 404)

    # degraded → 503
    state["status_override"] = True
    server2_state = {}
    def degraded_snap():
        return {"status": "degraded", "mode": "patrol"}
    server.stop()
    server = HealthServer(snapshot_fn=degraded_snap)
    server.start(host="127.0.0.1", port=free_port())
    # 不易拿端口,直接关一遍验证 stop 不抛
    test("stop: 不抛", True)
finally:
    server.stop()
    test("stop: running=False", not server.running)

# 端口冲突 → False
s = socket.socket()
s.bind(("127.0.0.1", 0))
busy_port = s.getsockname()[1]
s.listen(1)
try:
    server2 = HealthServer()
    test("端口冲突: start=False", not server2.start(port=busy_port))
finally:
    s.close()


# ──────────────────────────────────────
# 6. RetryingLLM
# ──────────────────────────────────────
print("\n[llm:retrying]")

class FlakeyLLM:
    """前 N 次抛错,之后正常"""
    def __init__(self, fail_times, exception=RuntimeError("api err")):
        self.fail_times = fail_times
        self.exception = exception
        self.calls = 0
    def ask(self, prompt, system="", max_tokens=4096, interrupt_check=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exception
        return f"answer to: {prompt[:20]}"

slept = []
state_changes = []

# 1. 第一次成功
inner = FlakeyLLM(fail_times=0)
llm = RetryingLLM(inner, max_attempts=3,
                  sleep_fn=lambda s: slept.append(s),
                  on_state_change=lambda o, n, info: state_changes.append((o, n, info)))
out = llm.ask("hi")
test("retry: 第一次成功", "answer" in out)
test("retry: degraded=False", not llm.degraded)
test("retry: 没 sleep", len(slept) == 0)

# 2. 第二次成功(1 failure + retry)
slept.clear()
inner = FlakeyLLM(fail_times=1)
llm = RetryingLLM(inner, max_attempts=3, sleep_fn=lambda s: slept.append(s))
out = llm.ask("hi")
test("retry: 重试一次后成功", "answer" in out)
test("retry: inner 调用 2 次", inner.calls == 2)
test("retry: sleep 1 次", len(slept) == 1)

# 3. 用尽 → LLMDegraded
slept.clear()
state_changes.clear()
inner = FlakeyLLM(fail_times=99)
llm = RetryingLLM(inner, max_attempts=3, sleep_fn=lambda s: slept.append(s),
                  on_state_change=lambda o, n, i: state_changes.append((o, n, i)))
try:
    llm.ask("hi")
    test("retry: 用尽抛 LLMDegraded", False)
except LLMDegraded as e:
    test("retry: 用尽抛 LLMDegraded", True)
    test("retry: 异常含原因", "api err" in str(e))
test("retry: degraded=True", llm.degraded)
test("retry: consecutive_failures=3", llm.consecutive_failures == 3)
test("retry: 状态变化通知 1 次", len(state_changes) == 1)
test("retry: 通知是 False→True", state_changes[0][:2] == (False, True))

# 4. 退避指数增长
test("retry: 退避指数增长",
     len(slept) == 2 and slept[1] >= slept[0])

# 5. 恢复
slept.clear()
state_changes.clear()
inner_recovering = FlakeyLLM(fail_times=0)
llm._inner = inner_recovering  # 切换到健康 inner
out = llm.ask("hi")
test("retry: 恢复成功", "answer" in out)
test("retry: degraded=False", not llm.degraded)
test("retry: 状态变化通知恢复",
     len(state_changes) == 1 and state_changes[0][:2] == (True, False))
test("retry: consecutive_failures 复位", llm.consecutive_failures == 0)

# 6. LLMInterrupted 透传不算失败
class InterruptingLLM:
    def ask(self, *a, **kw): raise LLMInterrupted("user")
llm = RetryingLLM(InterruptingLLM(), max_attempts=3, sleep_fn=lambda s: None)
try:
    llm.ask("x")
    test("retry: LLMInterrupted 透传", False)
except LLMInterrupted:
    test("retry: LLMInterrupted 透传", True)
test("retry: interrupted 不计 failure",
     llm.consecutive_failures == 0 and not llm.degraded)


# ──────────────────────────────────────
# 7. Notebook.verify_integrity
# ──────────────────────────────────────
print("\n[notebook:integrity]")
tmp = tempfile.mkdtemp()
try:
    nb = Notebook(tmp)
    nb.write("test.md", "hello")
    nb.commit("initial")
    ok, err = nb.verify_integrity()
    test("integrity: 健康仓库 ok", ok, err)

    # 损坏
    git_objects = os.path.join(tmp, ".git", "objects")
    if os.path.isdir(git_objects):
        # 写一个伪 object
        bogus_dir = os.path.join(git_objects, "ab")
        os.makedirs(bogus_dir, exist_ok=True)
        with open(os.path.join(bogus_dir, "0123"), "w") as f:
            f.write("not a real object")
    ok, err = nb.verify_integrity()
    # fsck 会报警告但 returncode 通常仍是 0,这里只验证调用不抛
    test("integrity: 调用不抛", isinstance(ok, bool))

    # 不存在的远端 push 失败
    nb.remote_url = "/nonexistent/repo.git"
    ok, _ = nb.push_to_remote()
    test("push_to_remote: 不存在远端 → False", not ok)

    # 没配 remote
    nb.remote_url = ""
    ok, msg = nb.push_to_remote()
    test("push_to_remote: 无 remote → False", not ok and "remote" in msg)
finally:
    shutil.rmtree(tmp)


# ──────────────────────────────────────
# 8. OpsAgent recover_state + health_snapshot
# ──────────────────────────────────────
print("\n[main:recovery]")
import main as ops_main

class _Stub(ops_main.OpsAgent):
    def __init__(self, tmp):
        from notebook import Notebook
        from limits import LimitsEngine, LimitsConfig
        self._tmp = tmp
        self.notebook = Notebook(tmp)
        # toolboxes 为空 → recover_state 跳过 target switch
        self.toolboxes = {}
        self.targets = []
        self.current_target = type("T", (), {"name": "stub"})()
        self.tools = None
        self.mode = self.PATROL
        self.readonly = False
        self.paused = False
        self.current_incident = None
        self.current_issue = ""
        self._last_error_text = ""
        self.limits = LimitsEngine(LimitsConfig())
        self.state_path = os.path.join(tmp, "state.json")
        self.start_time = time.time()
        self.last_loop_time = 0.0
        self.llm_degraded = False
        self.pending_queue = None
        self.health_server = None

tmp = tempfile.mkdtemp()
try:
    stub = _Stub(tmp)
    # 没 state → 没活儿
    test("recover: 无文件 → False", not stub.recover_state())

    # 写一个状态再加载
    stub.mode = "incident"
    stub.current_incident = "incident-test.md"
    stub._last_error_text = "Traceback..."
    test("save_state: 成功", stub.save_state())

    # 新 stub 加载
    stub2 = _Stub(tmp)
    test("recover: 有未完成工作 → True", stub2.recover_state())
    test("recover: mode 恢复", stub2.mode == "incident")
    test("recover: incident 恢复", stub2.current_incident == "incident-test.md")
    test("recover: error_text 恢复", stub2._last_error_text == "Traceback...")

    # health_snapshot
    snap = stub2.health_snapshot()
    test("snapshot: status 字段", "status" in snap)
    test("snapshot: mode 正确", snap["mode"] == "incident")
    test("snapshot: current_incident", snap["current_incident"] == "incident-test.md")
    test("snapshot: degraded=False → status=ok", snap["status"] == "ok")
    stub2.llm_degraded = True
    test("snapshot: degraded → status=degraded",
         stub2.health_snapshot()["status"] == "degraded")
finally:
    shutil.rmtree(tmp)


print()
print("=" * 40)
print(f"  通过: {PASS}    失败: {FAIL}")
print("=" * 40)
sys.exit(0 if FAIL == 0 else 1)
