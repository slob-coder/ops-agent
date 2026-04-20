"""test_self_repair.py - 自修复会话的核心逻辑单测

覆盖:
  1. FORBIDDEN_FILES / SENSITIVE_FILES 检查
  2. _parse_json_block 的鲁棒性
  3. SelfContext 的字段打包和长度上限
  4. SelfContext.to_prompt 的序列化
  5. probation marker 文件读写
"""
import os
import sys
import json
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.repair.self_repair import (
    SelfRepairSession, _parse_json_block,
    FORBIDDEN_FILES, SENSITIVE_FILES,
    SELFREPAIR_PENDING_FILE,
)
from src.repair.self_context import SelfContext, _truncate, _list_source_tree, _sanitize_state


passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        print(f"  ✓ {name}")
        passed += 1
    else:
        print(f"  ✗ {name}")
        failed += 1


print("=== _parse_json_block ===")

check("解析 ```json fenced block",
      _parse_json_block('```json\n{"a": 1}\n```') == {"a": 1})

check("解析裸 {...}",
      _parse_json_block('some text {"a": 2} tail') == {"a": 2})

check("损坏 json 返回 None",
      _parse_json_block('```json\n{not json}\n```') is None)

check("空字符串返回 None",
      _parse_json_block("") is None)

check("多行嵌套 json",
      _parse_json_block('```json\n{"a": {"b": 3}}\n```') == {"a": {"b": 3}})


print("\n=== FORBIDDEN / SENSITIVE 常量 ===")

check("self_repair.py 在禁改清单",
      "self_repair.py" in FORBIDDEN_FILES)
check("self_context.py 在禁改清单",
      "self_context.py" in FORBIDDEN_FILES)
check("self_diagnose.md 在禁改清单",
      "prompts/self_diagnose.md" in FORBIDDEN_FILES)
check("safety.py 在敏感清单",
      "safety.py" in SENSITIVE_FILES)
check("limits.py 在敏感清单",
      "limits.py" in SENSITIVE_FILES)
check("trust.py 在敏感清单",
      "trust.py" in SENSITIVE_FILES)
check("禁改清单和敏感清单不重叠",
      not (FORBIDDEN_FILES & SENSITIVE_FILES))


print("\n=== _check_forbidden / _check_sensitive ===")

# 构造一个裸对象,不真正初始化
class _FakeAgent:
    pass

session = SelfRepairSession.__new__(SelfRepairSession)
session.agent = _FakeAgent()
session.repo_path = "/tmp"
session.test_cmd = ""

check("命中 self_repair.py",
      session._check_forbidden(["self_repair.py:10"]) == "self_repair.py")
check("命中 prompts/self_diagnose.md",
      session._check_forbidden(["prompts/self_diagnose.md:5"]) == "prompts/self_diagnose.md")
check("main.py 不命中禁改",
      session._check_forbidden(["main.py:540"]) == "")
check("空列表不命中",
      session._check_forbidden([]) == "")
check("带 ./ 前缀也能识别",
      session._check_forbidden(["./self_repair.py:1"]) == "self_repair.py")
check("safety.py 命中敏感清单",
      session._check_sensitive(["safety.py:20"]) == "safety.py")
check("main.py 不命中敏感清单",
      session._check_sensitive(["main.py:100"]) == "")


print("\n=== _truncate ===")

check("短文本不截断",
      _truncate("hello", 100) == "hello")
check("空文本返回空",
      _truncate("", 100) == "")
check("长文本被截断且包含 omitted 标记",
      "omitted" in _truncate("x" * 1000, 300))
check("截断后长度受控",
      len(_truncate("x" * 10000, 300)) < 500)


print("\n=== _sanitize_state ===")

state = {
    "mode": "patrol",
    "api_key": "sk-secret",
    "some_token": "abc",
    "password": "pwd",
    "uptime": 100,
    "config": {"nested": True},
}
sanitized = _sanitize_state(state)
check("api_key 被脱敏",
      sanitized.get("api_key") == "<redacted>")
check("token 字段被脱敏",
      sanitized.get("some_token") == "<redacted>")
check("password 被脱敏",
      sanitized.get("password") == "<redacted>")
check("普通字段保留",
      sanitized.get("mode") == "patrol")
check("嵌套 dict 保留",
      sanitized.get("config") == {"nested": True})


print("\n=== _list_source_tree ===")

with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "a.py").write_text("line1\nline2\nline3\n")
    (Path(tmp) / "b.md").write_text("# heading\n\ntext\n")
    (Path(tmp) / "c.txt").write_text("ignored\n")
    (Path(tmp) / "__pycache__").mkdir()
    (Path(tmp) / "__pycache__" / "x.py").write_text("x\n")
    (Path(tmp) / "sub").mkdir()
    (Path(tmp) / "sub" / "d.yaml").write_text("k: v\n")

    tree = _list_source_tree(tmp)
    rels = [p for p, _ in tree]

    check("收录 .py 文件", "a.py" in rels)
    check("收录 .md 文件", "b.md" in rels)
    check("收录子目录 yaml", any("d.yaml" in r for r in rels))
    check("跳过 .txt", "c.txt" not in rels)
    check("跳过 __pycache__",
          not any("__pycache__" in r for r in rels))
    check("a.py 行数正确",
          next(n for p, n in tree if p == "a.py") == 3)


print("\n=== SelfContext.to_prompt ===")

ctx = SelfContext(
    user_description="主循环异常路径无 sleep",
    source_tree=[("main.py", 2000), ("self_repair.py", 500)],
    agent_state={"mode": "patrol", "paused": False, "api_key": "<redacted>"},
    recent_log_tail="[INFO] tick\n[WARN] slow",
    recent_incidents="最近无 incident",
    config_snapshot={"requirements.txt": "pyyaml\nrequests\n"},
    git_head={"sha": "abc123", "branch": "main", "dirty": "no"},
    repo_path="/opt/ops-agent-selfdev",
)
rendered = ctx.to_prompt()

check("to_prompt 包含用户描述",
      "主循环异常路径无 sleep" in rendered)
check("to_prompt 包含 git 信息",
      "abc123" in rendered and "branch" in rendered)
check("to_prompt 包含源码清单",
      "main.py" in rendered and "2000" in rendered)
check("to_prompt 包含 agent_state",
      "patrol" in rendered)
check("to_prompt 包含日志尾部",
      "[INFO] tick" in rendered)
check("to_prompt 包含配置快照",
      "pyyaml" in rendered)
check("to_prompt 没有 API key 明文",
      "sk-" not in rendered)


print("\n=== probation pending marker ===")

with tempfile.TemporaryDirectory() as tmp:
    marker_path = Path(tmp) / SELFREPAIR_PENDING_FILE
    data = {
        "sid": "selfrepair-1",
        "pre_tag": "selfrepair-pre-1",
        "branch": "selfrepair/1",
        "merged_at": 1234567890,
    }
    marker_path.write_text(json.dumps(data))
    loaded = json.loads(marker_path.read_text())
    check("pending marker 能写能读",
          loaded["sid"] == "selfrepair-1")
    check("marker 路径用 SELFREPAIR_PENDING_FILE 常量",
          marker_path.name == SELFREPAIR_PENDING_FILE)


print("\n" + "=" * 40)
print(f"  通过: {passed}    失败: {failed}")
print("=" * 40)
sys.exit(0 if failed == 0 else 1)
