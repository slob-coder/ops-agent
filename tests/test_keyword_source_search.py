"""测试关键词源码搜索 fallback + Plan 阶段改进

覆盖:
1. _extract_error_keywords — 从错误信息中提取搜索关键词
2. _search_source_by_keywords — 在 source_repos 中搜索匹配文件
3. _search_source_snippets_from_diagnosis — Plan 阶段 fallback
4. _build_confirmed_facts — 已确认事实清单
5. _detect_plan_stagnation — 进展检测
6. 端到端: 错误信息 → 关键词提取 → 源码搜索 → LocateResult
7. diagnose 阶段 fallback 流程（stack trace 失败时走关键词搜索）

运行: python tests/test_keyword_source_search.py
"""

import os
import sys
import tempfile
import shutil

# 确保项目根目录在 sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.agent.pipeline import PipelineMixin

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


# ─── 测试辅助：构造一个最小化的 PipelineMixin 实例 ───

class FakeRunResult:
    """模拟命令执行结果"""
    def __init__(self, output="", returncode=0, success=True):
        self.output = output
        self.returncode = returncode
        self.success = success

    def __str__(self):
        return self.output


class FakeTarget:
    """模拟 Target"""
    def __init__(self, repos=None):
        self.name = "test-target"
        self.source_repos = repos or []
        self.mode = "local"

    def get_source_repos(self):
        from src.infra.targets import SourceRepo
        return [SourceRepo.from_dict(r) if isinstance(r, dict) else r for r in self.source_repos]


class FakeAgent(PipelineMixin):
    """最小化的 agent，继承 PipelineMixin 以获取类属性"""
    pass


def make_agent_with_repo(repo_path, repo_name="test-repo", language="go"):
    """创建一个包含关键词搜索方法的 agent 实例"""
    from src.infra.targets import SourceRepo, Target

    agent = FakeAgent()

    # 注入必要属性
    agent.current_target = FakeTarget(repos=[
        {"name": repo_name, "path": repo_path, "language": language}
    ])

    # 从 PipelineMixin 复制方法
    agent._extract_error_keywords = PipelineMixin._extract_error_keywords.__get__(agent)
    agent._search_source_by_keywords = PipelineMixin._search_source_by_keywords.__get__(agent)
    agent._search_source_snippets_from_diagnosis = PipelineMixin._search_source_snippets_from_diagnosis.__get__(agent)
    agent._build_confirmed_facts = PipelineMixin._build_confirmed_facts.__get__(agent)
    agent._detect_plan_stagnation = PipelineMixin._detect_plan_stagnation.__get__(agent)
    agent._run_cmd = lambda cmd, timeout=15: FakeRunResult("")  # 默认空结果

    return agent


# ═══════════════════════════════════════════
# 1. _extract_error_keywords
# ═══════════════════════════════════════════

print("\n[_extract_error_keywords]")

# _extract_error_keywords 使用 self._KEYWORD_STOPWORDS（PipelineMixin 类属性）
_test_instance = FakeAgent()
_extract = _test_instance._extract_error_keywords

# 引号内标识符
kw = _extract('column "account_id" does not exist')
test("引号内标识符: account_id", "account_id" in kw, str(kw))

# 蛇形命名
kw = _extract("platform_account_id not found in table")
test("蛇形命名: platform_account_id", "platform_account_id" in kw, str(kw))

# 驼峰命名
kw = _extract("NullPointerException in UserService")
test("驼峰命名: NullPointerException", "NullPointerException" in kw, str(kw))

# 混合
kw = _extract('ERROR: column "account_id" of relation "publish_tasks" does not exist')
test("混合: 含 account_id", "account_id" in kw, str(kw))
test("混合: 含 publish_tasks", "publish_tasks" in kw, str(kw))

# 空输入
kw = _extract("")
test("空输入返回空列表", kw == [], str(kw))

# 通用词过滤
kw = _extract("error: the table does not exist")
test("通用词被过滤", "error" not in kw and "table" not in kw, str(kw))

# 短词过滤
kw = _extract("column 'id' not found")
test("短词 id 被过滤", "id" not in kw, str(kw))

# 去重
kw = _extract('column "account_id" and account_id are the same')
test("去重: 只出现一次", kw.count("account_id") == 1 if "account_id" in kw else True, str(kw))

# 最多 5 个关键词
long_text = " ".join(f'"{w}"' for w in ["alpha_bravo", "charlie_delta", "echo_foxtrot", "golf_hotel", "india_juliet", "kilo_lima"])
kw = _extract(long_text)
test("最多 5 个关键词", len(kw) <= 5, f"got {len(kw)}: {kw}")


