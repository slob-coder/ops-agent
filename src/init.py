"""
ops-agent init — 交互式配置引导

用法:
  python main.py init                     # 交互式
  python main.py init --from-env          # 全从环境变量读，缺了报错
  python main.py init --notebook ./nb     # 指定 notebook 目录

环境变量映射（--from-env 模式 / Docker 部署）:
  OPS_LLM_PROVIDER        LLM 提供商 (anthropic/openai/zhipu)
  OPS_LLM_API_KEY         API Key
  OPS_LLM_BASE_URL        API Base URL（可选）
  OPS_LLM_MODEL           模型名（可选）
  OPS_TARGET_NAME         目标名称
  OPS_TARGET_TYPE         目标类型 (ssh/docker/k8s/local)
  OPS_TARGET_HOST         SSH 地址 (user@host)
  OPS_TARGET_PORT         SSH 端口（默认 22）
  OPS_TARGET_KEY_FILE     SSH 密钥路径
  OPS_TARGET_PASSWORD_ENV SSH 密码环境变量名
  OPS_NOTIFIER_TYPE       通知类型 (slack/dingtalk/feishu/feishu_app/none)
  OPS_NOTIFIER_WEBHOOK_URL 通知 Webhook URL
"""

from __future__ import annotations

import os
import sys
import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

# ── 常量 ──

NOTEBOOK_CONFIG_DIR = "config"
TARGETS_FILE = "targets.yaml"
LIMITS_FILE = "limits.yaml"
PERMISSIONS_FILE = "permissions.md"
NOTIFIER_FILE = "notifier.yaml"

LLM_PROVIDERS = {
    "anthropic": {"model": "claude-sonnet-4-20250514", "base_url": "", "env_key": "ANTHROPIC_API_KEY"},
    "openai": {"model": "gpt-4o", "base_url": "", "env_key": "OPENAI_API_KEY"},
    "zhipu": {"model": "glm-4-plus", "base_url": "https://open.bigmodel.cn/api/paas/v4/", "env_key": "ZHIPU_API_KEY"},
}

TARGET_TYPES = ["ssh", "docker", "k8s", "local"]
CRITICALITY_LEVELS = ["low", "normal", "high", "critical"]
NOTIFIER_TYPES = ["none", "slack", "dingtalk", "feishu", "feishu_app"]


# ── 输入工具 ──

def _print_banner():
    print()
    print("🚀 Welcome to ops-agent setup!")
    print("   This will guide you through creating the configuration files.")
    print()


