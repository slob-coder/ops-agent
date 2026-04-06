# 磁盘空间不足

## 什么时候用我
- `df -h` 显示某分区使用率 > 90%
- 日志出现 `No space left on device`
- 应用写入失败、数据库报错

## 先查什么
1. 哪个分区满了：`df -h`
2. 大文件在哪：`du -sh /* 2>/dev/null | sort -rh | head -10`
3. 日志文件大小：`du -sh /var/log/* | sort -rh | head -10`
4. 已删除但未释放的文件：`lsof +D /var/log 2>/dev/null | grep deleted | head -10`
5. Docker 占用（如果有）：`docker system df 2>/dev/null`

## 修复方案

### 安全清理（L2）
按优先级执行：
1. 清理旧日志：`journalctl --vacuum-size=500M`
2. 清理 apt 缓存：`apt-get clean`
3. 压缩大日志：`gzip /var/log/*.log.1 2>/dev/null`
4. Docker 清理：`docker system prune -f 2>/dev/null`

### 定位大文件（L0）
- 找大文件：`find / -type f -size +100M 2>/dev/null | head -20`
- 找老文件：`find /var/log -type f -mtime +30 -name "*.log*" | head -20`

### 需要人类决策的
- 删除业务数据 → 升级给人类
- 扩容磁盘 → 升级给人类
- 修改日志 rotation 策略 → 可以建议但需人类确认

## 验证标准
- 目标分区使用率降到 85% 以下
- 应用写入恢复正常
- 持续观察确认不再快速增长

## 预防建议
- 配置 logrotate
- 设置磁盘告警阈值
- 定期清理临时文件

## 历史记录
（Agent 自动追加）