# ═══════════════════════════════════════════
# 2. _search_source_by_keywords — 使用真实文件
# ═══════════════════════════════════════════

print("\n[_search_source_by_keywords]")

# 创建临时仓库
tmp = tempfile.mkdtemp()
try:
    repo_path = os.path.join(tmp, "publish-service")
    os.makedirs(os.path.join(repo_path, "internal", "repository"))

    # 创建 Go 文件，模拟 motong 的 task.go
    task_go = os.path.join(repo_path, "internal", "repository", "task.go")
    with open(task_go, "w") as f:
        f.write('\n'.join([
            'package repository',
            '',
            'type PublishTask struct {',
            '    ID        int64  `db:"id" json:"id"`',
            '    AccountID int64  `db:"account_id" json:"account_id"`',
            '    Platform  string `db:"platform" json:"platform"`',
            '}',
            '',
            'func (r *TaskRepo) Create(ctx context.Context, t *PublishTask) error {',
            '    return r.db.QueryRowxContext(ctx,',
            '        `INSERT INTO publish_tasks (user_id, account_id, asset_id) VALUES (...)`,',
            '        t.UserID, t.AccountID, t.AssetID).Scan(&t.ID)',
            '}',
        ]) + '\n')

    agent = make_agent_with_repo(repo_path, language="go")

    # 模拟 grep 输出
    grep_output = (
        f"{task_go}:5:AccountID int64  `db:\"account_id\" json:\"account_id\"`\n"
        f"{task_go}:11:        `INSERT INTO publish_tasks (user_id, account_id, asset_id) VALUES (...)`,\n"
    )
    agent._run_cmd = lambda cmd, timeout=15: FakeRunResult(grep_output)

    result = agent._search_source_by_keywords(["account_id"])
    test("关键词搜索: 返回非空", result is not None)
    if result:
        test("关键词搜索: 有 locations", len(result.locations) > 0, f"got {len(result.locations)}")
        if result.locations:
            loc = result.locations[0]
            test("关键词搜索: repo_name 正确", loc.repo_name == "test-repo", loc.repo_name)
            test("关键词搜索: local_file 正确", loc.local_file == task_go, loc.local_file)
            test("关键词搜索: target_line 含 account_id", "account_id" in loc.target_line, loc.target_line)
            test("关键词搜索: render 非空", bool(loc.render(max_chars=2000)), "render is empty")

    # 无匹配的情况
    agent._run_cmd = lambda cmd, timeout=15: FakeRunResult("")
    result = agent._search_source_by_keywords(["nonexistent_column_xyz"])
    test("无匹配: 返回 None", result is None)

finally:
    shutil.rmtree(tmp)


# ═══════════════════════════════════════════
# 3. _search_source_snippets_from_diagnosis
# ═══════════════════════════════════════════

print("\n[_search_source_snippets_from_diagnosis]")

tmp = tempfile.mkdtemp()
try:
    repo_path = os.path.join(tmp, "publish-service")
    os.makedirs(os.path.join(repo_path, "internal", "repository"))

    task_go = os.path.join(repo_path, "internal", "repository", "task.go")
    with open(task_go, "w") as f:
        f.write('package repository\n\nAccountID int64 `db:"account_id"`\n')

    agent = make_agent_with_repo(repo_path, language="go")
    agent._last_error_text = 'ERROR: column "account_id" does not exist'

    grep_output = f'{task_go}:3:AccountID int64 `db:"account_id"`\n'
    agent._run_cmd = lambda cmd, timeout=15: FakeRunResult(grep_output)

    diagnosis = {
        "hypothesis": "代码使用 account_id 但数据库实际列名为 platform_account_id",
        "facts": "publish-service 调度器持续报错 account_id 列不存在",
        "type": "code_bug",
        "confidence": 85,
    }

    result_text = agent._search_source_snippets_from_diagnosis(diagnosis)
    test("Plan fallback: 返回非空", result_text != "（无）", result_text[:100])
    test("Plan fallback: 含关键词标记", "关键词搜索" in result_text, result_text[:100])
    test("Plan fallback: 含 account_id", "account_id" in result_text)

    # 无 target 时返回 （无）
    agent.current_target = None
    result_text = agent._search_source_snippets_from_diagnosis(diagnosis)
    test("无 target 返回 （无）", result_text == "（无）")

finally:
    shutil.rmtree(tmp)


