"""输出格式化器 — 概览 / 详情 / 对比 三种视图"""

import difflib
from typing import Optional

from .parser import Phase, Section
from .analyzer import PhaseStat, SectionStat


# ═══════════════════════════════════════════
#  概览模式
# ═══════════════════════════════════════════

def format_overview(phases: list[Phase], stats: list[PhaseStat],
                    incident_title: str = "") -> str:
    """格式化概览输出"""
    lines: list[str] = []

    # 标题
    if incident_title:
        lines.append(f"Incident: {incident_title}")
    else:
        lines.append("Trace Overview")

    # 时间范围
    if phases:
        first_ts = phases[0].timestamp
        last_ts = phases[-1].timestamp
        lines.append(f"Duration: {first_ts} → {last_ts}")
    lines.append("")

    # Phase Timeline
    lines.append("Phase Timeline:")
    for phase, stat in zip(phases, stats):
        marker = ""
        if phase.is_prompt:
            marker = f"(prompt: {_fmt_size(stat.total_chars)}"
            if stat.sections:
                marker += f" | {len(stat.sections)} sections"
            marker += ")"
        elif phase.is_response:
            na = stat.next_action
            summary = stat.response_summary[:120]
            marker = f"→ {na}" if na else ""
            if summary:
                marker += f"  ({summary})" if marker else f"({summary})"
            marker = marker or "(response)"
        else:
            # 非 request/response 的 trace 记录
            summary = phase.raw_content[:80].replace("\n", " ")
            marker = f"  {summary}"

        label = f"  {phase.timestamp}  {phase.name}"
        if phase.direction:
            label += f" [{phase.direction}]"
        lines.append(f"{label}  {marker}")

    # Context Budget Breakdown（只对 REQUEST 类型，展示第一个）
    prompt_phases = [(p, s) for p, s in zip(phases, stats) if p.is_prompt]
    if prompt_phases:
        lines.append("")
        lines.append("Context Budget Breakdown:")

        # 按阶段类型分组，每组只展示第一轮
        shown_bases: set[str] = set()
        for phase, stat in prompt_phases:
            base = phase.base_name
            if base in shown_bases:
                continue
            shown_bases.add(base)

            if not stat.sections:
                continue

            lines.append(f"")
            lines.append(f"  {phase.name} [{phase.direction}] (R{phase.round_num}):")

            # 表头
            lines.append(f"    {'Section':<30s} {'Chars':>8s} {'%':>6s}  Truncated?")
            lines.append(f"    {'─' * 30} {'─' * 8} {'─' * 6}  {'─' * 20}")

            for sec in stat.sections:
                trunc = sec.truncated or "—"
                lines.append(
                    f"    {sec.title[:30]:<30s} {sec.chars:>8d} {sec.percent:>5.1f}%  {trunc}"
                )

            lines.append(f"    {'─' * 30} {'─' * 8} {'─' * 6}  {'─' * 20}")
            lines.append(f"    {'Total':<30s} {stat.total_chars:>8d}")

    # 截断警告汇总
    all_warnings: list[str] = []
    for phase, stat in zip(phases, stats):
        for sec in stat.sections:
            if sec.truncated and sec.truncated.startswith("⚠️"):
                all_warnings.append(
                    f"  ⚠️  {phase.name} R{phase.round_num} → {sec.title}: {sec.truncated}"
                )

    if all_warnings:
        lines.append("")
        lines.append("Truncation Warnings:")
        lines.extend(all_warnings)

    return "\n".join(lines)


# ═══════════════════════════════════════════
#  详情模式
# ═══════════════════════════════════════════

