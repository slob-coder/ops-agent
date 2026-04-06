# Nginx 502 Bad Gateway

## 什么时候用我
nginx error.log 出现以下任一关键字：
- `connect() failed (111: Connection refused)`
- `upstream timed out`
- `no live upstreams`

## 先查什么
按顺序执行：
1. 上游服务状态：`systemctl status <backend_service>`
2. 上游端口是否监听：`ss -tlnp | grep <upstream_port>`
3. 上游最近日志：`tail -50 <backend_log_path>`
4. 系统资源：`free -h` 和 `df -h`
5. nginx 当前连接数：`ss -s`

## 可能的原因和修复

### 原因 A：上游进程挂了
- 查看进程是否存在：`ps aux | grep <backend>`
- 查看是否被 OOM Killer 杀死：`dmesg | grep -i "oom\|killed"`
- 修复：`systemctl restart <backend_service>`
- 如果确认是 OOM，参考 playbook/oom-killer.md

### 原因 B：上游端口变化
- 检查上游配置：`cat /etc/nginx/conf.d/*.conf | grep upstream -A5`
- 核对实际监听端口：`ss -tlnp`
- 修正 nginx 配置中的 upstream 地址

### 原因 C：连接数超限
- 检查当前连接：`ss -s`
- 临时方案：增大 `worker_connections`
- 长期方案：排查连接泄漏

### 原因 D：后端响应太慢
- 检查后端日志有无慢请求
- 临时方案：增大 `proxy_read_timeout`

## 验证标准
- `curl -s -o /dev/null -w "%{http_code}" http://localhost` 连续返回 200
- nginx error.log 不再出现 502 相关错误
- 持续观察 5 分钟确认稳定

## 历史记录
（Agent 自动追加）
