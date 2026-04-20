"""Self-repair session for ops-agent.

按需的"自我修复"会话:
  1. 由人类通过 `self-fix <description>` 命令触发
  2. 暂停巡检,冻结主循环
  3. 采集自身上下文 -> LLM 自诊断
  4. 用现有 PatchLoop 在 selfdev 工作区生成补丁 + pytest 验证
  5. 人类审批后合并到 main
  6. 优雅退出,由 systemd 拉起新进程

关键设计:
  - 不参与巡检轮询,纯人触发
  - selfdev 工作区 != 运行目录,物理隔离
  - 禁改文件前置检查,保护自修复链路本身
  - pre-restart tag 用于崩了回退
"""
from __future__ import annotations

import os
import re
import json
import time
import logging
import subprocess
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from repair.self_context import SelfContext
from repair.stack_parser import StackFrame
from infra.targets import SourceRepo

logger = logging.getLogger("ops-agent.self_repair")

# ─── 禁改清单 ──────────────────────────────────────
# LLM 如果提议修改这些文件,直接 reject。保护自修复链路本身。
FORBIDDEN_FILES = frozenset({
    "self_repair.py",
    "self_context.py",
    "prompts/self_diagnose.md",
})

# ─── 只允许"加严"不允许"放宽"的文件 ────────────────
# 对这些文件的修改需要额外人类确认
SENSITIVE_FILES = frozenset({
    "safety.py",
    "limits.py",
    "trust.py",
})

# ─── 默认测试命令 ────────────────────────────────
DEFAULT_TEST_CMD = (
    "python3 test_basic.py && "
    "python3 test_sprint1.py && "
    "python3 test_sprint2.py && "
    "python3 test_sprint3.py && "
    "python3 test_sprint4.py && "
    "python3 test_sprint5.py && "
    "python3 test_sprint6.py && "
    "python3 test_blacklist.py"
)

# ─── 状态文件(告诉新进程需要做 probation 自检)────
SELFREPAIR_PENDING_FILE = "selfrepair-pending.json"


@dataclass
class SelfRepairResult:
    success: bool
    reason: str = ""
    branch: str = ""
    pre_tag: str = ""
    patch_summary: str = ""


