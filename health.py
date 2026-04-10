"""
health — 健康检查 HTTP 端点

供 watchdog / systemd / k8s probe 使用。

设计要点:
- 用 stdlib http.server,零依赖
- 后台线程,不阻塞主循环
- 默认只监听 127.0.0.1,不暴露公网
- 端点轻量:只读快照,不触发任何计算
- 出错绝不影响主进程
"""

from __future__ import annotations

import json
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger("ops-agent.health")


class HealthServer:
    """轻量健康检查服务。

    用法:
        server = HealthServer(snapshot_fn=lambda: {"status":"ok",...})
        server.start(host="127.0.0.1", port=9876)
        ...
        server.stop()
    """

    def __init__(self, snapshot_fn=None, metrics_fn=None):
        """snapshot_fn() -> dict — 主进程提供的状态快照函数。
        metrics_fn() -> str  — Sprint 6: 返回 Prometheus 格式文本(可选)。

        必须是只读、快速、不抛异常的。
        """
        self._snapshot_fn = snapshot_fn or (lambda: {"status": "ok"})
        self._metrics_fn = metrics_fn
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._started_at: float = 0.0

    # ──────────── 启动 / 停止 ────────────

    def start(self, host: str = "127.0.0.1", port: int = 9876) -> bool:
        """启动后台 HTTP 服务。失败返回 False(端口被占等)。"""
        if self._httpd is not None:
            return True

        snapshot_fn = self._snapshot_fn
        metrics_fn = self._metrics_fn

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return  # 静音 access log

            def do_GET(self):
                try:
                    if self.path == "/metrics":
                        if metrics_fn is None:
                            self.send_response(404)
                            self.end_headers()
                            return
                        try:
                            text = metrics_fn() or ""
                        except Exception as e:
                            text = f"# error: {e}\n"
                        body = text.encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain; version=0.0.4")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if self.path in ("/healthz", "/health", "/"):
                        try:
                            data = snapshot_fn() or {}
                        except Exception as e:
                            data = {"status": "error", "error": str(e)}
                        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                        code = 200 if data.get("status") == "ok" else 503
                        self.send_response(code)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    else:
                        self.send_response(404)
                        self.end_headers()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    try:
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(str(e).encode("utf-8"))
                    except Exception:
                        pass

        try:
            self._httpd = HTTPServer((host, port), Handler)
        except OSError as e:
            logger.warning(f"health server bind failed on {host}:{port}: {e}")
            return False

        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="health-server", daemon=True,
        )
        self._thread.start()
        logger.info(f"health server listening on http://{host}:{port}/healthz")
        return True

    def stop(self):
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
        self._httpd = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._httpd is not None and self._thread is not None and self._thread.is_alive()

    @property
    def uptime(self) -> float:
        return time.time() - self._started_at if self._started_at else 0.0
