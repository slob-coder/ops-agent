# Notebook 扩展性

ops-agent 的 Notebook 是可插拔的。内置 Basic Notebook（文件系统 + git），也支持安装第三方扩展 Notebook 来增强能力（知识图谱、智能感知、成长引擎等）。

## 工作原理

启动时自动检测：`import <扩展包>` 成功 → 用扩展实现，失败 → 用 Basic Notebook。零配置，无需修改任何代码。

```
ops-agent 启动
  → create_notebook(notebook_path, llm)
    → import <扩展包> 成功？
      → YES: 扩展包.create_bridge(notebook_path, llm) → 扩展 Notebook
      → NO:  Notebook(notebook_path) → Basic Notebook
```

所有 Notebook 实现都遵循 `NotebookProtocol`（定义在 `src/infra/notebook.py`），ops-agent 核心代码只依赖这个协议，不依赖具体实现。

## NotebookProtocol 接口

扩展 Notebook **必须**实现以下方法（duck-typing，无需 import Protocol）：

| 方法 | 签名 | 说明 |
|------|------|------|
| `read` | `(relative_path: str) -> str` | 读文件，不存在返回空串 |
| `exists` | `(relative_path: str) -> bool` | 文件是否存在 |
| `list_dir` | `(relative_path: str) -> list[str]` | 列出目录文件名 |
| `write` | `(relative_path: str, content: str) -> None` | 写文件 |
| `append` | `(relative_path: str, content: str) -> None` | 追加内容 |
| `commit` | `(message: str) -> str` | git commit |
| `search` | `(keyword: str) -> list[str]` | grep 搜索 |
| `find_relevant` | `(context: str, top_k: int = 5) -> list[str]` | 语义搜索 |
| `read_playbooks_summary` | `() -> str` | Playbook 摘要 |
| `create_incident` | `(title: str) -> str` | 创建 Incident，返回文件名 |
| `append_to_incident` | `(filename: str, content: str) -> None` | 追加 Incident |
| `close_incident` | `(filename: str, summary: str) -> None` | 关闭并归档 Incident |
| `read_incident` | `(filename: str) -> str` | 读取 Incident |
| `log_conversation` | `(role: str, message: str) -> None` | 记录对话 |
| `get_recent_conversation` | `(limit: int = 20) -> str` | 获取最近对话 |
| `update_readme_growth` | `() -> None` | 更新成长状态 |
| `verify_integrity` | `() -> tuple[bool, str]` | git 完整性校验 |
| `push_to_remote` | `() -> tuple[bool, str]` | 推送到远端 |
| `restore_from_remote` | `() -> tuple[bool, str]` | 从远端恢复 |

此外，实现必须暴露 `path: Path` 属性（Notebook 根目录的绝对路径）。

## 增强 API（可选）

扩展 Notebook 可以提供额外方法，ops-agent 通过 `hasattr()` 检测后调用，不存在则静默跳过：

| 方法 | 调用场景 | Basic 降级行为 |
|------|----------|---------------|
| `gather_knowledge(error_signature)` | 诊断时获取相关上下文 | 返回 None |
| `assess_logs(log_lines, source)` | 观察时过滤日志噪音 | 返回空列表 |
| `run_maintenance()` | 巡检间隙执行维护 | 返回空 dict |
| `record_fp_rejection(pattern, ...)` | 人类标记误报时记录 | 不调用 |
| `get_smart_stats()` | 查看扩展子系统状态 | 不调用 |

## 开发自定义 Notebook 扩展

### 1. 创建包

```bash
mkdir my-notebook-ext && cd my-notebook-ext
mkdir -p my_notebook_ext
```

### 2. 实现 `create_bridge()` 入口

ops-agent 通过 `import my_notebook_ext; my_notebook_ext.create_bridge(...)` 加载扩展。你的包必须暴露这个函数：

```python
# my_notebook_ext/__init__.py
from .bridge import NotebookBridge

def create_bridge(notebook_path: str = "notebook", llm=None, **kwargs):
    """
    ops-agent 调用入口。

    Args:
        notebook_path: notebook 目录路径
        llm: ops-agent 的 LLM 实例（有 .chat() 方法），可选用

    Returns:
        符合 NotebookProtocol 的实例
    """
    return NotebookBridge(notebook_path, llm=llm)
```

