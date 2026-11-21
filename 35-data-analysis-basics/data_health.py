"""Inspect a travel booking CSV before loading it into DataPilot."""

import json
from pathlib import Path

import pandas as pd


# PROJECT_DIR 保存 CSV、报告和本课入口。
PROJECT_DIR = Path(__file__).resolve().parent
# BOOKINGS_FILE 是待体检的旅行订单文件。
BOOKINGS_FILE = PROJECT_DIR / "bookings.csv"
# NUMERIC_COLUMNS 是进入分析前必须转换的数值字段。
NUMERIC_COLUMNS = ["nights", "guests", "total_price"]
# REQUIRED_COLUMNS 是 DataPilot 依赖的基础字段。
REQUIRED_COLUMNS = {
    "booking_id",
    "city",
    "check_in",
    "nights",
    "guests",
    "total_price",
    "channel",
    "canceled",
}


class DataHealthError(ValueError):
    """The CSV cannot be used for the health check."""


def load_bookings(path):
    """Read raw CSV cells as text so missing and invalid values remain visible."""
    try:
        raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    except FileNotFoundError as error:
        raise DataHealthError(f"CSV 文件不存在：{path}") from error

    raw.columns = [column.strip() for column in raw.columns]
    missing_columns = REQUIRED_COLUMNS.difference(raw.columns)
    if missing_columns:
        raise DataHealthError(f"CSV 缺少字段：{', '.join(sorted(missing_columns))}")

    # 去掉每个文本单元格两侧的空白，避免空格被误认为有效值。
    return raw.apply(lambda column: column.str.strip())


def parse_numeric(raw):
    """Convert required numeric columns and keep invalid values as NaN."""
    parsed = raw.copy()
    for column in NUMERIC_COLUMNS:
        parsed[column] = pd.to_numeric(parsed[column], errors="coerce")
    return parsed


def column_profile(raw, parsed):
    """Describe raw types, parsed types, missing values, and uniqueness."""
    profiles = []
    row_count = len(raw)
    for column in raw.columns:
        missing = int((raw[column] == "").sum())
        profiles.append(
            {
                "column": column,
                "raw_type": pd.api.types.infer_dtype(raw[column], skipna=False),
                "parsed_type": str(parsed[column].dtype),
                "missing": missing,
                "missing_rate": round(missing / row_count, 4) if row_count else 0.0,
                "unique": int(raw[column].nunique(dropna=False)),
            }
        )
    return profiles


def find_invalid_rows(raw, parsed):
    """Find rows whose numeric fields are missing, non-numeric, or out of range."""
    invalid_rows = []
    for index, row in parsed.iterrows():
        problems = []
        for column in NUMERIC_COLUMNS:
            value = row[column]
            if pd.isna(value):
                problems.append(f"{column} 不是有效数字")
            elif value <= 0:
                problems.append(f"{column} 必须大于 0，当前为 {value:g}")
        if problems:
            invalid_rows.append(
                {
                    "row_number": int(index) + 2,
                    "booking_id": raw.at[index, "booking_id"] or "<空>",
                    "problems": problems,
                }
            )
    return invalid_rows


def find_price_outliers(parsed):
    """Use the IQR rule to find unusually expensive or cheap bookings."""
    valid_prices = parsed.loc[
        parsed["total_price"].notna() & (parsed["total_price"] > 0),
        "total_price",
    ]
    q1 = valid_prices.quantile(0.25)
    q3 = valid_prices.quantile(0.75)
    iqr = q3 - q1
    # 价格不可能为负数，因此下界至少停在 0。
    lower_bound = max(0, q1 - 1.5 * iqr)
    upper_bound = q3 + 1.5 * iqr
    positive_prices = parsed[
        parsed["total_price"].notna() & (parsed["total_price"] > 0)
    ]
    outliers = positive_prices[
        (positive_prices["total_price"] < lower_bound)
        | (positive_prices["total_price"] > upper_bound)
    ]
    return [
        {
            "booking_id": row["booking_id"],
            "total_price": float(row["total_price"]),
            "lower_bound": round(float(lower_bound), 2),
            "upper_bound": round(float(upper_bound), 2),
        }
        for _, row in outliers.iterrows()
    ]


