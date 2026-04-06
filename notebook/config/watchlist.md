# 观察源

> 入职后 Agent 会根据探索结果自动更新此文件

## 巡检间隔
- patrol 模式：每 60 秒巡检一次
- investigate 模式：每 5 秒
- incident 模式：每 1 秒

## 默认观察源
以下是 Agent 在任何系统上都会检查的基础项：

### 系统级
- `systemctl --failed --no-pager` — 检查有没有失败的服务
- `dmesg --time-format=iso | tail -20` — 内核日志
- `journalctl -p err --since="5 min ago" --no-pager -n 30` — 最近 5 分钟的错误级别日志
- `df -h | awk '$5+0 > 85'` — 磁盘使用率超过 85% 的分区
- `free -h` — 内存使用情况

### 自定义观察源
（Agent 入职探索后自动填充，人类也可以手动添加）

示例格式：
- 每 60 秒：`tail -n 20 /var/log/nginx/error.log` — 关注 502/503/upstream 相关错误
- 每 300 秒：`curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/health` — 业务健康检查
