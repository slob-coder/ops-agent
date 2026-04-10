"""
Flaky App — 故意有问题的演示服务

提供几个故障开关给 OpsAgent 演示用:
  GET /healthy - 健康
  GET /leak    - 内存泄漏(每次请求保留 10MB,直到 OOM)
  GET /crash   - 立即崩溃
  GET /slow    - 慢响应
  GET /error   - 抛 NPE 风格异常
"""

from flask import Flask, jsonify
import time
import sys
import os

app = Flask(__name__)

# 故意保留引用,模拟内存泄漏
_leaked = []


@app.route("/healthy")
def healthy():
    return jsonify({"status": "ok", "pid": os.getpid()})


@app.route("/leak")
def leak():
    # 故意泄漏 10MB
    _leaked.append(b"x" * (10 * 1024 * 1024))
    return jsonify({
        "status": "leaked",
        "total_leaked_mb": len(_leaked) * 10,
    })


@app.route("/crash")
def crash():
    print("CRASH triggered, exiting...", flush=True)
    os._exit(1)


@app.route("/slow")
def slow():
    time.sleep(10)
    return jsonify({"status": "slow but ok"})


@app.route("/error")
def error_endpoint():
    # 故意触发空指针错误,让 traceback 出现在 stderr
    user = None
    return jsonify({"name": user.name})  # AttributeError: 'NoneType' object has no attribute 'name'


@app.route("/")
def index():
    return jsonify({
        "service": "flaky-app",
        "endpoints": ["/healthy", "/leak", "/crash", "/slow", "/error"],
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
