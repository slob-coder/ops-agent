"""
reporter — 每日健康报告生成器

每天定时基于审计日志 + limits 状态生成日报,通过 LLM 总结成
人类可读的 markdown,然后发给 IM 通道。

设计要点:
- 不依赖具体 LLM,接收任何 .ask(prompt) 兼容对象
- LLM 失败 → 回退到模板化纯统计日报(永不失败)
- 一天一次,主循环里检查"今天有没有发过"
- 发送通过 PolicyNotifier(complies notify_on / quiet_hours)
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.reliability.audit import AuditLog
from src.i18n import t, get_lang

logger = logging.getLogger("ops-agent.reporter")


class DailyReporter:
    """每日健康报告。

    用法:
        reporter = DailyReporter(audit, llm, notifier)
        if reporter.should_send_today():
            reporter.send_report_for(date_str=None)  # 默认昨天
    """

    def __init__(self, audit: AuditLog, llm=None, notifier=None,
                 limits=None, marker_dir: str = ""):
        self.audit = audit
        self.llm = llm
        self.notifier = notifier  # PolicyNotifier 或类似
        self.limits = limits
        # 用于"今天发过没"的防重标记目录(默认放在 audit dir 旁边)
        self.marker_dir = marker_dir or os.path.join(
            os.path.dirname(audit.dir_path) or ".", "reporter-markers"
        )
        try:
            os.makedirs(self.marker_dir, exist_ok=True)
        except OSError as e:
            logger.warning(f"reporter marker dir failed: {e}")

    # ──────────── 调度 ────────────

    def should_send_today(self, today: str | None = None) -> bool:
        today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        marker = os.path.join(self.marker_dir, f"sent-{today}")
        return not os.path.exists(marker)

    def mark_sent(self, today: str | None = None):
        today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        marker = os.path.join(self.marker_dir, f"sent-{today}")
        try:
            with open(marker, "w") as f:
                f.write(datetime.now(timezone.utc).isoformat())
        except OSError:
            pass

    # ──────────── Prompt 加载 ────────────

    @staticmethod
    def _load_prompt_template() -> str:
        """加载 reporter LLM prompt 模板，按语言目录查找"""
        prompts_root = Path(__file__).parent.parent / "prompts"
        lang = get_lang()
        lang_path = prompts_root / lang / "reporter.md"
        fallback_path = prompts_root / "zh" / "reporter.md"
        for p in [lang_path, fallback_path]:
            if p.exists():
                return p.read_text(encoding="utf-8")
        # 最后兜底：内联模板
        return (
            "Based on the following audit data for {date}, generate a concise daily report.\n\n"
            "## Event Statistics\n{event_counts}\n\n"
            "## Limits Status\n{limits_status}\n\n"
            "## Event Samples\n{event_samples}\n"
        )

    # ──────────── 生成 ────────────

    def generate(self, date_str: str | None = None) -> str:
        """生成日报 markdown。LLM 失败 → 回退模板。"""
        # 默认昨天
        if not date_str:
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            date_str = yesterday.strftime("%Y-%m-%d")

        events = self.audit.read_day(date_str)
        counts = self.audit.count_by_type(date_str)
        limits_status = self._render_limits()
        samples = self._render_samples(events)

        # LLM 优先
        if self.llm is not None:
            template = self._load_prompt_template()
            prompt = (template
                      .replace("{date}", date_str)
                      .replace("{event_counts}", self._render_counts(counts) or t("reporter.fallback_none"))
                      .replace("{limits_status}", limits_status)
                      .replace("{event_samples}", samples))
            try:
                report = self.llm.ask(prompt, max_tokens=1500)
                if report and report.strip():
                    title = t("reporter.title", date=date_str)
                    return f"# {title}\n\n{report.strip()}\n"
            except Exception as e:
                logger.warning(f"LLM report failed, fallback: {e}")

        # 回退:纯统计日报
        return self._fallback_report(date_str, counts, limits_status, len(events))

    def send_report_for(self, date_str: str | None = None,
                        urgency: str = "info") -> bool:
        """生成 + 通过 notifier 发送 + 打标记"""
        report = self.generate(date_str)
        date_str = date_str or (datetime.now(timezone.utc)
                                - timedelta(days=1)).strftime("%Y-%m-%d")
        title = t("reporter.title", date=date_str)

        sent = False
        if self.notifier is not None:
            try:
                # 支持 PolicyNotifier 或裸 Notifier
                if hasattr(self.notifier, "maybe_notify"):
                    sent = self.notifier.maybe_notify(
                        "daily_report", title, report, urgency,
                    )
                else:
                    sent = self.notifier.send(title, report, urgency)
            except Exception as e:
                logger.warning(f"notifier send failed: {e}")
                sent = False

        if sent:
            self.mark_sent()
        return sent

    # ──────────── 渲染 ────────────

    @staticmethod
    def _render_counts(counts: dict) -> str:
        if not counts:
            return ""
        lines = []
        for k, v in sorted(counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {k}: {v}")
        return "\n".join(lines)

    def _render_limits(self) -> str:
        if not self.limits:
            return t("reporter.fallback_no_limits")
        try:
            s = self.limits.status() or {}
        except Exception:
            return t("reporter.fallback_read_fail")
        if not s:
            return t("reporter.fallback_none")
        lines = []
        for k in ("actions_last_hour", "actions_last_day", "active_incidents",
                  "tokens_last_hour", "in_cooldown"):
            if k in s:
                lines.append(f"- {k}: {s[k]}")
        return "\n".join(lines) if lines else t("reporter.fallback_none")

    @staticmethod
    def _render_samples(events: list, max_n: int = 30) -> str:
        if not events:
            return t("reporter.fallback_none")
        lines = []
        for e in events[:max_n]:
            ts = e.get("timestamp", "")[:19]
            t_type = e.get("type", "?")
            extras = {k: v for k, v in e.items() if k not in ("timestamp", "type")}
            extras_s = ", ".join(f"{k}={v}" for k, v in list(extras.items())[:4])
            lines.append(f"- [{ts}] {t_type} {extras_s}")
        return "\n".join(lines)

    def _fallback_report(self, date_str, counts, limits_status, total) -> str:
        title = t("reporter.title", date=date_str)
        lines = [f"# {title}\n"]
        lines.append(f"> {t('reporter.fallback_total', total=total)}\n")
        lines.append(f"## {t('reporter.stats_header')}")
        cs = self._render_counts(counts) or t("reporter.fallback_no_events")
        lines.append(cs)
        lines.append(f"\n## {t('reporter.limits_header')}")
        lines.append(limits_status)
        return "\n".join(lines) + "\n"
