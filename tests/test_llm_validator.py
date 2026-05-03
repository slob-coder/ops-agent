"""LLM Validator 测试 — 覆盖校验规则、日志写入、摘要统计"""

import json
import os
import shutil
import tempfile

import pytest

from src.reliability.llm_validator import (
    LLMValidator,
    ValidationResult,
    CheckResult,
    _base_phase,
    _extract_json_from_text,
    _validate_common,
    _validate_json_phase,
    _validate_assess,
    VALID_NEXT_ACTIONS,
    VALID_DIAGNOSE_TYPES,
    VALID_VERIFY_RESULTS,
)


# ═══════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def validator(tmp_dir):
    return LLMValidator(tmp_dir)


# ═══════════════════════════════════════════
#  _base_phase
# ═══════════════════════════════════════════

class TestBasePhase:
    def test_simple(self):
        assert _base_phase("DIAGNOSE") == "DIAGNOSE"

    def test_with_round(self):
        assert _base_phase("PLAN_R2") == "PLAN"

    def test_with_retry(self):
        assert _base_phase("PLAN_R2_RETRY") == "PLAN"

    def test_verify(self):
        assert _base_phase("VERIFY") == "VERIFY"

    def test_assess(self):
        assert _base_phase("ASSESS") == "ASSESS"

    def test_observe(self):
        assert _base_phase("OBSERVE") == "OBSERVE"

    def test_lowercase_untouched(self):
        # _base_phase upper() after regex, so lowercase _r1 won't match
        assert _base_phase("plan_r1") == "PLAN_R1"


# ═══════════════════════════════════════════
#  _extract_json_from_text
# ═══════════════════════════════════════════

class TestExtractJson:
    def test_pure_json(self):
        data = _extract_json_from_text('{"key": "value"}')
        assert data == {"key": "value"}

    def test_code_fence(self):
        text = '```json\n{"key": "value"}\n```'
        data = _extract_json_from_text(text)
        assert data == {"key": "value"}

    def test_text_plus_code_fence(self):
        text = 'Some explanation\n\n```json\n{"key": "value"}\n```'
        data = _extract_json_from_text(text)
        assert data == {"key": "value"}

    def test_embedded_braces(self):
        text = 'Result: {"a": 1, "b": 2} end'
        data = _extract_json_from_text(text)
        assert data == {"a": 1, "b": 2}

    def test_no_json(self):
        data = _extract_json_from_text("just plain text")
        assert data is None

    def test_empty_string(self):
        data = _extract_json_from_text("")
        assert data is None

    def test_whitespace_only(self):
        data = _extract_json_from_text("   \n  ")
        assert data is None

    def test_invalid_json(self):
        data = _extract_json_from_text("{invalid}")
        assert data is None

    def test_nested_json(self):
        text = '{"outer": {"inner": 42}}'
        data = _extract_json_from_text(text)
        assert data == {"outer": {"inner": 42}}

    def test_json_with_array(self):
        text = '{"items": [1, 2, 3]}'
        data = _extract_json_from_text(text)
        assert data == {"items": [1, 2, 3]}


# ═══════════════════════════════════════════
#  _validate_common
# ═══════════════════════════════════════════

class TestValidateCommon:
    def test_non_empty(self):
        checks = _validate_common("PLAN", "hello")
        assert len(checks) == 1
        assert checks[0].passed

    def test_empty_string(self):
        checks = _validate_common("PLAN", "")
        assert not checks[0].passed

    def test_whitespace_only(self):
        checks = _validate_common("PLAN", "   \n  ")
        assert not checks[0].passed


# ═══════════════════════════════════════════
#  _validate_json_phase — PLAN
# ═══════════════════════════════════════════

