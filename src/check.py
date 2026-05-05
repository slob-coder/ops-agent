"""
ops-agent check — 配置校验

用法:
  ops-agent check
  ops-agent check --workspace /root/.ops-agent

检查:
1. .env — LLM 凭据是否存在 (workspace/.env)
2. notebook/config/targets.yaml — 格式和必填字段
3. notebook/config/limits.yaml — 格式和关键字段
4. notebook/config/permissions.md — 是否存在
5. LLM 连通性（可选）
6. SSH 连通性（可选）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

from src.i18n import t

try:
    import yaml
except ImportError:
    yaml = None


class CheckResult:
    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.infos: List[str] = []

    def error(self, msg: str):
        self.errors.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)

    def info(self, msg: str):
        self.infos.append(msg)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def print_report(self):
        for msg in self.infos:
            print(f"  ℹ️  {msg}")
        for msg in self.warnings:
            print(f"  ⚠️  {msg}")
        for msg in self.errors:
            print(f"  ❌ {msg}")

        print()
        if self.ok:
            if self.warnings:
                print(t("check.config_ok_warn"))
            else:
                print(t("check.config_ok"))
        else:
            print(t("check.config_error", count=len(self.errors)))


def _load_env_file(env_path: Path) -> dict:
    """从 .env 文件加载环境变量"""
    env = {}
    if not env_path.exists():
        return env
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def check_env(workspace: str, result: CheckResult):
    """检查 LLM 环境变量"""
    env_path = Path(workspace) / ".env"
    # 向后兼容：workspace/notebook/.env
    if not env_path.exists():
        legacy = Path(workspace) / "notebook" / ".env"
        if legacy.exists():
            env_path = legacy
    env = _load_env_file(env_path)

    # 合并系统环境变量（系统优先）
    provider = os.environ.get("OPS_LLM_PROVIDER") or env.get("OPS_LLM_PROVIDER", "")
    api_key = os.environ.get("OPS_LLM_API_KEY") or env.get("OPS_LLM_API_KEY", "")
    model = os.environ.get("OPS_LLM_MODEL") or env.get("OPS_LLM_MODEL", "")
    base_url = os.environ.get("OPS_LLM_BASE_URL") or env.get("OPS_LLM_BASE_URL", "")

    if not provider and not api_key:
        result.error(t("check.llm_no_provider_key"))
        return

    if not api_key:
        result.error(t("check.llm_no_key"))
    else:
        result.info(f"LLM Provider: {provider or 'anthropic'}")
        result.info(f"LLM Model: {model or '(default)'}")
        if base_url:
            result.info(f"LLM Base URL: {base_url}")

    if not env_path.exists():
        result.warn(t("check.env_missing", path=env_path))


def check_targets(notebook_path: str, result: CheckResult):
    """检查 targets.yaml"""
    config_dir = Path(notebook_path) / "config"
    targets_path = config_dir / "targets.yaml"

    if not targets_path.exists():
        # 检查 example
        example_path = Path(__file__).parent.parent / "templates" / "targets.example.yaml"
        result.error(t("check.targets_missing", path=targets_path))
        if example_path.exists():
            result.info(t("check.template_ref", path=example_path))
        return

    if yaml is None:
        result.warn(t("check.pyyaml_missing"))
        return

    try:
        with open(targets_path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        result.error(t("check.targets_format_error", error=e))
        return

    if not data or "targets" not in data:
        result.error(t("check.targets_no_key"))
        return

    targets = data["targets"]
    if not targets:
        result.warn(t("check.targets_empty"))
        return

    for i, t in enumerate(targets):
        prefix = f"target[{i}]"
        name = t.get("name", f"unnamed-{i}")
        prefix = f"target '{name}'"

        if not t.get("name"):
            result.error(t("check.target_missing_name", prefix=prefix))
        if not t.get("type"):
            result.error(t("check.target_missing_type", prefix=prefix))
        elif t["type"] not in ("ssh", "docker", "k8s", "local"):
            result.error(t("check.target_invalid_type", prefix=prefix, type=t['type']))

        if t.get("type") == "ssh" and not t.get("host"):
            result.error(t("check.target_ssh_no_host", prefix=prefix))

        for repo in t.get("source_repos", []):
            repo_prefix = f"{prefix}.source_repos[{repo.get('name', '?')}]"
            if not repo.get("path"):
                result.error(t("check.repo_missing_path", prefix=repo_prefix))

    result.info(t("check.target_count", count=len(targets)))
    for t in targets:
        ttype = t.get("type", "?")
        tname = t.get("name", "?")
        result.info(f"  - {tname} ({ttype})")


def check_limits(notebook_path: str, result: CheckResult):
    """检查 limits.yaml"""
    config_dir = Path(notebook_path) / "config"
    limits_path = config_dir / "limits.yaml"

    if not limits_path.exists():
        result.warn(t("check.limits_missing"))
        return

    if yaml is None:
        return

    try:
        with open(limits_path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        result.error(t("check.limits_format_error", error=e))
        return

    if not data:
        result.warn(t("check.limits_empty"))
        return

    if data.get("enabled") is not True:
        result.warn(t("check.limits_not_enabled"))

    result.info(f"limits: enabled={data.get('enabled')}, "
                f"max_actions/hour={data.get('max_actions_per_hour', '?')}")


def check_permissions(notebook_path: str, result: CheckResult):
    """检查 permissions.md"""
    config_dir = Path(notebook_path) / "config"
    perm_path = config_dir / "permissions.md"

    if not perm_path.exists():
        result.warn(t("check.perms_missing"))
    else:
        content = perm_path.read_text()
        if len(content) < 50:
            result.warn(t("check.perms_too_short"))
        else:
            result.info("permissions.md ✓")


def check_notifier(notebook_path: str, result: CheckResult):
    """检查 notifier.yaml"""
    config_dir = Path(notebook_path) / "config"
    notifier_path = config_dir / "notifier.yaml"

    if not notifier_path.exists():
        result.info(t("check.notifier_missing"))
        return

    if yaml is None:
        return

    try:
        with open(notifier_path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        result.error(t("check.notifier_format_error", error=e))
        return

    ntype = data.get("type", "none") if data else "none"
    result.info(f"notifier: type={ntype}")


def check_llm_connection(result: CheckResult):
    """测试 LLM API 连通性"""
    provider = os.environ.get("OPS_LLM_PROVIDER", "anthropic").lower()
    api_key = os.environ.get("OPS_LLM_API_KEY", "")
    base_url = os.environ.get("OPS_LLM_BASE_URL", "")

    PROVIDER_DEFAULTS = {
        "anthropic": {"model": "claude-sonnet-4-20250514", "base_url": ""},
        "openai": {"model": "gpt-4o", "base_url": ""},
        "zhipu": {"model": "glm-4-plus", "base_url": "https://open.bigmodel.cn/api/paas/v4/"},
    }

    if provider not in PROVIDER_DEFAULTS:
        result.error(t("check.unsupported_provider", provider=provider))
        return

    if not api_key:
        result.error(t("check.llm_no_key_test"))
        return

    defaults = PROVIDER_DEFAULTS[provider]
    model = os.environ.get("OPS_LLM_MODEL") or defaults["model"]
    if not base_url:
        base_url = defaults["base_url"]

    try:
        if provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            client.messages.create(model=model, max_tokens=10,
                                   messages=[{"role": "user", "content": "Say OK"}])
        elif provider in ("openai", "zhipu"):
            import openai
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = openai.OpenAI(**kwargs)
            client.chat.completions.create(model=model, max_tokens=10,
                                           messages=[{"role": "user", "content": "Say OK"}])
        result.info(t("check.llm_test_ok"))
    except Exception as e:
        result.error(t("check.llm_test_failed", error=e))


def run_check(workspace_path: str = "~/.ops-agent", test_llm: bool = False):
    """运行完整校验"""
    workspace = Path(workspace_path).expanduser().resolve()
    notebook_path = str(workspace / "notebook")

    result = CheckResult()

    print()
    print(t("check.title"))
    print("━━━━━━━━━━━━━━━━━━━━")
    print()

    # 先加载 .env
    env_path = workspace / ".env"
    if not env_path.exists():
        legacy = workspace / "notebook" / ".env"
        if legacy.exists():
            env_path = legacy
    env = _load_env_file(env_path)
    for key, value in env.items():
        if key not in os.environ:
            os.environ[key] = value

    print(t("check.section_llm"))
    check_env(str(workspace), result)

    print()
    print(t("check.section_targets"))
    check_targets(notebook_path, result)

    print()
    print(t("check.section_limits"))
    check_limits(notebook_path, result)

    print()
    print(t("check.section_perms"))
    check_permissions(notebook_path, result)

    print()
    print(t("check.section_notifier"))
    check_notifier(notebook_path, result)

    if test_llm and result.ok:
        print()
        print(t("check.section_llm_test"))
        check_llm_connection(result)

    print()
    print(t("check.result_header"))
    result.print_report()

    if not result.ok:
        sys.exit(1)
