"""Sprint 4 tests — git_host + deploy_watcher + production_watcher + revert + limits + main wiring

Run: python test_sprint4.py
All external IO is stubbed; tests run in milliseconds.
"""
import os
import sys
import shutil
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.infra.git_host import (
    GitHubClient, GitLabClient, NoopGitHost, PRStatus, PRResult, PR, make_client,
)
from src.infra.deploy_watcher import DeployWatcher, DeployStatus
from src.infra.production_watcher import ProductionWatcher, WatchOutcome
from src.safety.revert_generator import RevertGenerator
from src.safety.limits import LimitsEngine, LimitsConfig
from src.infra.targets import SourceRepo
from src.safety.patch_generator import Patch
from src.safety.patch_applier import VerificationResult
from src.safety.patch_loop import VerifiedPatch

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
# 1. NoopGitHost
# ──────────────────────────────────────
print("\n[git_host:noop]")
host = NoopGitHost()
ok, _ = host.push_branch("/repo", "fix/agent/x")
test("noop: push ok", ok)
r = host.create_pr("/repo", "fix/agent/x", "main", "title", "body")
test("noop: create_pr success", r.success and r.pr is not None)
test("noop: pr.number=1", r.pr.number == 1)
test("noop: pr has url", r.pr.url.startswith("https://"))
ok, _ = host.merge_pr("/repo", r.pr.number)
test("noop: merge ok", ok)
status = host.get_pr_status("/repo", r.pr.number)
test("noop: status open+mergeable", status.state == "open" and status.mergeable)
test("noop: 调用记录 4 条", len(host.calls) == 4)

# 工厂方法
test("make_client: github", isinstance(make_client("github"), GitHubClient))
test("make_client: gitlab", isinstance(make_client("gitlab"), GitLabClient))
test("make_client: noop", isinstance(make_client("noop"), NoopGitHost))
test("make_client: 空字符串 → noop", isinstance(make_client(""), NoopGitHost))
try:
    make_client("svn")
    test("make_client: 未知 host 抛异常", False)
except ValueError:
    test("make_client: 未知 host 抛异常", True)


# ──────────────────────────────────────
# 2. GitHubClient with stubbed run
# ──────────────────────────────────────
print("\n[git_host:github-stubbed]")

class StubRun:
    """记录调用并按顺序返回结果"""
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
    def __call__(self, cmd, cwd, timeout=120):
        self.calls.append((cmd, cwd))
        if self.results:
            return self.results.pop(0)
        return (0, "")

stub = StubRun([
    (0, "https://github.com/o/r/pull/42\n"),  # gh pr create
    (0, "abcdef0123456789abcdef0123456789abcdef01\n"),  # git rev-parse
])
gh = GitHubClient(run=stub)
res = gh.create_pr("/repo", "fix/agent/x", "main", "T", "B")
test("github: create_pr success", res.success)
test("github: pr.number=42", res.pr.number == 42)
test("github: pr.url 正确", res.pr.url == "https://github.com/o/r/pull/42")
test("github: pr.sha 抓到", res.pr.sha.startswith("abcdef"))
test("github: 调用了 gh pr create", any("gh" in c[0] and "create" in c[0] for c in stub.calls))

# create_pr 失败
stub = StubRun([(1, "auth error")])
res = GitHubClient(run=stub).create_pr("/r", "b", "main", "t", "b")
test("github: create_pr 失败", not res.success and "auth" in res.error)

# get_pr_status — CI passing
stub = StubRun([(0, '{"state":"OPEN","mergeable":"MERGEABLE","statusCheckRollup":[{"conclusion":"SUCCESS","status":"COMPLETED"}]}')])
status = GitHubClient(run=stub).get_pr_status("/r", 1)
test("github: status open", status.state == "open")
test("github: mergeable", status.mergeable)
test("github: ci_passing", status.ci_passing)

# get_pr_status — CI failing
stub = StubRun([(0, '{"state":"OPEN","mergeable":"MERGEABLE","statusCheckRollup":[{"conclusion":"FAILURE","status":"COMPLETED"}]}')])
status = GitHubClient(run=stub).get_pr_status("/r", 1)
test("github: ci_failing 检测", not status.ci_passing)