class TestValidatePlan:
    def _make_plan(self, **overrides):
        base = {
            "next_action": "READY",
            "steps": [{"command": "echo hi", "purpose": "test", "wait_seconds": 0}],
            "rollback_steps": [],
            "verify_steps": [],
        }
        base.update(overrides)
        return json.dumps(base)

    def test_valid_ready(self):
        checks = _validate_json_phase("PLAN_R1", self._make_plan())
        assert all(c.passed for c in checks)

    def test_valid_collect_more(self):
        checks = _validate_json_phase("PLAN_R1", self._make_plan(
            next_action="COLLECT_MORE",
            steps=[],
            gaps=[{"description": "need info", "command": "cat /etc/hosts"}],
        ))
        assert all(c.passed for c in checks)

    def test_valid_escalate(self):
        checks = _validate_json_phase("PLAN_R1", self._make_plan(
            next_action="ESCALATE",
            steps=[],
        ))
        # ESCALATE can have empty steps
        assert all(c.passed for c in checks)

    def test_empty_response(self):
        checks = _validate_json_phase("PLAN_R2", "")
        assert not checks[0].passed  # response_non_empty

    def test_not_json(self):
        checks = _validate_json_phase("PLAN_R1", "I need more info.\n```commands\ncat /etc/hosts\n```")
        failed = [c for c in checks if not c.passed]
        assert any(c.rule == "json_extractable" for c in failed)

    def test_ready_no_steps(self):
        checks = _validate_json_phase("PLAN_R1", self._make_plan(steps=[]))
        failed = [c for c in checks if not c.passed]
        assert any(c.rule == "ready_has_steps" for c in failed)

    def test_ready_steps_no_command(self):
        checks = _validate_json_phase("PLAN_R1", self._make_plan(steps=[{"purpose": "no cmd"}]))
        failed = [c for c in checks if not c.passed]
        assert any(c.rule == "ready_has_steps" for c in failed)

    def test_collect_more_no_gaps(self):
        checks = _validate_json_phase("PLAN_R1", self._make_plan(
            next_action="COLLECT_MORE", steps=[], gaps=[],
        ))
        failed = [c for c in checks if not c.passed]
        assert any(c.rule == "collect_more_has_gaps" for c in failed)

    def test_invalid_next_action(self):
        checks = _validate_json_phase("PLAN_R1", self._make_plan(next_action="INVALID"))
        failed = [c for c in checks if not c.passed]
        assert any(c.rule == "valid_next_action" for c in failed)

    def test_code_fence_wrapped(self):
        text = '```json\n' + self._make_plan() + '\n```'
        checks = _validate_json_phase("PLAN_R1", text)
        assert all(c.passed for c in checks)

    def test_text_before_json(self):
        text = 'Here is my plan:\n\n```json\n' + self._make_plan() + '\n```'
        checks = _validate_json_phase("PLAN_R1", text)
        assert all(c.passed for c in checks)


# ═══════════════════════════════════════════
#  _validate_json_phase — DIAGNOSE
# ═══════════════════════════════════════════

class TestValidateDiagnose:
    def _make_diagnose(self, **overrides):
        base = {
            "facts": "container restarted",
            "hypothesis": "OOM killer",
            "confidence": 75,
            "type": "runtime",
            "next_action": "FIX",
            "gaps": [],
        }
        base.update(overrides)
        return json.dumps(base)

    def test_valid(self):
        checks = _validate_json_phase("DIAGNOSE", self._make_diagnose())
        assert all(c.passed for c in checks)

    def test_empty(self):
        checks = _validate_json_phase("DIAGNOSE", "")
        assert not checks[0].passed

    def test_no_hypothesis(self):
        checks = _validate_json_phase("DIAGNOSE", self._make_diagnose(hypothesis=""))
        failed = [c for c in checks if not c.passed]
        assert any(c.rule == "has_hypothesis" for c in failed)

    def test_invalid_next_action(self):
        checks = _validate_json_phase("DIAGNOSE", self._make_diagnose(next_action="JUMP"))
        failed = [c for c in checks if not c.passed]
        assert any(c.rule == "valid_next_action" for c in failed)

    def test_invalid_type(self):
        checks = _validate_json_phase("DIAGNOSE", self._make_diagnose(type="hardware"))
        failed = [c for c in checks if not c.passed]
        assert any(c.rule == "valid_type" for c in failed)

    def test_all_valid_types(self):
        for t in VALID_DIAGNOSE_TYPES:
            checks = _validate_json_phase("DIAGNOSE", self._make_diagnose(type=t))
            type_checks = [c for c in checks if c.rule == "valid_type"]
            assert all(c.passed for c in type_checks), f"type={t} should be valid"

    def test_all_valid_next_actions(self):
        for na in VALID_NEXT_ACTIONS:
            if na == "READY":
                continue  # READY not typical for diagnose
            checks = _validate_json_phase("DIAGNOSE", self._make_diagnose(next_action=na))
            na_checks = [c for c in checks if c.rule == "valid_next_action"]
            assert all(c.passed for c in na_checks), f"next_action={na} should be valid"

    def test_not_json(self):
        checks = _validate_json_phase("DIAGNOSE", "The issue is probably OOM.")
        failed = [c for c in checks if not c.passed]
        assert any(c.rule == "json_extractable" for c in failed)