# ═══════════════════════════════════════════
# 4. _build_confirmed_facts
# ═══════════════════════════════════════════

print("\n[_build_confirmed_facts]")

mixin_obj = FakeAgent()
_build_facts = PipelineMixin._build_confirmed_facts.__get__(mixin_obj)

# 完整诊断
diagnosis = {
    "hypothesis": "代码与数据库 Schema 命名不一致",
    "facts": "account_id 列不存在",
    "type": "code_bug",
    "confidence": 85,
}
facts = _build_facts(diagnosis, "（无）")
test("完整诊断: 含现象", "account_id" in facts, facts)
test("完整诊断: 含根因", "命名不一致" in facts, facts)
test("完整诊断: 含类型", "code_bug" in facts, facts)

# 含代码搜索结果
code_search = "### test-repo:task.go:5\n```go\nAccountID int64 `db:\"account_id\"`\n```"
facts = _build_facts(diagnosis, code_search)
test("含代码搜索: 含文件名", "task.go" in facts, facts)

# 空诊断
facts = _build_facts({}, "（无）")
test("空诊断: 返回 （无）", facts == "（无）", facts)


# ═══════════════════════════════════════════
# 5. _detect_plan_stagnation
# ═══════════════════════════════════════════

print("\n[_detect_plan_stagnation]")

mixin_obj = FakeAgent()
_detect = PipelineMixin._detect_plan_stagnation.__get__(mixin_obj)

# 不足 2 轮
test("不足 2 轮不检测", _detect([]) == False)
test("1 轮不检测", _detect([{"action": "COLLECT_MORE", "gaps_desc": ["查看文件"]}]) == False)

# 连续 2 轮 COLLECT_MORE，gaps 完全重叠
history = [
    {"action": "COLLECT_MORE", "gaps_desc": ["查看 account_id 在代码中的使用", "检查数据库 schema"]},
    {"action": "COLLECT_MORE", "gaps_desc": ["查看 account_id 在代码中的使用", "检查数据库 schema"]},
]
test("连续 2 轮相同 gaps 检测到打转", _detect(history) == True)

# 连续 2 轮 COLLECT_MORE，gaps 部分重叠
history = [
    {"action": "COLLECT_MORE", "gaps_desc": ["查看 account_id 在代码中的使用"]},
    {"action": "COLLECT_MORE", "gaps_desc": ["查看 account_id 在代码中的使用", "检查数据库迁移文件"]},
]
test("部分重叠也检测到", _detect(history) == True)

# 不同 action 不检测
history = [
    {"action": "READY", "gaps_desc": []},
    {"action": "COLLECT_MORE", "gaps_desc": ["查看配置"]},
]
test("非连续 COLLECT_MORE 不检测", _detect(history) == False)

# 前缀模糊匹配
history = [
    {"action": "COLLECT_MORE", "gaps_desc": ["查看 publish_tasks 表的列定义和索引信息"]},
    {"action": "COLLECT_MORE", "gaps_desc": ["查看 publish_tasks 表的列定义和约束信息"]},
]
test("前缀模糊匹配检测到", _detect(history) == True)

# 不重叠的 gaps 不检测
history = [
    {"action": "COLLECT_MORE", "gaps_desc": ["查看数据库连接配置"]},
    {"action": "COLLECT_MORE", "gaps_desc": ["查看日志中的错误详情"]},
]
test("不重叠的 gaps 不检测", _detect(history) == False)

# 空 gaps 不检测
history = [
    {"action": "COLLECT_MORE", "gaps_desc": []},
    {"action": "COLLECT_MORE", "gaps_desc": []},
]
test("空 gaps 不检测", _detect(history) == False)


# ═══════════════════════════════════════════
# 6. 端到端: motong 场景
# ═══════════════════════════════════════════

print("\n[end-to-end: motong publish-service account_id]")

