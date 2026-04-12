"""Sprint 3 tests — patch generator + applier + loop

运行: python test_sprint3.py
使用真实 git 在临时目录;LLM 用 fake stub。
"""
import os
import sys
import shutil
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from patch_generator import PatchGenerator, Patch
from patch_applier import PatchApplier, VerificationResult
from patch_loop import PatchLoop
from targets import SourceRepo
from stack_parser import StackFrame

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


def make_git_repo(files: dict) -> str:
    """创建一个临时 git repo,内容为 {relpath: content}"""
    d = tempfile.mkdtemp(prefix="opsagent-test-")
    for rel, content in files.items():
        full = os.path.join(d, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "test"
    env["GIT_AUTHOR_EMAIL"] = "test@test"
    env["GIT_COMMITTER_NAME"] = "test"
    env["GIT_COMMITTER_EMAIL"] = "test@test"
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=d, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True, env=env)
    subprocess.run(
        ["git", "-c", "user.email=test@test", "-c", "user.name=test",
         "commit", "-q", "-m", "init"],
        cwd=d, check=True, env=env,
    )
    return d


class FakeLLM:
    """按队列返回响应的假 LLM"""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def ask(self, prompt, system="", max_tokens=4096, **kw):
        self.calls.append(prompt)
        if not self.responses:
            return ""
        return self.responses.pop(0)


# ─────────────────────────────────────
# 1. Patch dataclass
# ─────────────────────────────────────
print("\n[Patch dataclass]")
p = Patch(repo_name="r", repo_path="/r", diff="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
          files_changed=["x"])
test("is_valid: 合法 diff", p.is_valid())
test("is_valid: 空 diff", not Patch("r", "/r", "").is_valid())
test("is_valid: 无 @@", not Patch("r", "/r", "--- a/x\n+++ b/x\nblah").is_valid())
test("touches_only_tests: 普通文件", not p.touches_only_tests())
test("touches_only_tests: test_x.py",
     Patch("r", "/r", "x", files_changed=["test_foo.py"]).touches_only_tests())
test("touches_only_tests: tests/ 目录",
     Patch("r", "/r", "x", files_changed=["src/tests/util.py"]).touches_only_tests())
test("touches_only_tests: 混合不算",
     not Patch("r", "/r", "x",
               files_changed=["test_x.py", "src/main.py"]).touches_only_tests())
test("touches_only_tests: foo_test.go",
     Patch("r", "/r", "x", files_changed=["pkg/foo_test.go"]).touches_only_tests())

# ─────────────────────────────────────
# 2. PatchGenerator.parse_response
# ─────────────────────────────────────
print("\n[PatchGenerator.parse_response]")
gen = PatchGenerator(llm=FakeLLM([]))

GOOD_RESP = """## 修改说明
Add a None check for the user lookup so handlers don't crash on missing users.

## 修改的文件
- handlers/user.py

## Diff
```diff
--- a/handlers/user.py
+++ b/handlers/user.py
@@ -1,3 +1,5 @@
 def get_user(uid):
     u = db.fetch(uid)
+    if u is None:
+        return None
     return u.name
```
"""

repo = SourceRepo(name="backend", path="/tmp/x", language="python")
patch = gen.parse_response(GOOD_RESP, repo)
test("parse: 返回 Patch", patch is not None)
test("parse: repo_name", patch and patch.repo_name == "backend")
test("parse: files_changed 抽出", patch and patch.files_changed == ["handlers/user.py"])
test("parse: description 抽出",
     patch and "None check" in patch.description)
test("parse: diff 含 @@", patch and "@@" in patch.diff)
test("parse: is_valid", patch and patch.is_valid())

# 没有 diff 块
test("parse: 空响应 → None", gen.parse_response("", repo) is None)
test("parse: 无 diff 块 → None",
     gen.parse_response("## 修改说明\njust talk", repo) is None)
test("parse: diff 没有 @@ → None",
     gen.parse_response("```diff\n--- a/x\n+++ b/x\nhi\n```", repo) is None)

# diff 块没标签
no_tag = "## 修改说明\nfix\n```\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```\n"
test("parse: 无 diff 标签也接受", gen.parse_response(no_tag, repo) is not None)

# ─────────────────────────────────────
# 3. PatchApplier — 成功路径
# ─────────────────────────────────────
print("\n[PatchApplier:happy-path]")
SRC_BUGGY = "def get_user(uid):\n    u = db.fetch(uid)\n    return u.name\n"
SRC_FIXED = ("def get_user(uid):\n    u = db.fetch(uid)\n"
             "    if u is None:\n        return None\n    return u.name\n")
