# OpsAgent Demo: docker-compose

一个故意有问题的小项目,用来演示 OpsAgent 的能力。

## 架构

```
你的浏览器 ──→ nginx:8080 ──→ flaky-app:5000
                                  │
                                  └─ 故障开关:
                                     /healthy  正常
                                     /leak     内存泄漏
                                     /crash    立即崩溃
                                     /slow     慢响应
                                     /error    抛 Python 异常
```

## 启动 Demo

```bash
# 1. 启动 docker-compose
cd examples/docker-compose-demo
docker compose up -d --build

# 2. 配置 OpsAgent 目标
cp targets-demo.yaml ../../notebook/config/targets.yaml

# 3. 启动 OpsAgent
cd ../..
export OPS_LLM_API_KEY="sk-ant-..."
python main.py
```

## 演示场景

### 场景 1: 容器崩溃后自愈

```bash
# 触发崩溃
curl http://localhost:8080/crash

# OpsAgent 应该:
# 1. 巡检时发现 flaky-app 容器 status != running
# 2. 创建 Incident
# 3. 诊断 → 决定 docker restart flaky-app
# 4. 等待容器健康
# 5. 关闭 Incident,写复盘到 Notebook
```

### 场景 2: 内存泄漏

```bash
# 持续触发泄漏
for i in {1..30}; do curl http://localhost:8080/leak; sleep 1; done

# OpsAgent 应该:
# 1. 在 docker stats 或日志里发现内存异常增长
# 2. 创建 Incident,标记为高严重度
# 3. 决定 restart 容器作为临时缓解
# 4. 在复盘中标记"长期方案需要修代码"
```

### 场景 3: Python 异常

```bash
# 触发 NPE
curl http://localhost:8080/error

# OpsAgent 应该看到 docker logs 里的 Python traceback。
# 这个场景需要 Sprint 2 的源码反向定位能力才能完整修复。
```

## 清理

```bash
docker compose down
rm -f ../../notebook/config/targets.yaml
```