class SelfRepairSession:
    """一次自修复会话(单次使用,不复用)"""

    def __init__(self, agent, repo_path: str, test_cmd: str = ""):
        """
        参数:
            agent: OpsAgent 实例
            repo_path: selfdev 工作区路径(必须是独立的 git checkout,
                       不等于运行进程所在目录)
            test_cmd: pytest 命令,默认跑全套现有测试
        """
        self.agent = agent
        self.repo_path = repo_path
        self.test_cmd = test_cmd or DEFAULT_TEST_CMD
        self._restart_scheduled = False

    # ═══════════════════════════════════════════
    #  主入口
    # ═══════════════════════════════════════════

    def run(self, description: str) -> SelfRepairResult:
        """执行一次完整的自修复会话。"""
        sid = f"selfrepair-{int(time.time())}"
        self._audit("selfrepair_start", sid=sid, desc=description)

        # ── 0. 前置检查:selfdev 工作区是否存在且是 git 仓库 ──
        if not self._preflight():
            return SelfRepairResult(False, "selfdev 工作区不可用")

        # ── 1. 冻结巡检 ──
        prev_paused = self.agent.paused
        self.agent.paused = True
        self.agent.chat.say(
            f"进入自修复会话 {sid}。巡检已暂停。", "info"
        )

        try:
            # ── 2. 同步 main + 打 pre-restart tag ──
            pre_tag = f"selfrepair-pre-{sid}"
            ok, msg = self._git_sync_and_tag(pre_tag)
            if not ok:
                return SelfRepairResult(False, f"git 同步失败: {msg}")

            # ── 3. pytest 基准必须绿 ──
            self.agent.chat.say("跑 pytest 基准...", "info")
            rc, out = self._run_tests()
            if rc != 0:
                self.agent.chat.escalate(
                    "自修复放弃",
                    f"当前 main 的 pytest 基准就不绿,拒绝在此之上改代码。\n"
                    f"输出尾部:\n{out[-1500:]}"
                )
                self._audit("selfrepair_abort", sid=sid, reason="baseline_red")
                return SelfRepairResult(False, "pytest 基准不绿")

            # ── 4. 采集自身上下文 ──
            self.agent.chat.say("采集自身上下文...", "info")
            ctx = SelfContext.collect(
                repo_path=self.repo_path,
                description=description,
                agent_state=self.agent.snapshot_state(),
                recent_log_tail=self._read_own_log_tail(lines=500),
                recent_incidents=self.agent._recent_incidents_summary(),
            )

            # ── 5. LLM 自诊断 ──
            self.agent.chat.say("LLM 自诊断中...", "info")
            diagnosis = self._self_diagnose(ctx)
            if not diagnosis:
                return SelfRepairResult(False, "自诊断解析失败")

            # 置信度闸
            confidence = int(diagnosis.get("confidence", 0))
            if confidence < 60:
                self.agent.chat.escalate(
                    "自诊断置信度不足",
                    f"假设: {diagnosis.get('hypothesis', '?')}\n"
                    f"置信度: {confidence}\n"
                    f"缺失信息: {diagnosis.get('need_more_info', '(未说明)')}\n"
                    f"请补充后重新 self-fix。"
                )
                self._audit("selfrepair_low_conf", sid=sid, confidence=confidence)
                return SelfRepairResult(False, f"置信度 {confidence} < 60")

            # ── 6. 禁改文件前置检查 ──
            suspected = diagnosis.get("suspected_files", [])
            forbidden_hit = self._check_forbidden(suspected)
            if forbidden_hit:
                self.agent.chat.escalate(
                    "自修复被禁改名单拦截",
                    f"LLM 想修改受保护文件: {forbidden_hit}\n"
                    f"这些文件是自修复链路本身,不允许被自动修改。"
                )
                self._audit("selfrepair_forbidden", sid=sid, hit=forbidden_hit)
                return SelfRepairResult(False, f"命中禁改清单: {forbidden_hit}")

            # ── 7. 敏感文件额外确认 ──
            sensitive_hit = self._check_sensitive(suspected)
            if sensitive_hit:
                approved = self.agent.chat.request_approval(
                    f"⚠️ LLM 想修改安全基座文件 {sensitive_hit}\n"
                    f"假设: {diagnosis.get('hypothesis')}\n"
                    f"这类文件只允许加严规则。是否允许继续?"
                )
                if not approved:
                    self._audit("selfrepair_sensitive_reject", sid=sid)
                    return SelfRepairResult(False, "敏感文件修改被人类拒绝")

            self.agent.chat.say(
                f"诊断: {diagnosis.get('hypothesis')}\n"
                f"可疑文件: {suspected}\n"
                f"置信度: {confidence}",
                "info"
            )

            # ── 8. 源码定位(复用 SourceLocator)──
            locations = self._locate_sources(suspected, ctx)
            if not locations:
                return SelfRepairResult(False, "源码定位失败")

            # ── 9. 跑 PatchLoop(复用现有三次重试循环)──
            self.agent.chat.say("进入补丁生成循环...", "info")
            repo_handle = self._build_repo_handle()
            verified = self.agent.patch_loop.run(
                diagnosis=diagnosis,
                locations=locations,
                repo=repo_handle,
                incident_id=sid,
            )
            if not verified:
                self.agent.chat.escalate(
                    "自修复:3 次补丁尝试均失败",
                    f"已保留失败分支供人类检查,selfdev 路径: {self.repo_path}"
                )
                self._audit("selfrepair_patch_failed", sid=sid)
                return SelfRepairResult(False, "PatchLoop 失败")

            branch = verified.result.branch_name
            patch_desc = verified.patch.description or ""

            # ── 10. 再次跑一遍完整测试套(PatchLoop 可能只跑了 repo.test_cmd)──
            self.agent.chat.say(f"补丁在分支 {branch},跑最终完整测试...", "info")
            rc, out = self._run_tests(branch=branch)
            if rc != 0:
                self.agent.chat.escalate(
                    "自修复:最终测试套未通过",
                    f"分支 {branch} 跑 {self.test_cmd} 失败。\n"
                    f"输出尾部:\n{out[-1500:]}"
                )
                self._audit("selfrepair_final_test_failed", sid=sid, branch=branch)
                return SelfRepairResult(False, "最终测试套失败")

            # ── 11. 人类最终审批(强制 ASK,绕过 trust 策略)──
            approved = self.agent.chat.request_approval(
                f"✅ 自修复补丁通过全部测试\n"
                f"分支: {branch}\n"
                f"pre-restart tag: {pre_tag}\n"
                f"说明: {patch_desc}\n\n"
                f"是否合并到 main 并重启生效?"
            )
            if not approved:
                self.agent.chat.say(
                    f"已保留分支 {branch},你可以手动 review 后再处理。",
                    "info"
                )
                self._audit("selfrepair_human_reject", sid=sid, branch=branch)
                return SelfRepairResult(
                    False, "人类未批准合并", branch=branch, pre_tag=pre_tag
                )

            # ── 12. 合并 + 写 pending 标记 + 计划重启 ──
            ok, msg = self._merge_branch(branch)
            if not ok:
                self.agent.chat.escalate("自修复:合并失败", msg)
                return SelfRepairResult(False, f"merge 失败: {msg}", branch=branch)

            self._write_pending_marker(sid, pre_tag, branch, patch_desc)
            self._schedule_restart(sid)

            self._audit("selfrepair_success", sid=sid, branch=branch, pre_tag=pre_tag)
            return SelfRepairResult(
                True, "修复已合并,3 秒后重启",
                branch=branch, pre_tag=pre_tag, patch_summary=patch_desc
            )

        except Exception as e:
            logger.exception("SelfRepairSession 崩溃")
            self.agent.chat.escalate("自修复会话崩溃", str(e))
            self._audit("selfrepair_crashed", sid=sid, error=str(e))
            return SelfRepairResult(False, f"异常: {e}")

        finally:
            if not self._restart_scheduled:
                self.agent.paused = prev_paused
                self.agent.chat.say(
                    "退出自修复会话,巡检已恢复。", "info"
                )

    # ═══════════════════════════════════════════
    #  各阶段实现
    # ═══════════════════════════════════════════

    def _preflight(self) -> bool:
        """检查 selfdev 工作区是否可用"""
        if not self.repo_path:
            self.agent.chat.say(
                "自修复未配置 selfdev 工作区(OPS_AGENT_SELFDEV_PATH)。", "warning"
            )
            return False
        if not os.path.isdir(self.repo_path):
            self.agent.chat.say(
                f"selfdev 路径不存在: {self.repo_path}", "warning"
            )
            return False
        if not os.path.isdir(os.path.join(self.repo_path, ".git")):
            self.agent.chat.say(
                f"selfdev 路径不是 git 仓库: {self.repo_path}", "warning"
            )
            return False
        # 不能与运行进程的 CWD 相同 —— 否则改的就是自己
        running_dir = os.path.realpath(
            os.path.dirname(os.path.abspath(__file__))
        )
        selfdev_dir = os.path.realpath(self.repo_path)
        if running_dir == selfdev_dir:
            self.agent.chat.say(
                "selfdev 路径不能与运行目录相同,拒绝自修复。", "critical"
            )
            return False
        return True

    def _git_sync_and_tag(self, pre_tag: str) -> tuple[bool, str]:
        """fetch + checkout main + pull + 打保护 tag"""
        try:
            self._git("fetch", "origin")
            self._git("checkout", "main")
            self._git("pull", "--ff-only", "origin", "main")
            # 清理可能残留的旧同名 tag(本地)
            try:
                self._git("tag", "-d", pre_tag)
            except Exception:
                pass
            self._git("tag", pre_tag)
            return True, ""
        except Exception as e:
            return False, str(e)

    def _run_tests(self, branch: str = "") -> tuple[int, str]:
        """在 selfdev 工作区执行测试命令。"""
        if branch:
            try:
                self._git("checkout", branch)
            except Exception as e:
                return 1, f"checkout {branch} 失败: {e}"
        try:
            result = subprocess.run(
                self.test_cmd,
                shell=True,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=600,
            )
            out = (result.stdout or "") + "\n" + (result.stderr or "")
            return result.returncode, out
        except subprocess.TimeoutExpired:
            return 124, "测试超时(600s)"
        except Exception as e:
            return 1, f"测试执行异常: {e}"
        finally:
            if branch:
                try:
                    self._git("checkout", "main")
                except Exception:
                    pass

    def _self_diagnose(self, ctx: SelfContext) -> Optional[dict]:
        """用 self_diagnose prompt 调一次 LLM,解析 JSON 返回。"""
        prompt_path = Path(self.repo_path) / "prompts" / "self_diagnose.md"
        if not prompt_path.exists():
            # fallback: 用运行目录的 prompt
            prompt_path = Path(
                os.path.dirname(os.path.abspath(__file__))
            ) / "prompts" / "self_diagnose.md"
        if not prompt_path.exists():
            logger.error("self_diagnose.md not found")
            return None

        template = prompt_path.read_text(encoding="utf-8")
        filled = template.replace(
            "{user_description}", ctx.user_description
        ).replace(
            "{self_context}", ctx.to_prompt()
        )

        try:
            response = self.agent.llm.ask(filled)
        except Exception as e:
            logger.error(f"LLM 自诊断调用失败: {e}")
            return None

        return _parse_json_block(response)

    def _check_forbidden(self, suspected_files: list) -> str:
        """返回第一个命中禁改清单的文件(空串表示未命中)"""
        for entry in suspected_files:
            path = entry.split(":", 1)[0].strip()
            # 规范化相对路径
            norm = path.replace("\\", "/").lstrip("./")
            if norm in FORBIDDEN_FILES:
                return norm
        return ""

    def _check_sensitive(self, suspected_files: list) -> str:
        for entry in suspected_files:
            path = entry.split(":", 1)[0].strip()
            norm = path.replace("\\", "/").lstrip("./")
            if norm in SENSITIVE_FILES:
                return norm
        return ""

    def _locate_sources(self, suspected_files: list, ctx: SelfContext) -> list:
        """把 LLM 的 `file:line` 提示转成 SourceLocation 列表。

        做法:合成 StackFrame -> 交给 SourceLocator。这样我们复用现有的
        函数抽取、上下文裁剪逻辑。
        """
        frames = []
        for entry in suspected_files:
            m = re.match(r"^([^:]+):(\d+)", entry.strip())
            if m:
                path, lineno = m.group(1), int(m.group(2))
            else:
                path = entry.strip()
                lineno = 1
            frames.append(StackFrame(
                file=path, line=lineno,
                function="", module="", language="python",
            ))
        if not frames:
            return []

        # 构造一个指向 selfdev 的 SourceRepo 给 SourceLocator
        repo = SourceRepo(
            name="ops-agent-self",
            path=self.repo_path,
            language="python",
        )
        try:
            from repair.source_locator import SourceLocator
            locator = SourceLocator(repos=[repo])
            result = locator.locate(frames)
            return result.locations if hasattr(result, "locations") else []
        except Exception as e:
            logger.exception("SourceLocator 失败")
            self.agent.chat.say(f"源码定位失败: {e}", "warning")
            return []

    def _build_repo_handle(self) -> SourceRepo:
        """给 PatchApplier / PatchLoop 用的 repo 对象"""
        return SourceRepo(
            name="ops-agent-self",
            path=self.repo_path,
            language="python",
            build_cmd="python3 -m py_compile main.py",
            test_cmd=self.test_cmd,
            base_branch="main",
            git_host="",  # 不自动推 PR,只在本地做分支
        )

    def _merge_branch(self, branch: str) -> tuple[bool, str]:
        try:
            self._git("checkout", "main")
            self._git("merge", "--no-ff", "-m",
                      f"selfrepair: merge {branch}", branch)
            return True, ""
        except Exception as e:
            return False, str(e)

    def _write_pending_marker(self, sid: str, pre_tag: str,
                              branch: str, patch_desc: str):
        """写下 pending 标记,让新进程启动时做 probation 自检"""
        notebook_dir = Path(self.agent.notebook.path)
        marker = notebook_dir / SELFREPAIR_PENDING_FILE
        marker.write_text(json.dumps({
            "sid": sid,
            "pre_tag": pre_tag,
            "branch": branch,
            "patch_desc": patch_desc,
            "merged_at": time.time(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _schedule_restart(self, sid: str):
        """优雅退出,由 systemd 的 Restart=always 拉起新进程"""
        self.agent.chat.say(
            f"补丁已合并,{sid} 将在 3 秒后重启生效。", "success"
        )
        self._restart_scheduled = True

        def _delayed_exit():
            time.sleep(3)
            logger.info(f"selfrepair {sid}: exiting for systemd restart")
            # 先让主循环跳出
            self.agent._running = False
            # 再强制退出
            os._exit(0)

        threading.Thread(target=_delayed_exit, daemon=True).start()

    # ═══════════════════════════════════════════
    #  工具方法
    # ═══════════════════════════════════════════

    def _git(self, *args) -> str:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed: {result.stderr.strip()}"
            )
        return result.stdout

    def _read_own_log_tail(self, lines: int = 500) -> str:
        """读自身日志尾部。优先 systemd journal,其次查找常见日志路径。"""
        # 尝试 journalctl
        try:
            result = subprocess.run(
                ["journalctl", "-u", "ops-agent.service",
                 "-n", str(lines), "--no-pager"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except Exception:
            pass
        # 退路:看 notebook 下的日志
        log_candidates = [
            "/var/log/ops-agent.log",
            os.path.join(self.agent.notebook.path, "agent.log"),
        ]
        for p in log_candidates:
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        all_lines = f.readlines()
                    return "".join(all_lines[-lines:])
                except Exception:
                    continue
        return "(无法读取自身日志)"

    def _audit(self, event: str, **kwargs):
        """写审计日志"""
        try:
            if getattr(self.agent, "audit", None):
                self.agent.audit.log(event=event, **kwargs)
        except Exception:
            logger.debug("audit 写入失败", exc_info=True)


# ═══════════════════════════════════════════════════════
#  probation: 新进程启动时调用,确认自修复没把自己改死
# ═══════════════════════════════════════════════════════

def run_probation_if_pending(agent, repo_path: str,
                             test_cmd: str = "") -> Optional[str]:
    """在 OpsAgent 启动流程早期调用。

    逻辑:
      1. 检查 notebook/selfrepair-pending.json 是否存在
      2. 不存在 -> 正常启动,返回 None
      3. 存在 -> 跑 pytest + 简单自检
         - 通过: 删除 marker,返回 "probation_passed"
         - 失败: git reset --hard <pre_tag>,退出,返回 "probation_failed"
                (systemd 会拉起旧版本)
      4. 连续两次失败 -> 写 EMERGENCY_STOP_SELF_MODIFY 永久关闭 self-fix
    """
    notebook_dir = Path(agent.notebook.path)
    marker = notebook_dir / SELFREPAIR_PENDING_FILE
    if not marker.exists():
        return None

    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"pending marker 损坏: {e},直接删除")
        marker.unlink(missing_ok=True)
        return None

    sid = data.get("sid", "?")
    pre_tag = data.get("pre_tag", "")
    agent.chat.say(
        f"检测到上次自修复 {sid} 刚合并,进入 probation 自检...",
        "warning"
    )

    # 跑测试
    cmd = test_cmd or DEFAULT_TEST_CMD
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=repo_path,
            capture_output=True, text=True, timeout=600,
        )
        rc = result.returncode
        out = (result.stdout or "") + "\n" + (result.stderr or "")
    except Exception as e:
        rc = 1
        out = f"probation 测试异常: {e}"

    if rc == 0:
        agent.chat.say(
            f"✅ probation 通过,自修复 {sid} 生效。", "success"
        )
        marker.unlink(missing_ok=True)
        _log_audit(agent, "probation_passed", sid=sid)
        return "probation_passed"

    # 失败:尝试回退到 pre_tag
    agent.chat.escalate(
        f"🚨 自修复 probation 失败",
        f"sid={sid}\n测试输出尾部:\n{out[-1500:]}\n"
        f"正在回退到 {pre_tag} 并再次重启..."
    )
    _log_audit(agent, "probation_failed", sid=sid, pre_tag=pre_tag)

    # 计数连续失败次数
    fail_count_file = notebook_dir / "selfrepair-fail-count"
    try:
        fail_count = int(fail_count_file.read_text().strip()) if fail_count_file.exists() else 0
    except Exception:
        fail_count = 0
    fail_count += 1
    fail_count_file.write_text(str(fail_count))

    if fail_count >= 2:
        # 永久关闭自修复
        (notebook_dir / "EMERGENCY_STOP_SELF_MODIFY").write_text(
            f"连续 {fail_count} 次 probation 失败,永久关闭自修复。\n"
            f"人类确认后删除此文件可重新开启。\n"
        )
        agent.chat.escalate(
            "🚨 自修复已被冻结",
            f"连续 {fail_count} 次 probation 失败。\n"
            f"已写 EMERGENCY_STOP_SELF_MODIFY 标志,self-fix 命令已禁用。\n"
            f"需要人类检查并删除该标志才能恢复。"
        )

    # 回退 + 退出
    if pre_tag:
        try:
            subprocess.run(
                ["git", "reset", "--hard", pre_tag],
                cwd=repo_path, capture_output=True, timeout=30,
            )
            subprocess.run(
                ["git", "tag", "-d", pre_tag],
                cwd=repo_path, capture_output=True, timeout=10,
            )
        except Exception as e:
            logger.error(f"回退失败: {e}")

    # 删 marker 防止下次又进 probation(下一次启动是回退后的旧版本)
    marker.unlink(missing_ok=True)
    # 退出让 systemd 拉起新进程
    threading.Thread(
        target=lambda: (time.sleep(2), os._exit(1)),
        daemon=True,
    ).start()
    return "probation_failed"


def _log_audit(agent, event: str, **kwargs):
    try:
        if getattr(agent, "audit", None):
            agent.audit.log(event=event, **kwargs)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════
#  JSON 解析工具
# ═══════════════════════════════════════════════════════

def _parse_json_block(text: str) -> Optional[dict]:
    """从 LLM 响应里提取 JSON 对象,兼容 ```json fenced block。"""
    if not text:
        return None
    # 优先找 ```json 块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 退路:找第一个完整的 {...} 块
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None
