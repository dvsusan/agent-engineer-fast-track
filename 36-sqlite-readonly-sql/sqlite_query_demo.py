"""Run a schema-aware, read-only SQLite analysis for DataPilot."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


# PROJECT_DIR 保存 CSV、数据库、Schema 和查询报告。
PROJECT_DIR = Path(__file__).resolve().parent
CSV_FILE = PROJECT_DIR / "bookings.csv"
DB_FILE = PROJECT_DIR / "datapilot-36.db"
SCHEMA_FILE = PROJECT_DIR / "schema-context.json"
REPORT_FILE = PROJECT_DIR / "query-report.json"
REPORT_MD_FILE = PROJECT_DIR / "query-report.md"
# 一次查询最多返回 100 行，防止误查全表后把大量数据带进上下文。
MAX_ROWS = 100
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
ALLOWED_TABLES = {"bookings"}


class QuerySecurityError(ValueError):
    """The SQL is not allowed by the read-only policy."""


class QueryExecutionError(ValueError):
    """The query cannot be executed against the current schema."""


def parse_int(value):
    """Convert a CSV integer cell to int, returning None for an invalid cell."""
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_float(value):
    """Convert a CSV numeric cell to float, returning None for an invalid cell."""
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def create_database(csv_file=CSV_FILE, db_file=DB_FILE):
    """Import the raw CSV into a local SQLite database for this lesson."""
    if not csv_file.exists():
        raise QueryExecutionError(f"CSV 文件不存在：{csv_file}")

    # 生成文件是教学数据库，每次运行都从原始 CSV 重建，避免旧结果混入。
    if db_file.exists():
        db_file.unlink()

    connection = sqlite3.connect(db_file)
    try:
        connection.execute(
            """
            CREATE TABLE bookings (
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_id TEXT NOT NULL,
                city TEXT,
                check_in TEXT,
                nights INTEGER,
                guests REAL,
                total_price REAL,
                channel TEXT,
                canceled TEXT NOT NULL
            )
            """
        )
        # 这张表故意不在 ALLOWED_TABLES 中，用来验证 Schema 和 authorizer 都会隐藏它。
        connection.execute("CREATE TABLE private_notes (note TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO private_notes (note) VALUES ('只有管理员可见的内部备注')"
        )

        with csv_file.open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            fieldnames = {name.strip() for name in (reader.fieldnames or [])}
            missing_columns = REQUIRED_COLUMNS.difference(fieldnames)
            if missing_columns:
                raise QueryExecutionError(
                    f"CSV 缺少字段：{', '.join(sorted(missing_columns))}"
                )

            rows = []
            for row in reader:
                rows.append(
                    (
                        row["booking_id"].strip(),
                        row["city"].strip() or None,
                        row["check_in"].strip() or None,
                        parse_int(row["nights"]),
                        parse_float(row["guests"]),
                        parse_float(row["total_price"]),
                        row["channel"].strip() or None,
                        row["canceled"].strip(),
                    )
                )

        connection.executemany(
            """
            INSERT INTO bookings (
                booking_id, city, check_in, nights, guests,
                total_price, channel, canceled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.commit()
        return len(rows)
    finally:
        connection.close()


def read_schema(connection):
    """Read table and column metadata before a read-only authorizer is attached."""
    tables = []
    for table_name in sorted(ALLOWED_TABLES):
        columns = []
        for row in connection.execute(f"PRAGMA table_info({table_name})"):
            columns.append(
                {
                    "name": row["name"],
                    "type": row["type"],
                    "not_null": bool(row["notnull"]),
                    "primary_key": bool(row["pk"]),
                }
            )
        row_count = connection.execute(
            f"SELECT COUNT(*) AS row_count FROM {table_name}"
        ).fetchone()["row_count"]
        tables.append(
            {
                "name": table_name,
                "row_count": int(row_count),
                "columns": columns,
            }
        )
    return {"database": DB_FILE.name, "tables": tables}


def render_schema_prompt(schema):
    """Render schema as a short context block for a query planner."""
    lines = [f"数据库：{schema['database']}"]
    for table in schema["tables"]:
        columns = ", ".join(
            f"{column['name']} {column['type']}" for column in table["columns"]
        )
        lines.append(f"表 {table['name']}，共 {table['row_count']} 行：{columns}")
    return "\n".join(lines)


FORBIDDEN_SQL = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|"
    r"PRAGMA|VACUUM|REINDEX|ANALYZE|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|"
    r"RELEASE|TRIGGER)\b",
    re.IGNORECASE,
)


