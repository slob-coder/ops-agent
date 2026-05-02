"""trace-view CLI 入口 + 交互式模式

子命令:
  ops-agent trace-view <trace-file>                概览
  ops-agent trace-view <trace-file> show [N]       详情
  ops-agent trace-view <trace-file> diff <N> <M>   对比
  ops-agent trace-view <trace-file> -i             交互式

N, M 为 phase 编号（从概览中看到的序号）
"""

import re
import sys
import argparse
from pathlib import Path
from typing import Optional

from .parser import parse_trace, Phase
from .analyzer import analyze_phase, detect_truncation, load_context_limits, PhaseStat
from .formatter import (
    format_overview, format_detail, format_diff,
    format_phase_list, format_section_list,
)
from .system_rebuilder import rebuild_system_prompt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops-agent trace-view",
        description="OpsAgent trace 日志分析器",
    )
    parser.add_argument("trace_file", help="trace .md 文件路径")
    parser.add_argument("--notebook", default="", help="notebook 目录路径（用于截断检测和 system prompt 重建）")

    sub = parser.add_subparsers(dest="command")

    # show
    show_p = sub.add_parser("show", help="查看 phase 详情")
    show_p.add_argument("phase_num", type=int, nargs="?", default=None,
                        help="phase 序号（概览中的编号）")
    show_p.add_argument("--section", "-s", default="", help="只显示匹配此正则的 section")
    show_p.add_argument("--with-response", "-r", action="store_true",
                        help="同时显示对应的 RESPONSE")
    show_p.add_argument("--system", action="store_true",
                        help="显示 system prompt（需要 --notebook）")

    # diff
    diff_p = sub.add_parser("diff", help="对比两个 phase")
    diff_p.add_argument("phase_a", type=int, help="phase A 序号")
    diff_p.add_argument("phase_b", type=int, help="phase B 序号")
    diff_p.add_argument("--section", "-s", default="", help="只对比匹配此正则的 section")

    # 交互式
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="交互式模式")

    return parser


def run(args: Optional[list[str]] = None):
    """trace-view 主入口"""
    parser = build_parser()
    parsed = parser.parse_args(args)

    trace_file = Path(parsed.trace_file)
    if not trace_file.exists():
        print(f"❌ 文件不存在: {trace_file}")
        sys.exit(1)

    # 解析
    phases = parse_trace(trace_file)

    if not phases:
        print("❌ trace 文件为空或格式不匹配")
        sys.exit(1)

    # 加载 context limits
    ctx_limits = None
    if parsed.notebook:
        ctx_limits = load_context_limits(parsed.notebook)

    # 分析
    stats = [analyze_phase(p, ctx_limits) for p in phases]

    # 从文件名提取 incident 标题
    incident_title = trace_file.stem
    # 清理文件名中的 .md 后缀和日期前缀
    title_clean = re.sub(r"^\d{4}-\d{2}-\d{2}-\d{4}-", "", incident_title)
    title_clean = re.sub(r"\.md$", "", title_clean)

    # ── 路由到不同模式 ──

    if parsed.interactive:
        _interactive_mode(phases, stats, parsed, ctx_limits, title_clean)
        return

    if parsed.command == "show":
        _show_mode(phases, stats, parsed, ctx_limits)
        return

    if parsed.command == "diff":
        _diff_mode(phases, stats, parsed)
        return

    # 默认：概览
    print(format_overview(phases, stats, incident_title=title_clean))

    # 截断警告
    if ctx_limits:
        warnings = detect_truncation(phases, ctx_limits)
        if warnings:
            print()
            for w in warnings:
                print(w)


# ═══════════════════════════════════════════
#  Show 模式
# ═══════════════════════════════════════════

