"""
ToolBox — Agent 的双手
对 shell 命令的薄封装，分信任等级。支持本地执行和远程 SSH 执行。
"""

import os
import re
import subprocess
import shlex
import time
import logging
from dataclasses import dataclass, field

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
    password: str = ""           # SSH password (需要 sshpass)
    kubectl_context: str = ""    # K8s context (optional)

    @classmethod
    def local(cls):
        return cls(mode="local")

    @classmethod
    def ssh(cls, host: str, port: int = 22, key_file: str = "", password: str = ""):
        return cls(mode="ssh", host=host, port=port, key_file=key_file, password=password)


class ToolBox:
    """Agent 的工具箱"""

    # 命令黑名单 — 硬禁止
    # 使用正则精确匹配，避免误伤 --format 等合法参数
    BLACKLIST_PATTERNS = [
        # 毁灭性删除：rm -rf / 或 rm -rf /*（但允许 rm -rf /tmp/xxx）
        (r"\brm\s+-[rf]*r[rf]*\s+/(\s|$|\*)", "rm -rf /"),
        (r"\brm\s+-[rf]*f[rf]*\s+/(\s|$|\*)", "rm -rf /"),
        # 格式化文件系统
        (r"\bmkfs(\.|\s)", "mkfs"),
        (r"\bmke2fs\b", "mke2fs"),
        # dd 写入磁盘设备
        (r"\bdd\s+.*of=/dev/(sd|nvme|hd|vd|xvd)", "dd to disk device"),
        # 重定向写入到磁盘设备
        (r">\s*/dev/(sd|nvme|hd|vd|xvd)", "write to disk device"),
        # SQL 破坏性操作（作为独立关键字，避免误伤文本中的 drop）
        (r"\bDROP\s+(DATABASE|TABLE|SCHEMA)\b", "DROP DATABASE/TABLE"),
        (r"\bTRUNCATE\s+TABLE\b", "TRUNCATE TABLE"),
        # Fork 炸弹
        (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
        # 重启/关机
        (r"\bshutdown\s+(-[hrP]|now)", "shutdown"),
        (r"\breboot\b", "reboot"),
        (r"\bhalt\b(?!\w)", "halt"),
        (r"\bpoweroff\b", "poweroff"),
        # Windows 格式化（作为独立命令，不匹配 --format）
        (r"(?:^|\s|;|&&|\|\|)format\s+[a-z]:", "format drive"),
        # chmod/chown 递归改根目录
        (r"\bchmod\s+-R\s+\S+\s+/(\s|$)", "chmod -R /"),
        (r"\bchown\s+-R\s+\S+\s+/(\s|$)", "chown -R /"),
    ]

    def __init__(self, target: "TargetConfig"):
        self.target = target
        # 预编译正则，提升性能
        self._compiled_blacklist = [
            (re.compile(pattern, re.IGNORECASE), label)
            for pattern, label in self.BLACKLIST_PATTERNS
        ]
        # SSH 连接复用：每个 ToolBox 实例使用一个 ControlMaster socket
        # 这样所有 ssh 命令复用同一个底层 TCP 连接，避免每次重新握手
        self._ssh_control_path = ""
        if target.mode == "ssh":
            import tempfile
            import atexit
            # 用 PID 隔离，多 Agent 实例不冲突
            self._ssh_control_path = os.path.join(
                tempfile.gettempdir(),
                f"ops_agent_ssh_{os.getpid()}_%h_%p_%r"
            )
            # 进程退出时清理 master 连接
            atexit.register(self._cleanup_ssh_master)

    def _cleanup_ssh_master(self):
        """退出时关闭 SSH master 连接"""
        if not self._ssh_control_path:
            return
        try:
            subprocess.run(
                ["ssh", "-O", "exit",
                 "-o", f"ControlPath={self._ssh_control_path}",
                 self.target.host],
                capture_output=True, timeout=5
            )
            logger.info("SSH master connection closed")
        except Exception:
            pass

    def _build_ssh_options(self) -> list[str]:
        """构造 ssh 公共选项，包含 ControlMaster 和保活配置"""
        opts = [
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=10",
            # ── 连接复用：关键 ──
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._ssh_control_path}",
            "-o", "ControlPersist=600",   # master 连接闲置 10 分钟后自动关闭
            # ── 保活：客户端每 30 秒发一次心跳，3 次失败才断 ──
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            # ── TCP 层保活（防 NAT 会话超时）──
            "-o", "TCPKeepAlive=yes",
        ]
        if self.target.port != 22:
            opts += ["-p", str(self.target.port)]
        if self.target.key_file:
            opts += ["-i", self.target.key_file]
        if self.target.password:
            opts += ["-o", "PubkeyAuthentication=no",
                     "-o", "PreferredAuthentications=password"]
        return opts

    # ═══════════════════════════════════════════
    #  底层执行
    # ═══════════════════════════════════════════

    def _check_blacklist(self, cmd: str):
        for pattern, label in self._compiled_blacklist:
            if pattern.search(cmd):
                raise PermissionError(f"Command blocked (matches '{label}'): {cmd}")

    def run(self, cmd: str, timeout: int = 30, _retry: int = 0) -> CommandResult:
        """在目标系统上执行命令

        - 自动通过 SSH ControlMaster 复用底层连接
        - 第一次连接失败时自动重试一次（清理可能损坏的 master socket）
        """
        self._check_blacklist(cmd)
        start = time.time()

        try:
            if self.target.mode == "ssh":
                ssh_opts = self._build_ssh_options()
                ssh_base = ["ssh"] + ssh_opts

                if self.target.password:
                    # 密码模式：sshpass 包装
                    # 注意：ControlMaster 模式下，只有第一次（建立 master）真正用到密码，
                    # 后续命令复用 master socket，根本不会再次认证
                    ssh_cmd = ["sshpass", "-e"] + ssh_base + [self.target.host, cmd]
                    env = {
                        "SSHPASS": self.target.password,
                        "PATH": "/usr/bin:/bin:/usr/local/bin",
                        "HOME": os.environ.get("HOME", "/tmp"),
                    }
                    result = subprocess.run(
                        ssh_cmd, capture_output=True, text=True,
                        timeout=timeout, env=env
                    )
                else:
                    ssh_cmd = ssh_base + [self.target.host, cmd]
                    result = subprocess.run(
                        ssh_cmd, capture_output=True, text=True, timeout=timeout
                    )
            else:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=timeout
                )

            elapsed = time.time() - start
            cr = CommandResult(cmd, result.stdout, result.stderr, result.returncode, elapsed)

            # ── 失败自动重试一次：清掉可能损坏的 master 连接后重连 ──
            if (self.target.mode == "ssh" and not cr.success and _retry == 0):
                if self._is_connection_error(cr.stderr):
                    logger.warning(f"SSH connection error, resetting master and retrying: {cr.stderr.strip()[:200]}")
                    self._cleanup_ssh_master()
                    return self.run(cmd, timeout=timeout, _retry=1)

            logger.debug(str(cr))
            return cr

        except FileNotFoundError as e:
            elapsed = time.time() - start
            if "sshpass" in str(e):
                msg = ("使用密码认证需要安装 sshpass。"
                       "macOS: brew install hudochenkov/sshpass/sshpass；"
                       "Ubuntu/Debian: apt install sshpass；"
                       "或改用 --key 密钥认证。")
            else:
                msg = str(e)
            return CommandResult(cmd, "", msg, -1, elapsed)
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            # 超时也尝试重置一次 master（可能 master 卡死）
            if self.target.mode == "ssh" and _retry == 0:
                logger.warning(f"SSH command timeout, resetting master and retrying")
                self._cleanup_ssh_master()
                return self.run(cmd, timeout=timeout, _retry=1)
            return CommandResult(cmd, "", f"Timeout after {timeout}s", -1, elapsed)
        except Exception as e:
            elapsed = time.time() - start
            return CommandResult(cmd, "", str(e), -1, elapsed)

    @staticmethod
    def _is_connection_error(stderr: str) -> bool:
        """判断 stderr 是不是连接层错误（值得重试）"""
        if not stderr:
            return False
        markers = [
            "Connection closed",
            "Connection reset",
            "Connection refused",
            "Connection timed out",
            "Broken pipe",
            "ssh_exchange_identification",
            "control socket",
            "mux_client",
            "channel open failed",
            "Network is unreachable",
            "Host is unreachable",
            "Permission denied",  # master 损坏时偶现
            "kex_exchange_identification",
        ]
        return any(m.lower() in stderr.lower() for m in markers)

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