tmp = tempfile.mkdtemp()
try:
    # 模拟 motong publish-service 仓库
    repo_path = os.path.join(tmp, "motong")
    os.makedirs(os.path.join(repo_path, "backend", "services", "publish-service", "internal", "repository"))
    os.makedirs(os.path.join(repo_path, "backend", "services", "publish-service", "internal", "handler"))
    os.makedirs(os.path.join(repo_path, "backend", "services", "publish-service", "internal", "service"))
    os.makedirs(os.path.join(repo_path, "infra", "scripts"))

    # task.go — 代码用了 account_id
    task_go = os.path.join(repo_path, "backend", "services", "publish-service", "internal", "repository", "task.go")
    with open(task_go, "w") as f:
        f.write('\n'.join([
            'package repository',
            '',
            'type PublishTask struct {',
            '    ID             int64      `db:"id" json:"id"`',
            '    UserID         int64      `db:"user_id" json:"user_id"`',
            '    AccountID      int64      `db:"account_id" json:"account_id"`',
            '    AssetID        string     `db:"asset_id" json:"asset_id"`',
            '    Platform       string     `db:"platform" json:"platform"`',
            '}',
            '',
            'func (r *TaskRepo) Create(ctx context.Context, t *PublishTask) error {',
            '    return r.db.QueryRowxContext(ctx,',
            '        `INSERT INTO publish.publish_tasks (user_id, account_id, asset_id, platform, title) VALUES (...)`,',
            '        t.UserID, t.AccountID, t.AssetID, t.Platform, t.Title).Scan(&t.ID)',
            '}',
        ]) + '\n')

    # adapter.go
    adapter_go = os.path.join(repo_path, "backend", "services", "publish-service", "internal", "handler", "adapter.go")
    with open(adapter_go, "w") as f:
        f.write('package handler\n\nAccountID string `json:"account_id"`\n')

    # init-db.sql — 数据库用的是 platform_account_id
    init_sql = os.path.join(repo_path, "infra", "scripts", "init-db.sql")
    with open(init_sql, "w") as f:
        f.write("CREATE TABLE publish.publish_tasks (\n"
                "    platform_account_id UUID NOT NULL REFERENCES publish.platform_accounts(id),\n"
                "    ...\n);\n")

    # 测试关键词提取
    error_msg = 'ERROR: column "account_id" does not exist in publish.publish_tasks'
    kw = _extract(error_msg)
    test("e2e: 提取到 account_id", "account_id" in kw, str(kw))
    test("e2e: 提取到 publish_tasks", "publish_tasks" in kw, str(kw))

    # 测试源码搜索
    agent = make_agent_with_repo(repo_path, repo_name="motong", language="go")
    
    # 模拟 grep 输出（多个文件匹配）
    grep_output = (
        f"{task_go}:6:AccountID      int64      `db:\"account_id\" json:\"account_id\"`\n"
        f"{task_go}:13:        `INSERT INTO publish.publish_tasks (user_id, account_id, asset_id, platform, title) VALUES (...)`,\n"
        f"{adapter_go}:3:AccountID string `json:\"account_id\"`\n"
    )
    agent._run_cmd = lambda cmd, timeout=15: FakeRunResult(grep_output)

    result = agent._search_source_by_keywords(["account_id"])
    test("e2e: 搜索返回非空", result is not None)
    if result:
        test("e2e: 定位到 2 个文件", len(result.locations) == 2, f"got {len(result.locations)}")
        files_found = [os.path.basename(loc.local_file) for loc in result.locations]
        test("e2e: 找到 task.go", "task.go" in files_found, str(files_found))
        test("e2e: 找到 adapter.go", "adapter.go" in files_found, str(files_found))

    # 测试 Plan fallback
    agent._last_error_text = error_msg
    diagnosis = {
        "hypothesis": "代码使用 account_id 但数据库实际列名为 platform_account_id",
        "facts": "publish-service 调度器持续报错 account_id 列不存在",
        "type": "code_bug",
        "confidence": 85,
    }
    result_text = agent._search_source_snippets_from_diagnosis(diagnosis)
    test("e2e: Plan fallback 非空", result_text != "（无）")
    test("e2e: Plan fallback 含 task.go", "task.go" in result_text)
    test("e2e: Plan fallback 含 account_id", "account_id" in result_text)

    # 测试 confirmed_facts
    facts = _build_facts(diagnosis, result_text)
    test("e2e: facts 含文件名", "task.go" in facts, facts)

finally:
    shutil.rmtree(tmp)


# ═══════════════════════════════════════════
# 7. diagnose 阶段 fallback 流程
# ═══════════════════════════════════════════

print("\n[diagnose fallback: stack trace 失败时走关键词搜索]")

