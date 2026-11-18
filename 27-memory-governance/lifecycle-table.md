# TripPilot 记忆生命周期表

| 操作 | 用户 | 偏好键 | 结果 | 原因或边界 |
|---|---|---|---|---|
| 写入 | alice | interests | TTL 至 2026-11-18 | 用户明确偏好 |
| 越权读取 | alice | * | 拒绝 | Bob 不能读取 Alice 的记忆 |
| 纠错 | alice | pace | 极紧凑 → 轻松 | 先核对旧值，再写新版本 |
| 过期清理 | alice | interests | 已移除 | as_of 2026-11-19 |
| 删除 | alice | travel_budget | 已删除 | 用户要求删除预算记忆 |
