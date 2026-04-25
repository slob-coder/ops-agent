"""
FeishuBackend — 飞书交互通道后端

通过飞书自建应用实现双向交互：
- 收消息：HTTP 事件回调 → inbox / approval_queue
- 发消息：飞书 API 发送文本/富文本消息

配置在 notifier.yaml 的 feishu_app.interactive 段。
"""

from __future__ import annotations

import json
import re
import time
import logging
import queue
import threading
import hashlib
import base64
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from src.infra.channel_backend import ChannelBackend

logger = logging.getLogger("ops-agent.feishu")

# ── 紧急度 → emoji 映射 ──
URGENCY_EMOJI = {
    "info":     "💬",
    "success":  "✅",
    "warning":  "⚠️",
    "critical": "🚨",
    "question": "❓",
    "action":   "🔧",
    "observe":  "·",
}

# 飞书消息内容最大长度（保守值）
_MAX_MSG_LEN = 3500


class FeishuTokenManager:
    """飞书 tenant_access_token 管理，供 Backend 和 Notifier 共享"""

    _TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = ""
        self._token_expires = 0.0

    def get_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        }
        body = json.dumps(payload).encode("utf-8")
        req = Request(
            self._TOKEN_URL, data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if data.get("code") != 0:
                logger.warning(f"feishu token error: {data.get('msg')}")
                return ""
            self._token = data["tenant_access_token"]
            self._token_expires = time.time() + data.get("expire", 7200) - 60
            return self._token
        except Exception as e:
            logger.warning(f"feishu token request failed: {e}")
            return ""


class FeishuBackend(ChannelBackend):
    """飞书交互通道后端"""

    _MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

    def __init__(self, app_id: str, app_secret: str, chat_id: str,
                 callback_port: int = 9877,
                 encrypt_key: str = "",
                 verification_token: str = ""):
        self.app_id = app_id
        self.app_secret = app_secret
        self.chat_id = chat_id
        self.callback_port = callback_port
        self.encrypt_key = encrypt_key
        self.verification_token = verification_token

        self._token_mgr = FeishuTokenManager(app_id, app_secret)
        self._inbox: queue.Queue = queue.Queue()
        self._approval_queue: queue.Queue = queue.Queue()
        self._interrupted: threading.Event = threading.Event()
        self._server: Optional[HTTPServer] = None
        self._seen_events: dict = {}  # event_id -> timestamp, dedup
        self._running = True
        self._waiting_approval = False
        self._bot_open_id: str = ""  # 启动时动态获取

    # ──────────── ChannelBackend 接口 ────────────

    def start(self, inbox: queue.Queue, approval_queue: queue.Queue,
              interrupted: threading.Event) -> None:
        self._inbox = inbox
        self._approval_queue = approval_queue
        self._interrupted = interrupted

        # 动态获取机器人 open_id
        self._fetch_bot_open_id()

        # 启动 HTTP 回调服务器
        backend = self
        port = self.callback_port

        class EventHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return  # 静音 access log

            def do_POST(self):
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length > 1_000_000:  # 安全限制
                    self.send_response(413)
                    self.end_headers()
                    return
                raw = self.rfile.read(content_length)
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.end_headers()
                    return

                # URL verification challenge
                if body.get("type") == "url_verification":
                    challenge = body.get("challenge", "")
                    resp = json.dumps({"challenge": challenge}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                    return

                # 事件回调
                event = body.get("event") or {}
                event_id = body.get("header", {}).get("event_id") or ""
                event_type = body.get("header", {}).get("event_type") or ""

                # 去重
                if event_id and not backend._check_event(event_id):
                    self._send_ok()
                    return

                # 处理消息事件
                if event_type == "im.message.receive_v1":
                    msg_type = event.get("message", {}).get("message_type", "")
                    chat_id = event.get("message", {}).get("chat_id", "")
                    chat_type = event.get("message", {}).get("chat_type", "")

                    # 群聊只响应 @机器人 的消息，私聊始终响应
                    if not backend._should_respond(event, chat_type):
                        self._send_ok()
                        return

                    # 只处理配置的 chat_id（私聊不过滤）
                    if chat_type != "p2p" and backend.chat_id and chat_id != backend.chat_id:
                        self._send_ok()
                        return

                    text = backend._extract_text(event, msg_type)
                    if text:
                        backend._on_message(text)

                self._send_ok()

            def _send_ok(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                resp = b'{"code":0}'
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        try:
            self._server = HTTPServer(("0.0.0.0", port), EventHandler)
            t = threading.Thread(
                target=self._server.serve_forever,
                name="feishu-callback-server", daemon=True,
            )
            t.start()
            logger.info(f"FeishuBackend callback server listening on :{port}")
        except OSError as e:
            logger.warning(f"FeishuBackend callback server bind failed on :{port}: {e}")

    def send(self, message: str, urgency: str = "info") -> None:
        emoji = URGENCY_EMOJI.get(urgency, "💬")
        ts = datetime.now().strftime("%H:%M:%S")
        text = f"{emoji} [{ts}] {message}"
        self._send_text(text)

    def send_log(self, message: str, urgency: str = "observe") -> None:
        # 日志类消息降级为轻量输出，只推 warning 及以上
        if urgency in ("warning", "critical"):
            emoji = URGENCY_EMOJI.get(urgency, "·")
            ts = datetime.now().strftime("%H:%M:%S")
            text = f"{emoji} [{ts}] {message}"
            self._send_text(text)

    def send_cmd_log(self, cmd: str) -> None:
        # 命令日志不推飞书（太碎），静默
        pass

    def request_approval(self, action_description: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        text = (
            f"⚠️ [{ts}] 需要批准：\n"
            f"{action_description}\n"
            f"▸ 回复 y 批准 / n 否决 / 其他作为指示"
        )
        self._send_text(text)

    def stop(self) -> None:
        self._running = False
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass

    # ──────────── 内部方法 ────────────

    def set_waiting_approval(self, value: bool) -> None:
        """HumanChannel 同步等待批准状态"""
        self._waiting_approval = value

    def _fetch_bot_open_id(self) -> None:
        """启动时调用飞书 API 获取机器人自身的 open_id"""
        token = self._token_mgr.get_token()
        if not token:
            logger.warning("FeishuBackend: failed to fetch bot open_id (no token)")
            return
        req = Request(
            "https://open.feishu.cn/open-apis/bot/v3/info/",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if data.get("code") == 0:
                self._bot_open_id = data.get("bot", {}).get("open_id", "")
                logger.info(f"FeishuBackend bot open_id: {self._bot_open_id or '(empty)'}")
            else:
                logger.warning(f"FeishuBackend fetch bot info failed: {data.get('msg')}")
        except Exception as e:
            logger.warning(f"FeishuBackend fetch bot info error: {e}")

    def _should_respond(self, event: dict, chat_type: str) -> bool:
        """判断是否应响应此消息。私聊始终响应，群聊仅 @机器人 或 @所有人 时响应"""
        if chat_type != "group":
            return True  # 私聊始终响应

        mentions = event.get("message", {}).get("mentions") or []
        if not mentions:
            # 飞书 @所有人 不会产生 mention 条目，检查消息内容
            content_json = event.get("message", {}).get("content", "{}")
            try:
                content = json.loads(content_json)
                raw_text = content.get("text", "")
            except (json.JSONDecodeError, TypeError):
                raw_text = ""
            if "@所有人" in raw_text:
                return True
            return False  # 普通群消息，无 @

        # 检查机器人是否在 mentions 中
        if self._bot_open_id:
            for m in mentions:
                if m.get("id", {}).get("open_id") == self._bot_open_id:
                    return True
            return False  # @了别人，没 @机器人

        # fallback: 没拿到 bot open_id 时，只响应有 mentions 的消息（宽松模式）
        logger.debug("FeishuBackend: bot open_id unknown, responding to all mentioned messages")
        return True

    def _on_message(self, text: str):
        """收到飞书消息，路由到 inbox 或 approval_queue"""
        logger.info(f"FeishuBackend received: {text[:100]}")
        if self._waiting_approval:
            self._approval_queue.put(("feishu", text))
        else:
            self._inbox.put(("feishu", text))
        self._interrupted.set()

    def _check_event(self, event_id: str) -> bool:
        """去重检查，5 分钟窗口。返回 True 表示新事件，False 表示重复"""
        now = time.time()
        # 清理过期
        self._seen_events = {k: v for k, v in self._seen_events.items() if now - v < 300}
        if event_id in self._seen_events:
            return False
        self._seen_events[event_id] = now
        return True

    def _extract_text(self, event: dict, msg_type: str) -> str:
        """从飞书事件中提取文本内容"""
        if msg_type == "text":
            content_json = event.get("message", {}).get("content", "{}")
            try:
                content = json.loads(content_json)
                text = content.get("text", "").strip()
                # 剥掉飞书 @mention 前缀（如 @_user_1）
                text = re.sub(r"@_\w+\s*", "", text).strip()
                return text
            except json.JSONDecodeError:
                return ""
        # 其他类型暂不支持
        return ""

    def _send_text(self, text: str) -> bool:
        """通过飞书 API 发送文本消息"""
        # 截断
        if len(text) > _MAX_MSG_LEN:
            text = text[:_MAX_MSG_LEN] + "\n...(已截断)"

        token = self._token_mgr.get_token()
        if not token:
            logger.warning("FeishuBackend: no token, skip send")
            return False

        msg_payload = {
            "receive_id": self.chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        body = json.dumps(msg_payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            self._MSG_URL + "?receive_id_type=chat_id",
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if data.get("code") != 0:
                logger.warning(f"feishu send error: {data.get('msg')}")
                return False
            return True
        except Exception as e:
            logger.warning(f"feishu send failed: {e}")
            return False


class FeishuBackendConfig:
    """FeishuBackend 的配置"""

    def __init__(self, app_id: str = "", app_secret: str = "", chat_id: str = "",
                 callback_port: int = 9877, encrypt_key: str = "",
                 verification_token: str = "", enabled: bool = False):
        self.app_id = app_id
        self.app_secret = app_secret
        self.chat_id = chat_id
        self.callback_port = callback_port
        self.encrypt_key = encrypt_key
        self.verification_token = verification_token
        self.enabled = enabled

    @classmethod
    def from_yaml(cls, path: str) -> "FeishuBackendConfig":
        """从 notifier.yaml 加载交互通道配置"""
        import os as _os
        if not _os.path.exists(path):
            return cls()

        try:
            import yaml
        except ImportError:
            return cls()

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except OSError:
            return cls()

        feishu_app = data.get("feishu_app") or {}
        interactive = feishu_app.get("interactive") or {}

        cfg = cls(
            app_id=feishu_app.get("app_id", ""),
            app_secret=feishu_app.get("app_secret", ""),
            chat_id=feishu_app.get("chat_id", ""),
            callback_port=interactive.get("callback_port", 9877),
            encrypt_key=interactive.get("encrypt_key", ""),
            verification_token=interactive.get("verification_token", ""),
            enabled=bool(interactive.get("enabled", False)),
        )

        # 环境变量覆盖
        for key, env_key in [
            ("app_id", "OPS_FEISHU_APP_ID"),
            ("app_secret", "OPS_FEISHU_APP_SECRET"),
            ("chat_id", "OPS_FEISHU_CHAT_ID"),
        ]:
            env_val = _os.environ.get(env_key)
            if env_val:
                setattr(cfg, key, env_val)

        return cfg