def _show_mode(phases: list[Phase], stats: list[PhaseStat],
               parsed, ctx_limits: Optional[dict]):
    if parsed.phase_num is None:
        print("❌ 请指定 phase 序号，如: trace-view <file> show 3")
        print()
        print(format_phase_list(phases, stats))
        return

    idx = parsed.phase_num - 1
    if idx < 0 or idx >= len(phases):
        print(f"❌ 序号 {parsed.phase_num} 超出范围 (1-{len(phases)})")
        return

    phase = phases[idx]
    stat = stats[idx]

    # 显示 system prompt
    if parsed.system and parsed.notebook:
        system = rebuild_system_prompt(parsed.notebook, phase.name, phase.timestamp)
        print("=" * 60)
        print("SYSTEM PROMPT (reconstructed)")
        print("=" * 60)
        print(system)
        print()

    # 显示 phase 详情
    print(format_detail(phase, stat,
                        section_filter=parsed.section or None))

    # 显示对应 response
    if parsed.with_response and phase.is_prompt:
        resp = _find_response(phases, stats, phase)
        if resp:
            resp_phase, resp_stat = resp
            print()
            print("=" * 60)
            print(f"RESPONSE: {resp_phase.timestamp} {resp_phase.name}")
            print("=" * 60)
            print(resp_phase.raw_content)


# ═══════════════════════════════════════════
#  Diff 模式
# ═══════════════════════════════════════════

def _diff_mode(phases: list[Phase], stats: list[PhaseStat], parsed):
    idx_a = parsed.phase_a - 1
    idx_b = parsed.phase_b - 1

    if idx_a < 0 or idx_a >= len(phases) or idx_b < 0 or idx_b >= len(phases):
        print(f"❌ 序号超出范围 (1-{len(phases)})")
        return

    phase_a = phases[idx_a]
    stat_a = stats[idx_a]
    stat_b = stats[idx_b]

    print(format_diff(phase_a, stat_a, stat_b,
                      section_filter=parsed.section or None))


# ═══════════════════════════════════════════
#  交互式模式
# ═══════════════════════════════════════════

