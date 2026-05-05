"""
notifier — IM 通知抽象(Slack / DingTalk / Feishu / Noop)

设计要点:
- 统一接口:notifier.send(title, content, urgency)
- 通知策略:notify_on 白名单 + quiet_hours(免打扰时段,critical 例外)
- 发送通过 stdlib urllib,无 requests 依赖
- 失败不抛异常(只记 logger.warning)
- 配置从 yaml 加载,环境变量可覆盖 webhook_url
- 启动时可选发一条测试消息(test_send)
"""

from __future__ import annotations

import os
import json
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logger = logging.getLogger("ops-agent.notifier")

URGENCY_LEVELS = ("info", "warning", "critical")


# ──────────────────────────────────────
# 配置
# ──────────────────────────────────────

@dataclass
class NotifierConfig:
    type: str = "none"                       # slack | dingtalk | feishu | feishu_app | none
    webhook_url: str = ""
    # 飞书应用机器人模式
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_chat_id: str = ""                 # 接收消息的群聊 ID
    notify_on: list = field(default_factory=lambda: [
        "incident_opened", "incident_closed", "pr_merged",
        "revert_triggered", "critical_failure", "llm_degraded",
    ])
    quiet_hours_start: str = ""              # "22:00"
    quiet_hours_end: str = ""                # "08:00"
    quiet_except_urgency: list = field(default_factory=lambda: ["critical"])

    @classmethod
    def from_yaml(cls, path: str) -> "NotifierConfig":
        if not os.path.exists(path):
            return cls()
        try:
            import yaml
        except ImportError:
            return cls()
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = yaml.safe_load(f) or {}
        except OSError:
            return cls()

        # quiet_hours 是嵌套结构
        quiet = data.get("quiet_hours") or {}
        feishu_app = data.get("feishu_app") or {}
        cfg = cls(
            type=(data.get("type") or "none"),
            webhook_url=(data.get("webhook_url") or ""),
            feishu_app_id=(feishu_app.get("app_id") or ""),
            feishu_app_secret=(feishu_app.get("app_secret") or ""),
            feishu_chat_id=(feishu_app.get("chat_id") or ""),
            notify_on=list(data.get("notify_on") or cls().notify_on),
            quiet_hours_start=(quiet.get("start") or ""),
            quiet_hours_end=(quiet.get("end") or ""),
            quiet_except_urgency=list(quiet.get("except_urgency") or ["critical"]),
        )
        # 环境变量优先
        env_url = os.environ.get("OPS_NOTIFIER_WEBHOOK_URL")
        if env_url:
            cfg.webhook_url = env_url
        for key, env_key in [
            ("feishu_app_id", "OPS_FEISHU_APP_ID"),
            ("feishu_app_secret", "OPS_FEISHU_APP_SECRET"),
            ("feishu_chat_id", "OPS_FEISHU_CHAT_ID"),
        ]:
            env_val = os.environ.get(env_key)
            if env_val:
                setattr(cfg, key, env_val)
        return cfg

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        """now 是否在免打扰时段内。空字段 → 永远不静音。"""
        if not self.quiet_hours_start or not self.quiet_hours_end:
            return False
        now = now or datetime.now()
        try:
            sh, sm = map(int, self.quiet_hours_start.split(":"))
            eh, em = map(int, self.quiet_hours_end.split(":"))
        except ValueError:
            return False
        cur = now.hour * 60 + now.minute
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= end:
            return start <= cur < end
        # 跨日 e.g. 22:00 → 08:00
        return cur >= start or cur < end


# ──────────────────────────────────────
# 通知器抽象 + 实现
# ──────────────────────────────────────

class Notifier(ABC):
    @abstractmethod
    def send(self, title: str, content: str, urgency: str = "info") -> bool: ...

    def test_send(self) -> bool:
        from src.i18n import t
        return self.send(t("notifier.test_title"), t("notifier.test_content"), "info")


class NoOpNotifier(Notifier):
    def __init__(self):
        self.calls = []

    def send(self, title, content, urgency="info"):
        self.calls.append((title, content, urgency))
        return True


class _HTTPNotifier(Notifier):
    """共享 HTTP POST 实现"""

    def __init__(self, webhook_url: str, http_fn=None):
        self.webhook_url = webhook_url
        self._http = http_fn or self._default_http

    def _post(self, payload: dict) -> bool:
        if not self.webhook_url:
            logger.debug("notifier: no webhook_url")
            return False
        try:
            return self._http(self.webhook_url, payload)
        except Exception as e:
            logger.warning(f"notifier post failed: {e}")
            return False

    @staticmethod
    def _default_http(url: str, payload: dict) -> bool:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            url, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "OpsAgent/1.0"},
        )
        try:
            with urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except HTTPError as e:
            logger.warning(f"http error {e.code}")
            return False
        except URLError as e:
            logger.warning(f"url error {e.reason}")
            return False


