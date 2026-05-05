基于以下昨日审计日志和统计数据,为运维负责人生成一份简洁的中文日报。

## 日期
{date}

## 事件统计
{event_counts}

## limits 配额状态
{limits_status}

## 审计事件样本(最多 30 条)
{event_samples}

## 输出要求
用 markdown 列表,3-6 条要点,涵盖:
1. 昨天处理了多少 Incident,自动解决/升级各多少
2. 关键动作摘要(重启 / 补丁 / PR / revert)
3. 异常或需要关注的趋势
4. Token 成本

风格:简洁,像运维工程师写的日报,不要客套。
