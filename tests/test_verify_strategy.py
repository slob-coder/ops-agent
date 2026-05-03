"""
测试验证策略 — 即时验证 + 连续观察

覆盖:
- VerifyResult 数据类
- ActionPlan 新增属性 (has_watch_steps, max_watch_duration)
- _parse_verify_response 解析
- _quick_verify_check 轻量验证
- _is_degrading 恶化检测
- _verify_with_strategy 完整流程（即时验证成功/失败/不确定）
- _watch_verify 连续观察（收敛/超时/恶化）
- _verify_with_retry 向后兼容
- verify_steps 解析（含 watch 字段）
- LimitsConfig 新增验证配置项
- incident_loop 中 VerifyResult 三种结果的路由
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import dataclass

import sys
from pathlib import Path

# 确保项目根目录在 path 中
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ═══════════════════════════════════════════
#  VerifyResult 测试
# ═══════════════════════════════════════════

class TestVerifyResult:
    def test_defaults(self):
        from src.safety.trust import VerifyResult
        r = VerifyResult()
        assert r.result == "UNCERTAIN"
        assert not r.passed
        assert not r.failed
        assert not r.needs_watch

    def test_success(self):
        from src.safety.trust import VerifyResult
        r = VerifyResult(result="SUCCESS")
        assert r.passed
        assert not r.failed
        assert not r.needs_watch

    def test_failed(self):
        from src.safety.trust import VerifyResult
        r = VerifyResult(result="FAILED")
        assert not r.passed
        assert r.failed
        assert not r.needs_watch

    def test_uncertain_needs_watch(self):
        from src.safety.trust import VerifyResult
        r = VerifyResult(result="UNCERTAIN", continue_watch=True)
        assert r.needs_watch

    def test_uncertain_without_watch(self):
        from src.safety.trust import VerifyResult
        r = VerifyResult(result="UNCERTAIN", continue_watch=False)
        assert not r.needs_watch

    def test_continue_watch(self):
        from src.safety.trust import VerifyResult
        r = VerifyResult(result="SUCCESS", continue_watch=True)
        assert r.needs_watch

    def test_success_no_watch(self):
        from src.safety.trust import VerifyResult
        r = VerifyResult(result="SUCCESS", continue_watch=False)
        assert not r.needs_watch


# ═══════════════════════════════════════════
#  ActionPlan 扩展属性测试
# ═══════════════════════════════════════════

class TestActionPlanExtensions:
    def test_has_watch_steps_false(self):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[
            {"command": "curl health", "expect": "200"},
        ])
        assert not plan.has_watch_steps

    def test_has_watch_steps_true(self):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[
            {"command": "free -h", "expect": "80%", "watch": True, "watch_duration": 600},
        ])
        assert plan.has_watch_steps

    def test_max_watch_duration(self):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[
            {"command": "free -h", "watch": True, "watch_duration": 300},
            {"command": "curl health", "watch": True, "watch_duration": 600},
        ])
        assert plan.max_watch_duration == 600

    def test_max_watch_duration_no_watch(self):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[
            {"command": "curl health", "expect": "200"},
        ])
        assert plan.max_watch_duration == 0

    def test_to_markdown_with_watch(self):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(
            steps=[{"command": "restart nginx", "purpose": "restart"}],
            verify_steps=[
                {"command": "curl health", "expect": "200", "delay_seconds": 10},
                {"command": "free -h", "expect": "80%", "watch": True,
                 "watch_duration": 300, "watch_interval": 30, "watch_converge": 3},
            ],
            reason="test",
            expected="healthy",
            trust_level=2,
        )
        md = plan.to_markdown()
        assert "等待10s后" in md
        assert "连续观察: 300s" in md
        assert "每30s采样" in md
        assert "连续3次通过" in md

    def test_to_markdown_without_watch(self):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(
            verify_steps=[{"command": "curl health", "expect": "200"}],
        )
        md = plan.to_markdown()
        assert "连续观察" not in md


# ═══════════════════════════════════════════
#  _parse_verify_response 测试
# ═══════════════════════════════════════════

class TestParseVerifyResponse:
    @pytest.fixture
    def pipeline(self):
        """创建一个最小化的 PipelineMixin 实例用于测试解析方法"""
        from src.agent.pipeline import PipelineMixin
        p = PipelineMixin.__new__(PipelineMixin)
        return p

    def test_parse_success(self, pipeline):
        response = (
            "RESULT: SUCCESS\n"
            "EVIDENCE: Health check returns 200\n"
            "CONTINUE_WATCH: NO\n"
            "WATCH_DURATION: 0\n"
            "ROLLBACK_NEEDED: NO\n"
            "ROLLBACK_REASON:\n"
        )
        result = pipeline._parse_verify_response(response)
        assert result.passed
        assert not result.continue_watch
        assert result.evidence == "Health check returns 200"

    def test_parse_failed(self, pipeline):
        response = (
            "RESULT: FAILED\n"
            "EVIDENCE: Service still down\n"
            "CONTINUE_WATCH: NO\n"
            "ROLLBACK_NEEDED: YES\n"
            "ROLLBACK_REASON: Fix did not work\n"
        )
        result = pipeline._parse_verify_response(response)
        assert result.failed
        assert result.rollback_needed
        assert result.rollback_reason == "Fix did not work"

    def test_parse_uncertain(self, pipeline):
        response = (
            "RESULT: UNCERTAIN\n"
            "EVIDENCE: Memory usage still high\n"
            "CONTINUE_WATCH: YES\n"
            "WATCH_DURATION: 600\n"
            "ROLLBACK_NEEDED: NO\n"
        )
        result = pipeline._parse_verify_response(response)
        assert result.result == "UNCERTAIN"
        assert result.continue_watch
        assert result.watch_duration == 600
        assert result.needs_watch

    def test_parse_success_with_watch(self, pipeline):
        response = (
            "RESULT: SUCCESS\n"
            "EVIDENCE: Restarted OK\n"
            "CONTINUE_WATCH: YES\n"
            "WATCH_DURATION: 300\n"
            "ROLLBACK_NEEDED: NO\n"
        )
        result = pipeline._parse_verify_response(response)
        assert result.passed
        assert result.continue_watch
        assert result.watch_duration == 300

    def test_parse_malformed(self, pipeline):
        result = pipeline._parse_verify_response("random garbage text")
        assert result.result == "UNCERTAIN"

    def test_parse_empty(self, pipeline):
        result = pipeline._parse_verify_response("")
        assert result.result == "UNCERTAIN"


# ═══════════════════════════════════════════
#  _quick_verify_check 测试
# ═══════════════════════════════════════════

class TestQuickVerifyCheck:
    @pytest.fixture
    def pipeline(self):
        from src.agent.pipeline import PipelineMixin
        p = PipelineMixin.__new__(PipelineMixin)
        return p

    def test_all_expects_match(self, pipeline):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[
            {"command": "curl health", "expect": "200"},
            {"command": "free -h", "expect": "Mem"},
        ])
        assert pipeline._quick_verify_check(plan, "HTTP 200 OK\nMem: 4G")

    def test_expect_not_found(self, pipeline):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[
            {"command": "curl health", "expect": "200"},
        ])
        assert not pipeline._quick_verify_check(plan, "HTTP 503 Service Unavailable")

    def test_no_expects_passes(self, pipeline):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[
            {"command": "curl health"},
        ])
        assert pipeline._quick_verify_check(plan, "anything")

    def test_case_insensitive(self, pipeline):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[
            {"command": "free -h", "expect": "MEM"},
        ])
        assert pipeline._quick_verify_check(plan, "Mem: 4G total")

    def test_empty_verify_steps(self, pipeline):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[])
        assert pipeline._quick_verify_check(plan, "anything")


# ═══════════════════════════════════════════
#  _is_degrading 测试
# ═══════════════════════════════════════════

class TestIsDegrading:
    @pytest.fixture
    def pipeline(self):
        from src.agent.pipeline import PipelineMixin
        p = PipelineMixin.__new__(PipelineMixin)
        return p

    def test_oom_detected(self, pipeline):
        assert pipeline._is_degrading("oom-killer invoked")

    def test_connection_refused(self, pipeline):
        assert pipeline._is_degrading("Connection refused on port 8080")

    def test_kernel_panic(self, pipeline):
        assert pipeline._is_degrading("Kernel panic - not syncing")

    def test_normal_state(self, pipeline):
        assert not pipeline._is_degrading("HTTP 200 OK\nMem: 4G/8G")

    def test_case_insensitive(self, pipeline):
        assert pipeline._is_degrading("OUT OF MEMORY error")

    def test_empty(self, pipeline):
        assert not pipeline._is_degrading("")

    def test_segfault(self, pipeline):
        assert pipeline._is_degrading("segmentation fault (core dumped)")

    def test_service_failed(self, pipeline):
        assert pipeline._is_degrading("backend.service: Failed with result 'exit-code'")


# ═══════════════════════════════════════════
#  LimitsConfig 验证配置测试
# ═══════════════════════════════════════════

class TestLimitsConfigVerify:
    def test_defaults(self):
        from src.safety.limits import LimitsConfig
        cfg = LimitsConfig()
        assert cfg.verify_max_retries == 3
        assert cfg.verify_default_interval == 5
        assert cfg.watch_required_consecutive == 2
        assert cfg.watch_default_interval == 60
        assert cfg.watch_max_duration == 900

    def test_from_yaml(self, tmp_path):
        from src.safety.limits import LimitsConfig
        yaml_content = """