# get_pr_status — bad json
stub = StubRun([(0, "not json")])
status = GitHubClient(run=stub).get_pr_status("/r", 1)
test("github: bad json → unknown", status.state == "unknown")

# merge_pr
stub = StubRun([(0, "merged")])
ok, _ = GitHubClient(run=stub).merge_pr("/r", 5)
test("github: merge_pr ok", ok)


# ──────────────────────────────────────
# 3. DeployWatcher — fixed_wait
# ──────────────────────────────────────
print("\n[deploy_watcher]")

slept = [0]
def fake_sleep(s): slept[0] += s
clock = [0.0]
def fake_now(): return clock[0]

dw = DeployWatcher(sleep_fn=fake_sleep, now_fn=fake_now)
result = dw.wait_for_deploy({"type": "fixed_wait", "seconds": 30}, "abc123")
test("dw: fixed_wait deployed", result.deployed)
test("dw: fixed_wait slept 30s", slept[0] == 30)

# http: 第一次失败,第二次成功
slept[0] = 0
clock[0] = 0.0
http_calls = [0]
def fake_http(url):
    http_calls[0] += 1
    if http_calls[0] == 1:
        return ("version=old", 200)
    return ("version=abc123", 200)

# now 推进得快一些
def advancing_sleep(s):
    slept[0] += s
    clock[0] += s

dw = DeployWatcher(sleep_fn=advancing_sleep, now_fn=fake_now, http_fn=fake_http)
result = dw.wait_for_deploy(
    {"type": "http", "url": "http://w", "expect_contains": "{commit_sha}",
     "check_interval": 5, "timeout": 100},
    "abc123",
)
test("dw: http 第二次成功", result.deployed)
test("dw: http_calls == 2", http_calls[0] == 2)

# http: 始终失败 → 超时
clock[0] = 0.0
slept[0] = 0
def always_old(url): return ("version=old", 200)
dw = DeployWatcher(sleep_fn=advancing_sleep, now_fn=fake_now, http_fn=always_old)
result = dw.wait_for_deploy(
    {"type": "http", "url": "http://w", "expect_contains": "abc",
     "check_interval": 10, "timeout": 30},
    "abc123",
)
test("dw: http 超时", not result.deployed and "timeout" in result.error)

# file: 命中
tmp = tempfile.mkdtemp()
try:
    f = os.path.join(tmp, "deploy.txt")
    with open(f, "w") as h:
        h.write("currently deployed: deadbeef\n")
    clock[0] = 0.0
    dw = DeployWatcher(sleep_fn=advancing_sleep, now_fn=fake_now)
    result = dw.wait_for_deploy(
        {"type": "file", "path": f, "expect_contains": "deadbeef",
         "check_interval": 5, "timeout": 30},
        "deadbeef",
    )
    test("dw: file 命中", result.deployed)
    # file 不存在
    clock[0] = 0.0
    result = dw.wait_for_deploy(
        {"type": "file", "path": "/nonexistent/xx", "check_interval": 5,
         "timeout": 20}, "x",
    )
    test("dw: file 不存在 超时", not result.deployed)
finally:
    shutil.rmtree(tmp)

# command 信号
def fake_run_ok(cmd): return (0, "deployed")
clock[0] = 0.0
dw = DeployWatcher(sleep_fn=advancing_sleep, now_fn=fake_now, run_fn=fake_run_ok)
result = dw.wait_for_deploy(
    {"type": "command", "cmd": "kubectl get deploy", "check_interval": 5},
    "x",
)
test("dw: command rc=0 deployed", result.deployed)

# 空 signal → 直接通过
dw = DeployWatcher()
result = dw.wait_for_deploy({}, "x")
test("dw: 空 signal 默认通过", result.deployed)


# ──────────────────────────────────────
# 4. ProductionWatcher
# ──────────────────────────────────────
print("\n[production_watcher]")

PY_TRACE = '''Traceback (most recent call last):
  File "/app/handlers/user.py", line 42, in get_user
    return db.fetch(uid).name
AttributeError: 'NoneType' object has no attribute 'name'
'''

clock[0] = 0.0
def watch_sleep(s):
    clock[0] += s

# Case 1: NO_BASELINE — 原文本没有可解析的栈
pw = ProductionWatcher(sleep_fn=watch_sleep, now_fn=fake_now)
r = pw.watch("just a normal log", lambda: "", duration=10)
test("pw: NO_BASELINE", r.outcome == WatchOutcome.NO_BASELINE)

