"""用 LangChain 组织一次可审计的 DataPilot 订单分析。

本文件只使用 LangChain 的底层可组合组件，不调用隐藏的高层 Agent：
1. ChatOpenAI 统一聊天模型调用；
2. ChatPromptTemplate 约束模型输入；
3. @tool 与 bind_tools 让模型提出工具调用；
4. 业务代码显式执行工具，再把结果交给输出解析器；
5. PydanticOutputParser 负责结构校验，校验失败时不生成看似成功的报告。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from dotenv import load_dotenv
from langchain_core.messages import AIMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field


# 所有路径都以代码文件所在目录为基准，避免切换终端目录后读到另一份数据。
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_FILE = PROJECT_DIR / "bookings.csv"
DEFAULT_REPORT_FILE = PROJECT_DIR / "langchain-report.json"
DEFAULT_FAILURE_REPORT_FILE = PROJECT_DIR / "langchain-failure-report.json"
DEFAULT_QUESTION = "这批旅行订单里有多少有效订单？平均客单价是多少？数据有什么风险？"

# 只有存在真实密钥时才允许进入模型调用；模板文件本身不会被当作配置。
load_dotenv(PROJECT_DIR / ".env")

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
NUMERIC_COLUMNS = ("nights", "guests", "total_price")


class PipelineError(RuntimeError):
    """分析链的可分类失败；stage 指明配置、选工具、执行工具或解析输出。"""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


class ConfigurationError(PipelineError):
    """本地模型配置缺失或无效，尚未发出真实请求。"""

    def __init__(self, message: str) -> None:
        super().__init__("configuration", message)


class ToolEvidenceError(PipelineError):
    """工具没有产出可用证据；后续自然语言步骤必须停止。"""

    def __init__(self, message: str) -> None:
        super().__init__("tool_execution", message)


class NarrativeParseError(PipelineError):
    """模型正文无法通过 Pydantic 结构校验。"""

    def __init__(self, message: str) -> None:
        super().__init__("output_parsing", message)


@dataclass(frozen=True)
class ModelSettings:
    """一次运行所需的模型配置；密钥字段禁止进入 repr 和报告。"""

    base_url: str
    model: str
    timeout_seconds: float
    # repr=False 是安全边界：调试打印配置时不能把真实密钥带进终端或日志。
    api_key: str = field(repr=False)

    def public_view(self) -> dict[str, Any]:
        """返回可安全写入 JSON 的脱敏配置。"""
        return {
            "provider": "deepseek",
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "api_key_configured": bool(self.api_key),
        }


@dataclass(frozen=True)
class ToolSelection:
    """第一次模型调用的选工具结果，以及业务代码真正执行的工具记录。"""

    tool_calls: list[dict[str, Any]]
    tool_name: str
    result_text: str
    elapsed_ms: float
    usage: dict[str, Any]


class AnalysisNarrative(BaseModel):
    """输出解析器接受的最小业务结构；缺字段或类型错误都会被拒绝。"""

    # forbid 防止模型偷偷增加字段后，上游仍误以为报告符合原契约。
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=15, max_length=600, description="面向业务人员的一段结论")
    key_findings: list[str] = Field(
        min_length=2,
        max_length=5,
        description="只能来自工具证据的 2 到 5 条关键发现",
    )
    data_risks: list[str] = Field(
        min_length=1,
        max_length=5,
        description="缺失、重复、非法值或异常高客单价等数据风险",
    )
    next_actions: list[str] = Field(
        min_length=1,
        max_length=4,
        description="基于当前证据可以执行的下一步动作",
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="对结论可信度的判断；数据缺陷越多，置信度应越低"
    )


def read_model_settings() -> ModelSettings:
    """从环境变量读取 DeepSeek 配置，并在发请求前完成格式校验。"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ConfigurationError(
            "没有配置 DEEPSEEK_API_KEY，请参考 .env.example 创建 .env"
        )

    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise ConfigurationError("DEEPSEEK_BASE_URL 必须以 https:// 或 http:// 开头")

    model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
    if not model:
        raise ConfigurationError("DEEPSEEK_MODEL 不能为空")

    raw_timeout = os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "20").strip()
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError as error:
        raise ConfigurationError("DEEPSEEK_TIMEOUT_SECONDS 必须是数字") from error
    if timeout_seconds <= 0:
        raise ConfigurationError("DEEPSEEK_TIMEOUT_SECONDS 必须大于 0")

    return ModelSettings(
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        api_key=api_key,
    )


