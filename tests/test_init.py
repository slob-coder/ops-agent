"""Tests for ops-agent init"""

import os
import sys
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

# 确保可以 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.init import (
    _generate_targets_yaml,
    _generate_limits_yaml,
    _generate_permissions_md,
    _generate_notifier_yaml,
    _ask,
    _get_env,
    run_init,
)


class TestTargetsYaml:
    """targets.yaml 生成测试"""

    def test_ssh_target_minimal(self):
        target = {"type": "ssh", "name": "my-ssh", "host": "ubuntu@10.0.0.1"}
        result = _generate_targets_yaml(target, None)
        assert "name: my-ssh" in result
        assert "type: ssh" in result
        assert "host: ubuntu@10.0.0.1" in result
        assert "source_repos" not in result

    def test_ssh_target_with_key(self):
        target = {
            "type": "ssh", "name": "prod", "host": "root@1.2.3.4",
            "port": 2222, "key_file": "~/.ssh/id_rsa",
            "criticality": "high",
        }
        result = _generate_targets_yaml(target, None)
        assert "port: 2222" in result
        assert "key_file: ~/.ssh/id_rsa" in result
        assert "criticality: high" in result

    def test_ssh_target_default_port_omitted(self):
        target = {"type": "ssh", "name": "t", "host": "u@h", "port": 22}
        result = _generate_targets_yaml(target, None)
        assert "port:" not in result

    def test_docker_target(self):
        target = {
            "type": "docker", "name": "local-docker",
            "compose_file": "./docker-compose.yaml",
        }
        result = _generate_targets_yaml(target, None)
        assert "type: docker" in result
        assert "compose_file" in result

    def test_k8s_target(self):
        target = {
            "type": "k8s", "name": "prod-k8s",
            "kubeconfig": "~/.kube/config", "namespace": "production",
        }
        result = _generate_targets_yaml(target, None)
        assert "type: k8s" in result
        assert "namespace: production" in result

    def test_local_target(self):
        target = {"type": "local", "name": "local"}
        result = _generate_targets_yaml(target, None)
        assert "type: local" in result
        assert "host:" not in result

    def test_with_source_repo(self):
        target = {"type": "ssh", "name": "web", "host": "u@h"}
        repo = {
            "name": "backend", "path": "/opt/sources/backend",
            "repo_url": "git@github.com:co/backend.git",
            "branch": "main", "language": "python",
            "build_cmd": "make build", "test_cmd": "pytest",
            "deploy_cmd": "make deploy",
        }
        result = _generate_targets_yaml(target, repo)
        assert "source_repos:" in result
        assert "name: backend" in result
        assert "language: python" in result
        assert "build_cmd: make build" in result

    def test_with_description(self):
        target = {"type": "ssh", "name": "t", "host": "u@h", "description": "prod server"}
        result = _generate_targets_yaml(target, None)
        assert "description: prod server" in result

    def test_yaml_parseable(self):
        """生成的 YAML 能被 yaml.safe_load 正确解析"""
        import yaml
        target = {
            "type": "ssh", "name": "web-prod", "host": "ubuntu@10.0.0.10",
            "key_file": "~/.ssh/id_rsa", "criticality": "high",
            "description": "生产 web 服务器",
        }
        repo = {
            "name": "backend", "path": "/opt/src/backend",
            "repo_url": "git@github.com:co/backend.git",
            "branch": "main", "language": "python",
            "build_cmd": "make build",
        }
        result = _generate_targets_yaml(target, repo)
        data = yaml.safe_load(result)
        assert len(data["targets"]) == 1
        t = data["targets"][0]
        assert t["name"] == "web-prod"
        assert t["host"] == "ubuntu@10.0.0.10"
        assert t["key_file"] == "~/.ssh/id_rsa"
        assert t["source_repos"][0]["repo_url"] == "git@github.com:co/backend.git"

    def test_no_double_quotes(self):
        """确保不会出现双引号问题"""
        target = {
            "type": "docker", "name": "local-docker",
            "description": "本地 docker-compose 项目",
        }
        result = _generate_targets_yaml(target, None)
        assert '""' not in result  # 不应该有连续双引号