DIFF_OK = (
    "--- a/handlers/user.py\n"
    "+++ b/handlers/user.py\n"
    "@@ -1,3 +1,5 @@\n"
    " def get_user(uid):\n"
    "     u = db.fetch(uid)\n"
    "+    if u is None:\n"
    "+        return None\n"
    "     return u.name\n"
)

repo_dir = make_git_repo({
    "handlers/user.py": SRC_BUGGY,
    "build.sh": "#!/bin/sh\npython -m py_compile handlers/user.py\n",
    "test.sh": "#!/bin/sh\npython -c 'import ast; ast.parse(open(\"handlers/user.py\").read())'\n",
})
try:
    repo = SourceRepo(
        name="backend", path=repo_dir, language="python",
        build_cmd="sh build.sh", test_cmd="sh test.sh",
    )
    patch = Patch(repo_name="backend", repo_path=repo_dir, diff=DIFF_OK,
                  description="add None check", files_changed=["handlers/user.py"])
    app = PatchApplier()
    result = app.apply_and_verify(patch, repo, incident_id="test1")
    test("apply: success=True", result.success, result.error_message)
    test("apply: stage=tested", result.stage == "tested", result.stage)
    test("apply: branch 名带前缀",
         result.branch_name.startswith("fix/agent/"))
    test("apply: commit_sha 非空", len(result.commit_sha) >= 7)
    # 文件内容已修改
    with open(os.path.join(repo_dir, "handlers/user.py")) as f:
        new_content = f.read()
    test("apply: 文件确实修改了", "if u is None" in new_content)
finally:
    shutil.rmtree(repo_dir)

# ─────────────────────────────────────
# 4. PatchApplier — build 失败 → 完整回滚
# ─────────────────────────────────────
print("\n[PatchApplier:build-fail-rollback]")
DIFF_BAD_SYNTAX = (
    "--- a/handlers/user.py\n"
    "+++ b/handlers/user.py\n"
    "@@ -1,3 +1,3 @@\n"
    " def get_user(uid):\n"
    "-    u = db.fetch(uid)\n"
    "+    u = db.fetch(uid  # 故意语法错\n"
    "     return u.name\n"
)
repo_dir = make_git_repo({
    "handlers/user.py": SRC_BUGGY,
    "build.sh": "#!/bin/sh\npython -m py_compile handlers/user.py\n",
})
try:
    repo = SourceRepo(name="backend", path=repo_dir, language="python",
                      build_cmd="sh build.sh")
    patch = Patch(repo_name="backend", repo_path=repo_dir, diff=DIFF_BAD_SYNTAX,
                  files_changed=["handlers/user.py"])
    result = PatchApplier().apply_and_verify(patch, repo, incident_id="t2")
    test("build-fail: success=False", not result.success)
    test("build-fail: stage=failed-at-build", result.stage == "failed-at-build", result.stage)
    test("build-fail: build_output 非空", bool(result.build_output))
    # 文件已回滚
    with open(os.path.join(repo_dir, "handlers/user.py")) as f:
        content = f.read()
    test("build-fail: 文件回滚到原始", content == SRC_BUGGY)
    # 分支已删除
    rc, branches = subprocess.run(["git", "branch"], cwd=repo_dir,
                                   capture_output=True, text=True).returncode, ""
    branches = subprocess.run(["git", "branch"], cwd=repo_dir,
                              capture_output=True, text=True).stdout
    test("build-fail: 分支已删除", "fix/agent" not in branches, branches)
    # 工作区干净
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir,
                            capture_output=True, text=True).stdout
    test("build-fail: 工作区干净", status == "", repr(status))
finally:
    shutil.rmtree(repo_dir)

# ─────────────────────────────────────
# 5. PatchApplier — test 失败 → 回滚
# ─────────────────────────────────────
print("\n[PatchApplier:test-fail-rollback]")
repo_dir = make_git_repo({
    "handlers/user.py": SRC_BUGGY,
    "build.sh": "#!/bin/sh\ntrue\n",
    "test.sh": "#!/bin/sh\nfalse\n",   # 永远失败
})
try:
    repo = SourceRepo(name="backend", path=repo_dir, language="python",
                      build_cmd="sh build.sh", test_cmd="sh test.sh")
    patch = Patch(repo_name="backend", repo_path=repo_dir, diff=DIFF_OK,
                  files_changed=["handlers/user.py"])
    result = PatchApplier().apply_and_verify(patch, repo, incident_id="t3")
    test("test-fail: success=False", not result.success)
    test("test-fail: stage=failed-at-test", result.stage == "failed-at-test")
    with open(os.path.join(repo_dir, "handlers/user.py")) as f:
        content = f.read()
    test("test-fail: 文件回滚", content == SRC_BUGGY)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir,
                            capture_output=True, text=True).stdout
    test("test-fail: 工作区干净", status == "")
