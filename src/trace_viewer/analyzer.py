"""内容构成分析器 — 分析 prompt 的 section 占比、截断检测"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Union
from typing import Optional

from .parser import Phase, Section


@dataclass
class SectionStat:
    """单个 section 的统计"""
    title: str
    chars: int
    percent: float
    truncated: Optional[str] = None  # None | "⚠️ No (limit:N)" | "Yes (at limit)"


@dataclass
class PhaseStat:
    """单个 phase 的完整统计"""
    phase: Phase
    total_chars: int
    sections: list[SectionStat]
    response_summary: str = ""  # response 的简要摘要
    next_action: str = ""       # 从 response 中提取的 next_action


# ── 已知 section 对应的 context_limits 配置 ──
# key: section 标题模糊匹配模式, value: context_limits 属性名
_SECTION_LIMITS = {
    r"相关 Playbook|匹配的 Playbook": "playbook_content_chars",
    r"收集到的详细信息|observations": "max_observations_chars",
    r"历史类似事件|similar_incidents": "incident_history_chars",
    r"源码上下文|source_locations": "source_context_trace_chars",
}

# 默认截断值（与 context_limits.py 保持一致）
_DEFAULT_LIMITS = {
    "playbook_content_chars": 1500,
    "playbook_search_chars": 500,
    "incident_history_chars": 1500,
    "max_observations_chars": 8000,
    "source_context_trace_chars": 2000,
}


def load_context_limits(notebook_path: Union[str, Path]) -> dict[str, int]:
    """从 notebook 加载 context_limits 配置，失败则用默认值"""
    limits = dict(_DEFAULT_LIMITS)
    config_path = Path(notebook_path) / "config" / "limits.yaml"
    if not config_path.exists():
        return limits

    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        for key in _DEFAULT_LIMITS:
            if key in cfg:
                limits[key] = int(cfg[key])
    except Exception:
        pass

    return limits


def analyze_phase(phase: Phase, ctx_limits: Optional[dict[str, int]] = None) -> PhaseStat:
    """分析单个 phase 的内容构成"""
    limits = ctx_limits or dict(_DEFAULT_LIMITS)

    if not phase.is_prompt:
        return PhaseStat(
            phase=phase,
            total_chars=phase.content_size,
            sections=[],
            response_summary=_summarize_response(phase.raw_content),
            next_action=_extract_next_action(phase.raw_content),
        )

    total = phase.content_size
    section_stats: list[SectionStat] = []

    for sec in phase.sections:
        chars = len(sec.content)
        percent = (chars / total * 100) if total > 0 else 0

        truncated = None
        limit_key = _match_section_limit(sec.title)
        if limit_key and limit_key in limits:
            limit_val = limits[limit_key]
            if chars > limit_val:
                truncated = f"⚠️ No (limit:{limit_val})"
            elif chars >= limit_val * 0.9:
                truncated = f"~Yes (at {chars}/{limit_val})"

        section_stats.append(SectionStat(
            title=sec.title,
            chars=chars,
            percent=round(percent, 1),
            truncated=truncated,
        ))

    return PhaseStat(
        phase=phase,
        total_chars=total,
        sections=section_stats,
    )


def detect_truncation(phases: list[Phase], ctx_limits: Optional[dict[str, int]] = None) -> list[str]:
    """检测所有 phase 中的截断问题，返回警告列表"""
    warnings: list[str] = []
    limits = ctx_limits or dict(_DEFAULT_LIMITS)

    for phase in phases:
        if not phase.is_prompt:
            continue
        stat = analyze_phase(phase, limits)
        for sec_stat in stat.sections:
            if sec_stat.truncated and sec_stat.truncated.startswith("⚠️"):
                warnings.append(
                    f"{phase.name} R{phase.round_num} → {sec_stat.title}: "
                    f"{sec_stat.chars} chars, {sec_stat.truncated}"
                )

    return warnings


def _match_section_limit(title: str) -> Optional[str]:
    """匹配 section 标题到 context_limits 键"""
    for pattern, key in _SECTION_LIMITS.items():
        if re.search(pattern, title, re.IGNORECASE):
            return key
    return None


def _summarize_response(text: str, max_len: int = 200) -> str:
    """生成 response 的简要摘要"""
    text = text.strip()
    if not text:
        return "(empty)"
    # 尝试提取 JSON 中的关键字段
    try:
        data = json.loads(text)
        parts = []
        for key in ("next_action", "hypothesis", "facts", "expected", "reason"):
            if key in data:
                val = str(data[key])[:80]
                parts.append(f"{key}: {val}")
        if parts:
            return " | ".join(parts)
    except (json.JSONDecodeError, TypeError):
        pass
    # 纯文本截断
    first_line = text.split("\n")[0][:max_len]
    return first_line


def _extract_next_action(text: str) -> str:
    """从 response 中提取 next_action"""
    try:
        data = json.loads(text.strip())
        return data.get("next_action", "")
    except (json.JSONDecodeError, TypeError):
        return ""