class TestLimitsYaml:
    """limits.yaml 生成测试"""

    def test_generates_valid_yaml(self):
        import yaml
        content = _generate_limits_yaml()
        data = yaml.safe_load(content)
        assert data["enabled"] is True
        assert data["max_actions_per_hour"] == 20
        assert data["max_concurrent_incidents"] == 2
        assert data["cooldown_after_failure_seconds"] == 600

    def test_has_all_required_fields(self):
        content = _generate_limits_yaml()
        required = [
            "enabled", "max_actions_per_hour", "max_actions_per_day",
            "max_concurrent_incidents", "cooldown_after_failure_seconds",
            "silence_window_seconds", "max_total_rounds",
            "max_fix_attempts", "max_diagnose_rounds",
        ]
        for field in required:
            assert field in content, f"Missing field: {field}"


class TestPermissionsMd:
    """permissions.md 生成测试"""

    def test_generates_content(self):
        content = _generate_permissions_md()
        assert "L0" in content
        assert "L4" in content
        assert "Core Services" in content


class TestNotifierYaml:
    """notifier.yaml 生成测试"""

    def test_none_type(self):
        result = _generate_notifier_yaml("none")
        assert "type: none" in result

    def test_slack_type(self):
        result = _generate_notifier_yaml("slack")
        assert "type: slack" in result

    def test_feishu_app_type(self):
        with patch.dict(os.environ, {
            "OPS_FEISHU_APP_ID": "cli_test",
            "OPS_FEISHU_APP_SECRET": "secret_test",
            "OPS_FEISHU_CHAT_ID": "oc_test",
        }):
            result = _generate_notifier_yaml("feishu_app")
            assert "type: feishu_app" in result
            assert "cli_test" in result


class TestGetEnv:
    """_get_env 函数测试"""

    def test_reads_from_env(self):
        with patch.dict(os.environ, {"MY_FIELD": "hello"}):
            val = _get_env("MY_FIELD", "Some prompt")
            assert val == "hello"

    def test_from_env_mode_missing(self):
        with patch.dict(os.environ, {"OPS_INIT_FROM_ENV": "1"}, clear=False):
            os.environ.pop("MISSING_FIELD_XYZ", None)
            with pytest.raises(SystemExit):
                _get_env("MISSING_FIELD_XYZ", "Some prompt", required=True)

    def test_from_env_mode_missing_with_default(self):
        """非 required 的字段缺 env var 时返回默认值"""
        with patch.dict(os.environ, {"OPS_INIT_FROM_ENV": "1"}, clear=False):
            os.environ.pop("MISSING_FIELD_XYZ", None)
            val = _get_env("MISSING_FIELD_XYZ", "Some prompt", default="fallback")
            assert val == "fallback"

    def test_from_env_mode_with_default(self):
        with patch.dict(os.environ, {"OPS_INIT_FROM_ENV": "1"}, clear=False):
            os.environ.pop("SOME_FIELD_XYZ", None)
            val = _get_env("SOME_FIELD_XYZ", "Some prompt", default="fallback")
            assert val == "fallback"

    def test_choices_validation(self):
        with patch.dict(os.environ, {"MY_FIELD": "invalid"}, clear=False):
            with pytest.raises(SystemExit):
                _get_env("MY_FIELD", "Pick", choices=["a", "b"])


