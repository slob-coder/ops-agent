"""trace.md 解析器 — 将 trace 文件解析为结构化 Phase 列表

trace 文件格式:
    ### [YYYY-MM-DD HH:MM:SS] PHASE_NAME [REQUEST|RESPONSE]
    ```

    内容

    ```

解析为 Phase 对象列表，每个 Phase 包含 timestamp、name、round、direction、sections。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union, Optional


@dataclass
class Section:
    """Prompt 中的一个 section（按 ## 标题切分）"""
    title: str
    content: str
    start_offset: int = 0  # 在原始 content 中的字符偏移


@dataclass
class Phase:
    """trace 中的一个阶段记录"""
    timestamp: str
    name: str           # e.g. DIAGNOSE, PLAN_R1, EXECUTE
    direction: str      # REQUEST, RESPONSE, or "" (非 prompt/response 的 trace)
    raw_content: str    # 原始内容（不含 markdown code fence）
    sections: list[Section] = field(default_factory=list)
    round_num: int = 1  # 同名阶段的轮次（自动计算）

    # ── 便捷属性 ──

    @property
    def is_prompt(self) -> bool:
        return self.direction == "REQUEST"

    @property
    def is_response(self) -> bool:
        return self.direction == "RESPONSE"

    @property
    def base_name(self) -> str:
        """去掉轮次后缀的基础名称，如 PLAN_R2 → PLAN"""
        m = re.match(r"^(.+?)_R\d+$", self.name)
        return m.group(1) if m else self.name

    @property
    def content_size(self) -> int:
        return len(self.raw_content)

    def get_section(self, title_pattern: str) -> Optional[Section]:
        """按标题模糊匹配获取 section"""
        pattern = re.compile(title_pattern, re.IGNORECASE)
        for s in self.sections:
            if pattern.search(s.title):
                return s
        return None


# ── 解析正则 ──

# 匹配 ### [2026-05-02 09:44:05] DIAGNOSE [REQUEST]
_RE_PHASE_HEADER = re.compile(
    r"^###\s*\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]\s+(.+?)$"
)

# 匹配 ## 标题行（prompt 内的 section 分隔）
_RE_SECTION_HEADER = re.compile(r"^##\s+(.+)$", re.MULTILINE)

# 模板中的变量占位符，如 {relevant_playbooks}
_RE_TEMPLATE_VAR = re.compile(r"\{([a-z_]+)\}")


def _load_template_sections(template_name: str) -> list[str]:
    """加载 prompt 模板中定义的 ## section 标题列表

    返回模板中所有 ## 标题的文字（去掉了变量占位符后的纯文本部分），
    用于在 trace 中精确识别 section 边界。
    """
    template_path = Path(__file__).parent.parent.parent / "prompts" / f"{template_name}.md"
    if not template_path.exists():
        return []

    template = template_path.read_text(encoding="utf-8")
    titles = []
    for m in _RE_SECTION_HEADER.finditer(template):
        title = m.group(1).strip()
        titles.append(title)
    return titles


# 缓存模板 section 标题
_template_cache: dict[str, list[str]] = {}


def _get_template_sections(template_name: str) -> list[str]:
    if template_name not in _template_cache:
        _template_cache[template_name] = _load_template_sections(template_name)
    return _template_cache[template_name]


def _infer_template_name(phase_name: str) -> Optional[str]:
    """从 phase 名称推断对应的 prompt 模板名"""
    base = re.sub(r"_R\d+$", "", phase_name).upper()
    mapping = {
        "DIAGNOSE": "diagnose",
        "PLAN": "plan",
        "OBSERVE": "observe",
        "ASSESS": "assess",
        "VERIFY": "verify",
        "REFLECT": "reflect",
        "SUMMARIZE": "observe",  # SUMMARIZE 用的是 observe 模板的变体
    }
    return mapping.get(base)


def _match_section_title(actual: str, template_titles: list[str]) -> bool:
    """判断 actual 标题是否匹配模板中的某个 section 标题

    匹配规则：
    1. 精确匹配（最快）
    2. 模板标题中的变量占位符替换为通配后正则匹配
    3. 中文标题前缀匹配（actual 以模板标题开头，或模板标题以 actual 开头）
       但要求最小长度 >= 2 个字符，避免短标题误匹配
    """
    for tmpl in template_titles:
        # 1. 精确匹配
        if actual == tmpl:
            return True

        # 2. 变量通配正则匹配
        pattern = re.escape(tmpl)
        pattern = _RE_TEMPLATE_VAR.sub(r".*", pattern)
        if re.fullmatch(pattern, actual, re.DOTALL):
            return True

        # 3. 前缀匹配（避免子串误匹配：只用 longer.startswith(shorter)，
        #    且 shorter 长度 >= 2）
        if len(tmpl) >= 2 and (actual.startswith(tmpl) or tmpl.startswith(actual)):
            return True

    return False