def _ask(prompt: str, default: str = "", choices: Optional[List[str]] = None,
         required: bool = False) -> str:
    """交互式输入，支持默认值和选项校验
    
    required=False 时，空输入直接返回 default（适合 optional 字段）
    required=True 时，空输入提示必填
    """
    suffix = f" ({default})" if default else ""
    if choices:
        suffix += f" [{'/'.join(choices)}]"
    while True:
        try:
            val = input(f"? {prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if not val:
            if not required:
                return default
            if default:
                return default
            print("  ⚠️  This field is required.")
            continue
        if choices and val not in choices:
            print(f"  ⚠️  Please choose from: {', '.join(choices)}")
            continue
        return val


def _ask_yesno(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        try:
            val = input(f"? {prompt}{suffix}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if not val:
            return default
        if val in ("y", "yes"):
            return True
        if val in ("n", "no"):
            return False
        print("  ⚠️  Please enter y or n.")


def _ask_password(prompt: str) -> str:
    """安全密码输入"""
    import getpass
    try:
        return getpass.getpass(f"? {prompt}: ")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)


def _get_env(field: str, prompt: str, default: str = "", choices: Optional[List[str]] = None,
             required: bool = False) -> str:
    """从环境变量读，缺了在交互模式下提问，--from-env 模式下报错"""
    val = os.environ.get(field, "")
    if val:
        if choices and val not in choices:
            print(f"❌ {field}={val} is invalid. Choose from: {', '.join(choices)}")
            sys.exit(1)
        return val
    # 没有环境变量
    if os.environ.get("OPS_INIT_FROM_ENV") == "1":
        if required:
            print(f"❌ Missing required env var: {field}")
            sys.exit(1)
        return default
    # 交互模式
    return _ask(prompt, default=default, choices=choices, required=required)


# ── 配置生成 ──

def _generate_targets_yaml(target: dict, source_repo: Optional[dict]) -> str:
    """生成 targets.yaml 内容，用 yaml.dump 确保格式正确无引号问题"""
    try:
        import yaml
    except ImportError:
        raise RuntimeError("pip install pyyaml")

    entry = {
        "name": target.get("name", ""),
        "type": target.get("type", "ssh"),
    }
    if target.get("description"):
        entry["description"] = target["description"]
    entry["criticality"] = target.get("criticality", "normal")

    if target["type"] == "ssh":
        entry["host"] = target.get("host", "")
        if target.get("port") and target["port"] != 22:
            entry["port"] = target["port"]
        if target.get("key_file"):
            entry["key_file"] = target["key_file"]
        if target.get("password_env"):
            entry["password_env"] = target["password_env"]
    elif target["type"] == "docker":
        if target.get("docker_host"):
            entry["docker_host"] = target["docker_host"]
        if target.get("compose_file"):
            entry["compose_file"] = target["compose_file"]
    elif target["type"] == "k8s":
        if target.get("kubeconfig"):
            entry["kubeconfig"] = target["kubeconfig"]
        if target.get("context"):
            entry["context"] = target["context"]
        if target.get("namespace"):
            entry["namespace"] = target["namespace"]

    if source_repo:
        repo = {"name": source_repo["name"], "path": source_repo["path"]}
        for key in ("repo_url", "branch", "language", "build_cmd", "test_cmd",
                     "deploy_cmd", "runtime_service", "log_path", "git_host"):
            if source_repo.get(key):
                repo[key] = source_repo[key]
        entry["source_repos"] = [repo]

    data = {"targets": [entry]}
    content = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return "# Auto-generated by ops-agent init\n\n" + content


def _generate_limits_yaml() -> str:
    """生成安全的默认 limits.yaml"""
    return """\
# Auto-generated by ops-agent init — safe defaults
# Modify as needed. Agent reloads on next loop cycle.

enabled: true
max_actions_per_hour: 20
max_actions_per_day: 100
max_restarts_per_service_per_hour: 3
max_restarts_per_service_per_day: 5
max_concurrent_incidents: 2
cooldown_after_failure_seconds: 600
llm_tokens_per_hour: 200000
llm_tokens_per_day: 1000000
max_collab_auto_rounds: 30
max_observations_chars: 8000
max_total_rounds: 40
max_diagnose_rounds: 25
max_fix_attempts: 3
silence_window_seconds: 1800
max_observe_commands: 15
max_verify_steps: 15
max_quick_observe_commands: 20
max_gap_commands: 20
max_generated_gap_commands: 20
max_chat_commands: 20
max_collab_history_rounds: 25
max_recent_incidents: 15
max_patch_attempts: 3
max_source_locations: 15
max_unresolved_frames: 5
"""


def _generate_permissions_md() -> str:
    """生成默认 permissions.md"""
    return """\
# Authorization Rules (Auto-generated by ops-agent init)

## Default Policy
- Read-only observation commands (L0): execute directly
- Write Notebook (L1): execute directly
- Restart non-core services (L2): notify human, then execute
- Restart core services (L2-core): require human approval
- Modify config files (L2): notify human, backup first
- Modify code / submit PR (L3): require human approval
- Destructive operations (L4): always deny

## Core Services
Restart/stop of these services requires human approval:
- mysql / mariadb / postgresql
- redis / memcached
- nginx / haproxy
- docker / containerd

## Trust Evolution
- First 7 days: all L2 operations require approval
- 10 consecutive successful L2 operations: non-core L2 downgrades to notify-then-do
- Any L2 failure causing rollback: trust resets, L2 requires approval again

## Emergency
In severe failure situations (service completely down, data loss risk),
Agent may execute L2 operations while notifying human, without waiting for approval.
Reason must be documented in the Incident.
"""


def _generate_notifier_yaml(notifier_type: str) -> str:
    """生成 notifier.yaml"""
    if notifier_type == "none":
        return """\
# Auto-generated by ops-agent init
type: none
"""
    lines = [
        "# Auto-generated by ops-agent init",
        f"type: {notifier_type}",
        "",
        f'webhook_url: "{os.environ.get("OPS_NOTIFIER_WEBHOOK_URL", "")}"',
    ]
    if notifier_type == "feishu_app":
        lines.extend([
            "",
            "feishu_app:",
            f'  app_id: "{os.environ.get("OPS_FEISHU_APP_ID", "")}"',
            f'  app_secret: "{os.environ.get("OPS_FEISHU_APP_SECRET", "")}"',
            f'  chat_id: "{os.environ.get("OPS_FEISHU_CHAT_ID", "")}"',
            "  interactive:",
            "    enabled: false",
            "    callback_port: 9877",
            '    encrypt_key: ""',
            '    verification_token: ""',
        ])
    lines.extend([
        "",
        "notify_on:",
        "  - incident_opened",
        "  - incident_closed",
        "  - critical_failure",
        "",
        'quiet_hours:',
        '  start: "22:00"',
        '  end: "08:00"',
        "  except_urgency:",
        "    - critical",
        "",
    ])
    return "\n".join(lines)


# ── 连通性测试 ──

def _test_llm(provider: str, api_key: str, base_url: str, model: str) -> bool:
    """测试 LLM API 连通性"""
    try:
        if provider == "anthropic":
            import anthropic
            kwargs = {"api_key": api_key}
            client = anthropic.Anthropic(**kwargs)
            # 发一个最小请求测试连通
            resp = client.messages.create(
                model=model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Say OK"}],
            )
            return True
        elif provider in ("openai", "zhipu"):
            import openai
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = openai.OpenAI(**kwargs)
            resp = client.chat.completions.create(
                model=model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Say OK"}],
            )
            return True
    except Exception as e:
        print(f"  ❌ LLM connection test failed: {e}")
        return False


def _test_ssh(host: str, port: int = 22, key_file: str = "", password_env: str = "") -> bool:
    """测试 SSH 连通性"""
    cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no", "-p", str(port)]
    if key_file:
        cmd.extend(["-i", key_file])
    cmd.append(host)
    cmd.append("echo OK")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and "OK" in result.stdout:
            return True
        # 尝试 sshpass
        if password_env:
            password = os.environ.get(password_env, "")
            if password:
                sshpass_cmd = ["sshpass", "-p", password] + cmd
                result = subprocess.run(sshpass_cmd, capture_output=True, text=True, timeout=10)
                return result.returncode == 0 and "OK" in result.stdout
        print(f"  ❌ SSH connection failed: {result.stderr.strip()}")
        return False
    except FileNotFoundError:
        print("  ❌ ssh command not found")
        return False
    except subprocess.TimeoutExpired:
        print("  ❌ SSH connection timed out")
        return False


# ── 主流程 ──

def run_init(notebook_path: str = "./notebook", from_env: bool = False):
    """运行 init 引导"""

    if from_env:
        os.environ["OPS_INIT_FROM_ENV"] = "1"

    config_dir = Path(notebook_path) / NOTEBOOK_CONFIG_DIR

    # 检查已有配置
    targets_path = config_dir / TARGETS_FILE
    if targets_path.exists():
        if not from_env:
            overwrite = _ask_yesno(
                f"Found existing {targets_path}. Overwrite?", default=False
            )
            if not overwrite:
                print("ℹ️  Keeping existing configuration. Exiting.")
                return
        else:
            print(f"⚠️  Overwriting existing {targets_path}")

    _print_banner()

    # ── 1. LLM 配置 ──
    print("━━━ LLM Configuration ━━━")
    provider = _get_env("OPS_LLM_PROVIDER", "LLM Provider", default="anthropic", choices=list(LLM_PROVIDERS.keys()))
    defaults = LLM_PROVIDERS[provider]

    api_key = os.environ.get("OPS_LLM_API_KEY", "")
    if not api_key and not from_env:
        # 尝试从 provider 默认环境变量读
        default_env_key = defaults["env_key"]
        api_key = os.environ.get(default_env_key, "")
    if not api_key and not from_env:
        api_key = _ask_password(f"API Key (env: {defaults['env_key']})")
    elif not api_key:
        print(f"❌ Missing API key. Set OPS_LLM_API_KEY or {defaults['env_key']}")
        sys.exit(1)

    base_url = _get_env("OPS_LLM_BASE_URL", "API Base URL", default=defaults["base_url"])
    model = _get_env("OPS_LLM_MODEL", "Model", default=defaults["model"])

    # 设置环境变量供后续 Agent 使用
    os.environ["OPS_LLM_PROVIDER"] = provider
    os.environ["OPS_LLM_API_KEY"] = api_key
    if base_url:
        os.environ["OPS_LLM_BASE_URL"] = base_url
    os.environ["OPS_LLM_MODEL"] = model

    # 测试 LLM
    if not from_env:
        do_test = _ask_yesno("Test LLM connection now?", default=True)
        if do_test:
            print("  Testing LLM connection...")
            if _test_llm(provider, api_key, base_url, model):
                print("  ✅ LLM connection OK")
            else:
                print("  ⚠️  LLM test failed. You can fix config later and re-run init.")

    # ── 2. 目标配置 ──
    print()
    print("━━━ Target Configuration ━━━")
    target_type = _get_env("OPS_TARGET_TYPE", "Target type", default="ssh", choices=TARGET_TYPES)

    target = {"type": target_type}
    target["name"] = _get_env("OPS_TARGET_NAME", "Target name", default=f"my-{target_type}")
    target["criticality"] = _get_env(
        "OPS_TARGET_CRITICALITY", "Criticality",
        default="normal", choices=CRITICALITY_LEVELS,
    )
    target["description"] = _get_env(
        "OPS_TARGET_DESCRIPTION", "Description (optional)", default="",
    )

    if target_type == "ssh":
        target["host"] = _get_env("OPS_TARGET_HOST", "SSH address (user@host)", required=True)
        target["port"] = int(_get_env("OPS_TARGET_PORT", "SSH port", default="22"))
        target["key_file"] = _get_env("OPS_TARGET_KEY_FILE", "SSH key path (optional)", default="")
        target["password_env"] = _get_env(
            "OPS_TARGET_PASSWORD_ENV",
            "SSH password env var name (optional, e.g. WEB_PROD_PASSWORD)",
            default="",
        )
    elif target_type == "docker":
        target["docker_host"] = _get_env(
            "OPS_TARGET_DOCKER_HOST", "Docker host (empty=local socket)", default="",
        )
        target["compose_file"] = _get_env(
            "OPS_TARGET_COMPOSE_FILE", "docker-compose.yaml path (optional)", default="",
        )
    elif target_type == "k8s":
        target["kubeconfig"] = _get_env(
            "OPS_TARGET_KUBECONFIG", "kubeconfig path", default="~/.kube/config",
        )
        target["context"] = _get_env("OPS_TARGET_CONTEXT", "kubectl context", default="")
        target["namespace"] = _get_env("OPS_TARGET_NAMESPACE", "Namespace", default="default")

    # 测试 SSH
    if target_type == "ssh" and not from_env:
        do_test = _ask_yesno("Test SSH connection now?", default=True)
        if do_test:
            print("  Testing SSH connection...")
            if _test_ssh(
                target["host"], target.get("port", 22),
                target.get("key_file", ""), target.get("password_env", ""),
            ):
                print("  ✅ SSH connection OK")
            else:
                print("  ⚠️  SSH test failed. You can fix config later.")

    # ── 3. 源码仓库（可选）──
    # 源码总目录：所有 source_repos.path 必须是其子目录
    # Docker 部署时此目录通过 volume 挂载进容器
    source_repos_dir = _get_env(
        "OPS_SOURCE_REPOS_DIR", "Source repos base directory",
        default="/opt/vol/source-projs",
    )
    source_repos_dir = os.path.abspath(source_repos_dir)

    source_repo = None
    if not from_env:
        add_repo = _ask_yesno("Configure a source repo for this target?", default=False)
    else:
        # from-env 模式：有 OPS_REPO_NAME 就配
        add_repo = bool(os.environ.get("OPS_REPO_NAME"))

    if add_repo:
        repo = {}
        repo["name"] = _get_env("OPS_REPO_NAME", "Repo name", default="app")

        # repo path 必须在 source_repos_dir 下
        if not from_env:
            print(f"  ℹ️  Repo path must be under {source_repos_dir}/")
            repo_subdir = _get_env(
                "OPS_REPO_SUBDIR", "Subdirectory under source repos dir",
                default=repo["name"],
            )
            repo["path"] = os.path.join(source_repos_dir, repo_subdir)
            print(f"  → Full path: {repo['path']}")
        else:
            repo["path"] = _get_env("OPS_REPO_PATH", "Local clone path", required=True)

        # 校验路径存在
        if os.path.isdir(repo["path"]):
            print(f"  ✅ Path exists: {repo['path']}")
        else:
            print(f"  ⚠️  Path does not exist: {repo['path']}")
            print(f"     Make sure to clone the repo before running ops-agent.")
            if not from_env:
                if not _ask_yesno("Continue anyway?", default=False):
                    repo["path"] = input("  Enter correct repo path: ").strip()

        # 校验路径在 source_repos_dir 下
        if not os.path.abspath(repo["path"]).startswith(source_repos_dir + os.sep) and \
           os.path.abspath(repo["path"]) != source_repos_dir:
            print(f"  ⚠️  Path {repo['path']} is not under {source_repos_dir}")
            print(f"     Docker deployments require all repos under SOURCE_REPOS_DIR.")
            print(f"     Please update .env SOURCE_REPOS_DIR or move the repo.")
        repo["repo_url"] = _get_env("OPS_REPO_URL", "Git remote URL (optional)", default="")
        repo["branch"] = _get_env("OPS_REPO_BRANCH", "Branch", default="main")
        repo["language"] = _get_env(
            "OPS_REPO_LANGUAGE", "Language (python/java/go/node/rust/...)",
            default="",
        )
        repo["build_cmd"] = _get_env("OPS_REPO_BUILD_CMD", "Build command (optional)", default="")
        repo["test_cmd"] = _get_env("OPS_REPO_TEST_CMD", "Test command (optional)", default="")
        repo["deploy_cmd"] = _get_env("OPS_REPO_DEPLOY_CMD", "Deploy command (optional)", default="")
        repo["runtime_service"] = _get_env(
            "OPS_REPO_RUNTIME_SERVICE", "Runtime service name (optional)", default="",
        )
        repo["log_path"] = _get_env("OPS_REPO_LOG_PATH", "Log path (optional)", default="")
        repo["git_host"] = _get_env(
            "OPS_REPO_GIT_HOST", "Git host (github/gitlab/noop, optional)",
            default="",
        )
        source_repo = repo

    # ── 4. 通知配置 ──
    print()
    print("━━━ Notification (optional) ━━━")
    notifier_type = _get_env(
        "OPS_NOTIFIER_TYPE", "Notification type",
        default="none", choices=NOTIFIER_TYPES,
    )

    # ── 5. 写入文件 ──
    print()
    config_dir.mkdir(parents=True, exist_ok=True)

    # targets.yaml
    targets_content = _generate_targets_yaml(target, source_repo)
    targets_path.write_text(targets_content, encoding="utf-8")
    print(f"✅ {targets_path}")

    # limits.yaml — 只在不存在时生成
    limits_path = config_dir / LIMITS_FILE
    if not limits_path.exists():
        limits_path.write_text(_generate_limits_yaml(), encoding="utf-8")
        print(f"✅ {limits_path}")
    else:
        print(f"ℹ️  {limits_path} already exists, skipping")

    # permissions.md — 只在不存在时生成
    perm_path = config_dir / PERMISSIONS_FILE
    if not perm_path.exists():
        perm_path.write_text(_generate_permissions_md(), encoding="utf-8")
        print(f"✅ {perm_path}")
    else:
        print(f"ℹ️  {perm_path} already exists, skipping")

    # notifier.yaml
    if notifier_type != "none":
        notifier_path = config_dir / NOTIFIER_FILE
        notifier_path.write_text(_generate_notifier_yaml(notifier_type), encoding="utf-8")
        print(f"✅ {notifier_path}")

    # ── 6. 持久化环境变量 ──
    print()
    print("━━━ Environment Variables ━━━")

    # 写 .env 文件到 notebook 目录
    env_path = Path(notebook_path) / ".env"
    env_lines = [
        f"OPS_LLM_PROVIDER={provider}",
        f"OPS_LLM_API_KEY={api_key}",
    ]
    if base_url:
        env_lines.append(f"OPS_LLM_BASE_URL={base_url}")
    env_lines.append(f"OPS_LLM_MODEL={model}")
    if target_type == "ssh" and target.get("password_env"):
        env_lines.append(f"# {target['password_env']}=<your-password>")

    # 保留已有 .env 中非 ops 的行
    existing_ops_lines = set()
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("OPS_"):
                existing_ops_lines.add(line.split("=")[0])

    existing_other = []
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if not line.startswith("OPS_") and line.strip() and not line.startswith("#"):
                existing_other.append(line)

    env_content = "# Auto-generated by ops-agent init\n"
    env_content += "# Source: source .env or setenv\n\n"
    env_content += "\n".join(env_lines) + "\n"
    if existing_other:
        env_content += "\n# Other variables\n" + "\n".join(existing_other) + "\n"
    env_path.write_text(env_content, encoding="utf-8")
    print(f"✅ {env_path}")

    # 写入 shell rc
    shell_rc = ""
    for rc in [".bashrc", ".zshrc", ".profile"]:
        rc_path = Path.home() / rc
        if rc_path.exists():
            shell_rc = str(rc_path)
            break

    if shell_rc:
        rc_content = Path(shell_rc).read_text()
        exports_added = []
        for var in ["OPS_LLM_PROVIDER", "OPS_LLM_API_KEY", "OPS_LLM_BASE_URL", "OPS_LLM_MODEL"]:
            # 检查是否已有
            if var not in rc_content:
                if var == "OPS_LLM_BASE_URL" and not base_url:
                    continue
                if var == "OPS_LLM_API_KEY":
                    # API key 用 .env 文件 source，不直接写 rc
                    continue
                if var == "OPS_LLM_PROVIDER":
                    exports_added.append(f"export OPS_LLM_PROVIDER={provider}")
                elif var == "OPS_LLM_MODEL":
                    exports_added.append(f"export OPS_LLM_MODEL={model}")
                elif var == "OPS_LLM_BASE_URL":
                    exports_added.append(f"export OPS_LLM_BASE_URL={base_url}")

        # 添加 source .env 到 shell rc
        env_source_line = f'source "{env_path}"'
        if env_source_line not in rc_content and str(env_path) not in rc_content:
            exports_added.insert(0, f"# ops-agent env")
            exports_added.append(env_source_line)

        if exports_added:
            with open(shell_rc, "a") as f:
                f.write("\n" + "\n".join(exports_added) + "\n")
            print(f"✅ Added to {shell_rc}")
        else:
            print(f"ℹ️  {shell_rc} already configured")
    else:
        print("⚠️  No shell rc found, please manually set env vars")

    # ── 7. 下一步 ──
    print()
    print("━━━ Next Steps ━━━")
    print(f"  1. Load env:  source {env_path}")
    print(f"  2. Start:     ops-agent --notebook {notebook_path}")