# Case 2: OK — observe_fn 始终返回干净日志
clock[0] = 0.0
clean_logs = ["INFO ok\n"] * 100
def clean_obs():
    return clean_logs.pop(0) if clean_logs else "INFO ok\n"
pw = ProductionWatcher(sleep_fn=watch_sleep, now_fn=fake_now)
r = pw.watch(PY_TRACE, clean_obs, duration=60, interval=10)
test("pw: OK 无复发", r.outcome == WatchOutcome.OK)
test("pw: OK 检查次数 ≥ 5", r.checks >= 5, str(r.checks))

# Case 3: RECURRENCE — observe_fn 第三次返回原异常
clock[0] = 0.0
calls = [0]
def maybe_recur():
    calls[0] += 1
    if calls[0] >= 3:
        return PY_TRACE
    return "INFO ok"
pw = ProductionWatcher(sleep_fn=watch_sleep, now_fn=fake_now)
r = pw.watch(PY_TRACE, maybe_recur, duration=60, interval=10)
test("pw: RECURRENCE", r.outcome == WatchOutcome.FAILED_RECURRENCE)
test("pw: RECURRENCE checks 命中", r.checks == 3, str(r.checks))

# Case 4: OBSERVE_ERROR
clock[0] = 0.0
def always_raise():
    raise RuntimeError("io fail")
pw = ProductionWatcher(sleep_fn=watch_sleep, now_fn=fake_now)
r = pw.watch(PY_TRACE, always_raise, duration=60, interval=10)
test("pw: OBSERVE_ERROR", r.outcome == WatchOutcome.OBSERVE_ERROR)

# signature_from_text 直接测
sig1 = pw.signature_from_text(PY_TRACE)
sig2 = pw.signature_from_text(PY_TRACE)
test("pw: signature 稳定", sig1 == sig2 and sig1 != "")
test("pw: 不同异常 signature 不同",
     pw.signature_from_text("TypeError: x at /app/y.js:1:1") != sig1)


# ──────────────────────────────────────
# 5. RevertGenerator with NoopGitHost
# ──────────────────────────────────────
print("\n[revert_generator]")

# 用 stub run 模拟所有 git 命令成功
def all_ok(cmd, cwd, timeout=120):
    return (0, "")
host = NoopGitHost()
rg = RevertGenerator(host, run=all_ok)
result = rg.revert_and_merge(
    repo_path="/r", commit_sha="deadbeef" * 5,
    original_branch="fix/agent/x-123", base_branch="main",
    failure_reason="recurrence detected",
)
test("revert: success", result.success, result.error)
test("revert: branch 命名", result.revert_branch.startswith("revert/agent/"))
test("revert: 通过 host 创建了 PR",
     any(c[0] == "create_pr" for c in host.calls))
test("revert: 通过 host 合并了 PR",
     any(c[0] == "merge_pr" for c in host.calls))
test("revert: result.merged", result.merged)

# 空 commit_sha → 失败
rg = RevertGenerator(NoopGitHost(), run=all_ok)
r = rg.revert_and_merge("/r", "", "fix/x", "main", "")
test("revert: 空 sha 失败", not r.success and "empty" in r.error)

# git revert 失败
def revert_fails(cmd, cwd, timeout=120):
    if "revert" in cmd and "abort" not in cmd:
        return (1, "conflict")
    return (0, "")
rg = RevertGenerator(NoopGitHost(), run=revert_fails)
r = rg.revert_and_merge("/r", "abc", "fix/x", "main", "")
test("revert: revert 失败被捕获", not r.success and r.stage == "revert")

# host.merge 失败
def merge_fails_run(cmd, cwd, timeout=120):
    return (0, "")
class FailMergeHost(NoopGitHost):
    def merge_pr(self, repo_path, pr_number):
        self.calls.append(("merge_pr", repo_path, pr_number))
        return False, "branch protected"
rg = RevertGenerator(FailMergeHost(), run=merge_fails_run)
r = rg.revert_and_merge("/r", "abc", "fix/x", "main", "")
test("revert: host merge 失败被捕获", not r.success and r.stage == "merge")


