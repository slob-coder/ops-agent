"""
ContextLimits — 上下文窗口限制

控制传入 LLM 的各类文本截断阈值。
与 limits.yaml（爆炸半径限制）分离，避免语义冲突。

配置文件: notebook/config/context_limits.yaml
"""

import os
from typing import Optional
import logging
from dataclasses import dataclass

logger = logging.getLogger("ops-agent.context_limits")


@dataclass
class ContextLimitsConfig:
    """上下文窗口限制配置 — 所有值单位为字符数（除 tree_entries 为条目数）"""

    # ── 入职探索 ──
    explore_output_chars: int = 1000

    # ── 观测阶段 ──
    observe_output_chars: int = 1500
    max_observations_chars: int = 8000

    # ── 诊断阶段 ──
    playbook_search_chars: int = 500
    playbook_content_chars: int = 1500
    incident_history_chars: int = 1000
    source_context_trace_chars: int = 2000
    diagnosis_json_chars: int = 500
    prev_summary_chars: int = 1000

    # ── 补充收集 ──
    gap_output_incident_chars: int = 2000
    gap_output_trace_chars: int = 1500

    # ── 计划阶段 ──
    verify_action_desc_chars: int = 500
    verify_expected_chars: int = 300

    # ── 执行阶段 ──
    exec_result_chars: int = 6000
    exec_result_for_rediagnose_chars: int = 4000

    # ── 验证阶段 ──
    verify_state_chars: int = 2000
    verify_response_trace_chars: int = 500

    # ── 复盘阶段 ──
    reflect_incident_chars: int = 4000

    # ── 源码定位 ──
    source_location_render_chars: int = 2000
    source_function_snippet_chars: int = 4000

    # ── 自修复上下文 ──
    self_repair_log_chars: int = 8000
    self_repair_tree_entries: int = 200
    self_repair_incident_chars: int = 3000
    self_repair_config_chars: int = 4000
    self_repair_state_dump_chars: int = 3000

    # ── 补丁应用 ──
    patch_output_truncate_chars: int = 5000

    # ── 人类交互 ──
    show_file_preview_chars: int = 2000
    conversation_incident_chars: int = 3000

    # ── 项目地图 (AGENTS.md) ──
    agents_md_chars: int = 8000

    # ── 自修复输出 ──
    self_repair_output_tail_chars: int = 1500

    @classmethod
    def from_yaml(cls, path: str) -> "ContextLimitsConfig":
        if not os.path.exists(path):
            logger.info(f"context_limits.yaml not found at {path}, using defaults")
            return cls()
        try:
            import yaml
        except ImportError:
            return cls()
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        normalized = {k.replace("-", "_"): v for k, v in data.items()}
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in normalized.items() if k in valid}
        return cls(**filtered)


# ── 模块级单例（懒加载） ──

_instance: Optional["ContextLimitsConfig"] = None


def get_context_limits(notebook_path: str = "") -> ContextLimitsConfig:
    """获取上下文限制配置（单例）"""
    global _instance
    if _instance is None:
        if notebook_path:
            path = os.path.join(notebook_path, "config", "context_limits.yaml")
        else:
            # 未提供 notebook_path 时尝试从默认 workspace 推导
            ws = Path("~/.ops-agent").expanduser()
            path = str(ws / "notebook" / "config" / "context_limits.yaml")
        _instance = ContextLimitsConfig.from_yaml(path)
    return _instance


def reload_context_limits(notebook_path: str = "") -> ContextLimitsConfig:
    """强制重新加载配置"""
    global _instance
    _instance = None
    return get_context_limits(notebook_path)