finally:
    shutil.rmtree(repo_dir)

# ─────────────────────────────────────
# 6. PatchApplier — diff 不能 apply
# ─────────────────────────────────────
print("\n[PatchApplier:apply-fail]")
DIFF_BROKEN = (
    "--- a/handlers/user.py\n"
    "+++ b/handlers/user.py\n"
    "@@ -1,3 +1,3 @@\n"
    " this line does not exist\n"
    "-also missing\n"
    "+replacement\n"
)
repo_dir = make_git_repo({"handlers/user.py": SRC_BUGGY,
                          "build.sh": "#!/bin/sh\ntrue\n"})
try:
    repo = SourceRepo(name="backend", path=repo_dir, language="python",
                      build_cmd="sh build.sh")
    patch = Patch(repo_name="backend", repo_path=repo_dir, diff=DIFF_BROKEN,
                  files_changed=["handlers/user.py"])
    result = PatchApplier().apply_and_verify(patch, repo, incident_id="t4")
    test("apply-fail: success=False", not result.success)
    test("apply-fail: stage=failed-at-apply", result.stage == "failed-at-apply", result.stage)
    branches = subprocess.run(["git", "branch"], cwd=repo_dir,
                              capture_output=True, text=True).stdout
    test("apply-fail: 分支已清理", "fix/agent" not in branches)
finally:
    shutil.rmtree(repo_dir)

# ─────────────────────────────────────
# 7. PatchApplier — 测试文件作弊守卫
# ─────────────────────────────────────
print("\n[PatchApplier:cheat-guard]")
repo_dir = make_git_repo({"test_x.py": "def test_a(): assert True\n",
                          "build.sh": "#!/bin/sh\ntrue\n"})
try:
    repo = SourceRepo(name="r", path=repo_dir, language="python",
                      build_cmd="sh build.sh")
    bogus = Patch(repo_name="r", repo_path=repo_dir,
                  diff="--- a/test_x.py\n+++ b/test_x.py\n@@ -1 +1 @@\n-x\n+y\n",
                  files_changed=["test_x.py"])
    result = PatchApplier().apply_and_verify(bogus, repo, incident_id="t5")
    test("cheat-guard: 拒绝", not result.success)
    test("cheat-guard: 提示信息", "test files" in result.error_message.lower()
         or "cheating" in result.error_message.lower())
finally:
    shutil.rmtree(repo_dir)

# ─────────────────────────────────────
# 8. PatchApplier — 没配 test_cmd 也能成功
# ─────────────────────────────────────
print("\n[PatchApplier:no-test-cmd]")
repo_dir = make_git_repo({
    "handlers/user.py": SRC_BUGGY,
    "build.sh": "#!/bin/sh\npython -m py_compile handlers/user.py\n",
})
try:
    repo = SourceRepo(name="backend", path=repo_dir, language="python",
                      build_cmd="sh build.sh", test_cmd="")
    patch = Patch(repo_name="backend", repo_path=repo_dir, diff=DIFF_OK,
                  files_changed=["handlers/user.py"])
    result = PatchApplier().apply_and_verify(patch, repo, incident_id="t6")
    test("no-test-cmd: success", result.success, result.error_message)
    test("no-test-cmd: stage=built", result.stage == "built")
finally:
    shutil.rmtree(repo_dir)

# ─────────────────────────────────────
# 9. PatchLoop — 第一次成功
# ─────────────────────────────────────
print("\n[PatchLoop:first-try-success]")
repo_dir = make_git_repo({
    "handlers/user.py": SRC_BUGGY,
    "build.sh": "#!/bin/sh\npython -m py_compile handlers/user.py\n",
})
try:
    repo = SourceRepo(name="backend", path=repo_dir, language="python",
                      build_cmd="sh build.sh")
    fake_llm = FakeLLM([
        f"## 修改说明\nfix\n## 修改的文件\n- handlers/user.py\n## Diff\n```diff\n{DIFF_OK}```\n"
    ])
    loop = PatchLoop(PatchGenerator(fake_llm), PatchApplier(),
                     logger_fn=lambda m: None)
    # 假 location 对象
    class FakeLoc:
        def render(self): return "code: ..."
    locs = [FakeLoc()]
    verified = loop.run({"type": "code_bug", "hypothesis": "null"},
                        locs, repo, incident_id="loop1")
    test("loop: 成功返回 VerifiedPatch", verified is not None)
    test("loop: attempts==1", verified and verified.attempts == 1)
    test("loop: result.success", verified and verified.result.success)
    test("loop: LLM 只调用 1 次", len(fake_llm.calls) == 1)