tmp = tempfile.mkdtemp()
try:
    repo_path = os.path.join(tmp, "backend")
    os.makedirs(os.path.join(repo_path, "services"))
    go_file = os.path.join(repo_path, "services", "handler.go")
    with open(go_file, "w") as f:
        f.write('package services\n\nAccountID int64 `db:"account_id"`\n')

    from src.repair.stack_parser import StackFrame
    from src.repair.source_locator import LocateResult, SourceLocation

    # 模拟 _locate_source_from_text 的逻辑
    # 场景: 没有异常栈（SQL 错误不产生 Go stack trace），但错误信息含标识符

    error_text = 'ERROR: column "account_id" does not exist in publish.publish_tasks'

    # Step 1: stack trace 解析
    from src.repair.stack_parser import StackTraceParser
    parsed = StackTraceParser().extract_and_parse(error_text)
    test("diagnose fallback: 无 stack trace", len(parsed.frames) == 0, f"got {len(parsed.frames)} frames")

    # Step 2: 关键词提取
    kw = _extract(error_text)
    test("diagnose fallback: 提取到关键词", len(kw) > 0, str(kw))

    # Step 3: 关键词搜索
    agent = make_agent_with_repo(repo_path, repo_name="backend", language="go")
    grep_output = f'{go_file}:3:AccountID int64 `db:"account_id"`\n'
    agent._run_cmd = lambda cmd, timeout=15: FakeRunResult(grep_output)

    result = agent._search_source_by_keywords(kw)
    test("diagnose fallback: 搜索找到源码", result is not None)
    if result:
        test("diagnose fallback: 定位到 handler.go",
             os.path.basename(result.locations[0].local_file) == "handler.go",
             os.path.basename(result.locations[0].local_file) if result.locations else "no locations")

finally:
    shutil.rmtree(tmp)


# ═══════════════════════════════════════════
# 8. 边界条件和错误容忍
# ═══════════════════════════════════════════

print("\n[edge cases and error tolerance]")

# grep 命令失败
agent = make_agent_with_repo("/nonexistent/path")
agent._run_cmd = lambda cmd, timeout=15: FakeRunResult("", returncode=1, success=False)
result = agent._search_source_by_keywords(["something"])
test("grep 失败返回 None", result is None)

# 无 target
agent = make_agent_with_repo("/tmp")
agent.current_target = None
result = agent._search_source_by_keywords(["account_id"])
test("无 target 返回 None", result is None)

# 空关键词
result = agent._search_source_by_keywords([])
test("空关键词返回 None", result is None)

# Plan fallback 无 target
result_text = agent._search_source_snippets_from_diagnosis({"hypothesis": "test"})
test("Plan fallback 无 target 返回 （无）", result_text == "（无）")

# Plan fallback 空诊断
agent2 = make_agent_with_repo("/tmp")
result_text = agent2._search_source_snippets_from_diagnosis({})
test("Plan fallback 空诊断返回 （无）", result_text == "（无）")

# _extract_error_keywords 只含通用词
kw = _extract("error: failed to connect")
test("只有通用词返回空列表", kw == [], str(kw))

# locate_source_from_text: 正常 stack trace 仍然走原有路径
from src.repair.source_locator import SourceLocator, LocateResult as SLR

tmp = tempfile.mkdtemp()
try:
    repo_path = os.path.join(tmp, "app")
    os.makedirs(repo_path)
    py_file = os.path.join(repo_path, "server.py")
    with open(py_file, "w") as f:
        f.write("def handle():\n    user = get_user(uid)\n    return user.name\n")

    from src.infra.targets import SourceRepo, Target
    repo = SourceRepo(name="app", path=repo_path, language="python", path_prefix_runtime="/app")

    # 模拟 agent 的 _locate_source_from_text
    agent3 = make_agent_with_repo(repo_path, language="python")
    agent3.current_target = FakeTarget(repos=[
        {"name": "app", "path": repo_path, "language": "python", "path_prefix_runtime": "/app"}
    ])

    # 正常 Python stack trace 应该走原有路径，不触发关键词搜索
    py_trace = '''Traceback (most recent call last):
  File "/app/server.py", line 2, in handle
    user = get_user(uid)
AttributeError: 'NoneType' object has no attribute 'name'
'''
    locate_result, parsed = PipelineMixin._locate_source_from_text(agent3, py_trace)
    test("正常 stack trace 仍走原有路径", locate_result is not None, f"locate_result={locate_result}, parsed={parsed}")
    if locate_result and locate_result.locations:
        test("正常 stack trace 定位到 server.py",
             "server.py" in locate_result.locations[0].local_file,
             locate_result.locations[0].local_file)

finally:
    shutil.rmtree(tmp)


# ═══════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════

print()
print("=" * 40)
print(f"  通过: {PASS}    失败: {FAIL}")
print("=" * 40)
sys.exit(0 if FAIL == 0 else 1)