def build_chat_model(settings: ModelSettings) -> ChatOpenAI:
    """创建唯一的 DeepSeek 聊天模型；框架只接管协议，不接管业务判断。"""
    return ChatOpenAI(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout=settings.timeout_seconds,
        # 本课需要看见第一次失败，而不是让 SDK 在后台重试并放大延迟和费用。
        max_retries=0,
        temperature=0.1,
        max_tokens=700,
        # DeepSeek 的思考内容也占用输出预算；报告场景只需要最终 JSON。
        extra_body={"thinking": {"type": "disabled"}},
    )


def _audit_bookings(path: Path) -> dict[str, Any]:
    """计算订单质量与经营指标；所有数值都来自传入的 CSV。"""
    try:
        raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    except FileNotFoundError as error:
        raise ValueError(f"订单文件不存在：{path.name}") from error
    except pd.errors.EmptyDataError as error:
        raise ValueError("订单文件为空") from error

    raw.columns = [column.strip() for column in raw.columns]
    missing_columns = REQUIRED_COLUMNS.difference(raw.columns)
    if missing_columns:
        raise ValueError(f"订单文件缺少字段：{', '.join(sorted(missing_columns))}")

    # 先保留原始文本，确保空字符串和 guests=x 不会被 pandas 静默转成缺失值。
    raw = raw.apply(lambda column: column.str.strip())
    parsed = raw.copy()
    for column in NUMERIC_COLUMNS:
        parsed[column] = pd.to_numeric(parsed[column], errors="coerce")

    duplicate_ids = sorted(
        raw.loc[raw["booking_id"].duplicated(keep=False), "booking_id"].unique()
    )

    # 重复订单号只保留首行；否则重复记录会把订单数和收入同时抬高。
    unique_rows = raw.drop_duplicates(subset=["booking_id"], keep="first").copy()
    unique_rows["_row_number"] = unique_rows.index + 2

    valid_indices: list[int] = []
    excluded_indices: list[int] = []
    canceled_ids: list[str] = []
    invalid_numeric_ids: list[str] = []
    missing_city_ids: list[str] = []
    quality_issues: list[dict[str, Any]] = []

    for index, row in unique_rows.iterrows():
        booking_id = row["booking_id"] or "<空>"
        problems: list[str] = []

        if not row["city"]:
            problems.append("city 为空")
            missing_city_ids.append(booking_id)

        for column in NUMERIC_COLUMNS:
            value = parsed.at[index, column]
            if pd.isna(value):
                problems.append(f"{column} 不是有效数字")
                if booking_id not in invalid_numeric_ids:
                    invalid_numeric_ids.append(booking_id)
            elif value <= 0:
                problems.append(f"{column} 必须大于 0，当前为 {value:g}")
                if booking_id not in invalid_numeric_ids:
                    invalid_numeric_ids.append(booking_id)

        if problems:
            quality_issues.append(
                {
                    "row_number": int(unique_rows.at[index, "_row_number"]),
                    "booking_id": booking_id,
                    "problems": problems,
                }
            )

        canceled = row["canceled"].strip().lower() in {"yes", "true", "1"}
        if canceled:
            canceled_ids.append(booking_id)
            excluded_indices.append(index)
        elif problems:
            excluded_indices.append(index)
        else:
            valid_indices.append(index)

    valid_rows = unique_rows.loc[valid_indices].copy()
    valid_rows["total_price"] = parsed.loc[valid_indices, "total_price"].astype(float)
    total_revenue = float(valid_rows["total_price"].sum())
    average_order_value = (
        round(total_revenue / len(valid_rows), 2) if len(valid_rows) else 0.0
    )

    city_breakdown = []
    for city, group in valid_rows.groupby("city"):
        revenue = float(group["total_price"].sum())
        city_breakdown.append(
            {
                "city": str(city),
                "orders": int(len(group)),
                "revenue": round(revenue, 2),
                "average_order_value": round(revenue / len(group), 2),
            }
        )
    city_breakdown.sort(key=lambda item: (-item["orders"], item["city"]))

    outliers: list[dict[str, Any]] = []
    if len(valid_rows) >= 4:
        prices = valid_rows["total_price"].astype(float)
        q1 = float(prices.quantile(0.25))
        q3 = float(prices.quantile(0.75))
        iqr = q3 - q1
        upper_bound = q3 + 1.5 * iqr
        for _, row in valid_rows.loc[valid_rows["total_price"] > upper_bound].iterrows():
            outliers.append(
                {
                    "booking_id": row["booking_id"],
                    "city": row["city"],
                    "total_price": round(float(row["total_price"]), 2),
                    "upper_bound": round(upper_bound, 2),
                }
            )

    warnings: list[str] = []
    if duplicate_ids:
        warnings.append(f"发现重复订单号：{', '.join(duplicate_ids)}，统计时已去重")
    if missing_city_ids:
        warnings.append(f"城市为空：{', '.join(missing_city_ids)}")
    if invalid_numeric_ids:
        warnings.append(f"数值字段无效：{', '.join(invalid_numeric_ids)}")
    if canceled_ids:
        warnings.append(f"取消订单未计入有效订单：{', '.join(canceled_ids)}")
    if outliers:
        warnings.append(
            f"发现高客单价离群订单：{', '.join(item['booking_id'] for item in outliers)}"
        )

    return {
        "status": "ok",
        "source_file": path.name,
        "row_count": int(len(raw)),
        "unique_booking_count": int(len(unique_rows)),
        "valid_booking_count": int(len(valid_rows)),
        "excluded_booking_count": int(len(excluded_indices)),
        "canceled_booking_count": int(len(canceled_ids)),
        "duplicate_booking_ids": duplicate_ids,
        "missing_city_booking_ids": missing_city_ids,
        "invalid_numeric_booking_ids": invalid_numeric_ids,
        "canceled_booking_ids": canceled_ids,
        "total_revenue": round(total_revenue, 2),
        "average_order_value": average_order_value,
        "city_breakdown": city_breakdown,
        "price_outliers": outliers,
        "quality_issues": quality_issues,
        "warnings": warnings,
    }