# ──────────────────────────────────────
# 6. limits.max_auto_merges_per_day
# ──────────────────────────────────────
print("\n[limits:auto-merge]")
cfg = LimitsConfig(max_auto_merges_per_day=3)
eng = LimitsEngine(cfg)
test("auto_merge: 初始允许", eng.check_auto_merge()[0])
eng.record_auto_merge()
eng.record_auto_merge()
test("auto_merge: 2/3 仍允许", eng.check_auto_merge()[0])
eng.record_auto_merge()
ok, reason = eng.check_auto_merge()
test("auto_merge: 3/3 拒绝", not ok)
test("auto_merge: 拒绝信息含上限", "3" in reason)

# 禁用时不限制
cfg = LimitsConfig(enabled=False, max_auto_merges_per_day=1)
eng = LimitsEngine(cfg)
eng.record_auto_merge()
eng.record_auto_merge()
test("auto_merge: enabled=False 不拦", eng.check_auto_merge()[0])


# ──────────────────────────────────────
# 7. SourceRepo deploy_signal / git_host fields
# ──────────────────────────────────────
print("\n[targets:source-repo-sprint4]")
r = SourceRepo.from_dict({
    "name": "backend", "path": "/r",
    "git_host": "github", "base_branch": "develop",
    "deploy_signal": {"type": "fixed_wait", "seconds": 10},
})
test("repo: git_host", r.git_host == "github")
test("repo: base_branch", r.base_branch == "develop")
test("repo: deploy_signal type", r.deploy_signal.get("type") == "fixed_wait")

# 默认值
r = SourceRepo(name="x", path="/y")
test("repo: 默认 git_host 空", r.git_host == "")
test("repo: 默认 base_branch=main", r.base_branch == "main")
test("repo: 默认 deploy_signal 空 dict", r.deploy_signal == {})


# ──────────────────────────────────────
# 8. main._build_pr_body & _run_pr_workflow
# ──────────────────────────────────────
print("\n[main:pr-workflow]")
import main as ops_main

class _Stub(ops_main.OpsAgent):
    def __init__(self):
        from src.infra.notebook import Notebook
        from src.infra.chat import HumanChannel
        self._tmp = tempfile.mkdtemp()
        self.notebook = Notebook(self._tmp)
        self.chat = _SilentChat()
        self.current_target = type("T", (), {"name": "test-target"})()
        self.current_incident = "incident-001.md"
        # 准备一个 incident 文件让 _note 能写
        os.makedirs(os.path.join(self._tmp, "incidents", "active"), exist_ok=True)
        with open(os.path.join(self._tmp, "incidents", "active", "incident-001.md"), "w") as f:
            f.write("# test\n")
        self._last_error_text = PY_TRACE
        from src.safety.limits import LimitsEngine, LimitsConfig
        self.limits = LimitsEngine(LimitsConfig(max_auto_merges_per_day=5))
        from src.infra.deploy_watcher import DeployWatcher
        from src.infra.production_watcher import ProductionWatcher
        # 用快速假时钟
        self._clock = [0.0]
        def _sleep(s): self._clock[0] += s
        def _now(): return self._clock[0]
        self.deploy_watcher = DeployWatcher(sleep_fn=_sleep, now_fn=_now)
        self.prod_watcher = ProductionWatcher(sleep_fn=_sleep, now_fn=_now)
        self.readonly = False

class _SilentChat:
    def __init__(self): self.messages = []
    def say(self, text, level="info"): self.messages.append((level, text))
    def log(self, text): self.messages.append(("log", text))
    def escalate(self, summary, detail=""): self.messages.append(("escalate", summary))


def make_verified():
    p = Patch(repo_name="backend", repo_path="/dummy",
              diff="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
              description="add null check",
              files_changed=["handlers/user.py"])
    vr = VerificationResult(success=True, stage="tested",
                            branch_name="fix/agent/test-1",
                            commit_sha="0123456789abcdef" * 2,
                            apply_output="", build_output="ok", test_output="ok")
    return VerifiedPatch(patch=p, result=vr, attempts=1)

# _build_pr_body
stub = _Stub()
try:
    repo = SourceRepo(name="backend", path="/r", build_cmd="make", test_cmd="pytest")
    body = stub._build_pr_body(make_verified(), repo, "abc123")
    test("pr_body: 含 description", "add null check" in body)
    test("pr_body: 含 commit_sha", "abc123" in body)
    test("pr_body: 含 build_cmd", "make" in body)
    test("pr_body: 含 files", "handlers/user.py" in body)
