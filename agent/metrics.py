"""
可观测性 Mixin — 审计、通知、Prometheus metrics、健康检查、日报
"""

import time
import logging

logger = logging.getLogger("ops-agent")


class MetricsMixin:
    """Sprint 6: 审计日志、通知策略、Prometheus 指标、健康检查服务、日报"""

    def _emit_audit(self, event_type: str, **kwargs) -> None:
        if not self.audit:
            return
        try:
            kwargs.setdefault("target", getattr(self.current_target, "name", ""))
            if self.current_incident:
                kwargs.setdefault("incident", self.current_incident)
            self.audit.record(event_type, **kwargs)
        except Exception as e:
            logger.debug(f"audit emit failed: {e}")

        if event_type == "action_executed":
            tgt = kwargs.get("target", "")
            kind = kwargs.get("kind", "unknown")
            key = (tgt, kind)
            self._counter_actions[key] = self._counter_actions.get(key, 0) + 1
        elif event_type in ("incident_opened", "incident_closed"):
            tgt = kwargs.get("target", "")
            status = "opened" if event_type == "incident_opened" else "closed"
            key = (tgt, status)
            self._counter_incidents[key] = self._counter_incidents.get(key, 0) + 1

    def _emit_notify(self, event_type: str, title: str, content: str,
                     urgency: str = "info") -> bool:
        if not self.notifier:
            return False
        try:
            return self.notifier.maybe_notify(event_type, title, content, urgency)
        except Exception as e:
            logger.debug(f"notify failed: {e}")
            return False

    def render_prometheus_metrics(self) -> str:
        try:
            lines = []
            lines.append("# HELP ops_agent_uptime_seconds Agent uptime")
            lines.append("# TYPE ops_agent_uptime_seconds gauge")
            lines.append(f"ops_agent_uptime_seconds {time.time() - self.start_time:.0f}")

            lines.append("# HELP ops_agent_mode Current mode")
            lines.append("# TYPE ops_agent_mode gauge")
            lines.append(f'ops_agent_mode{{mode="{self.mode}"}} 1')

            lines.append("# HELP ops_agent_llm_degraded LLM degraded state")
            lines.append("# TYPE ops_agent_llm_degraded gauge")
            lines.append(f"ops_agent_llm_degraded {1 if self.llm_degraded else 0}")

            lines.append("# HELP ops_agent_actions_total Actions executed")
            lines.append("# TYPE ops_agent_actions_total counter")
            for (tgt, kind), v in self._counter_actions.items():
                lines.append(
                    f'ops_agent_actions_total{{target="{tgt}",kind="{kind}"}} {v}'
                )

            lines.append("# HELP ops_agent_incidents_total Incidents by status")
            lines.append("# TYPE ops_agent_incidents_total counter")
            for (tgt, status), v in self._counter_incidents.items():
                lines.append(
                    f'ops_agent_incidents_total{{target="{tgt}",status="{status}"}} {v}'
                )

            try:
                s = self.limits.status() or {}
                if "tokens_last_hour" in s:
                    lines.append("# HELP ops_agent_llm_tokens_last_hour Tokens used last hour")
                    lines.append("# TYPE ops_agent_llm_tokens_last_hour gauge")
                    lines.append(f"ops_agent_llm_tokens_last_hour {s['tokens_last_hour']}")
                if "active_incidents" in s:
                    lines.append("# HELP ops_agent_active_incidents Concurrent incidents")
                    lines.append("# TYPE ops_agent_active_incidents gauge")
                    lines.append(f"ops_agent_active_incidents {s['active_incidents']}")
            except Exception:
                pass

            if self.pending_queue:
                lines.append("# HELP ops_agent_pending_events Pending events")
                lines.append("# TYPE ops_agent_pending_events gauge")
                lines.append(f"ops_agent_pending_events {self.pending_queue.size()}")

            return "\n".join(lines) + "\n"
        except Exception as e:
            return f"# error rendering metrics: {e}\n"

    def maybe_send_daily_report(self) -> bool:
        if not self.reporter:
            return False
        try:
            if self.reporter.should_send_today():
                ok = self.reporter.send_report_for()
                if ok:
                    self._emit_audit("daily_report_sent")
                return ok
        except Exception as e:
            logger.debug(f"daily report failed: {e}")
        return False

    def health_snapshot(self) -> dict:
        """供 HealthServer 调用的只读快照"""
        try:
            active_count = 0
            try:
                active_count = len(self.notebook.list_dir("incidents/active"))
            except Exception:
                pass
            return {
                "status": "degraded" if self.llm_degraded else "ok",
                "mode": self.mode,
                "uptime": time.time() - self.start_time,
                "current_target": getattr(self.current_target, "name", ""),
                "current_incident": self.current_incident or "",
                "active_incidents": active_count,
                "paused": self.paused,
                "readonly": self.readonly,
                "last_loop_time": self.last_loop_time,
                "llm_degraded": self.llm_degraded,
                "pending_events": (self.pending_queue.size()
                                    if self.pending_queue else 0),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def start_health_server(self, host: str = "127.0.0.1", port: int = 9876) -> bool:
        """启动健康检查后台线程。失败返回 False。"""
        try:
            from reliability.health import HealthServer
            self.health_server = HealthServer(
                snapshot_fn=self.health_snapshot,
                metrics_fn=self.render_prometheus_metrics,
            )
            return self.health_server.start(host=host, port=port)
        except Exception as e:
            logger.warning(f"health server start failed: {e}")
            return False

    def stop_health_server(self):
        if self.health_server:
            try:
                self.health_server.stop()
            except Exception:
                pass
            self.health_server = None