class SlackNotifier(_HTTPNotifier):
    COLORS = {"info": "#36a64f", "warning": "#ff9900", "critical": "#ff0000"}

    def send(self, title, content, urgency="info"):
        payload = {
            "attachments": [{
                "color": self.COLORS.get(urgency, "#cccccc"),
                "title": title,
                "text": content,
                "footer": "OpsAgent",
                "ts": int(time.time()),
            }]
        }
        return self._post(payload)


class DingTalkNotifier(_HTTPNotifier):
    def send(self, title, content, urgency="info"):
        prefix = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(urgency, "")
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"{prefix} {title}",
                "text": f"## {prefix} {title}\n\n{content}\n\n— OpsAgent",
            },
        }
        return self._post(payload)


class FeishuNotifier(_HTTPNotifier):
    """飞书 Webhook 机器人（群聊添加自定义机器人获取的 webhook 地址）"""
    def send(self, title, content, urgency="info"):
        prefix = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(urgency, "")
        payload = {
            "msg_type": "text",
            "content": {"text": f"{prefix} {title}\n\n{content}\n\n— OpsAgent"},
        }
        return self._post(payload)


class FeishuAppNotifier(Notifier):
    """飞书自建应用机器人（使用 App ID + App Secret 获取 tenant_access_token 发消息）

    配置需要:
    - app_id: 飞书开放平台创建的应用 App ID
    - app_secret: 飞书开放平台创建的应用 App Secret
    - chat_id: 接收消息的群聊 chat_id
    """
    _TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    _MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

    def __init__(self, app_id: str, app_secret: str, chat_id: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.chat_id = chat_id
        self._token = ""
        self._token_expires = 0.0

    def _get_token(self) -> str:
        """获取或刷新 tenant_access_token"""
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
            # 提前 60 秒过期，避免边界问题
            self._token_expires = time.time() + data.get("expire", 7200) - 60
            return self._token
        except Exception as e:
            logger.warning(f"feishu token request failed: {e}")
            return ""

    def send(self, title, content, urgency="info") -> bool:
        token = self._get_token()
        if not token:
            return False
        prefix = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(urgency, "")
        text = f"{prefix} {title}\n\n{content}\n\n— OpsAgent"
        msg_payload = {
            "receive_id": self.chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        body = json.dumps(msg_payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            self._MSG_URL + f"?receive_id_type=chat_id",
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


# ──────────────────────────────────────
# 工厂 + 策略包装
# ──────────────────────────────────────

def make_notifier(config: NotifierConfig, http_fn=None) -> Notifier:
    t = (config.type or "none").lower()
    if t in ("none", "noop", ""):
        return NoOpNotifier()
    if t == "slack":
        return SlackNotifier(config.webhook_url, http_fn=http_fn)
    if t == "dingtalk":
        return DingTalkNotifier(config.webhook_url, http_fn=http_fn)
    if t == "feishu":
        return FeishuNotifier(config.webhook_url, http_fn=http_fn)
    if t == "feishu_app":
        return FeishuAppNotifier(
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
            chat_id=config.feishu_chat_id,
        )
    raise ValueError(f"unknown notifier type: {t}")


class PolicyNotifier:
    """带通知策略的包装器:notify_on 白名单 + 免打扰时段。

    用法:
        pn = PolicyNotifier(make_notifier(cfg), cfg)
        pn.maybe_notify("incident_opened", "...", "...", "warning")
    """

    def __init__(self, notifier: Notifier, config: NotifierConfig):
        self.notifier = notifier
        self.config = config
        self.dropped: list[tuple[str, str]] = []  # 调试用:被策略过滤掉的事件

    def maybe_notify(self, event_type: str, title: str, content: str,
                     urgency: str = "info", now: datetime | None = None) -> bool:
        """根据策略决定是否真的发送。"""
        if event_type not in self.config.notify_on:
            self.dropped.append((event_type, "not in notify_on"))
            return False
        if self.config.in_quiet_hours(now):
            if urgency not in self.config.quiet_except_urgency:
                self.dropped.append((event_type, "quiet_hours"))
                return False
        try:
            return self.notifier.send(title, content, urgency)
        except Exception as e:
            logger.warning(f"notifier raised: {e}")
            return False