def validate_read_only_sql(sql):
    """Reject comments, multiple statements, and non-SELECT SQL."""
    if not isinstance(sql, str) or not sql.strip():
        raise QuerySecurityError("SQL 不能为空")

    query = sql.strip()
    if query.endswith(";"):
        query = query[:-1].rstrip()
    if not query:
        raise QuerySecurityError("SQL 不能只有分号")
    if ";" in query:
        raise QuerySecurityError("只允许一条 SELECT 语句，不能在中间出现分号")
    if "--" in query or "/*" in query or "*/" in query:
        raise QuerySecurityError("本课不允许 SQL 注释，避免隐藏危险关键字")
    if not re.match(r"(?is)^SELECT\b", query):
        raise QuerySecurityError("只允许以 SELECT 开头的查询")
    forbidden = FORBIDDEN_SQL.search(query)
    if forbidden:
        raise QuerySecurityError(f"查询包含禁止关键字：{forbidden.group(0).upper()}")
    return query


DENIED_ACTIONS = {
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_PRAGMA,
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
    sqlite3.SQLITE_TRANSACTION,
    sqlite3.SQLITE_SAVEPOINT,
}


def read_only_authorizer(action, arg1, arg2, database_name, trigger_name):
    """Deny write actions and reads from tables outside the allowlist."""
    if action in DENIED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and arg1 not in ALLOWED_TABLES:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@dataclass(frozen=True)
class QuerySpec:
    name: str
    question: str
    keywords: tuple[str, ...]
    sql: str
    required_columns: tuple[str, ...]


QUERY_CATALOG = (
    QuerySpec(
        name="city_summary",
        question="哪些城市的有效订单最多？平均订单价格是多少？",
        keywords=("城市", "订单最多", "平均"),
        sql="""
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
""".strip(),
        required_columns=("city", "canceled", "nights", "guests", "total_price"),
    ),
    QuerySpec(
        name="price_bands",
        question="有效订单主要落在哪些价格区间？",
        keywords=("价格", "区间", "分布"),
        sql="""
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
""".strip(),
        required_columns=("canceled", "nights", "guests", "total_price"),
    ),
)


class QueryAnalyst:
    """A deterministic stand-in for the Agent's schema-aware query layer."""

    def __init__(self, db_file=DB_FILE):
        if not db_file.exists():
            raise QueryExecutionError(f"数据库文件不存在：{db_file}")
        uri = f"{db_file.resolve().as_uri()}?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True)
        self.connection.row_factory = sqlite3.Row
        # schema 读取在 authorizer 之前完成；之后的用户查询只能读白名单表。
        self.connection.execute("PRAGMA query_only = ON")
        self.schema = read_schema(self.connection)
        self.connection.set_authorizer(read_only_authorizer)

    def close(self):
        self.connection.close()

    def schema_prompt(self):
        return render_schema_prompt(self.schema)

    def find_query(self, question):
        for spec in QUERY_CATALOG:
            if any(keyword in question for keyword in spec.keywords):
                return spec
        raise QueryExecutionError(f"没有匹配的安全查询模板：{question}")

    def check_columns(self, spec):
        available = {
            column["name"]
            for table in self.schema["tables"]
            if table["name"] == "bookings"
            for column in table["columns"]
        }
        missing = set(spec.required_columns).difference(available)
        if missing:
            raise QueryExecutionError(
                f"查询模板 {spec.name} 依赖缺失字段：{', '.join(sorted(missing))}"
            )

    def execute(self, sql):
        query = validate_read_only_sql(sql)
        try:
            cursor = self.connection.execute(query)
            rows = cursor.fetchmany(MAX_ROWS + 1)
        except sqlite3.DatabaseError as error:
            raise QueryExecutionError(f"SQL 执行失败：{error}") from error

        if len(rows) > MAX_ROWS:
            raise QuerySecurityError(f"查询结果超过 {MAX_ROWS} 行，请增加过滤条件")
        columns = [item[0] for item in cursor.description or []]
        return {
            "columns": columns,
            "rows": [dict(zip(columns, row)) for row in rows],
            "row_count": len(rows),
        }

    def ask(self, question):
        spec = self.find_query(question)
        self.check_columns(spec)
        result = self.execute(spec.sql)
        return {
            "name": spec.name,
            "question": spec.question,
            "sql": spec.sql,
            **result,
        }


