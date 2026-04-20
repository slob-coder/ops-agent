"""Sprint 2 tests — stack parser + source locator

每项测试独立,使用 tempfile 隔离。运行: python test_sprint2.py
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.repair.stack_parser import StackTraceParser, StackFrame
from src.repair.source_locator import SourceLocator
from src.infra.targets import SourceRepo, Target

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


# ─────────────────────────────────────
# 1. stack parser - python
# ─────────────────────────────────────
print("\n[stack_parser:python]")
PY_TRACE = '''Traceback (most recent call last):
  File "/app/server.py", line 17, in handle
    user = get_user(uid)
  File "/app/handlers/user.py", line 42, in get_user
    return db.fetch(uid).name
AttributeError: 'NoneType' object has no attribute 'name'
'''
p = StackTraceParser().parse(PY_TRACE)
test("python: 语言识别", p.language == "python")
test("python: 帧数 == 2", len(p.frames) == 2)
test("python: 顶层是 user.py", p.frames[0].file == "/app/handlers/user.py", str(p.frames[0]))
test("python: 顶层行号 42", p.frames[0].line == 42)
test("python: 顶层函数 get_user", p.frames[0].function == "get_user")
test("python: 第二帧 server.py", p.frames[1].file == "/app/server.py")
test("python: exception_type", p.exception_type == "AttributeError")
test("python: exception_message 含 NoneType", "NoneType" in p.exception_message)
test("python: signature 非空", bool(p.signature()))

# ─────────────────────────────────────
# 2. stack parser - java
# ─────────────────────────────────────
print("\n[stack_parser:java]")
JAVA_TRACE = '''Exception in thread "main" java.lang.NullPointerException: user
\tat com.example.UserService.getProfile(UserService.java:85)
\tat com.example.ApiController.handle(ApiController.java:42)
\tat com.example.Main.main(Main.java:10)
'''
p = StackTraceParser().parse(JAVA_TRACE)
test("java: 语言识别", p.language == "java")
test("java: 帧数 == 3", len(p.frames) == 3, str(len(p.frames)))
test("java: 顶层 UserService.java:85", p.frames[0].file == "UserService.java" and p.frames[0].line == 85)
test("java: 顶层 method", p.frames[0].function == "getProfile")
test("java: module 含 com.example", "com.example" in p.frames[0].module)
test("java: exception_type", p.exception_type == "NullPointerException")

# ─────────────────────────────────────
# 3. stack parser - go
# ─────────────────────────────────────
print("\n[stack_parser:go]")
GO_TRACE = '''panic: runtime error: invalid memory address or nil pointer dereference
[signal SIGSEGV: segmentation violation]

goroutine 1 [running]:
main.handleRequest(0xc0001020e0)
\t/app/main.go:42 +0x1a
main.main()
\t/app/main.go:10 +0x55
'''
p = StackTraceParser().parse(GO_TRACE)
test("go: 语言识别", p.language == "go")
test("go: 至少 2 帧", len(p.frames) >= 2, str(len(p.frames)))
test("go: 顶层 main.go:42", p.frames[0].file == "/app/main.go" and p.frames[0].line == 42)
test("go: exception_type panic", p.exception_type == "panic")

# ─────────────────────────────────────
# 4. stack parser - node
# ─────────────────────────────────────
print("\n[stack_parser:node]")
NODE_TRACE = '''TypeError: Cannot read property 'name' of null
    at getUser (/app/handlers/user.js:42:15)
    at processRequest (/app/index.js:17:8)
    at /app/middleware.js:5:3
'''
p = StackTraceParser().parse(NODE_TRACE)
test("node: 语言识别", p.language == "node")
test("node: 帧数 >= 2", len(p.frames) >= 2)
test("node: 顶层 user.js:42", p.frames[0].file == "/app/handlers/user.js" and p.frames[0].line == 42)
test("node: 顶层 函数 getUser", p.frames[0].function == "getUser")
test("node: exception_type TypeError", p.exception_type == "TypeError")

# ─────────────────────────────────────
# 5. parser 宽容性
# ─────────────────────────────────────
print("\n[stack_parser:robustness]")
test("空字符串", len(StackTraceParser().parse("").frames) == 0)
test("无栈日志", len(StackTraceParser().parse("just a normal log line").frames) == 0)
test("乱码不崩溃", len(StackTraceParser().parse("\x00\x01\x02 garbage").frames) == 0)
# extract_and_parse 能从混合日志里挖出
mixed = "[INFO] starting...\n[ERROR] something\n" + PY_TRACE
test("extract_and_parse 混合日志", len(StackTraceParser().extract_and_parse(mixed).frames) == 2)

# ─────────────────────────────────────
# 6. source locator - 路径前缀映射
# ─────────────────────────────────────
print("\n[source_locator:prefix-mapping]")
tmp = tempfile.mkdtemp()
try:
    repo_path = os.path.join(tmp, "backend")
    os.makedirs(os.path.join(repo_path, "handlers"))
    user_py = os.path.join(repo_path, "handlers", "user.py")
    with open(user_py, "w") as f:
        f.write("\n".join([
            "def get_user(uid):",                              # 1
            "    db = get_db()",                                # 2
            "    record = db.fetch(uid)",                       # 3
            "    return record.name",                           # 4 ← target
            "",                                                  # 5
            "def other():",                                      # 6
            "    pass",                                          # 7
        ]) + "\n")

    repo = SourceRepo(
        name="backend", path=repo_path, language="python",
        path_prefix_runtime="/app", path_prefix_local="",
    )
    locator = SourceLocator([repo])
    frames = [StackFrame(file="/app/handlers/user.py", line=4, function="get_user", language="python")]
    result = locator.locate(frames)
    test("prefix 映射: 命中 1 条", len(result.locations) == 1)
    loc = result.locations[0]
    test("prefix 映射: local_file 正确", loc.local_file == user_py)
    test("prefix 映射: target_line 含 record.name", "record.name" in loc.target_line)
    test("prefix 映射: context_before 含 def", "def get_user" in loc.context_before)
    test("prefix 映射: function_def 提取出整个函数",
         "def get_user" in loc.function_definition and "record.name" in loc.function_definition)
    test("prefix 映射: render 不为空且含路径", bool(loc.render()) and "user.py" in loc.render())
finally:
    shutil.rmtree(tmp)

# ─────────────────────────────────────
# 7. source locator - 文件名匹配
# ─────────────────────────────────────
print("\n[source_locator:filename-fallback]")
tmp = tempfile.mkdtemp()
try:
    repo_path = os.path.join(tmp, "svc")
    os.makedirs(os.path.join(repo_path, "src", "deep", "nested"))
    target = os.path.join(repo_path, "src", "deep", "nested", "thing.py")
    with open(target, "w") as f:
        f.write("# line1\n# line2\nx = 1\n")
    repo = SourceRepo(name="svc", path=repo_path, language="python")
    locator = SourceLocator([repo])
    # frame 中没有 prefix,只有文件名
    frames = [StackFrame(file="thing.py", line=3, language="python")]
    result = locator.locate(frames)
    test("文件名匹配: 找到 1 条", len(result.locations) == 1)
    test("文件名匹配: 命中正确文件", result.locations[0].local_file == target)
finally:
    shutil.rmtree(tmp)

# ─────────────────────────────────────
# 8. source locator - 重名时按后缀打分
# ─────────────────────────────────────
print("\n[source_locator:disambiguate]")
tmp = tempfile.mkdtemp()
try:
    repo_path = os.path.join(tmp, "svc")
    os.makedirs(os.path.join(repo_path, "a", "x"))
    os.makedirs(os.path.join(repo_path, "b", "x"))
    f1 = os.path.join(repo_path, "a", "x", "utils.py")
    f2 = os.path.join(repo_path, "b", "x", "utils.py")
    for f in (f1, f2):
        with open(f, "w") as h:
            h.write("line1\nline2\nline3\n")
    repo = SourceRepo(name="svc", path=repo_path, language="python")
    locator = SourceLocator([repo])
    # 原始路径是 /opt/whatever/b/x/utils.py — 后缀 b/x/utils.py 应该选 f2
    frames = [StackFrame(file="/opt/whatever/b/x/utils.py", line=2, language="python")]
    result = locator.locate(frames)
    test("重名: 按后缀选择正确分支", len(result.locations) == 1 and result.locations[0].local_file == f2,
         f"got {result.locations[0].local_file if result.locations else 'none'}")
finally:
    shutil.rmtree(tmp)

# ─────────────────────────────────────
# 9. source locator - 大括号语言函数提取
# ─────────────────────────────────────
print("\n[source_locator:brace-fn-extract]")
tmp = tempfile.mkdtemp()
try:
    repo_path = os.path.join(tmp, "node")
    os.makedirs(repo_path)
    js_file = os.path.join(repo_path, "user.js")
    with open(js_file, "w") as f:
        f.write("\n".join([
            "function other() { return 1; }",                    # 1
            "function getUser(id) {",                             # 2
            "  const u = db.find(id);",                           # 3
            "  return u.name;",                                   # 4 ← target
            "}",                                                   # 5
            "function tail() {}",                                  # 6
        ]) + "\n")
    repo = SourceRepo(name="node", path=repo_path, language="node")
    locator = SourceLocator([repo])
    frames = [StackFrame(file="user.js", line=4, function="getUser", language="node")]
    result = locator.locate(frames)
    test("brace fn: 命中", len(result.locations) == 1)
    fn = result.locations[0].function_definition
    test("brace fn: 含 getUser", "getUser" in fn)
    test("brace fn: 不含 tail()", "tail" not in fn)
finally:
    shutil.rmtree(tmp)

# ─────────────────────────────────────
# 10. source locator - 错误宽容
# ─────────────────────────────────────
print("\n[source_locator:robustness]")
locator = SourceLocator([])
test("空仓库列表不崩溃", len(locator.locate([StackFrame(file="x", line=1)]).locations) == 0)
# 仓库路径不存在
locator = SourceLocator([SourceRepo(name="x", path="/nonexistent/zzz")])
test("仓库路径不存在不崩溃", len(locator.locate([StackFrame(file="x.py", line=1)]).locations) == 0)
# frame 行号超出文件
tmp = tempfile.mkdtemp()
try:
    p = os.path.join(tmp, "small.py")
    with open(p, "w") as f:
        f.write("x = 1\n")
    locator = SourceLocator([SourceRepo(name="r", path=tmp, language="python")])
    frames = [StackFrame(file="small.py", line=999, language="python")]
    test("行号越界返回空", len(locator.locate(frames).locations) == 0)
finally:
    shutil.rmtree(tmp)

# ─────────────────────────────────────
# 11. SourceRepo.from_dict 兼容性
# ─────────────────────────────────────
print("\n[targets:source-repo-compat]")
r = SourceRepo.from_dict({"name": "x", "path": "/tmp", "language": "python"})
test("from_dict 基本", r.name == "x" and r.language == "python")
r = SourceRepo.from_dict({"name": "x", "path": "/tmp", "path-prefix-runtime": "/app"})
test("from_dict 横线兼容", r.path_prefix_runtime == "/app")
r = SourceRepo.from_dict({"name": "x", "path": "/tmp", "unknown_field": "ignored"})
test("from_dict 未知字段被过滤", r.name == "x")
# Target.get_source_repos
t = Target(name="t", type="local", source_repos=[
    {"name": "a", "path": "/tmp/a", "language": "python"},
    {"name": "b", "path": "/tmp/b", "language": "go"},
])
repos = t.get_source_repos()
test("Target.get_source_repos 数量", len(repos) == 2)
test("Target.get_source_repos 类型", isinstance(repos[0], SourceRepo))

# ─────────────────────────────────────
# 12. 端到端: parse -> locate -> render
# ─────────────────────────────────────
print("\n[end-to-end]")
tmp = tempfile.mkdtemp()
try:
    repo_path = os.path.join(tmp, "backend")
    os.makedirs(os.path.join(repo_path, "handlers"))
    src = os.path.join(repo_path, "handlers", "user.py")
    with open(src, "w") as f:
        # 让目标行号 == 42
        lines = ["# header\n"] * 40
        lines.append("def get_user(uid):\n")             # 41
        lines.append("    return db.fetch(uid).name\n")  # 42
        f.writelines(lines)
    repo = SourceRepo(
        name="backend", path=repo_path, language="python",
        path_prefix_runtime="/app",
    )
    target = Target(name="prod", type="local", source_repos=[
        {"name": "backend", "path": repo_path, "language": "python",
         "path_prefix_runtime": "/app"},
    ])
    parsed = StackTraceParser().parse(PY_TRACE)
    result = SourceLocator(target.get_source_repos()).locate(parsed.frames)
    test("e2e: 至少 1 个 location", len(result.locations) >= 1)
    rendered = result.render()
    test("e2e: render 含路径", "user.py" in rendered)
    test("e2e: render 含行号 42", "42" in rendered)
finally:
    shutil.rmtree(tmp)


print()
print("=" * 40)
print(f"  通过: {PASS}    失败: {FAIL}")
print("=" * 40)
sys.exit(0 if FAIL == 0 else 1)
