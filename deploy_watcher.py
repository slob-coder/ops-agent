"""
deploy_watcher — 等待"已部署"信号

支持四种信号源(由 SourceRepo.deploy_signal 配置):
  - http       : GET 一个 URL,返回内容包含 commit_sha → 已部署
  - file       : 一个文件存在且包含 commit_sha → 已部署
  - command    : 跑一个命令,exit code 0 → 已部署
  - fixed_wait : 简单等待 N 秒,通常用于无 CD 的环境

任何错误都返回失败,绝不抛异常。
"""

from __future__ import annotations

import os
import time
import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger("ops-agent.deploy_watcher")


@dataclass
class DeployStatus:
    deployed: bool
    elapsed: float = 0.0
    detail: str = ""           # 简要描述发生了什么
    error: str = ""


class DeployWatcher:
    """部署信号监听。

    sleep_fn / now_fn / http_fn 都可注入,方便测试。
    """

    def __init__(self, sleep_fn=None, now_fn=None, http_fn=None, run_fn=None):
        self._sleep = sleep_fn or time.sleep
        self._now = now_fn or time.monotonic
        self._http = http_fn or self._default_http
        self._run = run_fn or self._default_run

    def wait_for_deploy(self, signal: dict, commit_sha: str,
                        timeout: int = 1800) -> DeployStatus:
        """等待部署信号。

        signal: dict 形如:
          {"type": "http", "url": "http://...", "expect_contains": "{commit_sha}",
           "check_interval": 10, "timeout": 1800}
          {"type": "file", "path": "/var/run/deploy.txt", "expect_contains": "{commit_sha}"}
          {"type": "command", "cmd": "kubectl get deploy ..."}
          {"type": "fixed_wait", "seconds": 60}
        """
        if not signal:
            return DeployStatus(
                deployed=True, elapsed=0,
                detail="no deploy_signal configured, assuming deployed",
            )

        sig_type = signal.get("type", "fixed_wait")
        # 允许 signal 自己覆盖 timeout
        timeout = int(signal.get("timeout", timeout))
        interval = int(signal.get("check_interval", 10))
        expect = (signal.get("expect_contains") or "").replace("{commit_sha}", commit_sha or "")
        if not expect and commit_sha:
            expect = commit_sha

        start = self._now()
        deadline = start + timeout

        if sig_type == "fixed_wait":
            secs = int(signal.get("seconds", 60))
            self._sleep(secs)
            return DeployStatus(deployed=True, elapsed=secs,
                                detail=f"waited {secs}s")

        # 轮询循环
        last_err = ""
        attempts = 0
        while self._now() < deadline:
            attempts += 1
            try:
                if sig_type == "http":
                    ok, detail = self._check_http(signal.get("url", ""), expect)
                elif sig_type == "file":
                    ok, detail = self._check_file(signal.get("path", ""), expect)
                elif sig_type == "command":
                    ok, detail = self._check_command(signal.get("cmd", ""))
                else:
                    return DeployStatus(deployed=False, error=f"unknown signal type: {sig_type}")
                if ok:
                    return DeployStatus(
                        deployed=True,
                        elapsed=self._now() - start,
                        detail=f"{sig_type} ok after {attempts} checks: {detail}",
                    )
                last_err = detail
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            self._sleep(interval)

        return DeployStatus(
            deployed=False,
            elapsed=self._now() - start,
            error=f"timeout after {timeout}s, last={last_err}",
        )

    # ──────────── 各信号检查 ────────────

    def _check_http(self, url: str, expect: str) -> tuple[bool, str]:
        if not url:
            return False, "no url"
        try:
            body, status = self._http(url)
        except Exception as e:
            return False, f"http error: {e}"
        if status >= 400:
            return False, f"http {status}"
        if expect and expect not in body:
            return False, f"http {status} but expected '{expect[:20]}' not found"
        return True, f"http {status}"

    def _check_file(self, path: str, expect: str) -> tuple[bool, str]:
        if not path or not os.path.exists(path):
            return False, "file missing"
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            return False, f"read error: {e}"
        if expect and expect not in content:
            return False, "expected content not found"
        return True, "file ok"

    def _check_command(self, cmd: str) -> tuple[bool, str]:
        if not cmd:
            return False, "no cmd"
        rc, out = self._run(cmd)
        if rc == 0:
            return True, "command ok"
        return False, f"command rc={rc}: {out[:120]}"

    # ──────────── 默认执行器 ────────────

    @staticmethod
    def _default_http(url: str) -> tuple[str, int]:
        # 用 stdlib 避免 requests 依赖
        from urllib.request import urlopen, Request
        from urllib.error import HTTPError, URLError
        try:
            req = Request(url, headers={"User-Agent": "OpsAgent/1.0"})
            with urlopen(req, timeout=10) as resp:
                return resp.read().decode("utf-8", errors="replace"), resp.status
        except HTTPError as e:
            return "", e.code
        except URLError as e:
            raise RuntimeError(str(e))

    @staticmethod
    def _default_run(cmd: str) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=60,
            )
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        except Exception as e:
            return 1, str(e)