### 3. 实现桥接类

```python
# my_notebook_ext/bridge.py
from pathlib import Path

class NotebookBridge:
    """自定义 Notebook 实现，必须兼容 NotebookProtocol"""

    def __init__(self, notebook_path: str, llm=None):
        # 用内置 BasicNotebook 处理基础文件/git 操作
        from my_notebook_ext.basic import BasicNotebook
        self.inner = BasicNotebook(notebook_path)
        self.path = self.inner.path
        self._llm = llm

    # ── 基础 API（委托 inner）──
    def read(self, relative_path): return self.inner.read(relative_path)
    def exists(self, relative_path): return self.inner.exists(relative_path)
    def list_dir(self, relative_path): return self.inner.list_dir(relative_path)
    def write(self, relative_path, content):
        self.inner.write(relative_path, content)
        # ← 在这里注入你的增强逻辑（如更新索引）
    def append(self, relative_path, content): self.inner.append(relative_path, content)
    def commit(self, message): return self.inner.commit(message)
    def search(self, keyword): return self.inner.search(keyword)
    def find_relevant(self, context, top_k=5):
        # ← 用你的增强搜索替换
        return self.inner.find_relevant(context, top_k)
    def read_playbooks_summary(self): return self.inner.read_playbooks_summary()
    def create_incident(self, title): return self.inner.create_incident(title)
    def append_to_incident(self, filename, content): self.inner.append_to_incident(filename, content)
    def close_incident(self, filename, summary):
        self.inner.close_incident(filename, summary)
        # ← 在这里注入关闭后处理（如知识提取）
    def read_incident(self, filename): return self.inner.read_incident(filename)
    def log_conversation(self, role, message): self.inner.log_conversation(role, message)
    def get_recent_conversation(self, limit=20): return self.inner.get_recent_conversation(limit)
    def update_readme_growth(self): self.inner.update_readme_growth()
    def verify_integrity(self): return self.inner.verify_integrity()
    def push_to_remote(self): return self.inner.push_to_remote()
    def restore_from_remote(self): return self.inner.restore_from_remote()

    # ── 增强 API（可选）──
    def gather_knowledge(self, error_signature, top_k=5):
        # 返回 KnowledgeContext 或 None
        return None

    def assess_logs(self, log_lines, source=""):
        # 返回 AssessmentResult 列表
        return []

    def run_maintenance(self):
        return {}

    def __getattr__(self, name):
        """未定义的方法透传到 inner"""
        return getattr(self.inner, name)
```

### 4. 配置 pyproject.toml

```toml
[project]
name = "my-notebook-ext"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["pyyaml>=6.0"]
```

### 5. 安装到 ops-agent 环境

```bash
# 方式一：本地开发安装
cd my-notebook-ext
pip install -e .

# 方式二：从 Git 安装
pip install git+https://github.com/you/my-notebook-ext.git

# 方式三：从 PyPI 安装（发布后）
pip install my-notebook-ext
```

### 6. 验证

启动 ops-agent，观察日志：

```
# 扩展已安装
INFO Notebook extension loaded: my_notebook_ext

# 未安装扩展
INFO No notebook extension installed → using basic mode
```

## 自定义配置

扩展可以在 `notebook/config/` 目录下读取自己的配置文件。`create_bridge()` 接收 `notebook_path` 参数，据此定位配置：

```python
def create_bridge(notebook_path="notebook", llm=None, **kwargs):
    config_path = Path(notebook_path) / "config" / "my_ext.yaml"
    if config_path.exists():
        # 加载用户配置
        ...
    return NotebookBridge(notebook_path, llm=llm, config=config)
```

## Docker 环境安装

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

## 验证扩展是否生效

```bash
# 容器日志
docker compose logs ops-agent | head -20
# Smart: INFO Notebook extension loaded: smart_notebook
# Basic: INFO No notebook extension installed → using basic mode

# 容器内 Python 验证
docker exec ops-agent python3 -c "import smart_notebook; print(smart_notebook.__version__)"

# 运行时行为：输入 fp test
# Smart: 已记录误报: test
# Basic: 当前未启用 Smart Notebook,误报记录不可用。
```
