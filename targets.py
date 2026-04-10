"""
Targets — 多目标管理

Agent 可以同时管理多个目标(SSH 服务器 / Docker 主机 / K8s 集群)。
每个目标有独立的 ToolBox 实例,但共享同一个 Notebook 和 LLM。

配置文件: notebook/config/targets.yaml
"""

import os
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ops-agent.targets")


@dataclass
class Target:
    """一个目标系统的完整配置"""
    name: str                           # 唯一标识,如 "web-prod-01"
    type: str                           # ssh | docker | k8s | local
    description: str = ""               # 给 LLM 看的描述
    criticality: str = "normal"         # low | normal | high | critical
    tags: list[str] = field(default_factory=list)

    # SSH 相关
    host: str = ""                      # user@host
    port: int = 22
    key_file: str = ""
    password_env: str = ""              # 从环境变量读密码的变量名

    # Docker 相关
    docker_host: str = ""               # 默认本地 unix socket
    compose_file: str = ""              # docker-compose.yaml 路径(可选)

    # K8s 相关
    kubeconfig: str = ""                # kubeconfig 路径
    context: str = ""                   # kubectl context
    namespace: str = "default"          # 默认 namespace

    # 源码地图(供 bug 修复用)
    source_repos: list[dict] = field(default_factory=list)


def load_targets(config_path: str) -> list[Target]:
    """加载 targets.yaml 文件"""
    if not os.path.exists(config_path):
        logger.warning(f"targets.yaml not found at {config_path}, using empty list")
        return []

    try:
        import yaml
    except ImportError:
        raise RuntimeError("pip install pyyaml")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    targets = []
    for item in data.get("targets", []):
        # 字段映射,允许 yaml 用 - 命名
        normalized = {k.replace("-", "_"): v for k, v in item.items()}
        # 过滤掉 Target 不认识的字段
        valid_fields = {f.name for f in Target.__dataclass_fields__.values()}
        filtered = {k: v for k, v in normalized.items() if k in valid_fields}
        targets.append(Target(**filtered))

    logger.info(f"Loaded {len(targets)} targets from {config_path}")
    return targets


def render_targets_summary(targets: list[Target]) -> str:
    """生成 targets 的文本摘要,给 LLM 在 system prompt 里看"""
    if not targets:
        return "(无目标配置)"
    lines = []
    for t in targets:
        line = f"- **{t.name}** ({t.type}, {t.criticality})"
        if t.description:
            line += f": {t.description}"
        if t.tags:
            line += f" [tags: {','.join(t.tags)}]"
        lines.append(line)
    return "\n".join(lines)
