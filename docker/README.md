# OpsAgent Docker 部署

## 快速体验（Demo）

只需 API key，零配置：

```bash
# 在项目根目录
docker compose -f docker/compose.yaml run --rm ops-agent demo
```

或在 .env 中只填 `OPS_LLM_API_KEY`，然后：

```bash
cd docker
cp .env.example .env
# 编辑 .env，填入 API key
docker compose run --rm ops-agent demo
```

## 正式部署

### 1. 配置

```bash
cd docker
cp .env.example .env
```

编辑 `.env`，最少填写：

```env
OPS_LLM_API_KEY=sk-ant-...        # 必填：你的 API key
OPS_TARGET_TYPE=ssh               # 监控类型
OPS_TARGET_HOST=ubuntu@10.0.0.10  # SSH 地址（ssh 类型必填）
```

> 完整参数说明见 `.env.example` 中的注释。

### 2. 初始化

```bash
docker compose run --rm ops-agent init --from-env
```

读取 `.env` 中的配置，自动生成 `notebook/` 下的所有配置文件。

### 3. 校验

```bash
docker compose run --rm ops-agent check --test-llm
```

### 4. 启动

```bash
docker compose up -d
```

查看日志：

```bash
docker compose logs -f
```

### 5. 管理

```bash
docker compose stop              # 停止
docker compose start             # 启动
docker compose restart           # 重启
docker compose exec ops-agent bash  # 进入容器
curl localhost:9876/healthz      # 健康检查
curl localhost:9876/metrics      # Prometheus metrics
```

## 多目标

编辑 `notebook/config/targets.yaml` 添加更多目标：

```yaml
targets:
  - name: web-prod
    type: ssh
    host: ubuntu@10.0.0.10
  - name: db-prod
    type: ssh
    host: ubuntu@10.0.0.20
```

## 自定义镜像

```bash
# 从源码构建
cd ..
docker build -t ops-agent .
cd docker
# 修改 compose.yaml 中的 image 或 build 路径
```
