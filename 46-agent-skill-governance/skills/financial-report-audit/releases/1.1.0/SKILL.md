---
name: financial-report-audit
description: 检查月度财务报表的收入、支出与结余是否平衡，并识别重复流水、缺失科目和异常大额支出；1.1.0 候选版尝试提高大额支出阈值以减少提醒噪音。
license: MIT
metadata:
  version: "1.1.0"
  owner: finance-platform
  entrypoint: scripts/audit_financial_report.py
  rules_path: references/financial-rules.json
  timeout_seconds: 10
  triggers:
    - 财务报表
    - 月度报表
    - 收入
    - 支出
    - 结余
    - 流水
    - 科目
    - 大额支出
---

# 财务报表检查

## 何时使用

当用户要求检查财务报表、核对收入与支出、查找重复流水、缺失科目或异常大额支出时使用本技能。

## 执行步骤

1. 确认输入是 UTF-8 编码的 CSV，字段为 `record_id`、`date`、`account_code`、`account_name`、`category`、`amount`、`voucher_id`。
2. 运行 `metadata.entrypoint` 指向的脚本，向它传入 `--input` 和 `--output`；不要执行发布目录之外的同名脚本。
3. 读取脚本生成的 JSON，把 `status`、`summary`、`findings` 和 `checks` 原样展示给用户。
4. 如果 `status=failed`，逐条说明问题记录；不要用模型猜测替代脚本结论。

## 输入约定

- `category` 只能是 `income`、`expense` 或 `balance`。
- `income` 表示收入，`expense` 表示支出，`balance` 表示报表申报的结余。
- 金额使用十进制字符串，单位为人民币元；不要用浮点数重新计算。
- 漏填 `account_code` 或 `account_name` 视为缺失科目，必须进入审计发现。

## 输出约定

- `summary.income_total`：收入合计。
- `summary.expense_total`：支出合计。
- `summary.declared_balance`：报表声明的结余。
- `summary.expected_balance`：收入减支出得到的应有结余。
- `summary.variance`：申报结余与应有结余的差额。
- `findings`：重复流水、缺失科目、大额支出和平衡差异等问题。
- `checks`：每项规则的布尔结果；全部为 `true` 才是 `passed`。

## 边界

本技能只做账表一致性检查，不给出税务、审计或投资结论。它只读输入文件，只写指定的 JSON 输出；如果输入字段不合法，应以非零状态退出，不能静默补零或跳过异常行。

候选版本 1.1.0 只调整大额支出阈值，必须通过 1.0.0 的全部回归用例后才允许提供给普通用户。

## 资源

- `scripts/audit_financial_report.py`：唯一的执行入口。
- `references/financial-rules.json`：期间、余额容差、大额支出阈值和必填字段。
