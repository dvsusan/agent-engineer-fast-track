#!/usr/bin/env python3
"""财务报表一致性检查脚本。

这个脚本是 `financial-report-audit` 技能的唯一执行入口。它只依赖 Python
标准库，负责读取 CSV、应用财务规则、输出结构化 JSON，并把业务问题编码成
`findings`；文件名、字段或规则不合法时才使用非零退出码。
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES = SKILL_ROOT / "references" / "financial-rules.json"
DEFAULT_OUTPUT = Path("financial-audit-report.json")

# 版本号必须与 SKILL.md 的 metadata.version 保持一致；第 46 课会用清单自动检查。
SKILL_VERSION = "1.0.0"


class AuditInputError(RuntimeError):
    """输入 CSV、规则文件或参数不合法，调用方不能把它当成普通业务发现。"""


def utc_now() -> str:
    """返回带时区的 ISO 8601 时间戳，确保报告可以跨机器排序。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def money_text(value: Decimal) -> str:
    """把金额固定格式化为两位小数，避免 JSON 里出现浮点误差。"""
    return format(value.quantize(Decimal("0.01")), "f")


def display_path(path: Path) -> str:
    """技能目录内的资源用相对路径展示，外部输入保留绝对路径。"""
    resolved = path.resolve()
    try:
        return resolved.relative_to(SKILL_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def load_rules(path: Path) -> dict[str, Any]:
    """读取 JSON 规则并校验必需字段，避免规则缺失时静默使用错误阈值。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AuditInputError(f"规则文件不存在：{path}") from error
    except json.JSONDecodeError as error:
        raise AuditInputError(f"规则文件不是合法 JSON：{path}") from error

    required_keys = {
        "currency",
        "period",
        "balance_tolerance",
        "large_expense_threshold",
        "allowed_categories",
        "required_fields",
    }
    missing = sorted(required_keys - payload.keys())
    if missing:
        raise AuditInputError(f"规则文件缺少字段：{', '.join(missing)}")
    if not isinstance(payload["allowed_categories"], list):
        raise AuditInputError("allowed_categories 必须是数组")
    if not isinstance(payload["required_fields"], list):
        raise AuditInputError("required_fields 必须是数组")
    return payload


def read_rows(path: Path, required_fields: list[str]) -> list[dict[str, str]]:
    """读取 UTF-8 CSV；表头缺失时在审计开始前直接失败。"""
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except FileNotFoundError as error:
        raise AuditInputError(f"输入文件不存在：{path}") from error

    with handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        missing = [field for field in required_fields if field not in headers]
        if missing:
            raise AuditInputError(f"CSV 缺少列：{', '.join(missing)}")
        rows = [dict(row) for row in reader]

    if not rows:
        raise AuditInputError("CSV 没有数据行")
    return rows


def parse_amount(
    raw_value: str | None,
    record_id: str,
    findings: list[dict[str, Any]],
) -> Decimal | None:
    """把金额解析为 Decimal；空值和非法数字分别留下可解释的发现。"""
    raw = (raw_value or "").strip()
    if not raw:
        findings.append(
            {
                "code": "INVALID_AMOUNT",
                "severity": "high",
                "message": f"记录 {record_id} 的 amount 为空",
                "record_ids": [record_id],
            }
        )
        return None

    try:
        amount = Decimal(raw)
    except InvalidOperation:
        findings.append(
            {
                "code": "INVALID_AMOUNT",
                "severity": "high",
                "message": f"记录 {record_id} 的 amount 不是合法数字：{raw}",
                "record_ids": [record_id],
            }
        )
        return None

    if not amount.is_finite() or amount <= 0:
        findings.append(
            {
                "code": "INVALID_AMOUNT",
                "severity": "high",
                "message": f"记录 {record_id} 的 amount 必须是大于 0 的有限数字",
                "record_ids": [record_id],
            }
        )
        return None
    return amount


def audit_rows(rows: list[dict[str, str]], rules: dict[str, Any]) -> dict[str, Any]:
    """按固定规则审计报表，返回可被 Agent 直接消费的结构化结果。"""
    findings: list[dict[str, Any]] = []
    totals: defaultdict[str, Decimal] = defaultdict(Decimal)
    vouchers: defaultdict[str, list[str]] = defaultdict(list)

    period = str(rules["period"])
    allowed_categories = set(rules["allowed_categories"])
    required_fields = list(rules["required_fields"])
    tolerance = Decimal(str(rules["balance_tolerance"]))
    large_expense_threshold = Decimal(str(rules["large_expense_threshold"]))

    for row in rows:
        record_id = (row.get("record_id") or "unknown").strip()
        missing_fields = [
            field for field in required_fields if not (row.get(field) or "").strip()
        ]
        if missing_fields:
            # 一个记录缺多个字段时合并成一条发现，避免一个问题被拆成多行噪音。
            findings.append(
                {
                    "code": "MISSING_FIELDS",
                    "severity": "medium",
                    "message": f"记录 {record_id} 缺少字段：{', '.join(missing_fields)}",
                    "record_ids": [record_id],
                }
            )

        record_date = (row.get("date") or "").strip()
        if record_date and not record_date.startswith(f"{period}-"):
            findings.append(
                {
                    "code": "DATE_OUT_OF_PERIOD",
                    "severity": "medium",
                    "message": f"记录 {record_id} 的日期 {record_date} 不在期间 {period}",
                    "record_ids": [record_id],
                }
            )

        category = (row.get("category") or "").strip()
        if category not in allowed_categories:
            findings.append(
                {
                    "code": "UNKNOWN_CATEGORY",
                    "severity": "high",
                    "message": f"记录 {record_id} 的 category 不合法：{category or '空'}",
                    "record_ids": [record_id],
                }
            )

        amount = parse_amount(row.get("amount"), record_id, findings)
        if amount is not None and category in allowed_categories:
            totals[category] += amount
            if category == "expense" and amount > large_expense_threshold:
                findings.append(
                    {
                        "code": "LARGE_EXPENSE",
                        "severity": "medium",
                        "message": (
                            f"记录 {record_id} 的支出 {money_text(amount)} "
                            f"超过阈值 {money_text(large_expense_threshold)}"
                        ),
                        "record_ids": [record_id],
                    }
                )

        voucher_id = (row.get("voucher_id") or "").strip()
        if voucher_id:
            vouchers[voucher_id].append(record_id)

    for voucher_id, record_ids in sorted(vouchers.items()):
        if len(record_ids) > 1:
            findings.append(
                {
                    "code": "DUPLICATE_VOUCHER",
                    "severity": "high",
                    "message": f"流水号 {voucher_id} 重复出现：{', '.join(record_ids)}",
                    "record_ids": record_ids,
                }
            )

    expected_balance = totals["income"] - totals["expense"]
    declared_balance = totals["balance"]
    variance = declared_balance - expected_balance
    if abs(variance) > tolerance:
        findings.append(
            {
                "code": "BALANCE_MISMATCH",
                "severity": "high",
                "message": (
                    f"收入 {money_text(totals['income'])} 减支出 "
                    f"{money_text(totals['expense'])} 应为 {money_text(expected_balance)}，"
                    f"报表申报 {money_text(declared_balance)}，差异 {money_text(variance)}"
                ),
                "record_ids": [],
            }
        )

    finding_codes = {finding["code"] for finding in findings}
    checks = {
        "balance_matches": "BALANCE_MISMATCH" not in finding_codes,
        "no_duplicate_vouchers": "DUPLICATE_VOUCHER" not in finding_codes,
        "no_missing_fields": "MISSING_FIELDS" not in finding_codes,
        "no_large_expenses": "LARGE_EXPENSE" not in finding_codes,
        "period_matches": "DATE_OUT_OF_PERIOD" not in finding_codes,
        "categories_valid": "UNKNOWN_CATEGORY" not in finding_codes,
        "amounts_valid": "INVALID_AMOUNT" not in finding_codes,
    }
    passed = all(checks.values())

    findings.sort(key=lambda item: (item["code"], item["record_ids"]))
    return {
        "skill": "financial-report-audit",
        "skill_version": SKILL_VERSION,
        "generated_at": utc_now(),
        "period": period,
        "currency": rules["currency"],
        "status": "passed" if passed else "failed",
        "summary": {
            "rows": len(rows),
            "income_total": money_text(totals["income"]),
            "expense_total": money_text(totals["expense"]),
            "declared_balance": money_text(declared_balance),
            "expected_balance": money_text(expected_balance),
            "variance": money_text(variance),
        },
        "checks": checks,
        "findings": findings,
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再替换，避免进程退出留下不完整 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


def parse_args() -> argparse.Namespace:
    """定义唯一允许的命令行参数，调用方不需要猜测内部函数签名。"""
    parser = argparse.ArgumentParser(description="财务报表一致性检查技能")
    parser.add_argument("--input", type=Path, required=True, help="输入 CSV 路径")
    parser.add_argument(
        "--rules",
        type=Path,
        default=DEFAULT_RULES,
        help="规则 JSON 路径，默认使用技能内置规则",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="审计结果 JSON 输出路径",
    )
    return parser.parse_args()


def main() -> int:
    """执行审计；业务失败写入报告，输入或规则错误返回 2。"""
    args = parse_args()
    try:
        rules = load_rules(args.rules)
        rows = read_rows(args.input, list(rules["required_fields"]))
        report = audit_rows(rows, rules)
        report["input"] = display_path(args.input)
        report["rules"] = display_path(args.rules)
        report["resources_used"] = [report["rules"], report["input"]]
        atomic_write_json(args.output, report)
    except AuditInputError as error:
        print(f"审计输入错误：{error}")
        return 2

    summary = report["summary"]
    print(f"财务报表检查完成：{report['status']}")
    print(
        f"收入={summary['income_total']} 支出={summary['expense_total']} "
        f"申报结余={summary['declared_balance']} "
        f"应有结余={summary['expected_balance']} 差异={summary['variance']}"
    )
    print(f"发现 {len(report['findings'])} 个问题")
    print(f"已保存：{args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
