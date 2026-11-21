# 旅行订单数据体检报告

## 概览

- 数据行数：16
- 字段数：8
- 缺失单元格：3
- 重复订单号：B003
- 非法数值行：4
- 价格离群行：1

## 字段类型与缺失值

| 字段 | 原始类型 | 解析后类型 | 缺失数 | 缺失率 | 唯一值 |
|---|---|---|---:|---:|---:|
| booking_id | string | str | 0 | 0.00% | 15 |
| city | string | str | 1 | 6.25% | 7 |
| check_in | string | str | 0 | 0.00% | 8 |
| nights | string | int64 | 0 | 0.00% | 4 |
| guests | string | float64 | 0 | 0.00% | 5 |
| total_price | string | float64 | 1 | 6.25% | 14 |
| channel | string | str | 1 | 6.25% | 4 |
| canceled | string | str | 0 | 0.00% | 2 |

## 发现的问题

- 重复订单号：B003
- 第 3 行 `B002`：total_price 不是有效数字
- 第 7 行 `B005`：guests 必须大于 0，当前为 0
- 第 8 行 `B006`：total_price 必须大于 0，当前为 -100
- 第 10 行 `B008`：guests 不是有效数字
- 订单 `B007` 的价格 5000 超出正常区间 0–2295

## 城市分布

| 城市 | 订单数 |
|---|---:|
| Hangzhou | 8 |
| Shanghai | 3 |
| <缺失> | 1 |
| Chengdu | 1 |
| Shenzhen | 1 |
| Beijing | 1 |
| Guangzhou | 1 |
