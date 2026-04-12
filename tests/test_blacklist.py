#!/usr/bin/env python3
"""
黑名单修复的回归测试
1. 原始报错的 docker ps --format 命令必须能通过
2. 真正的危险命令必须仍被拦截
"""

import sys
from tools import ToolBox, TargetConfig

tb = ToolBox(TargetConfig.local())

passed = 0
failed = 0

def should_pass(cmd, label):
    """这条命令应该通过黑名单"""
    global passed, failed
    try:
        tb._check_blacklist(cmd)
        print(f"  ✓ 放行: {label}")
        passed += 1
    except PermissionError as e:
        print(f"  ✗ 误杀: {label}  →  {e}")
        failed += 1

def should_block(cmd, label):
    """这条命令应该被黑名单拦截"""
    global passed, failed
    try:
        tb._check_blacklist(cmd)
        print(f"  ✗ 漏网: {label}")
        failed += 1
    except PermissionError:
        print(f"  ✓ 拦截: {label}")
        passed += 1


print("\n=== 应该通过的合法命令 ===")

# 用户报错的原始命令
should_pass(
    "docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null || echo 'Docker not available'",
    "docker ps --format"
)

# 其他常见 --format 用法
should_pass("docker ps --format json", "docker ps --format json")
should_pass("kubectl get pods -o json", "kubectl get pods")
should_pass("git log --format='%h %s'", "git log --format")
should_pass("systemctl list-units --format=json", "systemctl --format")

# 常见运维命令
should_pass("tail -n 100 /var/log/nginx/error.log", "tail nginx log")
should_pass("journalctl -u nginx --since '1h ago'", "journalctl")
should_pass("systemctl restart nginx", "restart nginx")
should_pass("systemctl status mysql", "systemctl status")
should_pass("ps aux --sort=-%mem | head", "ps aux")
should_pass("df -h", "df -h")
should_pass("free -h", "free -h")

# 删除特定目录（合法运维操作）
should_pass("rm -rf /tmp/cache", "rm -rf /tmp/cache")
should_pass("rm -rf /var/log/old-logs", "rm -rf /var/log/old-logs")
should_pass("rm -f /tmp/foo.txt", "rm -f /tmp/foo.txt")

# 含 drop 但非 SQL 的
should_pass("iptables -A INPUT -j DROP", "iptables DROP")
should_pass("grep -i 'drop' file.log", "grep drop")

# chmod/chown 特定目录
should_pass("chmod -R 755 /opt/app", "chmod -R /opt/app")
should_pass("chown -R nginx:nginx /var/www", "chown -R /var/www")

# 含 format 关键字但非破坏性的
should_pass("echo 'format disk'", "echo 含 format 字样")
should_pass("man format", "man format")


print("\n=== 应该被拦截的危险命令 ===")

# 毁灭性删除
should_block("rm -rf /", "rm -rf /")
should_block("rm -rf /*", "rm -rf /*")
should_block("rm -fr /", "rm -fr /（参数顺序变化）")

# 格式化
should_block("mkfs.ext4 /dev/sda1", "mkfs.ext4")
should_block("mkfs -t ext4 /dev/sda", "mkfs")
should_block("mke2fs /dev/sda", "mke2fs")

# dd 写磁盘
should_block("dd if=/dev/zero of=/dev/sda bs=1M", "dd to /dev/sda")
should_block("dd if=backup.img of=/dev/nvme0n1", "dd to /dev/nvme")

# 写磁盘设备
should_block("echo 'x' > /dev/sda", "> /dev/sda")

# SQL 破坏
should_block("mysql -e 'DROP DATABASE prod'", "DROP DATABASE")
should_block("psql -c 'DROP TABLE users'", "DROP TABLE")
should_block("mysql -e 'drop database test'", "drop database (小写)")

# Fork 炸弹
should_block(":(){ :|:& };:", "fork bomb")

# 重启/关机
should_block("shutdown -h now", "shutdown -h now")
should_block("shutdown -r now", "shutdown -r now")
should_block("reboot", "reboot")
should_block("poweroff", "poweroff")

# Windows format
should_block("format c:", "format c:")
should_block("; format d:", "; format d:")

# chmod/chown 根目录
should_block("chmod -R 777 /", "chmod -R 777 /")
should_block("chown -R user /", "chown -R /")


print(f"\n{'=' * 40}")
print(f"  通过: {passed}    失败: {failed}")
print(f"{'=' * 40}")
sys.exit(0 if failed == 0 else 1)
