"""
LLM Response 校验器 — 解析前校验，失败时记录结构化日志

设计要点:
- 在 _parse_* 之前调用，与解析逻辑完全解耦
- 校验失败时写入 notebook/llm_errors/YYYY-MM-DD.jsonl
- 日志与 trace 分离，独立于主流程，写入失败不影响运行
- 支持 DIAGNOSE / PLAN / VERIFY / ASSESS 四个需要结构化输出的阶段
"""

from __future__ import annotations

import json
import os
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("ops-agent.llm_validator")


# ═══════════════════════════════════════════
#  数据结构
# ═══════════════════════════════════════════

@dataclass
class CheckResult:
    """单条校验结果"""
    rule: str
    passed: bool
    detail: str = ""


@dataclass
class ValidationResult:
    """一次 validate 的完整结果"""
    passed: bool
    phase: str = ""
    error_type: str = ""        # empty_response | json_parse_failed | missing_required_field | invalid_field_value | invalid_steps | invalid_format
    error_detail: str = ""
    checks: list[CheckResult] = field(default_factory=list)


# ═══════════════════════════════════════════
#  校验规则
# ═══════════════════════════════════════════

# 合法枚举值
VALID_NEXT_ACTIONS = {"FIX", "COLLECT_MORE", "MONITOR", "ESCALATE", "READY"}
VALID_DIAGNOSE_TYPES = {"code_bug", "runtime", "config", "resource", "external", "unknown"}
VALID_VERIFY_RESULTS = {"SUCCESS", "FAILED", "UNCERTAIN"}