def build_audit_tool(csv_path: Path) -> BaseTool:
    """把已信任的 CSV 路径闭包进工具，模型不能借参数读取任意文件。"""

    @tool("audit_booking_file")
    def audit_booking_file() -> str:
        """分析指定订单 CSV 的有效订单、收入、城市分布和数据质量问题。"""
        try:
            payload = _audit_bookings(csv_path)
        except (ValueError, OSError) as error:
            # 工具失败也返回可解析 JSON，让编排层能保留证据并明确停止，
            # 而不是让模型看到一段异常文本后自行猜测数据。
            payload = {
                "status": "error",
                "source_file": csv_path.name,
                "error": str(error),
            }
        return json.dumps(payload, ensure_ascii=False)

    return audit_booking_file


def message_text(message: AIMessage) -> str:
    """兼容 LangChain 1.x 的字符串或内容块正文，供输出解析器读取。"""
    if isinstance(message.content, str):
        return message.content
    parts = [
        block.get("text", "")
        for block in message.content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "".join(parts)


def select_and_execute_tool(
    model: ChatOpenAI,
    audit_tool: BaseTool,
    question: str,
) -> ToolSelection:
    """让模型选择工具，再由业务代码执行；工具结果不交给模型伪造。"""
    selection_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "你是 DataPilot 的数据分析调度器。你必须先调用 audit_booking_file "
                "取得真实统计，不能凭常识估算订单、收入或数据风险。",
            ),
            ("human", "{question}"),
        ]
    )
    bound_model = model.bind_tools([audit_tool], tool_choice="required")
    started = time.perf_counter()
    selection_message = (selection_prompt | bound_model).invoke({"question": question})
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    tool_calls = list(selection_message.tool_calls)
    if not tool_calls:
        raise ToolEvidenceError("模型没有按契约调用 audit_booking_file")
    if len(tool_calls) != 1:
        # 本课只允许一个审计动作；多出来的调用必须显式拒绝，避免静默忽略副作用。
        raise ToolEvidenceError(f"本课只允许一个工具调用，实际收到 {len(tool_calls)} 个")

    first_call = tool_calls[0]
    tool_name = str(first_call.get("name", ""))
    if tool_name != audit_tool.name:
        raise ToolEvidenceError(f"模型请求了未授权工具：{tool_name or '<空>'}")

    tool_started = time.perf_counter()
    result_text = audit_tool.invoke(first_call.get("args", {}))
    tool_elapsed_ms = round((time.perf_counter() - tool_started) * 1000, 1)
    return ToolSelection(
        tool_calls=tool_calls,
        tool_name=tool_name,
        result_text=str(result_text),
        elapsed_ms=round(elapsed_ms + tool_elapsed_ms, 1),
        usage=dict(selection_message.usage_metadata or {}),
    )


