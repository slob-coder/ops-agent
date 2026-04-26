# Notebook 扩展安装指南

## Docker 环境

### 安装扩展

在已有 Basic Notebook 的 Docker 环境中启用扩展：

```bash
cd /opt/vol/ops-agent/docker

# 1. 停止当前容器
docker compose down

# 2. 重建镜像（加上扩展包）
docker compose build --build-arg NOTEBOOK_EXT=git+https://github.com/slob-coder/smart-notebook.git

# 3. 启动（notebook/ 目录通过 volume 挂载，已有数据不受影响）
docker compose up -d

# 4. 验证
docker compose logs -f ops-agent
# 应看到: INFO Notebook extension loaded: smart_notebook
```

也可以在 `.env` 中设置，避免每次手动传参：

```bash
# .env
NOTEBOOK_EXT=git+https://github.com/slob-coder/smart-notebook.git
```

然后：

```bash
docker compose build
docker compose up -d
```

### 私有仓库

如果扩展包在私有 GitHub 仓库中，通过 `GIT_TOKEN` 传入 GitHub PAT：

```bash
# 创建 PAT: GitHub → Settings → Developer settings → Personal access tokens
# 需要 repo 权限

docker compose build \
  --build-arg NOTEBOOK_EXT=git+https://github.com/slob-coder/smart-notebook.git \
  --build-arg GIT_TOKEN=ghp_xxxxxxxxxxxx
```

或在 `.env` 中设置：

```bash
# .env
NOTEBOOK_EXT=git+https://github.com/slob-coder/smart-notebook.git
GIT_TOKEN=ghp_xxxxxxxxxxxx
```

> ⚠️ `GIT_TOKEN` 会写入镜像层，不要将构建好的镜像推送到公共 registry。

### 回退到 Basic Notebook

```bash
# 不带 NOTEBOOK_EXT 重新构建
docker compose build --build-arg NOTEBOOK_EXT=

# 或清空 .env 中的 NOTEBOOK_EXT
docker compose build
docker compose up -d
```

### 已有数据处理

`notebook/` 目录通过 Docker volume 挂载，重建容器不会丢失数据。扩展包使用相同的 `BasicNotebook` 做 inner，读写路径完全一致，已有数据**无需迁移，直接兼容**。

| 数据 | 处理方式 |
|------|---------|
| `notebook/playbook/` | 直接兼容，扩展可读取并增强搜索 |
| `notebook/incidents/` | 直接兼容，归档的 incident 会被索引 |
| `notebook/lessons/` | 直接兼容 |
| `notebook/config/` | 直接兼容，`notebook.yaml` 中的 `smart:` 配置生效 |
| `notebook/.links.json` | 首次启动自动构建，无需手动处理 |

## 本地 / venv 环境

```bash
# 进入 ops-agent 虚拟环境
source ~/.ops-agent/.venv/bin/activate

# 从 GitHub 安装
pip install git+https://github.com/slob-coder/smart-notebook.git

# 重启 ops-agent
ops-agent  # 或 systemctl restart ops-agent
```

验证日志：

```
INFO Notebook extension loaded: smart_notebook
```

微调参数可编辑 `notebook/config/notebook.yaml` 中的 `smart:` 配置段，不编辑则用默认值。

卸载：

```bash
pip uninstall smart-notebook
```

重启后自动回退 Basic Notebook。

## 安装其他自定义扩展

任何符合 `NotebookProtocol` 并暴露 `create_bridge(notebook_path, llm)` 的包都可以作为扩展：

```bash
# Docker
docker compose build --build-arg NOTEBOOK_EXT=git+https://github.com/you/my-notebook-ext.git

# 本地
pip install git+https://github.com/you/my-notebook-ext.git
```

详见 [README.md](./README.md)「Notebook 扩展性」章节。