def _extract_json_from_text(text: str) -> dict | None:
    """尝试从文本中提取 JSON（复用 parsers 的逻辑）"""
    text = text.strip()
    # ```json ... ``` 块
    m = re.search(r'```json\s*\n(.*?)```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            pass
    # 直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # 最外层 { ... }
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _base_phase(phase: str) -> str:
    """提取阶段基础名: PLAN_R2_RETRY → PLAN, DIAGNOSE → DIAGNOSE"""
    return re.sub(r"_R\d+.*", "", phase).upper()


def _validate_common(phase: str, response: str) -> list[CheckResult]:
    """所有阶段共有的校验"""
    checks = []
    checks.append(CheckResult("response_non_empty", bool(response and response.strip()),
                              "LLM 返回空字符串" if not response or not response.strip() else ""))
    return checks


def _validate_json_phase(phase: str, response: str) -> list[CheckResult]:
    """JSON 格式阶段的校验（DIAGNOSE / PLAN / VERIFY）"""
    checks = _validate_common(phase, response)

    if not response or not response.strip():
        return checks

    data = _extract_json_from_text(response)
    checks.append(CheckResult("json_extractable", data is not None,
                              "无法从 response 中提取合法 JSON" if data is None else ""))

    if data is None:
        return checks

    base = _base_phase(phase)

    if base == "DIAGNOSE":
        has_hypothesis = "hypothesis" in data and data["hypothesis"]
        checks.append(CheckResult("has_hypothesis", has_hypothesis,
                                  "缺少 hypothesis 字段" if not has_hypothesis else ""))
        na = data.get("next_action", "")
        valid_na = na.upper() in VALID_NEXT_ACTIONS
        checks.append(CheckResult("valid_next_action", valid_na,
                                  f"next_action='{na}' 不在合法枚举内" if not valid_na else ""))
        dtype = data.get("type", "")
        valid_type = dtype in VALID_DIAGNOSE_TYPES
        checks.append(CheckResult("valid_type", valid_type,
                                  f"type='{dtype}' 不在合法枚举内" if not valid_type else ""))

    elif base == "PLAN":
        na = str(data.get("next_action", "")).upper()
        valid_na = na in VALID_NEXT_ACTIONS
        checks.append(CheckResult("valid_next_action", valid_na,
                                  f"next_action='{na}' 不在合法枚举内" if not valid_na else ""))

        if na == "READY":
            steps = data.get("steps", [])
            has_valid_steps = isinstance(steps, list) and len(steps) > 0 and any(
                isinstance(s, dict) and s.get("command") for s in steps
            )
            checks.append(CheckResult("ready_has_steps", has_valid_steps,
                                      "READY 但 steps 为空或无有效 command" if not has_valid_steps else ""))
        elif na == "COLLECT_MORE":
            gaps = data.get("gaps", [])
            has_gaps = isinstance(gaps, list) and len(gaps) > 0
            checks.append(CheckResult("collect_more_has_gaps", has_gaps,
                                      "COLLECT_MORE 但 gaps 为空" if not has_gaps else ""))

    elif base == "VERIFY":
        result_val = str(data.get("result", "")).upper()
        valid_result = result_val in VALID_VERIFY_RESULTS
        checks.append(CheckResult("valid_result", valid_result,
                                  f"result='{result_val}' 不在合法枚举内" if not valid_result else ""))

    return checks


def _validate_assess(phase: str, response: str) -> list[CheckResult]:
    """ASSESS 阶段校验（键值对文本格式）"""
    checks = _validate_common(phase, response)

    if not response or not response.strip():
        return checks

    upper = response.upper()
    has_status = "STATUS:" in upper
    has_severity = "SEVERITY:" in upper
    checks.append(CheckResult("has_status", has_status,
                              "缺少 STATUS: 行" if not has_status else ""))
    checks.append(CheckResult("has_severity", has_severity,
                              "缺少 SEVERITY: 行" if not has_severity else ""))

    return checks


# ═══════════════════════════════════════════
#  校验器主类
# ═══════════════════════════════════════════

# 需要校验的阶段
_VALIDATED_PHASES = {"DIAGNOSE", "PLAN", "VERIFY", "ASSESS"}

# 需要 JSON 的阶段
_JSON_PHASES = {"DIAGNOSE", "PLAN", "VERIFY"}


class LLMValidator:
    """LLM Response 校验器

    用法:
        validator = LLMValidator("notebook/llm_errors")
        result = validator.validate(phase, response, provider, model, ...)
        if not result.passed:
            validator.log_error(result, request_snippet, response_snippet)
    """

    def __init__(self, dir_path: str):
        self.dir_path = dir_path
        try:
            os.makedirs(self.dir_path, exist_ok=True)
        except OSError as e:
            logger.warning(f"llm_errors dir create failed: {e}")

    def validate(self, phase: str, response: str,
                 provider: str, model: str,
                 prompt_chars: int = 0, duration_ms: int = 0,
                 incident: str = "") -> ValidationResult:
        """校验 LLM response，返回校验结果

        不抛异常，不影响主流程。
        """
        base = _base_phase(phase)

        # 不需要校验的阶段，直接通过
        if base not in _VALIDATED_PHASES:
            return ValidationResult(passed=True, phase=phase)

        # 选择校验逻辑
        if base in _JSON_PHASES:
            checks = _validate_json_phase(phase, response)
        else:  # ASSESS
            checks = _validate_assess(phase, response)

        # 汇总
        passed = all(c.passed for c in checks)
        failed_checks = [c for c in checks if not c.passed]

        error_type = ""
        error_detail = ""
        if not passed:
            # 确定 error_type（按优先级）
            first_fail = failed_checks[0]
            if first_fail.rule == "response_non_empty":
                error_type = "empty_response"
                error_detail = first_fail.detail
            elif first_fail.rule == "json_extractable":
                error_type = "json_parse_failed"
                error_detail = first_fail.detail
            elif "has_" in first_fail.rule or "ready_has" in first_fail.rule or "collect_more_has" in first_fail.rule:
                error_type = "missing_required_field"
                error_detail = "; ".join(c.detail for c in failed_checks if c.detail)
            elif "valid_" in first_fail.rule:
                error_type = "invalid_field_value"
                error_detail = "; ".join(c.detail for c in failed_checks if c.detail)
            else:
                error_type = "invalid_format"
                error_detail = "; ".join(c.detail for c in failed_checks if c.detail)

        return ValidationResult(
            passed=passed,
            phase=phase,
            error_type=error_type,
            error_detail=error_detail,
            checks=checks,
        )

    def log_error(self, result: ValidationResult,
                  provider: str = "", model: str = "",
                  prompt_chars: int = 0, response_chars: int = 0,
                  duration_ms: int = 0, incident: str = "",
                  request_snippet: str = "", response_snippet: str = "") -> bool:
        """将校验失败结果写入 JSONL 日志

        失败静默，不影响主流程。
        """
        if result.passed:
            return True

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": result.phase,
            "incident": incident,
            "provider": provider,
            "model": model,
            "error_type": result.error_type,
            "error_detail": result.error_detail,
            "validation": [
                {"rule": c.rule, "passed": c.passed, "detail": c.detail}
                for c in result.checks if not c.passed
            ],
            "prompt_chars": prompt_chars,
            "response_chars": response_chars,
            "duration_ms": duration_ms,
            "request_snippet": request_snippet[:500],
            "response_snippet": response_snippet[:500],
        }

        path = self._today_file()
        try:
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except OSError as e:
            logger.warning(f"llm_errors write failed: {e}")
            return False

    def read_day(self, date_str: str = "") -> list[dict]:
        """读取某天的错误记录"""
        date_str = date_str or self._today_str()
        path = os.path.join(self.dir_path, f"{date_str}.jsonl")
        if not os.path.exists(path):
            return []
        out = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            logger.warning(f"llm_errors read failed: {e}")
        return out

    def summary(self, date_str: str = "") -> dict:
        """统计某天的错误摘要"""
        records = self.read_day(date_str)
        if not records:
            return {"total": 0}

        by_type: dict[str, int] = {}
        by_phase: dict[str, int] = {}
        by_provider: dict[str, int] = {}
        empty_count = 0

        for r in records:
            et = r.get("error_type", "unknown")
            by_type[et] = by_type.get(et, 0) + 1
            ph = r.get("phase", "unknown")
            by_phase[ph] = by_phase.get(ph, 0) + 1
            pv = r.get("provider", "unknown")
            by_provider[pv] = by_provider.get(pv, 0) + 1
            if et == "empty_response":
                empty_count += 1

        return {
            "total": len(records),
            "empty_response": empty_count,
            "by_type": by_type,
            "by_phase": by_phase,
            "by_provider": by_provider,
        }

    def _today_file(self) -> str:
        return os.path.join(self.dir_path, f"{self._today_str()}.jsonl")

    @staticmethod
    def _today_str() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
