# DataPilot 只读查询报告

## Schema 摘要

```text
数据库：datapilot-36.db
表 bookings，共 16 行：row_id INTEGER, booking_id TEXT, city TEXT, check_in TEXT, nights INTEGER, guests REAL, total_price REAL, channel TEXT, canceled TEXT
```

## 哪些城市的有效订单最多？平均订单价格是多少？

```sql
SELECT
    COALESCE(NULLIF(city, ''), '<缺失>') AS city,
    COUNT(*) AS valid_orders,
    ROUND(AVG(total_price), 2) AS avg_price
FROM bookings
WHERE canceled = 'no'
  AND nights > 0
  AND guests > 0
  AND total_price > 0
GROUP BY COALESCE(NULLIF(city, ''), '<缺失>')
ORDER BY valid_orders DESC, city ASC
```

| city | valid_orders | avg_price |
|---|---|---|
| Hangzhou | 5 | 832.0 |
| Shanghai | 3 | 1600.0 |
| <缺失> | 1 | 450.0 |
| Guangzhou | 1 | 980.0 |
| Shenzhen | 1 | 5000.0 |

## 有效订单主要落在哪些价格区间？

```sql
SELECT
    CASE
        WHEN total_price < 500 THEN '0-499'
        WHEN total_price < 1000 THEN '500-999'
        WHEN total_price < 2000 THEN '1000-1999'
        ELSE '2000+'
    END AS price_band,
    COUNT(*) AS orders
FROM bookings
WHERE canceled = 'no'
  AND nights > 0
  AND guests > 0
  AND total_price > 0
GROUP BY price_band
ORDER BY MIN(total_price)
```

| price_band | orders |
|---|---|
| 0-499 | 1 |
| 500-999 | 6 |
| 1000-1999 | 3 |
| 2000+ | 1 |

## 安全检查

| 检查 | 结果 |
|---|---|
| write_rejected | 只允许以 SELECT 开头的查询 |
| multi_statement_rejected | 只允许一条 SELECT 语句，不能在中间出现分号 |
| private_table_rejected | SQL 执行失败：access to private_notes.note is prohibited |

## 固定校验

| 检查 | 是否通过 |
|---|---|
| schema_has_16_rows | True |
| city_top_is_hangzhou | True |
| city_top_has_5_orders | True |
| valid_orders_total_is_11 | True |
| price_bands_total_is_11 | True |
| write_rejected | True |
| multi_statement_rejected | True |
| private_table_rejected | True |
