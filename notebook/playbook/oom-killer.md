# OOM Killer 杀掉进程

## 什么时候用我
出现以下任一信号：
- dmesg 中出现 `Out of memory: Killed process`
- journalctl 中出现 `oom-kill` 或 `oom_reaper`
- 某个服务突然变成 inactive (dead) 且无人手动停止
- Java 应用日志出现 `java.lang.OutOfMemoryError`

## 先查什么
1. 确认 OOM 事件：`dmesg | grep -i "oom\|killed" | tail -20`
2. 被杀的进程：`dmesg | grep "Killed process" | tail -5`
3. 当前内存情况：`free -h`
4. 内存占用排名：`ps aux --sort=-%mem | head -10`
5. 如果是 Java：`jstat -gcutil <pid>` 或查看启动参数中的 -Xmx

## 修复方案

### 紧急恢复
1. 重启被杀的服务：`systemctl restart <service>`
2. 确认服务恢复正常

### 根因分析
- 看是内存泄漏还是配置不足
- 内存泄漏迹象：进程内存持续增长，无回落
- 配置不足迹象：突发流量或数据量增大

### 临时缓解
- Java 应用：增大 -Xmx（修改启动脚本或 env 文件）
- 系统级：检查是否有 swap，考虑增加 swap
- 调整 OOM score：`echo -1000 > /proc/<pid>/oom_score_adj`（保护关键进程）

### 长期方案
- 排查内存泄漏
- 增加服务器内存
- 拆分服务到多台机器
- 添加内存监控告警

## 验证标准
- 被杀的服务已恢复运行
- `free -h` 显示内存使用合理（<80%）
- 持续观察 10 分钟无再次 OOM

## 历史记录
（Agent 自动追加）
