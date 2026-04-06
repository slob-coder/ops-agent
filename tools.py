"""
ToolBox — Agent 的双手
对 shell 命令的薄封装，分信任等级。支持本地执行和远程 SSH 执行。
"""

import subprocess
import shlex
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger("ops-agent.tools")


@dataclass
class CommandResult:
    """命令执行结果"""
    command: str
    stdout: str
    stderr: str
    returncode: int
    duration: float

    @property
    def success(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """合并输出，优先 stdout"""
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout.strip())
        if self.stderr.strip():
            parts.append(f"[stderr] {self.stderr.strip()}")
        return "\n".join(parts) if parts else "(no output)"

    def __str__(self):
        status = "✓" if self.success else "✗"
        return f"[{status}] $ {self.command}\n{self.output}"


@dataclass
class TargetConfig:
    """目标系统连接配置"""
    mode: str = "local"          # local | ssh
    host: str = ""               # SSH: user@host
    port: int = 22               # SSH port
    key_file: str = ""           # SSH key path
    kubectl_context: str = ""    # K8s context (optional)

    @classmethod
    def local(cls):
        return cls(mode="local")

    @classmethod
    def ssh(cls, host: str, port: int = 22, key_file: str = ""):
        return cls(mode="ssh", host=host, port=port, key_file=key_file)