def parse_trace(path: Union[str, Path]) -> list[Phase]:
    """解析 trace 文件，返回 Phase 列表"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Trace file not found: {path}")

    text = path.read_text(encoding="utf-8", errors="replace")

    # ── 第一步：按 ### 标题切分 ──
    raw_blocks: list[tuple[str, str]] = []  # (header, content)
    current_header = ""
    current_lines: list[str] = []

    for line in text.split("\n"):
        m = _RE_PHASE_HEADER.match(line)
        if m:
            if current_header:
                raw_blocks.append((current_header, "\n".join(current_lines)))
            current_header = line
            current_lines = []
        else:
            current_lines.append(line)

    if current_header:
        raw_blocks.append((current_header, "\n".join(current_lines)))

    # ── 第二步：解析每个块 ──
    phases: list[Phase] = []
    round_tracker: dict[str, int] = {}  # base_name → count

    for header, content in raw_blocks:
        m = _RE_PHASE_HEADER.match(header)
        if not m:
            continue

        timestamp = m.group(1)
        raw_name = m.group(2).strip()

        # 分离 name 和 direction
        dir_match = re.match(r"^(.+?)\s+\[(REQUEST|RESPONSE)\]$", raw_name)
        if dir_match:
            name = dir_match.group(1).strip()
            direction = dir_match.group(2)
        else:
            name = raw_name
            direction = ""

        # 去除 code fence
        body = _strip_code_fence(content)

        # 计算轮次（仅对 REQUEST 计数）
        base = re.sub(r"_R\d+$", "", name)
        if direction == "REQUEST":
            round_tracker[base] = round_tracker.get(base, 0) + 1
        round_num = round_tracker.get(base, 1)

        phase = Phase(
            timestamp=timestamp,
            name=name,
            direction=direction,
            raw_content=body,
            round_num=round_num,
        )

        # 解析 sections（仅对 REQUEST 类型的 prompt）
        if direction == "REQUEST":
            template_name = _infer_template_name(name)
            template_sections = _get_template_sections(template_name) if template_name else []
            phase.sections = _parse_sections(body, template_sections)

        phases.append(phase)

    return phases


def _strip_code_fence(text: str) -> str:
    """去除首尾的 ``` markdown code fence"""
    lines = text.strip().split("\n")
    # 去掉开头的 ```
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    # 去掉结尾的 ```
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _parse_sections(body: str, template_titles: list[str] = None) -> list[Section]:
    """按 ## 标题切分 prompt 内容为 sections

    如果提供了 template_titles，则只在匹配模板定义的 ## 标题处切分，
    内容中出现的其他 ## 标题不会被切分（避免 playbook 等内容被误切）。
    """
    sections: list[Section] = []

    # 找到所有 ## 标题的位置
    all_matches = list(_RE_SECTION_HEADER.finditer(body))

    if not all_matches:
        sections.append(Section(title="(preamble)", content=body.strip()))
        return sections

    # 筛选合法的 section 边界
    if template_titles:
        boundary_matches = []
        for m in all_matches:
            title = m.group(1).strip()
            if _match_section_title(title, template_titles):
                boundary_matches.append(m)
        # 如果模板匹配完全失败（可能是模板变了），回退到全部匹配
        if not boundary_matches:
            boundary_matches = all_matches
    else:
        boundary_matches = all_matches

    # preamble（第一个边界标题之前的内容）
    if boundary_matches[0].start() > 0:
        preamble = body[:boundary_matches[0].start()].strip()
        if preamble:
            sections.append(Section(title="(preamble)", content=preamble))

    # 各 section
    for i, m in enumerate(boundary_matches):
        title = m.group(1).strip()
        start = m.end()
        end = boundary_matches[i + 1].start() if i + 1 < len(boundary_matches) else len(body)
        content = body[start:end].strip()
        sections.append(Section(title=title, content=content, start_offset=m.start()))

    return sections