# ═══════════════════════════════════════════
#  _validate_json_phase — VERIFY
# ═══════════════════════════════════════════

class TestValidateVerify:
    def test_success(self):
        checks = _validate_json_phase("VERIFY", json.dumps({"result": "SUCCESS", "evidence": "ok"}))
        assert all(c.passed for c in checks)

    def test_failed(self):
        checks = _validate_json_phase("VERIFY", json.dumps({"result": "FAILED", "evidence": "still down"}))
        assert all(c.passed for c in checks)

    def test_uncertain(self):
        checks = _validate_json_phase("VERIFY", json.dumps({"result": "UNCERTAIN", "evidence": "maybe"}))
        assert all(c.passed for c in checks)

    def test_invalid_result(self):
        checks = _validate_json_phase("VERIFY", json.dumps({"result": "PARTIAL"}))
        failed = [c for c in checks if not c.passed]
        assert any(c.rule == "valid_result" for c in failed)

    def test_empty(self):
        checks = _validate_json_phase("VERIFY", "")
        assert not checks[0].passed

    def test_not_json(self):
        checks = _validate_json_phase("VERIFY", "RESULT: SUCCESS")
        failed = [c for c in checks if not c.passed]
        assert any(c.rule == "json_extractable" for c in failed)

    def test_evidence_mentions_failed(self):
        """Bug scenario: evidence mentions FAILED but result is SUCCESS"""
        checks = _validate_json_phase("VERIFY", json.dumps({
            "result": "SUCCESS",
            "evidence": "Previous attempt FAILED but now service is running",
        }))
        assert all(c.passed for c in checks)

    def test_all_valid_results(self):
        for r in VALID_VERIFY_RESULTS:
            checks = _validate_json_phase("VERIFY", json.dumps({"result": r}))
            result_checks = [c for c in checks if c.rule == "valid_result"]
            assert all(c.passed for c in result_checks), f"result={r} should be valid"


# ═══════════════════════════════════════════
#  _validate_assess
# ═══════════════════════════════════════════

class TestValidateAssess:
    def test_valid(self):
        checks = _validate_assess("ASSESS", "STATUS: ABNORMAL\nSEVERITY: 7\nSUMMARY: high CPU")
        assert all(c.passed for c in checks)

    def test_empty(self):
        checks = _validate_assess("ASSESS", "")
        assert not checks[0].passed

    def test_missing_status(self):
        checks = _validate_assess("ASSESS", "SEVERITY: 5\nSUMMARY: something")
        failed = [c for c in checks if not c.passed]
        assert any(c.rule == "has_status" for c in failed)

    def test_missing_severity(self):
        checks = _validate_assess("ASSESS", "STATUS: NORMAL\nSUMMARY: fine")
        failed = [c for c in checks if not c.passed]
        assert any(c.rule == "has_severity" for c in failed)

    def test_case_insensitive(self):
        checks = _validate_assess("ASSESS", "status: normal\nseverity: 3")
        assert all(c.passed for c in checks)


# ═══════════════════════════════════════════
#  LLMValidator.validate — 集成测试
# ═══════════════════════════════════════════

