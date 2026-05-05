"""Tests for ops-agent check"""

import os
import sys
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.check import run_check, CheckResult, check_env, check_targets, check_limits


class TestCheckResult:
    def test_ok_when_no_errors(self):
        r = CheckResult()
        assert r.ok
        r.warn("something")
        assert r.ok

    def test_not_ok_with_errors(self):
        r = CheckResult()
        r.error("bad")
        assert not r.ok


class TestCheckEnv:
    def test_missing_api_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OPS_LLM_API_KEY", None)
                os.environ.pop("OPS_LLM_PROVIDER", None)
                r = CheckResult()
                check_env(tmpdir, r)
                assert not r.ok

    def test_has_api_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text("OPS_LLM_API_KEY=sk-test\nOPS_LLM_PROVIDER=anthropic\n")
            # .env 加载由 run_check 做，这里直接设环境变量
            with patch.dict(os.environ, {"OPS_LLM_API_KEY": "sk-test", "OPS_LLM_PROVIDER": "anthropic"}):
                r = CheckResult()
                check_env(tmpdir, r)
                assert r.ok


class TestCheckTargets:
    def test_missing_targets_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            r = CheckResult()
            check_targets(tmpdir, r)
            assert not r.ok

    def test_valid_targets_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            (config_dir / "targets.yaml").write_text(
                "targets:\n  - name: web\n    type: ssh\n    host: ubuntu@10.0.0.1\n"
            )
            r = CheckResult()
            check_targets(tmpdir, r)
            assert r.ok

    def test_invalid_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            (config_dir / "targets.yaml").write_text("targets: [invalid yaml")
            r = CheckResult()
            check_targets(tmpdir, r)
            assert not r.ok

    def test_ssh_missing_host(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            (config_dir / "targets.yaml").write_text(
                "targets:\n  - name: web\n    type: ssh\n"
            )
            r = CheckResult()
            check_targets(tmpdir, r)
            assert not r.ok


class TestRunCheck:
    def test_full_check_pass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            (config_dir / "targets.yaml").write_text(
                "targets:\n  - name: web\n    type: local\n"
            )
            (config_dir / "limits.yaml").write_text(
                "enabled: true\nmax_actions_per_hour: 20\n"
            )
            (config_dir / "permissions.md").write_text(
                "# Authorization Rules\nL0: execute directly\n"
            )
            (Path(tmpdir) / ".env").write_text("OPS_LLM_API_KEY=sk-test\n")

            with patch.dict(os.environ, {"OPS_LLM_API_KEY": "sk-test"}):
                # should not exit
                run_check(workspace_path=tmpdir, test_llm=False)