def build_markdown(report):
    """Render a compact report that a beginner can inspect in Markdown."""
    lines = [
        "# 旅行订单数据体检报告",
        "",
        "## 概览",
        "",
        f"- 数据行数：{report['row_count']}",
        f"- 字段数：{report['column_count']}",
        f"- 缺失单元格：{report['missing_cells']}",
        f"- 重复订单号：{', '.join(report['duplicate_booking_ids']) or '无'}",
        f"- 非法数值行：{len(report['invalid_rows'])}",
        f"- 价格离群行：{len(report['price_outliers'])}",
        "",
        "## 字段类型与缺失值",
        "",
        "| 字段 | 原始类型 | 解析后类型 | 缺失数 | 缺失率 | 唯一值 |",
        "|---|---|---|---:|---:|---:|",
    ]
    for profile in report["columns"]:
        lines.append(
            f"| {profile['column']} | {profile['raw_type']} | {profile['parsed_type']} "
            f"| {profile['missing']} | {profile['missing_rate']:.2%} | {profile['unique']} |"
        )

    lines.extend(["", "## 发现的问题", ""])
    if report["duplicate_booking_ids"]:
        lines.append(
            f"- 重复订单号：{', '.join(report['duplicate_booking_ids'])}"
        )
    for item in report["invalid_rows"]:
        lines.append(
            f"- 第 {item['row_number']} 行 `{item['booking_id']}`："
            f"{'；'.join(item['problems'])}"
        )
    for item in report["price_outliers"]:
        lines.append(
            f"- 订单 `{item['booking_id']}` 的价格 {item['total_price']:.0f} "
            f"超出正常区间 {item['lower_bound']:.0f}–{item['upper_bound']:.0f}"
        )

    lines.extend(
        [
            "",
            "## 城市分布",
            "",
            "| 城市 | 订单数 |",
            "|---|---:|",
        ]
    )
    for city, count in report["city_counts"].items():
        lines.append(f"| {city} | {count} |")
    return "\n".join(lines) + "\n"


def run_health_check(output_dir=PROJECT_DIR):
    """Build and save the data health report."""
    raw = load_bookings(BOOKINGS_FILE)
    parsed = parse_numeric(raw)
    duplicate_ids = sorted(
        raw.loc[raw["booking_id"].duplicated(keep=False), "booking_id"].unique()
    )
    invalid_rows = find_invalid_rows(raw, parsed)
    price_outliers = find_price_outliers(parsed)
    city_counts = {
        str(city): int(count)
        for city, count in raw["city"].replace("", "<缺失>").value_counts().items()
    }
    report = {
        "row_count": int(len(raw)),
        "column_count": int(len(raw.columns)),
        "missing_cells": int((raw == "").sum().sum()),
        "duplicate_booking_ids": duplicate_ids,
        "columns": column_profile(raw, parsed),
        "invalid_rows": invalid_rows,
        "price_outliers": price_outliers,
        "city_counts": city_counts,
        "pandas_version": pd.__version__,
    }
    report["checks"] = {
        "row_count_is_16": report["row_count"] == 16,
        "missing_cells_found": report["missing_cells"] > 0,
        "duplicate_id_found": "B003" in duplicate_ids,
        "invalid_rows_found": len(invalid_rows) >= 3,
        "outlier_found": len(price_outliers) == 1,
        "missing_city_visible": city_counts.get("<缺失>") == 1,
    }
    failed_checks = [name for name, passed in report["checks"].items() if not passed]
    if failed_checks:
        raise AssertionError(f"数据体检失败：{', '.join(failed_checks)}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "data-health-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "data-health-report.md").write_text(
        build_markdown(report),
        encoding="utf-8",
    )

    print("数据体检结果：")
    print(f"行数={report['row_count']} 字段数={report['column_count']}")
    print(f"缺失单元格={report['missing_cells']}")
    print(f"重复订单号={duplicate_ids}")
    print(f"非法数值行={len(invalid_rows)}")
    print(f"价格离群行={len(price_outliers)}")
    print(f"城市分布={city_counts}")
    print(f"校验结果： {all(report['checks'].values())}")
    print("已保存：data-health-report.md, data-health-report.json")
    return report


if __name__ == "__main__":
    run_health_check()