class TestRunInit:
    """run_init 集成测试（from-env 模式）"""

    def test_from_env_generates_files(self):
        """测试 --from-env 模式完整流程"""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "OPS_INIT_FROM_ENV": "1",
                "OPS_LLM_PROVIDER": "anthropic",
                "OPS_LLM_API_KEY": "sk-test-fake-key-123",
                "OPS_LLM_MODEL": "claude-sonnet-4-20250514",
                "OPS_TARGET_TYPE": "ssh",
                "OPS_TARGET_NAME": "test-server",
                "OPS_TARGET_HOST": "ubuntu@10.0.0.1",
                "OPS_NOTIFIER_TYPE": "none",
            }
            with patch.dict(os.environ, env, clear=False):
                run_init(workspace_path=tmpdir, from_env=True)

            config_dir = Path(tmpdir) / "config"
            # 检查 targets.yaml
            targets_path = config_dir / "targets.yaml"
            assert targets_path.exists()
            content = targets_path.read_text()
            assert "test-server" in content
            assert "ubuntu@10.0.0.1" in content

            # 验证 YAML 可解析
            import yaml
            data = yaml.safe_load(content)
            assert data["targets"][0]["host"] == "ubuntu@10.0.0.1"

            # 检查 limits.yaml
            limits_path = config_dir / "limits.yaml"
            assert limits_path.exists()

            # 检查 permissions.md
            perm_path = config_dir / "permissions.md"
            assert perm_path.exists()

    def test_from_env_local_target(self):
        """测试 local 类型目标"""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "OPS_INIT_FROM_ENV": "1",
                "OPS_LLM_PROVIDER": "openai",
                "OPS_LLM_API_KEY": "sk-test",
                "OPS_TARGET_TYPE": "local",
                "OPS_TARGET_NAME": "localhost",
                "OPS_NOTIFIER_TYPE": "none",
            }
            with patch.dict(os.environ, env, clear=False):
                run_init(workspace_path=tmpdir, from_env=True)

            content = (Path(tmpdir) / "config" / "targets.yaml").read_text()
            assert "type: local" in content

    def test_from_env_with_repo(self):
        """测试带源码仓库的配置"""
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "OPS_INIT_FROM_ENV": "1",
                "OPS_LLM_PROVIDER": "anthropic",
                "OPS_LLM_API_KEY": "sk-test",
                "OPS_TARGET_TYPE": "ssh",
                "OPS_TARGET_NAME": "web",
                "OPS_TARGET_HOST": "u@h",
                "OPS_REPO_NAME": "backend",
                "OPS_REPO_PATH": "/opt/src/backend",
                "OPS_REPO_LANGUAGE": "python",
                "OPS_NOTIFIER_TYPE": "none",
            }
            with patch.dict(os.environ, env, clear=False):
                run_init(workspace_path=tmpdir, from_env=True)

            content = (Path(tmpdir) / "config" / "targets.yaml").read_text()
            assert "source_repos:" in content
            assert "name: backend" in content
            assert "language: python" in content

    def test_existing_limits_not_overwritten(self):
        """已有的 limits.yaml 不应被覆盖"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "limits.yaml").write_text("custom: true\n")

            env = {
                "OPS_INIT_FROM_ENV": "1",
                "OPS_LLM_PROVIDER": "anthropic",
                "OPS_LLM_API_KEY": "sk-test",
                "OPS_TARGET_TYPE": "local",
                "OPS_TARGET_NAME": "t",
                "OPS_NOTIFIER_TYPE": "none",
            }
            with patch.dict(os.environ, env, clear=False):
                run_init(workspace_path=tmpdir, from_env=True)

            content = (config_dir / "limits.yaml").read_text()
            assert "custom: true" in content

    def test_docker_target_with_compose(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "OPS_INIT_FROM_ENV": "1",
                "OPS_LLM_PROVIDER": "anthropic",
                "OPS_LLM_API_KEY": "sk-test",
                "OPS_TARGET_TYPE": "docker",
                "OPS_TARGET_NAME": "local-docker",
                "OPS_TARGET_COMPOSE_FILE": "./docker-compose.yaml",
                "OPS_NOTIFIER_TYPE": "none",
            }
            with patch.dict(os.environ, env, clear=False):
                run_init(workspace_path=tmpdir, from_env=True)

            content = (Path(tmpdir) / "config" / "targets.yaml").read_text()
            assert "type: docker" in content
            assert "compose_file" in content

    def test_k8s_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "OPS_INIT_FROM_ENV": "1",
                "OPS_LLM_PROVIDER": "anthropic",
                "OPS_LLM_API_KEY": "sk-test",
                "OPS_TARGET_TYPE": "k8s",
                "OPS_TARGET_NAME": "prod-k8s",
                "OPS_TARGET_NAMESPACE": "production",
                "OPS_NOTIFIER_TYPE": "none",
            }
            with patch.dict(os.environ, env, clear=False):
                run_init(workspace_path=tmpdir, from_env=True)

            content = (Path(tmpdir) / "config" / "targets.yaml").read_text()
            assert "type: k8s" in content
            assert "namespace: production" in content