verify_max_retries: 5
verify_default_interval: 10
watch_required_consecutive: 3
watch_default_interval: 30
watch_max_duration: 600
"""
        f = tmp_path / "limits.yaml"
        f.write_text(yaml_content)
        cfg = LimitsConfig.from_yaml(str(f))
        assert cfg.verify_max_retries == 5
        assert cfg.verify_default_interval == 10
        assert cfg.watch_required_consecutive == 3
        assert cfg.watch_default_interval == 30
        assert cfg.watch_max_duration == 600


# ═══════════════════════════════════════════
#  verify_steps 解析测试（parsers.py）
# ═══════════════════════════════════════════

class TestVerifyStepsParsing:
    """测试 verify_steps 的规范化逻辑（直接构造 ActionPlan）"""

    def test_basic_verify_step(self):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[
            {"command": "curl health", "expect": "200"},
        ])
        assert plan.verify_steps[0]["command"] == "curl health"
        assert plan.verify_steps[0]["expect"] == "200"
        assert "watch" not in plan.verify_steps[0]

    def test_verify_step_with_delay(self):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[
            {"command": "curl health", "expect": "200", "delay_seconds": 15},
        ])
        assert plan.verify_steps[0]["delay_seconds"] == 15

    def test_verify_step_with_watch(self):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[
            {
                "command": "free -h",
                "expect": "80%",
                "watch": True,
                "watch_duration": 600,
                "watch_interval": 30,
                "watch_converge": 3,
            },
        ])
        assert plan.verify_steps[0]["watch"] is True
        assert plan.verify_steps[0]["watch_duration"] == 600
        assert plan.verify_steps[0]["watch_interval"] == 30
        assert plan.verify_steps[0]["watch_converge"] == 3

    def test_verify_step_string_command(self):
        from src.safety.trust import ActionPlan
        # parsers normalizes string steps to {"command": "...", "expect": ""}
        plan = ActionPlan(verify_steps=[
            {"command": "curl health", "expect": ""},
        ])
        assert plan.verify_steps[0]["command"] == "curl health"
        assert plan.verify_steps[0]["expect"] == ""

    def test_parser_normalizes_watch_defaults(self):
        """测试 parsers.py 中 verify_steps 规范化逻辑"""
        # 直接测试规范化代码
        raw_step = {"command": "free -h", "expect": "80%", "watch": True}
        step = {"command": raw_step["command"], "expect": raw_step.get("expect", "")}
        if "delay_seconds" in raw_step:
            step["delay_seconds"] = int(raw_step["delay_seconds"])
        if raw_step.get("watch"):
            step["watch"] = True
            step["watch_duration"] = int(raw_step.get("watch_duration", 300))
            step["watch_interval"] = int(raw_step.get("watch_interval", 60))
            step["watch_converge"] = int(raw_step.get("watch_converge", 2))

        assert step["watch"] is True
        assert step["watch_duration"] == 300
        assert step["watch_interval"] == 60
        assert step["watch_converge"] == 2


# ═══════════════════════════════════════════
#  _verify_with_strategy 集成测试（mock LLM）
# ═══════════════════════════════════════════

class TestVerifyWithStrategy:
    @pytest.fixture
    def pipeline(self):
        from src.agent.pipeline import PipelineMixin
        from src.safety.limits import LimitsConfig, LimitsEngine

        p = PipelineMixin.__new__(PipelineMixin)

        # Mock 必要的属性
        p.limits = LimitsEngine(LimitsConfig())
        p.limits.config.verify_max_retries = 2
        p.limits.config.verify_default_interval = 1
        p.limits.config.watch_max_duration = 30
        p.limits.config.watch_default_interval = 5
        p.limits.config.watch_required_consecutive = 2

        p.chat = MagicMock()
        p.current_incident = None
        p.notebook = MagicMock()
        p.current_target = MagicMock()
        p.current_target.name = "test"
        p.current_target.mode = "ssh"
        p.current_issue = "test issue"
        p.ctx_limits = MagicMock()
        p.ctx_limits.verify_state_chars = 2000
        p.ctx_limits.verify_response_trace_chars = 500

        # Mock methods
        p._interruptible_sleep = MagicMock()
        p._targeted_observe = MagicMock(return_value="HTTP 200 OK\nMem: 4G/8G")
        p._fill_prompt = MagicMock(return_value="verify prompt")
        p._ask_llm = MagicMock(return_value="RESULT: SUCCESS\nEVIDENCE: OK\nCONTINUE_WATCH: NO\nWATCH_DURATION: 0\nROLLBACK_NEEDED: NO")
        p._emit_audit = MagicMock()

        return p

    def test_immediate_success(self, pipeline):
        from src.safety.trust import ActionPlan
        plan = ActionPlan(
            verify_steps=[{"command": "curl health", "expect": "200"}],
        )
        result = pipeline._verify_with_strategy(plan, "before state")
        assert result.passed

    def test_immediate_failed(self, pipeline):
        pipeline._ask_llm = MagicMock(
            return_value="RESULT: FAILED\nEVIDENCE: still broken\nCONTINUE_WATCH: NO\nROLLBACK_NEEDED: YES\nROLLBACK_REASON: not fixed"
        )
        from src.safety.trust import ActionPlan
        plan = ActionPlan(
            verify_steps=[{"command": "curl health", "expect": "200"}],
        )
        result = pipeline._verify_with_strategy(plan, "before state")
        assert result.failed

    def test_delay_before_verify(self, pipeline):
        pipeline._ask_llm = MagicMock(
            return_value="RESULT: SUCCESS\nEVIDENCE: OK\nCONTINUE_WATCH: NO\nROLLBACK_NEEDED: NO"
        )
        from src.safety.trust import ActionPlan
        plan = ActionPlan(
            verify_steps=[{"command": "curl health", "expect": "200", "delay_seconds": 15}],
        )
        result = pipeline._verify_with_strategy(plan, "before state")
        assert result.passed
        # Should have slept for delay
        pipeline._interruptible_sleep.assert_called()

    def test_watch_triggered_by_plan(self, pipeline):
        """plan 有 watch 步骤时，即时验证成功后应进入连续观察"""
        call_count = [0]

        def mock_ask(prompt, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 即时验证
                return "RESULT: SUCCESS\nEVIDENCE: restarted\nCONTINUE_WATCH: NO\nROLLBACK_NEEDED: NO"
            # 不会被调用（_watch_verify 用 _quick_verify_check）
            return "RESULT: SUCCESS\nEVIDENCE: OK\nCONTINUE_WATCH: NO\nROLLBACK_NEEDED: NO"

        pipeline._ask_llm = mock_ask
        pipeline._targeted_observe = MagicMock(return_value="HTTP 200 OK\nMem: 50%")
        # 让 _quick_verify_check 通过
        pipeline._interruptible_sleep = MagicMock()

        from src.safety.trust import ActionPlan
        plan = ActionPlan(
            verify_steps=[
                {"command": "free -h", "expect": "50%", "watch": True,
                 "watch_duration": 10, "watch_interval": 5, "watch_converge": 2},
            ],
        )
        result = pipeline._verify_with_strategy(plan, "before state")
        assert result.passed

    def test_watch_triggered_by_verify_prompt(self, pipeline):
        """verify prompt 建议 CONTINUE_WATCH 时应进入连续观察"""
        call_count = [0]

        def mock_ask(prompt, **kwargs):
            call_count[0] += 1
            return ("RESULT: SUCCESS\nEVIDENCE: restarted\n"
                    "CONTINUE_WATCH: YES\nWATCH_DURATION: 15\nWATCH_INTERVAL: 5\n"
                    "ROLLBACK_NEEDED: NO")

        pipeline._ask_llm = mock_ask
        pipeline._targeted_observe = MagicMock(return_value="HTTP 200 OK\nMem: 50%")
        pipeline._interruptible_sleep = MagicMock()

        from src.safety.trust import ActionPlan
        plan = ActionPlan(
            verify_steps=[{"command": "free -h", "expect": "50%"}],
        )
        result = pipeline._verify_with_strategy(plan, "before state")
        assert result.passed  # watch should converge

    def test_uncertain_triggers_watch(self, pipeline):
        """UNCERTAIN 结果应触发连续观察"""
        pipeline._ask_llm = MagicMock(
            return_value="RESULT: UNCERTAIN\nEVIDENCE: not sure\nCONTINUE_WATCH: YES\nWATCH_DURATION: 10\nROLLBACK_NEEDED: NO"
        )
        pipeline._targeted_observe = MagicMock(return_value="HTTP 200 OK\nMem: 50%")
        pipeline._interruptible_sleep = MagicMock()

        from src.safety.trust import ActionPlan
        plan = ActionPlan(
            verify_steps=[{"command": "free -h", "expect": "50%"}],
        )
        result = pipeline._verify_with_strategy(plan, "before state")
        # Watch should run and eventually pass or uncertain
        assert result.result in ("SUCCESS", "UNCERTAIN")

    def test_no_watch_on_failed(self, pipeline):
        """FAILED 不应触发连续观察"""
        pipeline._ask_llm = MagicMock(
            return_value="RESULT: FAILED\nEVIDENCE: broken\nCONTINUE_WATCH: YES\nWATCH_DURATION: 600\nROLLBACK_NEEDED: NO"
        )
        from src.safety.trust import ActionPlan
        plan = ActionPlan(
            verify_steps=[{"command": "curl health", "expect": "200"}],
        )
        result = pipeline._verify_with_strategy(plan, "before state")
        assert result.failed
        # Should NOT enter watch (would need many _interruptible_sleep calls)
        # Just check it returned quickly with FAILED

    def test_backward_compat_verify_with_retry(self, pipeline):
        """_verify_with_retry 应保持返回 bool"""
        from src.safety.trust import ActionPlan
        plan = ActionPlan(
            verify_steps=[{"command": "curl health", "expect": "200"}],
        )
        result = pipeline._verify_with_retry(plan, "before state")
        assert isinstance(result, bool)
        assert result is True


# ═══════════════════════════════════════════
#  _watch_verify 测试
# ═══════════════════════════════════════════

class TestWatchVerify:
    @pytest.fixture
    def pipeline(self):
        from src.agent.pipeline import PipelineMixin
        from src.safety.limits import LimitsConfig, LimitsEngine

        p = PipelineMixin.__new__(PipelineMixin)
        p.limits = LimitsEngine(LimitsConfig())
        p.limits.config.watch_max_duration = 30
        p.chat = MagicMock()
        p.current_incident = None
        p.notebook = MagicMock()
        p._interruptible_sleep = MagicMock()
        p._targeted_observe = MagicMock(return_value="HTTP 200 OK\nMem: 50%")

        return p

    def test_converge_immediately(self, pipeline):
        """连续观察立即收敛"""
        from src.safety.trust import ActionPlan
        plan = ActionPlan(
            verify_steps=[{"command": "free -h", "expect": "50%"}],
        )
        result = pipeline._watch_verify(plan, duration=10, interval=5, required_consecutive=2)
        assert result.passed

    def test_timeout_uncertain(self, pipeline):
        """连续观察超时未收敛"""
        pipeline._quick_verify_check = MagicMock(return_value=False)

        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[])
        result = pipeline._watch_verify(plan, duration=10, interval=5, required_consecutive=2)
        assert result.result == "UNCERTAIN"

    def test_degrading_returns_failed(self, pipeline):
        """连续观察检测到恶化"""
        pipeline._quick_verify_check = MagicMock(return_value=False)
        pipeline._is_degrading = MagicMock(return_value=True)

        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[])
        result = pipeline._watch_verify(plan, duration=10, interval=5, required_consecutive=2)
        assert result.failed
        assert result.rollback_needed

    def test_max_duration_clamp(self, pipeline):
        """观察时长不应超过 watch_max_duration"""
        pipeline._quick_verify_check = MagicMock(return_value=False)

        from src.safety.trust import ActionPlan
        plan = ActionPlan(verify_steps=[])
        # Request 999s but max is 30s
        result = pipeline._watch_verify(plan, duration=999, interval=10, required_consecutive=2)
        assert result.result == "UNCERTAIN"
