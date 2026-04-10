#!/bin/sh
# OpsAgent external watchdog. Run from cron every minute.
# 连续 3 次健康检查失败就 restart 服务。
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:9876/healthz}"
STATE_FILE="${STATE_FILE:-/var/lib/ops-agent/watchdog.state}"
SERVICE="${SERVICE:-ops-agent.service}"

if curl -sf -o /dev/null --max-time 5 "$HEALTH_URL"; then
    echo 0 > "$STATE_FILE"
    exit 0
fi

count=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$STATE_FILE"

if [ "$count" -ge 3 ]; then
    logger -t opsagent-watchdog "health check failed $count times, restarting"
    systemctl restart "$SERVICE"
    echo 0 > "$STATE_FILE"
fi