def expect_rejection(call, error_type):
    """Run a known-bad action and return its error message as evidence."""
    try:
        call()
    except error_type as error:
        return str(error)
    raise AssertionError(f"预期 {error_type.__name__}，但操作没有失败")


def build_markdown(report):
    """Render a human-readable report for the schema and query results."""
    lines = [
        "# DataPilot 只读查询报告",
        "",
        "## Schema 摘要",
        "",
        "```text",
        report["schema_prompt"],
        "```",
        "",
    ]
    for query in report["queries"]:
        lines.extend(
            [
                f"## {query['question']}",
                "",
                "```sql",
                query["sql"],
                "```",
                "",
                "| " + " | ".join(query["columns"]) + " |",
                "|" + "|".join("---" for _ in query["columns"]) + "|",
            ]
        )
        for row in query["rows"]:
            lines.append(
                "| " + " | ".join(str(row[column]) for column in query["columns"]) + " |"
            )
        lines.append("")

    lines.extend(["## 安全检查", "", "| 检查 | 结果 |", "|---|---|"])
    for name, message in report["security_checks"].items():
        lines.append(f"| {name} | {message} |")

    lines.extend(["", "## 固定校验", "", "| 检查 | 是否通过 |", "|---|---|"])
    for name, passed in report["checks"].items():
        lines.append(f"| {name} | {passed} |")
    return "\n".join(lines) + "\n"


def run_demo():
    """Build the database, run both queries, and save the reports."""
    imported_rows = create_database()
    analyst = QueryAnalyst()
    try:
        schema_report = analyst.schema
        SCHEMA_FILE.write_text(
            json.dumps(schema_report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        city_result = analyst.ask("哪些城市的有效订单最多？平均订单价格是多少？")
        price_result = analyst.ask("有效订单主要落在哪些价格区间？")
        security_checks = {
            "write_rejected": expect_rejection(
                lambda: analyst.execute("DELETE FROM bookings"),
                QuerySecurityError,
            ),
            "multi_statement_rejected": expect_rejection(
                lambda: analyst.execute(
                    "SELECT * FROM bookings; DELETE FROM bookings"
                ),
                QuerySecurityError,
            ),
            "private_table_rejected": expect_rejection(
                lambda: analyst.execute("SELECT * FROM private_notes"),
                QueryExecutionError,
            ),
        }
        city_rows = city_result["rows"]
        price_rows = price_result["rows"]
        checks = {
            "schema_has_16_rows": schema_report["tables"][0]["row_count"] == 16,
            "city_top_is_hangzhou": city_rows[0]["city"] == "Hangzhou",
            "city_top_has_5_orders": city_rows[0]["valid_orders"] == 5,
            "valid_orders_total_is_11": sum(
                row["valid_orders"] for row in city_rows
            )
            == 11,
            "price_bands_total_is_11": sum(
                row["orders"] for row in price_rows
            )
            == 11,
            "write_rejected": bool(security_checks["write_rejected"]),
            "multi_statement_rejected": bool(
                security_checks["multi_statement_rejected"]
            ),
            "private_table_rejected": bool(
                security_checks["private_table_rejected"]
            ),
        }
        report = {
            "imported_rows": imported_rows,
            "schema": schema_report,
            "schema_prompt": analyst.schema_prompt(),
            "queries": [city_result, price_result],
            "security_checks": security_checks,
            "checks": checks,
        }
        REPORT_FILE.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        REPORT_MD_FILE.write_text(build_markdown(report), encoding="utf-8")

        print("SQLite 只读查询结果：")
        print(f"导入原始订单={imported_rows} 行")
        print("Schema 摘要：")
        print(analyst.schema_prompt())
        for query in report["queries"]:
            print(f"\n查询：{query['question']}")
            print(f"返回行数={query['row_count']}")
            for row in query["rows"]:
                print(row)
        print("\n安全检查：")
        for name, message in security_checks.items():
            print(f"{name}: {message}")
        print(f"校验结果： {all(checks.values())}")
        print("已保存：schema-context.json, query-report.json, query-report.md")
        if not all(checks.values()):
            raise AssertionError("固定校验未通过")
        return report
    finally:
        analyst.close()


if __name__ == "__main__":
    run_demo()