finally:
    shutil.rmtree(repo_dir)

# ─────────────────────────────────────
# 10. PatchLoop — 第一次失败,第二次成功(重试)
# ─────────────────────────────────────
print("\n[PatchLoop:retry-success]")
repo_dir = make_git_repo({
    "handlers/user.py": SRC_BUGGY,
    "build.sh": "#!/bin/sh\npython -m py_compile handlers/user.py\n",
})
try:
    repo = SourceRepo(name="backend", path=repo_dir, language="python",
                      build_cmd="sh build.sh")
    fake_llm = FakeLLM([
        # 第一次:不可解析的 garbage
        "this is just text without any diff",
        # 第二次:正确的
        f"## 修改说明\nfix\n## 修改的文件\n- handlers/user.py\n## Diff\n```diff\n{DIFF_OK}```\n",
    ])
    loop = PatchLoop(PatchGenerator(fake_llm), PatchApplier(),
                     logger_fn=lambda m: None)
    class FakeLoc:
        def render(self): return "x"
    verified = loop.run({"type": "code_bug"}, [FakeLoc()], repo, "loop2")
    test("retry: 成功", verified is not None)
    test("retry: attempts==2", verified and verified.attempts == 2)
    test("retry: LLM 调用 2 次", len(fake_llm.calls) == 2)
    # 第二次 prompt 应该带上 retry_context
    test("retry: 第二次 prompt 含失败提示",
         "无法解析" in fake_llm.calls[1] or "diff" in fake_llm.calls[1].lower())
finally:
    shutil.rmtree(repo_dir)

# ─────────────────────────────────────
# 11. PatchLoop — 三次都失败 → None
# ─────────────────────────────────────
print("\n[PatchLoop:exhausted]")
repo_dir = make_git_repo({
    "handlers/user.py": SRC_BUGGY,
    "build.sh": "#!/bin/sh\nexit 1\n",  # 总是失败
})
try:
    repo = SourceRepo(name="backend", path=repo_dir, language="python",
                      build_cmd="sh build.sh")
    good_resp = (
        f"## 修改说明\nfix\n## 修改的文件\n- handlers/user.py\n"
        f"## Diff\n```diff\n{DIFF_OK}```\n"
    )
    fake_llm = FakeLLM([good_resp, good_resp, good_resp])
    loop = PatchLoop(PatchGenerator(fake_llm), PatchApplier(),
                     logger_fn=lambda m: None)
    class FakeLoc:
        def render(self): return "x"
    verified = loop.run({"type": "code_bug"}, [FakeLoc()], repo, "loop3")
    test("exhausted: 返回 None", verified is None)
    test("exhausted: LLM 调用 3 次", len(fake_llm.calls) == 3)
    # 工作区仍然干净
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir,
                            capture_output=True, text=True).stdout
    test("exhausted: 工作区干净", status == "")
finally:
    shutil.rmtree(repo_dir)

# ─────────────────────────────────────
# 12. PatchLoop — 防御性参数
# ─────────────────────────────────────
print("\n[PatchLoop:defensive]")
loop = PatchLoop(PatchGenerator(FakeLLM([])), PatchApplier(),
                 logger_fn=lambda m: None)
test("loop: 无 repo 返回 None", loop.run({"type": "code_bug"}, [object()], None) is None)
test("loop: 无 locations 返回 None", loop.run({"type": "code_bug"}, [], object()) is None)

# ─────────────────────────────────────
# 13. main._parse_diagnosis — type 字段
# ─────────────────────────────────────
print("\n[main._parse_diagnosis:type]")
import main as ops_main

# 用一个最小的 OpsAgent 来调 _parse_diagnosis(它是纯函数无副作用)
class _Stub(ops_main.OpsAgent):
    def __init__(self):
        pass
stub = _Stub()

resp1 = """### 1. 现象
something

### 6. 类型
code_bug
"""
d = stub._parse_diagnosis(resp1)
test("parse_diagnosis: type=code_bug", d["type"] == "code_bug", d.get("type"))

resp2 = """### 6. 类型
resource
"""
d = stub._parse_diagnosis(resp2)
test("parse_diagnosis: type=resource", d["type"] == "resource")

resp3 = """### 1. 现象
something
"""
d = stub._parse_diagnosis(resp3)
test("parse_diagnosis: 缺省 unknown", d["type"] == "unknown")


print()
print("=" * 40)
print(f"  通过: {PASS}    失败: {FAIL}")
print("=" * 40)
sys.exit(0 if FAIL == 0 else 1)
