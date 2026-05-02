"""System prompt 离线重建（方案 B）

根据 trace 时间点和 notebook 配置，尽可能还原当时的 system prompt。
注意：这是近似重建，可能与当时实际发送的有差异（比如 mode、active_incident 可能变了）。
"""

import re
from pathlib import Path
from typing import Union
from typing import Optional


def rebuild_system_prompt(notebook_path: Union[str, Path], phase_name: str = "",
                         timestamp: str = "") -> str:
    """从 notebook 配置重建 system prompt

    Args:
        notebook_path: notebook 目录路径
        phase_name: 阶段名称（用于推断 mode）
        timestamp: 时间戳（暂未使用，预留）
    """
    nb = Path(notebook_path)

    # 加载 system prompt 模板
    # 向上查找项目根目录
    project_root = nb.parent
    while project_root != project_root.parent:
        candidate = project_root / "prompts" / "system.md"
        if candidate.exists():
            system_template = candidate.read_text(encoding="utf-8")
            break
        project_root = project_root.parent
    else:
        return "(system.md 模板未找到)"

    # 收集各 section
    system_map = _read_file(nb / "system-map.md")
    permissions = _read_file(nb / "config" / "permissions.md")

    # 尝试读取 targets 配置
    target_info = _build_target_info_from_config(nb)

    # 填充模板
    result = system_template
    replacements = {
        "{mode}": _infer_mode(phase_name),
        "{readonly}": "否",  # 无法从 trace 推断，默认否
        "{active_incident}": "(未知)",
        "{permissions}": permissions or "(未配置,使用默认策略)",
        "{system_map}": system_map or "(尚未探索,系统拓扑未知)",
        "{target_info}": target_info,
        "{limits_status}": "(离线重建，限流状态不可用)",
        "{notebook_path}": str(nb),
    }

    for key, value in replacements.items():
        result = result.replace(key, str(value))

    # 清理未填充变量
    result = re.sub(r"\{[a-z_]+\}", "(无)", result)

    return result


def _read_file(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


def _infer_mode(phase_name: str) -> str:
    """从 phase 名称推断 mode"""
    name = phase_name.upper()
    if "OBSERVE" in name or "PATROL" in name:
        return "patrol"
    if any(k in name for k in ("DIAGNOSE", "PLAN", "EXECUTE", "VERIFY", "REFLECT")):
        return "incident"
    if "INVESTIGATE" in name:
        return "investigate"
    return "patrol"


def _build_target_info_from_config(nb: Path) -> str:
    """从 targets.yaml 重建 target info"""
    targets_yaml = nb / "config" / "targets.yaml"
    if not targets_yaml.exists():
        return "(未配置目标)"

    try:
        import yaml
        with open(targets_yaml) as f:
            cfg = yaml.safe_load(f) or {}

        targets = cfg.get("targets", [])
        if not targets:
            return "(未配置目标)"

        lines = []
        for t in targets:
            name = t.get("name", "?")
            mode = t.get("mode", "local")
            desc = t.get("description", "")
            line = f"  - {name} (类型: {mode})"
            if desc:
                line += f": {desc}"
            lines.append(line)

        return "当前目标:\n" + "\n".join(lines)
    except Exception as e:
        return f"(解析 targets.yaml 失败: {e})"