def _interactive_mode(phases: list[Phase], stats: list[PhaseStat],
                      parsed, ctx_limits: Optional[dict],
                      incident_title: str):
    """交互式浏览模式"""
    print(f"📋 Trace: {incident_title}")
    print(f"   {len(phases)} phases loaded")
    print()

    while True:
        print("\n" + "─" * 60)
        print("[1] Overview  [2] Select Phase  [3] Search  [q] Quit")
        try:
            choice = input("▶ ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice in ("q", "quit", "exit"):
            break

        if choice == "1":
            print(format_overview(phases, stats, incident_title=incident_title))

        elif choice == "2":
            _interactive_select_phase(phases, stats, parsed, ctx_limits)

        elif choice == "3":
            _interactive_search(phases, stats)


def _interactive_select_phase(phases: list[Phase], stats: list[PhaseStat],
                              parsed, ctx_limits: Optional[dict]):
    """交互式选择 phase"""
    print("\nPhases:")
    print(format_phase_list(phases, stats))
    print()
    print("输入序号查看详情，或按 Enter 返回")

    try:
        raw = input("▶ ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if not raw:
        return

    try:
        idx = int(raw) - 1
    except ValueError:
        print("❌ 请输入数字")
        return

    if idx < 0 or idx >= len(phases):
        print(f"❌ 序号超出范围")
        return

    phase = phases[idx]
    stat = stats[idx]

    while True:
        print()
        print(f"📖 {phase.timestamp} {phase.name} [{phase.direction}]")
        print(f"   {_fmt_phase_summary(stat)}")
        print()
        if phase.is_prompt and phase.sections:
            print("[1] Full Request  [2] Select Section  [3] View Response  [Enter] Back")
        elif phase.is_response:
            print("[1] Full Response  [Enter] Back")
        else:
            print("[1] Full Content  [Enter] Back")

        try:
            sub = input("▶ ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if not sub:
            return

        if sub == "1":
            # 分页显示
            _paged_print(phase.raw_content)

        elif sub == "2" and phase.is_prompt and phase.sections:
            _interactive_select_section(phase, stat)

        elif sub == "3" and phase.is_prompt:
            resp = _find_response(phases, stats, phase)
            if resp:
                resp_phase, resp_stat = resp
                print()
                print(f"📨 RESPONSE: {resp_phase.timestamp} {resp_phase.name}")
                _paged_print(resp_phase.raw_content)
            else:
                print("⚠️  未找到对应的 RESPONSE")


def _interactive_select_section(phase: Phase, stat: PhaseStat):
    """交互式选择 section"""
    print("\nSections:")
    print(format_section_list(phase, stat))
    print()
    print("输入序号查看，或按 Enter 返回")

    try:
        raw = input("▶ ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if not raw:
        return

    try:
        idx = int(raw) - 1
    except ValueError:
        print("❌ 请输入数字")
        return

    if idx < 0 or idx >= len(phase.sections):
        print("❌ 序号超出范围")
        return

    sec = phase.sections[idx]
    sec_stat = _find_sec_stat(stat, sec.title)
    size_info = f"({sec_stat.chars} chars, {sec_stat.percent}%)" if sec_stat else ""

    print()
    print(f"## {sec.title}  {size_info}")
    _paged_print(sec.content)


def _interactive_search(phases: list[Phase], stats: list[PhaseStat]):
    """交互式搜索"""
    print("输入关键词搜索 phase 和 section:")
    try:
        query = input("▶ ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if not query:
        return

    results: list[str] = []
    for i, (phase, stat) in enumerate(zip(phases, stats), 1):
        matched = False

        # 搜索 phase 名称
        if query.lower() in phase.name.lower():
            results.append(f"  {i}. {phase.timestamp} {phase.name} [{phase.direction}] (name match)")
            matched = True

        # 搜索 section 标题
        if phase.is_prompt:
            for sec in phase.sections:
                if query.lower() in sec.title.lower():
                    results.append(f"  {i}. {phase.name} → {sec.title} (section title)")
                    matched = True

        # 搜索内容
        if not matched and query.lower() in phase.raw_content.lower():
            # 找到出现的位置
            pos = phase.raw_content.lower().index(query.lower())
            context = phase.raw_content[max(0, pos - 30):pos + len(query) + 30]
            context = context.replace("\n", " ")
            results.append(f"  {i}. {phase.name} → ...{context}... (content)")

    if results:
        print(f"\n🔍 Found {len(results)} matches:")
        for r in results[:50]:  # 最多 50 条
            print(r)
    else:
        print("❌ 未找到匹配")


# ═══════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════

def _find_response(phases: list[Phase], stats: list[PhaseStat],
                   request_phase: Phase) -> Optional[tuple[Phase, PhaseStat]]:
    """找到 REQUEST 对应的 RESPONSE"""
    base_name = request_phase.base_name
    for i, phase in enumerate(phases):
        if (phase is request_phase):
            # 找后续的 RESPONSE
            for j in range(i + 1, len(phases)):
                p = phases[j]
                if p.base_name == base_name and p.is_response:
                    return p, stats[j]
                # 遇到下一个不同的 phase，停止
                if p.base_name != base_name and not p.direction == "":
                    break
            break
    return None


def _find_sec_stat(stat: PhaseStat, title: str):
    from .formatter import _find_sec_stat as _fss
    return _fss(stat, title)


def _fmt_phase_summary(stat: PhaseStat) -> str:
    if stat.phase.is_prompt:
        return f"{stat.total_chars} chars, {len(stat.sections)} sections"
    elif stat.phase.is_response:
        na = stat.next_action
        return f"→ {na}" if na else "response"
    return stat.phase.raw_content[:60].replace("\n", " ")


def _paged_print(text: str, page_size: int = 80):
    """分页打印长文本"""
    lines = text.split("\n")
    for i in range(0, len(lines), page_size):
        chunk = lines[i:i + page_size]
        print("\n".join(chunk))

        if i + page_size < len(lines):
            print()
            try:
                input(f"─── Press Enter for more ({i + page_size}/{len(lines)}) ───")
            except (EOFError, KeyboardInterrupt):
                print()
                return