class ToolBox:
    """Agent 的工具箱"""

    # 命令黑名单 — 硬禁止
    BLACKLIST = [
        "rm -rf /", "mkfs", "dd if=", "> /dev/sd",
        "DROP DATABASE", "DROP TABLE", "FORMAT",
        ":(){ :|:& };:", "shutdown", "reboot",
    ]

    def __init__(self, target: TargetConfig):
        self.target = target

    # ═══════════════════════════════════════════
    #  底层执行
    # ═══════════════════════════════════════════

    def _check_blacklist(self, cmd: str):
        for pattern in self.BLACKLIST:
            if pattern.lower() in cmd.lower():
                raise PermissionError(f"Command blocked (blacklist): {cmd}")

    def run(self, cmd: str, timeout: int = 30) -> CommandResult:
        """在目标系统上执行命令"""
        self._check_blacklist(cmd)
        start = time.time()

        try:
            if self.target.mode == "ssh":
                ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no",
                           "-o", "ConnectTimeout=10"]
                if self.target.port != 22:
                    ssh_cmd += ["-p", str(self.target.port)]
                if self.target.key_file:
                    ssh_cmd += ["-i", self.target.key_file]
                ssh_cmd += [self.target.host, cmd]
                result = subprocess.run(
                    ssh_cmd, capture_output=True, text=True, timeout=timeout
                )
            else:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=timeout
                )

            elapsed = time.time() - start
            cr = CommandResult(cmd, result.stdout, result.stderr, result.returncode, elapsed)
            logger.debug(str(cr))
            return cr

        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            return CommandResult(cmd, "", f"Timeout after {timeout}s", -1, elapsed)
        except Exception as e:
            elapsed = time.time() - start
            return CommandResult(cmd, "", str(e), -1, elapsed)

    def run_local(self, cmd: str, timeout: int = 60) -> CommandResult:
        """在运维工作站本地执行"""
        self._check_blacklist(cmd)
        start = time.time()
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=timeout
            )
            elapsed = time.time() - start
            return CommandResult(cmd, result.stdout, result.stderr, result.returncode, elapsed)
        except subprocess.TimeoutExpired:
            return CommandResult(cmd, "", f"Timeout after {timeout}s", -1, time.time() - start)
        except Exception as e:
            return CommandResult(cmd, "", str(e), -1, time.time() - start)

    # ═══════════════════════════════════════════
    #  L0：只读观察
    # ═══════════════════════════════════════════

    def tail(self, path: str, lines: int = 100) -> CommandResult:
        return self.run(f"tail -n {lines} {shlex.quote(path)}")

    def grep(self, path: str, pattern: str, lines: int = 50) -> CommandResult:
        return self.run(
            f"grep -i {shlex.quote(pattern)} {shlex.quote(path)} | tail -n {lines}"
        )

    def dmesg(self, lines: int = 50) -> CommandResult:
        return self.run(f"dmesg --time-format=iso | tail -n {lines}")

    def journalctl(self, unit: str = "", since: str = "1h ago", lines: int = 100) -> CommandResult:
        cmd = f"journalctl --no-pager -n {lines} --since='{since}'"
        if unit:
            cmd += f" -u {unit}"
        return self.run(cmd)

    def ps_aux(self) -> CommandResult:
        return self.run("ps aux --sort=-%mem | head -30")

    def systemctl_status(self, unit: str = "") -> CommandResult:
        if unit:
            return self.run(f"systemctl status {shlex.quote(unit)} --no-pager")
        return self.run("systemctl --failed --no-pager")

    def systemctl_list(self) -> CommandResult:
        return self.run("systemctl list-units --type=service --state=running --no-pager")

    def kubectl_logs(self, pod: str, namespace: str = "default", lines: int = 100) -> CommandResult:
        return self.run(f"kubectl logs {shlex.quote(pod)} -n {namespace} --tail={lines}")

    def kubectl_get_pods(self, namespace: str = "--all-namespaces") -> CommandResult:
        ns = f"-n {namespace}" if namespace != "--all-namespaces" else "--all-namespaces"
        return self.run(f"kubectl get pods {ns} --no-headers")

    def netstat(self) -> CommandResult:
        return self.run("ss -tlnp")

    def disk(self) -> CommandResult:
        return self.run("df -h")

    def free(self) -> CommandResult:
        return self.run("free -h")

    def uptime(self) -> CommandResult:
        return self.run("uptime")

    def explore(self) -> dict[str, CommandResult]:
        """入职探索：扫描目标系统"""
        commands = {
            "hostname": "hostname",
            "os_info": "cat /etc/os-release 2>/dev/null || ver 2>/dev/null || echo 'unknown'",
            "uname": "uname -a",
            "uptime": "uptime",
            "memory": "free -h",
            "disk": "df -h",
            "services": "systemctl list-units --type=service --state=running --no-pager 2>/dev/null | head -40",
            "failed_services": "systemctl --failed --no-pager 2>/dev/null",
            "log_dirs": "ls -la /var/log/ 2>/dev/null | head -30",
            "processes": "ps aux --sort=-%mem | head -20",
            "ports": "ss -tlnp 2>/dev/null | head -20",
            "docker": "docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null || echo 'Docker not available'",
            "k8s": "kubectl get pods --all-namespaces --no-headers 2>/dev/null | head -20 || echo 'K8s not available'",
            "crontab": "crontab -l 2>/dev/null || echo 'No crontab'",
        }
        results = {}
        for name, cmd in commands.items():
            logger.info(f"Exploring: {name}")
            results[name] = self.run(cmd, timeout=15)
        return results

    # ═══════════════════════════════════════════
    #  L2：服务级操作
    # ═══════════════════════════════════════════

    def restart_service(self, unit: str) -> CommandResult:
        logger.warning(f"L2 action: restart {unit}")
        return self.run(f"systemctl restart {shlex.quote(unit)}")

    def reload_service(self, unit: str) -> CommandResult:
        logger.warning(f"L2 action: reload {unit}")
        return self.run(f"systemctl reload {shlex.quote(unit)}")

    def backup_file(self, path: str) -> CommandResult:
        """备份文件（改配置前必须调用）"""
        ts = int(time.time())
        return self.run(f"cp {shlex.quote(path)} {shlex.quote(path)}.bak.{ts}")

    def edit_file(self, path: str, old_text: str, new_text: str) -> CommandResult:
        """sed 替换文件内容"""
        logger.warning(f"L2 action: edit {path}")
        # 先备份
        self.backup_file(path)
        safe_old = old_text.replace("/", "\\/").replace("&", "\\&")
        safe_new = new_text.replace("/", "\\/").replace("&", "\\&")
        return self.run(f"sed -i 's/{safe_old}/{safe_new}/g' {shlex.quote(path)}")

    def write_remote_file(self, path: str, content: str) -> CommandResult:
        """写入远程文件"""
        logger.warning(f"L2 action: write {path}")
        self.backup_file(path)
        escaped = content.replace("'", "'\\''")
        return self.run(f"cat > {shlex.quote(path)} << 'AGENTEOF'\n{content}\nAGENTEOF")

    # ═══════════════════════════════════════════
    #  L3：代码级修改（在本地工作站执行）
    # ═══════════════════════════════════════════

    def git_clone(self, repo: str, dest: str) -> CommandResult:
        logger.warning(f"L3 action: clone {repo}")
        return self.run_local(f"git clone {shlex.quote(repo)} {shlex.quote(dest)}", timeout=120)

    def apply_patch(self, repo_path: str, patch_content: str) -> CommandResult:
        """在本地仓库应用补丁"""
        logger.warning(f"L3 action: apply patch to {repo_path}")
        patch_file = f"/tmp/ops_agent_patch_{int(time.time())}.patch"
        with open(patch_file, "w") as f:
            f.write(patch_content)
        return self.run_local(f"cd {shlex.quote(repo_path)} && git apply {patch_file}")

    def create_pr(self, repo_path: str, branch: str, title: str, body: str) -> list[CommandResult]:
        """创建 Git PR（需要 gh CLI）"""
        logger.warning(f"L3 action: create PR '{title}'")
        results = []
        commands = [
            f"cd {shlex.quote(repo_path)} && git checkout -b {shlex.quote(branch)}",
            f"cd {shlex.quote(repo_path)} && git add -A && git commit -m {shlex.quote(title)}",
            f"cd {shlex.quote(repo_path)} && git push origin {shlex.quote(branch)}",
            f"cd {shlex.quote(repo_path)} && gh pr create --title {shlex.quote(title)} --body {shlex.quote(body)}",
        ]
        for cmd in commands:
            r = self.run_local(cmd, timeout=60)
            results.append(r)
            if not r.success:
                break
        return results

    # ═══════════════════════════════════════════
    #  通用执行（供 Agent 灵活调用）
    # ═══════════════════════════════════════════

    def execute(self, cmd: str, trust_level: int = 0) -> CommandResult:
        """通用执行接口，Agent 通过 LLM 生成的命令调用"""
        if trust_level >= 2:
            logger.warning(f"L{trust_level} action: {cmd}")
        return self.run(cmd)