def build_narrative(
    model: ChatOpenAI,
    question: str,
    tool_payload: dict[str, Any],
) -> tuple[AnalysisNarrative, dict[str, Any], float]:
    """把工具证据转成通过 Pydantic 校验的业务结论。"""
    parser = PydanticOutputParser(pydantic_object=AnalysisNarrative)
    narrative_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "你是 DataPilot 的业务分析员。只能使用工具证据，不得补造数字。"
                "如果数据存在缺失、重复、非法值或高客单价，必须写入 data_risks。"
                "只返回符合以下格式说明的 JSON，不要 Markdown 代码围栏。\n"
                "{format_instructions}",
            ),
            (
                "human",
                "用户问题：{question}\n\n工具证据：\n{evidence}",
            ),
        ]
    )
    started = time.perf_counter()
    narrative_message = (narrative_prompt | model).invoke(
        {
            "question": question,
            "evidence": json.dumps(tool_payload, ensure_ascii=False, indent=2),
            "format_instructions": parser.get_format_instructions(),
        }
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        narrative = parser.parse(message_text(narrative_message))
    except Exception as error:
        # 解析失败必须转成显式业务错误；不能把半截 JSON 当成成功报告继续写入磁盘。
        raise NarrativeParseError(f"模型输出未通过结构校验：{error}") from error
    return narrative, dict(narrative_message.usage_metadata or {}), elapsed_ms


def public_error_message(error: Exception, api_key: str) -> str:
    """生成可写入报告的错误文本，并移除可能被 SDK 带出的密钥。"""
    message = f"{type(error).__name__}: {error}"
    if api_key:
        message = message.replace(api_key, "***REDACTED***")
    return message[:1000]


def write_report(path: Path, report: dict[str, Any]) -> None:
    """以 UTF-8 写入报告；调用方保证 report 中不存在明文密钥。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def run_pipeline(
    settings: ModelSettings,
    question: str,
    input_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """执行两次模型调用和一次本地工具，并返回成功或失败报告。"""
    started = time.perf_counter()
    report: dict[str, Any] = {
        "status": "running",
        "question": question,
        "source_file": input_path.name,
        "model": settings.public_view(),
        "orchestration": {"tool_calls": [], "tool_selection_usage": {}},
        "tool": {},
        "narrative": None,
        "usage": {},
        "checks": {},
        "error": None,
        "error_stage": None,
        "elapsed_ms": None,
    }

    try:
        model = build_chat_model(settings)
        audit_tool = build_audit_tool(input_path)
        selection = select_and_execute_tool(model, audit_tool, question)
        tool_payload = json.loads(selection.result_text)

        report["orchestration"] = {
            "tool_calls": selection.tool_calls,
            "tool_selection_usage": selection.usage,
            "tool_elapsed_ms": selection.elapsed_ms,
        }
        report["tool"] = {
            "name": selection.tool_name,
            "result": tool_payload,
        }

        # 工具没有证据时立即停止；第二次模型调用不能替代缺失的原始数据。
        if tool_payload.get("status") != "ok":
            raise ToolEvidenceError(tool_payload.get("error", "工具未返回可用数据"))

        narrative, narrative_usage, narrative_elapsed_ms = build_narrative(
            model,
            question,
            tool_payload,
        )
        report["narrative"] = narrative.model_dump()
        report["orchestration"]["narrative_elapsed_ms"] = narrative_elapsed_ms
        report["usage"] = {
            "tool_selection": selection.usage,
            "narrative": narrative_usage,
            "total_tokens": int(selection.usage.get("total_tokens", 0))
            + int(narrative_usage.get("total_tokens", 0)),
        }
        report["checks"] = {
            "tool_called": bool(selection.tool_calls),
            "tool_completed": tool_payload.get("status") == "ok",
            "metrics_present": isinstance(tool_payload.get("valid_booking_count"), int),
            "narrative_validated": isinstance(narrative, AnalysisNarrative),
        }
        report["status"] = "ok"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = public_error_message(error, settings.api_key)
        report["error_stage"] = getattr(error, "stage", "runtime")
    finally:
        report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)

    write_report(output_path, report)
    return report


def print_summary(report: dict[str, Any], output_path: Path) -> None:
    """把报告中的关键字段打印到终端，方便读者立即判断成功或失败。"""
    print(f"model: {report['model']['model']}")
    print(f"status: {report['status']}")
    if report["status"] != "ok":
        print(f"error_stage: {report['error_stage']}")
        print(f"error: {report['error']}")
        print(f"已保存：{output_path.name}")
        return

    calls = report["orchestration"]["tool_calls"]
    print(f"tool_calls: {', '.join(call['name'] for call in calls)}")
    metrics = report["tool"]["result"]
    print(f"valid_booking_count: {metrics['valid_booking_count']}")
    print(f"average_order_value: {metrics['average_order_value']}")
    print(f"total_revenue: {metrics['total_revenue']}")
    print(f"warnings: {len(metrics['warnings'])}")
    narrative = report["narrative"]
    print("\nsummary:")
    print(narrative["summary"])
    print("\nkey_findings:")
    for finding in narrative["key_findings"]:
        print(f"- {finding}")
    print("\ndata_risks:")
    for risk in narrative["data_risks"]:
        print(f"- {risk}")
    print("\nchecks:")
    for name, passed in report["checks"].items():
        print(f"- {name}: {passed}")
    print(f"\n已保存：{output_path.name}")


def main() -> None:
    """解析命令行，运行分析链，并用退出码区分成功和失败。"""
    parser = argparse.ArgumentParser(description="Run the DataPilot LangChain analysis demo")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="业务问题")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_FILE, help="订单 CSV 路径")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_REPORT_FILE,
        help="分析报告路径",
    )
    args = parser.parse_args()

    try:
        settings = read_model_settings()
    except ConfigurationError as error:
        print(f"配置错误：{error}")
        print(f"请检查 {PROJECT_DIR / '.env'}，模板见 .env.example")
        raise SystemExit(2) from error

    input_path = args.input.expanduser()
    if not input_path.is_absolute():
        input_path = (Path.cwd() / input_path).resolve()
    output_path = args.output.expanduser()
    if not output_path.is_absolute():
        output_path = (Path.cwd() / output_path).resolve()

    report = run_pipeline(settings, args.question, input_path, output_path)
    print_summary(report, output_path)
    if report["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
