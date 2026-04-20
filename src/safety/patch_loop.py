"""
patch_loop — 补丁生成 / 应用 / 验证的重试循环

最多 MAX_ATTEMPTS 次:
  generate → apply → build → test
失败时把上次的 diff 和错误信息塞回 prompt 让 LLM 重试。

返回:
  VerifiedPatch — 全部通过(留有 git 分支供 Sprint 4 push)
  None          — 三次都失败,Sprint 3 中应该升级人类
"""

from __future__ import annotations

import logging
from src.context_limits import get_context_limits as _ctx
from dataclasses import dataclass

from src.safety.patch_generator import PatchGenerator, Patch
from src.safety.patch_applier import PatchApplier, VerificationResult

logger = logging.getLogger("ops-agent.patch_loop")


@dataclass
class VerifiedPatch:
    patch: Patch
    result: VerificationResult
    attempts: int                # 总共尝试了几次(1..MAX_ATTEMPTS)


class PatchLoop:

    def __init__(self, generator: PatchGenerator, applier: PatchApplier,
                 logger_fn=None, max_attempts: int = 3):
        self.generator = generator
        self.applier = applier
        self.max_attempts = max_attempts
        self._log = logger_fn or (lambda msg: logger.info(msg))

    def run(self, diagnosis: dict, locations: list,
            repo, incident_id: str = "") -> VerifiedPatch | None:
        """完整重试循环。"""
        if not repo:
            self._log("PatchLoop: no repo, abort")
            return None
        if not locations:
            self._log("PatchLoop: no source locations, abort")
            return None

        retry_context = ""
        last_result: VerificationResult | None = None
        last_patch: Patch | None = None

        for attempt in range(1, self.max_attempts + 1):
            self._log(f"补丁尝试 #{attempt}/{self.max_attempts}")

            patch = self.generator.generate(
                diagnosis, locations, repo, retry_context=retry_context,
            )
            if not patch:
                self._log(f"  ↳ 生成失败 (LLM 返回不可解析或为空)")
                retry_context = self._format_retry_for_parse_failure()
                continue

            result = self.applier.apply_and_verify(patch, repo, incident_id=incident_id)
            last_result = result
            last_patch = patch

            if result.success:
                self._log(f"  ↳ ✓ 通过 ({result.stage}) 分支={result.branch_name}")
                return VerifiedPatch(patch=patch, result=result, attempts=attempt)

            self._log(f"  ↳ ✗ 失败于 {result.stage}: {result.error_message}")
            retry_context = self._build_retry_context(patch, result)

        # 三次都失败
        if last_result:
            self._log(
                f"PatchLoop 三次尝试全部失败,最后阶段={last_result.stage}"
            )
        return None

    # ────────────────────────────────────────
    # 重试上下文构造
    # ────────────────────────────────────────

    @staticmethod
    def _format_retry_for_parse_failure() -> str:
        return (
            "上一次响应无法解析为有效的 unified diff。\n"
            "请严格按照模板输出三段:## 修改说明 / ## 修改的文件 / ## Diff,\n"
            "Diff 必须在 ```diff ... ``` 代码块中,包含 --- a/ +++ b/ 和 @@ 行。"
        )

    @staticmethod
    def _build_retry_context(patch: Patch, result: VerificationResult) -> str:
        """把上次失败转成给 LLM 的反馈(已截断,不会爆 prompt)"""
        parts = [
            "上次尝试失败了。请基于错误信息生成一个新的、不同的补丁。",
            f"失败阶段: {result.stage}",
            f"错误: {result.error_message}",
            "",
            "上次的 diff:",
            "```diff",
            patch.diff[:_ctx().self_repair_output_tail_chars],
            "```",
        ]
        if result.build_output:
            parts += ["", "构建输出(末尾):", "```", result.build_output[-_ctx().self_repair_output_tail_chars:], "```"]
        if result.test_output:
            parts += ["", "测试输出(末尾):", "```", result.test_output[-_ctx().self_repair_output_tail_chars:], "```"]
        if result.apply_output and not (result.build_output or result.test_output):
            parts += ["", "apply 输出:", "```", result.apply_output[-_ctx().self_repair_output_tail_chars:], "```"]
        return "\n".join(parts)