def format_detail(phase: Phase, stat: PhaseStat,
                  section_filter: Optional[str] = None,
                  show_response: bool = False) -> str:
    """格式化单个 phase 的详情

    Args:
        section_filter: 只显示匹配此正则的 section
        show_response: 如果 phase 是 REQUEST，也找对应的 RESPONSE 一起显示
    """
    lines: list[str] = []

    lines.append(f"### {phase.timestamp} {phase.name} [{phase.direction}]")
    lines.append(f"Total chars: {stat.total_chars}")
    lines.append("")

    if phase.is_prompt:
        import re
        for sec in phase.sections:
            if section_filter and not re.search(section_filter, sec.title, re.IGNORECASE):
                continue
            sec_stat = _find_sec_stat(stat, sec.title)
            size_info = f"({sec_stat.chars} chars, {sec_stat.percent}%)"
            if sec_stat and sec_stat.truncated:
                size_info += f" {sec_stat.truncated}"

            lines.append(f"## {sec.title}  {size_info}")
            lines.append(sec.content)
            lines.append("")
    else:
        lines.append(phase.raw_content)

    return "\n".join(lines)


# ═══════════════════════════════════════════
#  对比模式
# ═══════════════════════════════════════════

def format_diff(phase_a: Phase, stat_a: PhaseStat,
                phase_b: PhaseStat,
                section_filter: Optional[str] = None) -> str:
    """对比两个 phase 的差异"""
    lines: list[str] = []

    lines.append(f"Diff: {phase_a.name} R{phase_a.round_num} → R{phase_b.phase.round_num}")
    lines.append("")

    if phase_a.is_prompt and phase_b.phase.is_prompt:
        # 对比每个 section
        sections_a = {s.title: s for s in phase_a.sections}
        sections_b = {s.title: s for s in phase_b.phase.sections}

        all_titles = list(dict.fromkeys(
            [s.title for s in phase_a.sections] +
            [s.title for s in phase_b.phase.sections]
        ))

        import re
        for title in all_titles:
            if section_filter and not re.search(section_filter, title, re.IGNORECASE):
                continue

            sa = sections_a.get(title)
            sb = sections_b.get(title)

            if sa and sb:
                if sa.content == sb.content:
                    lines.append(f"## {title}  (identical)")
                    continue

                lines.append(f"## {title}  (changed)")
                diff = difflib.unified_diff(
                    sa.content.splitlines(keepends=True),
                    sb.content.splitlines(keepends=True),
                    fromfile=f"R{phase_a.round_num}",
                    tofile=f"R{phase_b.phase.round_num}",
                    lineterm="",
                )
                lines.extend(diff)
                lines.append("")
            elif sa:
                lines.append(f"## {title}  (only in R{phase_a.round_num})")
                lines.append(sa.content[:500])
                lines.append("")
            elif sb:
                lines.append(f"## {title}  (only in R{phase_b.phase.round_num})")
                lines.append(sb.content[:500])
                lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════
#  交互式选择列表
# ═══════════════════════════════════════════

def format_phase_list(phases: list[Phase], stats: list[PhaseStat]) -> str:
    """格式化交互式选择菜单"""
    lines: list[str] = []
    for i, (phase, stat) in enumerate(zip(phases, stats), 1):
        dir_tag = f"[{phase.direction}]" if phase.direction else ""
        extra = ""
        if phase.is_prompt:
            extra = f"  ({_fmt_size(stat.total_chars)})"
        elif phase.is_response:
            na = stat.next_action
            extra = f"  → {na}" if na else ""
        lines.append(f"  {i:>3d}. {phase.timestamp} {phase.name} {dir_tag}{extra}")
    return "\n".join(lines)


def format_section_list(phase: Phase, stat: PhaseStat) -> str:
    """格式化 section 选择菜单"""
    lines: list[str] = []
    for i, sec in enumerate(phase.sections, 1):
        sec_stat = _find_sec_stat(stat, sec.title)
        size = f"({sec_stat.chars} chars)" if sec_stat else ""
        trunc = f" {sec_stat.truncated}" if sec_stat and sec_stat.truncated else ""
        lines.append(f"  {i:>3d}. {sec.title} {size}{trunc}")
    return "\n".join(lines)


# ═══════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════

def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def _find_sec_stat(stat: PhaseStat, title: str) -> Optional[SectionStat]:
    for s in stat.sections:
        if s.title == title:
            return s
    # 模糊匹配
    for s in stat.sections:
        if title in s.title or s.title in title:
            return s
    return None
