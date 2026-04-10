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
class SourceRepo:
    """一个源码仓库的配置(Sprint 2 引入)

    描述运行时实体到本地源码 clone 的映射关系,供 source_locator 使用。
    """
    name: str                              # 仓库别名,如 "backend"
    path: str                              # 本地 clone 的绝对路径(Agent 工作站上)
    repo_url: str = ""                     # git 远端
    branch: str = "main"
    language: str = ""                     # python / java / go / node / rust / cpp / ...
    build_cmd: str = ""                    # 编译/语法检查命令
    test_cmd: str = ""                     # 单元测试命令
    runtime_service: str = ""              # 对应运行时的服务/容器/pod 名
    log_path: str = ""                     # 对应日志路径(可选)
    # 路径前缀映射:处理容器内外路径差异
    # 容器里是 /app/src/main.py,工作站 clone 是 /opt/sources/backend/src/main.py
    path_prefix_runtime: str = ""          # 运行时前缀 "/app"
    path_prefix_local: str = ""            # 本地前缀(相对 path,通常为 "")

    # Sprint 4: PR 工作流 + 部署观察
    git_host: str = ""                     # github | gitlab | noop | "" (禁用自动 PR)
    base_branch: str = "main"              # PR 目标分支
    deploy_signal: dict = field(default_factory=dict)
    # deploy_signal 形如:
    #   {"type": "http", "url": "http://web/version", "expect_contains": "{commit_sha}",
    #    "check_interval": 10, "timeout": 1800}
    # 或 {"type": "fixed_wait", "seconds": 60}

    @classmethod
    def from_dict(cls, d: dict) -> "SourceRepo":
        """从 dict 构造,兼容 yaml 加载和旧版配置"""
        if isinstance(d, cls):
            return d
        normalized = {k.replace("-", "_"): v for k, v in d.items()}
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in normalized.items() if k in valid}
        # name 和 path 是必填
        filtered.setdefault("name", "unnamed")
        filtered.setdefault("path", "")
        return cls(**filtered)


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

    # 源码地图(供 bug 修复用) — 保持 list[dict] 以兼容 Sprint 1
    source_repos: list[dict] = field(default_factory=list)

    def get_source_repos(self) -> list["SourceRepo"]:
        """返回 SourceRepo 对象列表(Sprint 2 新增)

        保持向后兼容:target.source_repos 仍然是 list[dict],
        但调用 get_source_repos() 可以拿到类型化的对象。
        """
        return [SourceRepo.from_dict(r) for r in (self.source_repos or [])]


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