class TestValidatorValidate:
    def test_unvalidated_phase_passes(self, validator):
        for phase in ["OBSERVE", "REFLECT", "SUMMARIZE", "GAP_COMMANDS", ""]:
            r = validator.validate(phase, "anything", "anthropic", "model")
            assert r.passed, f"{phase} should pass without validation"

    def test_error_type_empty_response(self, validator):
        r = validator.validate("PLAN_R1", "", "anthropic", "model")
        assert not r.passed
        assert r.error_type == "empty_response"

    def test_error_type_json_parse_failed(self, validator):
        r = validator.validate("PLAN_R1", "not json at all", "anthropic", "model")
        assert not r.passed
        assert r.error_type == "json_parse_failed"

    def test_error_type_missing_required_field(self, validator):
        r = validator.validate("PLAN_R1", '{"next_action": "READY", "gaps": []}', "anthropic", "model")
        assert not r.passed
        assert r.error_type == "missing_required_field"

    def test_error_type_invalid_field_value(self, validator):
        r = validator.validate("VERIFY", '{"result": "PARTIAL"}', "anthropic", "model")
        assert not r.passed
        assert r.error_type == "invalid_field_value"

    def test_valid_response_passes(self, validator):
        r = validator.validate("PLAN_R1", json.dumps({
            "next_action": "READY",
            "steps": [{"command": "echo hi", "purpose": "test"}],
        }), "anthropic", "model")
        assert r.passed

    def test_diagnose_empty_response(self, validator):
        r = validator.validate("DIAGNOSE", "   ", "zhipu", "glm-4-plus")
        assert not r.passed
        assert r.error_type == "empty_response"

    def test_diagnose_valid(self, validator):
        r = validator.validate("DIAGNOSE", json.dumps({
            "hypothesis": "OOM", "next_action": "FIX", "type": "runtime",
        }), "zhipu", "glm-4-plus")
        assert r.passed

    def test_assess_missing_fields(self, validator):
        r = validator.validate("ASSESS", "looks fine", "anthropic", "model")
        assert not r.passed

    def test_assess_valid(self, validator):
        r = validator.validate("ASSESS", "STATUS: NORMAL\nSEVERITY: 0", "anthropic", "model")
        assert r.passed

    def test_phase_with_retry_suffix(self, validator):
        r = validator.validate("PLAN_R3_RETRY", "", "anthropic", "model")
        assert not r.passed
        assert r.error_type == "empty_response"


# ═══════════════════════════════════════════
#  LLMValidator.log_error + read_day
# ═══════════════════════════════════════════

class TestValidatorLogging:
    def test_log_and_read(self, validator, tmp_dir):
        r = validator.validate("PLAN_R1", "", "anthropic", "claude-sonnet-4-20250514")
        validator.log_error(r, provider="anthropic", model="claude-sonnet-4-20250514",
                           prompt_chars=8000, response_chars=0, duration_ms=1500,
                           incident="test-incident", request_snippet="prompt...",
                           response_snippet="")
        records = validator.read_day()
        assert len(records) == 1
        assert records[0]["phase"] == "PLAN_R1"
        assert records[0]["error_type"] == "empty_response"
        assert records[0]["provider"] == "anthropic"
        assert records[0]["model"] == "claude-sonnet-4-20250514"
        assert records[0]["duration_ms"] == 1500
        assert records[0]["prompt_chars"] == 8000
        assert records[0]["response_chars"] == 0
        assert records[0]["incident"] == "test-incident"

    def test_log_snippets_truncated(self, validator):
        long_text = "x" * 1000
        r = validator.validate("PLAN_R1", "", "anthropic", "model")
        validator.log_error(r, request_snippet=long_text, response_snippet=long_text)
        records = validator.read_day()
        assert len(records[0]["request_snippet"]) <= 500
        assert len(records[0]["response_snippet"]) <= 500

    def test_log_validation_details(self, validator):
        r = validator.validate("PLAN_R1", "not json", "anthropic", "model")
        validator.log_error(r, provider="anthropic", model="model",
                           request_snippet="p", response_snippet="r")
        records = validator.read_day()
        assert len(records[0]["validation"]) > 0
        assert all("rule" in v and "passed" in v for v in records[0]["validation"])

    def test_multiple_errors(self, validator):
        for i in range(3):
            r = validator.validate("PLAN_R1", "", "anthropic", "model")
            validator.log_error(r, provider="anthropic", model="model",
                               request_snippet="p", response_snippet="")
        records = validator.read_day()
        assert len(records) == 3

    def test_passed_not_logged(self, validator):
        r = validator.validate("OBSERVE", "anything", "anthropic", "model")
        result = validator.log_error(r)
        assert result is True  # passed=True, no-op
        records = validator.read_day()
        assert len(records) == 0

    def test_read_nonexistent_day(self, validator):
        records = validator.read_day("2099-01-01")
        assert records == []

    def test_log_with_unicode(self, validator):
        r = validator.validate("PLAN_R1", "", "anthropic", "model")
        validator.log_error(r, incident="中文事件-2026", request_snippet="包含中文的prompt",
                           response_snippet="")
        records = validator.read_day()
        assert "中文" in records[0]["incident"]
        assert "中文" in records[0]["request_snippet"]


