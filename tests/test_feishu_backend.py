"""
FeishuBackend 测试
"""

import json
import queue
import threading
import time
import pytest
from unittest.mock import patch, MagicMock

from src.infra.feishu_backend import (
    FeishuBackend, FeishuBackendConfig, FeishuTokenManager, URGENCY_EMOJI,
)


class TestFeishuTokenManager:
    def test_get_token_success(self):
        mgr = FeishuTokenManager("app123", "secret123")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "code": 0, "tenant_access_token": "tk_abc", "expire": 7200
        }).encode()
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("src.infra.feishu_backend.urlopen", return_value=mock_resp):
            token = mgr.get_token()
        assert token == "tk_abc"

    def test_get_token_failure(self):
        mgr = FeishuTokenManager("app123", "secret123")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "code": 10001, "msg": "invalid app_id"
        }).encode()
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("src.infra.feishu_backend.urlopen", return_value=mock_resp):
            token = mgr.get_token()
        assert token == ""

    def test_token_cached(self):
        mgr = FeishuTokenManager("app123", "secret123")
        mgr._token = "tk_cached"
        mgr._token_expires = time.time() + 3600
        token = mgr.get_token()
        assert token == "tk_cached"


class TestFeishuBackend:
    def setup_method(self):
        self.inbox = queue.Queue()
        self.approval_queue = queue.Queue()
        self.interrupted = threading.Event()
        self.backend = FeishuBackend(
            app_id="test_app", app_secret="test_secret",
            chat_id="oc_test", callback_port=19877,
        )

    def test_start_and_stop(self):
        self.backend.start(self.inbox, self.approval_queue, self.interrupted)
        assert self.backend._server is not None
        self.backend.stop()

    def test_on_message_inbox(self):
        self.backend._waiting_approval = False
        self.backend._inbox = self.inbox
        self.backend._interrupted = self.interrupted
        self.backend._on_message("status")
        assert not self.inbox.empty()
        source, text = self.inbox.get_nowait()
        assert source == "feishu"
        assert text == "status"
        assert self.interrupted.is_set()

    def test_on_message_approval(self):
        self.backend._waiting_approval = True
        self.backend._approval_queue = self.approval_queue
        self.backend._interrupted = self.interrupted
        self.backend._on_message("y")
        assert not self.approval_queue.empty()
        source, text = self.approval_queue.get_nowait()
        assert source == "feishu"
        assert text == "y"

    def test_set_waiting_approval(self):
        assert self.backend._waiting_approval is False
        self.backend.set_waiting_approval(True)
        assert self.backend._waiting_approval is True

    def test_send_text(self):
        self.backend._token_mgr._token = "tk_test"
        self.backend._token_mgr._token_expires = time.time() + 3600
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"code": 0}).encode()
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("src.infra.feishu_backend.urlopen", return_value=mock_resp) as mock_urlopen:
            result = self.backend._send_text("hello")
        assert result is True
        # verify the request was made
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "Bearer tk_test" in req.get_header("Authorization")

    def test_send_text_truncation(self):
        long_text = "x" * 5000
        truncated = self.backend._send_text.__code__  # just check it doesn't crash
        # Actually test truncation in _send_text
        self.backend._token_mgr._token = "tk_test"
        self.backend._token_mgr._token_expires = time.time() + 3600
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"code": 0}).encode()
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)

        sent_body = {}
        def capture_urlopen(req, timeout=10):
            body = json.loads(req.data.decode())
            sent_body.update(body)
            return mock_resp

        with patch("src.infra.feishu_backend.urlopen", side_effect=capture_urlopen):
            self.backend._send_text("x" * 5000)
        content = json.loads(sent_body.get("content", "{}"))
        assert len(content["text"]) <= 3510  # 3500 + "\n...(已截断)"

    def test_send_log_only_warning_and_critical(self):
        """log 只推 warning 和 critical"""
        self.backend._token_mgr._token = "tk_test"
        self.backend._token_mgr._token_expires = time.time() + 3600
        with patch("src.infra.feishu_backend.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"code": 0}).encode()
            mock_resp.__enter__ = lambda s: mock_resp
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            self.backend.send_log("observe msg", "observe")
            assert mock_urlopen.call_count == 0

            self.backend.send_log("warning msg", "warning")
            assert mock_urlopen.call_count == 1

            self.backend.send_log("critical msg", "critical")
            assert mock_urlopen.call_count == 2

    def test_cmd_log_silent(self):
        """cmd_log 不推飞书"""
        with patch("src.infra.feishu_backend.urlopen") as mock_urlopen:
            self.backend.send_cmd_log("ls -la")
            assert mock_urlopen.call_count == 0

    def test_extract_text(self):
        event = {
            "message": {
                "message_type": "text",
                "content": json.dumps({"text": "hello world"}),
            }
        }
        text = self.backend._extract_text(event, "text")
        assert text == "hello world"

    def test_extract_text_empty(self):
        event = {"message": {"message_type": "image", "content": "{}"}}
        text = self.backend._extract_text(event, "image")
        assert text == ""

    def test_check_event_dedup(self):
        assert self.backend._check_event("evt1") is True
        assert self.backend._check_event("evt1") is False
        assert self.backend._check_event("evt2") is True


class TestFeishuBackendConfig:
    def test_defaults(self):
        cfg = FeishuBackendConfig()
        assert cfg.enabled is False
        assert cfg.callback_port == 9877

    def test_from_yaml_missing_file(self, tmp_path):
        cfg = FeishuBackendConfig.from_yaml(str(tmp_path / "nonexistent.yaml"))
        assert cfg.enabled is False

    def test_from_yaml_with_config(self, tmp_path):
        yaml_content = """
feishu_app:
  app_id: cli_test
  app_secret: secret_test
  chat_id: oc_test
  interactive:
    enabled: true
    callback_port: 9999
    encrypt_key: ek123
    verification_token: vt123
"""
        cfg_path = tmp_path / "notifier.yaml"
        cfg_path.write_text(yaml_content)
        cfg = FeishuBackendConfig.from_yaml(str(cfg_path))
        assert cfg.enabled is True
        assert cfg.app_id == "cli_test"
        assert cfg.callback_port == 9999
        assert cfg.encrypt_key == "ek123"

    def test_from_yaml_env_override(self, tmp_path):
        yaml_content = """
feishu_app:
  app_id: cli_yaml
  app_secret: secret_yaml
  chat_id: oc_yaml
  interactive:
    enabled: true
"""
        cfg_path = tmp_path / "notifier.yaml"
        cfg_path.write_text(yaml_content)
        with patch.dict("os.environ", {"OPS_FEISHU_APP_ID": "cli_env"}):
            cfg = FeishuBackendConfig.from_yaml(str(cfg_path))
        assert cfg.app_id == "cli_env"

    def test_from_yaml_not_enabled(self, tmp_path):
        yaml_content = """
feishu_app:
  app_id: cli_test
  app_secret: secret_test
  chat_id: oc_test
  interactive:
    enabled: false
"""
        cfg_path = tmp_path / "notifier.yaml"
        cfg_path.write_text(yaml_content)
        cfg = FeishuBackendConfig.from_yaml(str(cfg_path))
        assert cfg.enabled is False