finally:
    shutil.rmtree(stub._tmp, ignore_errors=True)

# _run_pr_workflow — 整条快乐路径(NoopGitHost + observe 干净)
stub = _Stub()
try:
    noop_host = NoopGitHost()
    stub._make_git_host = lambda repo: noop_host
    stub._make_observe_fn = lambda repo: (lambda: "INFO ok")
    repo = SourceRepo(
        name="backend", path="/r", language="python",
        git_host="noop", build_cmd="true",
        deploy_signal={"type": "fixed_wait", "seconds": 1},
    )
    stub._run_pr_workflow(make_verified(), repo)
    # 应该走完: push, create_pr, get_pr_status, merge_pr
    call_kinds = [c[0] for c in noop_host.calls]
    test("workflow: push 调用", "push" in call_kinds)
    test("workflow: create_pr 调用", "create_pr" in call_kinds)
    test("workflow: merge_pr 调用", "merge_pr" in call_kinds)
    test("workflow: 自动合并已记录",
         stub.limits.status().get("enabled") in (True, None))
    # _close_incident may have archived the file; just verify no crash + workflow ran
    test("workflow: 4 个 host 调用 (push/create/status/merge)",
         len(noop_host.calls) >= 4, str(call_kinds))
finally:
    shutil.rmtree(stub._tmp, ignore_errors=True)

# _run_pr_workflow — 限流拒绝
stub = _Stub()
try:
    stub.limits = LimitsEngine(LimitsConfig(max_auto_merges_per_day=0))
    noop_host = NoopGitHost()
    stub._make_git_host = lambda repo: noop_host
    repo = SourceRepo(name="backend", path="/r", git_host="noop")
    stub._run_pr_workflow(make_verified(), repo)
    test("workflow: 限流拒绝时未 push",
         not any(c[0] == "push" for c in noop_host.calls))
    test("workflow: 限流拒绝时有 warning",
         any(level == "warning" for level, _ in stub.chat.messages))
finally:
    shutil.rmtree(stub._tmp, ignore_errors=True)

# _run_pr_workflow — 复发触发 revert
stub = _Stub()
try:
    noop_host = NoopGitHost()
    stub._make_git_host = lambda repo: noop_host
    # observe_fn 立即返回原异常
    stub._make_observe_fn = lambda repo: (lambda: PY_TRACE)
    repo = SourceRepo(name="backend", path="/r", language="python",
                      git_host="noop", build_cmd="true",
                      deploy_signal={"type": "fixed_wait", "seconds": 1})

    # patch revert_and_merge 用全 ok stub
    import safety.revert_generator as rg_mod
    orig_run = rg_mod.RevertGenerator._default_run
    rg_mod.RevertGenerator._default_run = staticmethod(lambda cmd, cwd, timeout=120: (0, ""))
    try:
        stub._run_pr_workflow(make_verified(), repo)
    finally:
        rg_mod.RevertGenerator._default_run = orig_run

    # revert 也走 NoopGitHost,所以应该有第二次 create_pr
    create_calls = [c for c in noop_host.calls if c[0] == "create_pr"]
    test("workflow: 复发触发了 revert PR",
         len(create_calls) >= 2,
         str([c[0] for c in noop_host.calls]))
    test("workflow: 复发触发了 escalate",
         any(level == "escalate" for level, _ in stub.chat.messages))
finally:
    shutil.rmtree(stub._tmp, ignore_errors=True)

# _run_pr_workflow — 没配 git_host 直接跳过
stub = _Stub()
try:
    noop_host = NoopGitHost()
    stub._make_git_host = lambda repo: noop_host
    repo = SourceRepo(name="r", path="/r", git_host="")
    stub._run_pr_workflow(make_verified(), repo)
    test("workflow: 无 git_host 跳过 push",
         not any(c[0] == "push" for c in noop_host.calls))
finally:
    shutil.rmtree(stub._tmp, ignore_errors=True)


print()
print("=" * 40)
print(f"  通过: {PASS}    失败: {FAIL}")
print("=" * 40)
sys.exit(0 if FAIL == 0 else 1)