# ═══════════════════════════════════════════
#  LLMValidator.summary
# ═══════════════════════════════════════════

class TestValidatorSummary:
    def test_empty(self, validator):
        s = validator.summary()
        assert s["total"] == 0

    def test_mixed_errors(self, validator):
        # empty response
        r1 = validator.validate("PLAN_R2", "", "anthropic", "model")
        validator.log_error(r1, provider="anthropic", model="model",
                           incident="inc1", request_snippet="p", response_snippet="")
        # json parse failed
        r2 = validator.validate("PLAN_R1", "not json", "anthropic", "model")
        validator.log_error(r2, provider="anthropic", model="model",
                           incident="inc1", request_snippet="p", response_snippet="r")
        # invalid field
        r3 = validator.validate("VERIFY", '{"result": "PARTIAL"}', "zhipu", "glm-4-plus")
        validator.log_error(r3, provider="zhipu", model="glm-4-plus",
                           incident="inc2", request_snippet="p", response_snippet="r")

        s = validator.summary()
        assert s["total"] == 3
        assert s["empty_response"] == 1
        assert s["by_type"]["json_parse_failed"] == 1
        assert s["by_type"]["invalid_field_value"] == 1
        assert s["by_provider"]["anthropic"] == 2
        assert s["by_provider"]["zhipu"] == 1

    def test_summary_nonexistent_day(self, validator):
        s = validator.summary("2099-01-01")
        assert s["total"] == 0


# ═══════════════════════════════════════════
#  边界 & 容错
# ═══════════════════════════════════════════

class TestEdgeCases:
    def test_dir_creation_failure(self):
        """初始化时目录创建失败不应抛异常"""
        v = LLMValidator("/dev/null/impossible")
        # validate 仍然可以工作（不依赖文件）
        r = v.validate("PLAN_R1", "", "anthropic", "model")
        assert not r.passed
        # log_error 失败不应抛异常
        result = v.log_error(r, request_snippet="p", response_snippet="")
        # 可能返回 False，但不抛异常

    def test_validate_with_none_response(self, validator):
        """response 为 None 时不应崩溃"""
        r = validator.validate("PLAN_R1", None, "anthropic", "model")
        assert not r.passed

    def test_validate_whitespace_response(self, validator):
        r = validator.validate("PLAN_R1", "   \n\t  ", "anthropic", "model")
        assert not r.passed
        assert r.error_type == "empty_response"

    def test_json_with_extra_text_inside(self, validator):
        """JSON 值中包含特殊字符"""
        text = json.dumps({
            "next_action": "READY",
            "steps": [{"command": "echo 'hello \"world\"'", "purpose": "test"}],
        })
        r = validator.validate("PLAN_R1", text, "anthropic", "model")
        assert r.passed

    def test_plan_collect_more_with_gaps_as_string(self, validator):
        """gaps 包含字符串项"""
        text = json.dumps({
            "next_action": "COLLECT_MORE",
            "steps": [],
            "gaps": ["need more info"],
        })
        r = validator.validate("PLAN_R1", text, "anthropic", "model")
        # gaps 有内容就算通过
        assert r.passed

    def test_diagnose_missing_hypothesis_key(self, validator):
        """JSON 没有 hypothesis 字段"""
        text = json.dumps({"next_action": "FIX", "type": "runtime"})
        r = validator.validate("DIAGNOSE", text, "anthropic", "model")
        assert not r.passed

    def test_verify_result_case_insensitive(self, validator):
        """result 值大小写"""
        text = json.dumps({"result": "success"})
        r = validator.validate("VERIFY", text, "anthropic", "model")
        assert r.passed

    def test_log_error_non_json_safe_values(self, validator):
        """log_error 传入非 JSON-safe 值不应崩溃"""
        r = validator.validate("PLAN_R1", "", "anthropic", "model")
        # incident 传入非字符串
        validator.log_error(r, incident=12345, request_snippet="p", response_snippet="")
        records = validator.read_day()
        assert len(records) == 1
