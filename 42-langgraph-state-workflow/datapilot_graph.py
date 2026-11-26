"""用 LangGraph 编排可暂停、可恢复的 DataPilot 订单分析流程。

图中有三种结果：
1. 审计失败：直接写失败记录，不调用模型；
2. 低风险：模型生成草稿后自动完成；
3. 高风险：模型生成草稿后停在人工审批，审批结果写入 SQLite Checkpoint。

模型只负责生成分析草稿，审计指标、风险分级、审批权和最终导出仍由业务代码控制。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

import pandas as pd
from dotenv import load_dotenv
from langchain_core.messages import AIMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_FILE = PROJECT_DIR / "bookings.csv"
DEFAULT_CHECKPOINT_FILE = PROJECT_DIR / "checkpoint.sqlite"
DEFAULT_THREAD_ID = "demo-approval"
DEFAULT_QUESTION = "这批旅行订单的数据质量如何？能否导出经营分析报告？"

# .env 只在本地开发时补充进程环境变量；生产环境应由密钥管理系统注入。
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


class WorkflowError(RuntimeError):
    """命令行无法安全启动或恢复流程时抛出的业务错误。"""


class DataPilotState(TypedDict, total=False):
    """LangGraph 在节点之间传递的共享状态；每个节点只返回自己负责的字段。"""

    thread_id: str
    question: str
    input_path: str
    audit: dict[str, Any]
    draft: dict[str, Any]
    model_public: dict[str, Any]
    draft_usage: dict[str, Any]
    draft_elapsed_ms: float
    risk_level: Literal["low", "high", "blocked"]
    approval: dict[str, Any]
    final_status: str
    error: str
    report_path: str


class DraftAnalysis(BaseModel):
    """模型草稿必须满足的结构；缺字段时不会进入审批节点。"""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=15, max_length=600)
    key_findings: list[str] = Field(min_length=2, max_length=5)
    data_risks: list[str] = Field(min_length=1, max_length=6)
    approval_recommendation: Literal["approve", "reject"] = Field(
        description="基于数据风险给出的审批建议，最终决定权仍属于人工审批者"
    )
    confidence: Literal["high", "medium", "low"]


def read_model_settings() -> dict[str, Any]:
    """读取 DeepSeek 配置；密钥字段只留在当前调用内存中。"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise WorkflowError("没有配置 DEEPSEEK_API_KEY，请参考 .env.example")

    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise WorkflowError("DEEPSEEK_BASE_URL 必须以 https:// 或 http:// 开头")

    model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
    if not model:
        raise WorkflowError("DEEPSEEK_MODEL 不能为空")

    raw_timeout = os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "20").strip()
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError as error:
        raise WorkflowError("DEEPSEEK_TIMEOUT_SECONDS 必须是数字") from error
    if timeout_seconds <= 0:
        raise WorkflowError("DEEPSEEK_TIMEOUT_SECONDS 必须大于 0")

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "timeout_seconds": timeout_seconds,
    }


def public_model_view(settings: dict[str, Any]) -> dict[str, Any]:
    """生成可写进日志和报告的脱敏模型配置。"""
    return {
        "provider": "deepseek",
        "base_url": settings["base_url"],
        "model": settings["model"],
        "timeout_seconds": settings["timeout_seconds"],
        "api_key_configured": bool(settings["api_key"]),
    }


def build_chat_model(settings: dict[str, Any]) -> ChatOpenAI:
    """创建真实 DeepSeek 模型；重试交给外层流程决定，SDK 不隐藏失败。"""
    return ChatOpenAI(
        model=settings["model"],
        api_key=settings["api_key"],
        base_url=settings["base_url"],
        timeout=settings["timeout_seconds"],
        max_retries=0,
        temperature=0.1,
        max_tokens=700,
        # 思考内容会占用输出预算；本节点只需要稳定的 JSON 草稿。
        extra_body={"thinking": {"type": "disabled"}},
    )


def audit_bookings(path: Path) -> dict[str, Any]:
    """计算订单质量、收入和风险等级；任何统计数字都不能由模型补写。"""
    try:
        raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    except FileNotFoundError as error:
        raise WorkflowError(f"订单文件不存在：{path}") from error
    except pd.errors.EmptyDataError as error:
        raise WorkflowError("订单文件为空") from error

    raw.columns = [column.strip() for column in raw.columns]
    missing_columns = REQUIRED_COLUMNS.difference(raw.columns)
    if missing_columns:
        raise WorkflowError(f"订单文件缺少字段：{', '.join(sorted(missing_columns))}")

    # 先保留文本再转数值，避免缺失金额或 guests=x 被静默变成合法数字。
    raw = raw.apply(lambda column: column.str.strip())
    parsed = raw.copy()
    for column in NUMERIC_COLUMNS:
        parsed[column] = pd.to_numeric(parsed[column], errors="coerce")

    duplicate_ids = sorted(
        raw.loc[raw["booking_id"].duplicated(keep=False), "booking_id"].unique()
    )
    unique_rows = raw.drop_duplicates(subset=["booking_id"], keep="first").copy()

    valid_indices: list[int] = []
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

        canceled = row["canceled"].strip().lower() in {"yes", "true", "1"}
        if canceled:
            canceled_ids.append(booking_id)
        elif not problems:
            valid_indices.append(index)

        if problems:
            quality_issues.append(
                {"booking_id": booking_id, "problems": problems}
            )

    valid_rows = unique_rows.loc[valid_indices].copy()
    valid_rows["total_price"] = parsed.loc[valid_indices, "total_price"].astype(float)
    total_revenue = float(valid_rows["total_price"].sum())
    average_order_value = (
        round(total_revenue / len(valid_rows), 2) if len(valid_rows) else 0.0
    )

    city_breakdown = {
        str(city): int(count)
        for city, count in valid_rows["city"].value_counts().items()
    }

    warnings: list[str] = []
    if duplicate_ids:
        warnings.append(f"重复订单号：{', '.join(duplicate_ids)}")
    if missing_city_ids:
        warnings.append(f"城市为空：{', '.join(missing_city_ids)}")
    if invalid_numeric_ids:
        warnings.append(f"非法数值：{', '.join(invalid_numeric_ids)}")
    if canceled_ids:
        warnings.append(f"取消订单：{', '.join(canceled_ids)}")

    # 只要存在质量问题，就要求人工确认；真实项目可以把规则拆得更细。
    risk_level: Literal["low", "high"] = "high" if warnings else "low"
    return {
        "status": "ok",
        "source_file": path.name,
        "row_count": int(len(raw)),
        "unique_booking_count": int(len(unique_rows)),
        "valid_booking_count": int(len(valid_rows)),
        "total_revenue": round(total_revenue, 2),
        "average_order_value": average_order_value,
        "duplicate_booking_ids": duplicate_ids,
        "missing_city_booking_ids": missing_city_ids,
        "invalid_numeric_booking_ids": invalid_numeric_ids,
        "canceled_booking_ids": canceled_ids,
        "city_breakdown": city_breakdown,
        "quality_issues": quality_issues,
        "warnings": warnings,
        "risk_level": risk_level,
    }


def message_text(message: AIMessage) -> str:
    """兼容 LangChain 1.x 的字符串或内容块响应。"""
    if isinstance(message.content, str):
        return message.content
    return "".join(
        block.get("text", "")
        for block in message.content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def audit_node(state: DataPilotState) -> dict[str, Any]:
    """读取订单并计算审计证据；失败时只返回错误，不生成任何业务结论。"""
    try:
        audit = audit_bookings(Path(state["input_path"]))
    except (WorkflowError, OSError) as error:
        return {"error": str(error), "risk_level": "blocked"}
    return {"audit": audit, "risk_level": audit["risk_level"]}


def draft_node(state: DataPilotState) -> dict[str, Any]:
    """调用真实模型生成分析草稿，再把草稿交给条件边决定是否审批。"""
    started = time.perf_counter()
    try:
        settings = read_model_settings()
        model_public = public_model_view(settings)
        model = build_chat_model(settings)
    except (WorkflowError, ValueError) as error:
        return {"error": str(error), "risk_level": "blocked"}

    parser = PydanticOutputParser(pydantic_object=DraftAnalysis)
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "你是 DataPilot 的财务数据分析员。只能引用提供的审计 JSON，"
                "不得补造订单、金额或风险。只返回符合格式说明的 JSON。\n"
                "{format_instructions}",
            ),
            ("human", "分析问题：{question}\n\n审计结果：\n{audit_json}"),
        ]
    )

    try:
        message = (prompt | model).invoke(
            {
                "question": state["question"],
                "audit_json": json.dumps(state["audit"], ensure_ascii=False, indent=2),
                "format_instructions": parser.get_format_instructions(),
            }
        )
        draft = parser.parse(message_text(message))
    except Exception as error:
        # 模型或解析失败会走 failed 节点；不能把异常草稿留给审批人确认。
        return {
            "error": f"{type(error).__name__}: {str(error)[:600]}",
            "risk_level": "blocked",
            "model_public": model_public,
        }

    return {
        "draft": draft.model_dump(),
        "model_public": model_public,
        "draft_usage": dict(message.usage_metadata or {}),
        "draft_elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "risk_level": state["audit"]["risk_level"],
    }


def approval_node(state: DataPilotState) -> dict[str, Any]:
    """在高风险草稿后暂停，等待审批人提交 approve 或 reject。"""
    request = {
        "kind": "data_export_approval",
        "thread_id": state["thread_id"],
        "question": state["question"],
        "risk_level": state["risk_level"],
        "warnings": state["audit"]["warnings"],
        "valid_booking_count": state["audit"]["valid_booking_count"],
        "average_order_value": state["audit"]["average_order_value"],
        "draft": state["draft"],
    }
    # interrupt 会把执行权交还调用方；恢复时，下一次进入本节点会返回 resume 值。
    decision = interrupt(request)

    if not isinstance(decision, dict):
        # 非结构化恢复值按拒批处理，避免“格式错误反而放行”的权限漏洞。
        return {
            "approval": {
                "decision": "reject",
                "reviewer": "unknown",
                "feedback": "恢复值不是对象，已按安全默认值拒绝",
                "valid": False,
            }
        }
    if decision.get("decision") not in {"approve", "reject"}:
        return {
            "approval": {
                "decision": "reject",
                "reviewer": str(decision.get("reviewer", "unknown")),
                "feedback": "审批值只能是 approve 或 reject，已按安全默认值拒绝",
                "valid": False,
            }
        }

    return {
        "approval": {
            "decision": decision["decision"],
            "reviewer": str(decision.get("reviewer", "local-cli")),
            "feedback": str(decision.get("feedback", "")),
            "valid": True,
        }
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再替换，避免进程中断后留下半截 JSON 报告。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def report_path_for(thread_id: str) -> Path:
    """把线程编号限制为安全文件名，防止路径穿越覆盖其他文件。"""
    safe_thread_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", thread_id).strip("._")
    if not safe_thread_id:
        safe_thread_id = "default"
    return PROJECT_DIR / f"analysis-{safe_thread_id}.json"


def build_report(state: DataPilotState, status: str) -> dict[str, Any]:
    """从共享状态生成终态记录；不把 API Key 写进任何字段。"""
    return {
        "status": status,
        "thread_id": state["thread_id"],
        "generated_at": datetime.now(UTC).isoformat(),
        "question": state.get("question"),
        "source_file": state.get("audit", {}).get("source_file"),
        "model": state.get("model_public"),
        "audit": state.get("audit"),
        "draft": state.get("draft"),
        "draft_usage": state.get("draft_usage", {}),
        "draft_elapsed_ms": state.get("draft_elapsed_ms"),
        "risk_level": state.get("risk_level"),
        "approval": state.get("approval"),
        "error": state.get("error"),
    }


def finalize_node(state: DataPilotState) -> dict[str, Any]:
    """审批通过或低风险自动通过后，写入最终分析记录。"""
    status = "approved" if state.get("approval") else "auto_approved"
    path = report_path_for(state["thread_id"])
    atomic_write_json(path, build_report(state, status))
    return {"final_status": status, "report_path": str(path)}


def rejected_node(state: DataPilotState) -> dict[str, Any]:
    """人工拒绝后保留审计、草稿和反馈，便于重新整理口径后再运行。"""
    path = report_path_for(state["thread_id"])
    atomic_write_json(path, build_report(state, "rejected"))
    return {"final_status": "rejected", "report_path": str(path)}


def failed_node(state: DataPilotState) -> dict[str, Any]:
    """审计或模型失败后写入失败记录，不进入审批和最终导出。"""
    path = report_path_for(state["thread_id"])
    atomic_write_json(path, build_report(state, "failed"))
    return {"final_status": "failed", "report_path": str(path)}


def route_after_audit(state: DataPilotState) -> str:
    """审计没有证据时直接失败，否则允许生成模型草稿。"""
    return "failed" if state.get("risk_level") == "blocked" else "draft"


def route_after_draft(state: DataPilotState) -> str:
    """草稿生成失败走失败节点；高风险必须审批，低风险可自动完成。"""
    if state.get("risk_level") == "blocked":
        return "failed"
    if state.get("risk_level") == "high":
        return "approval"
    return "finalize"


def route_after_approval(state: DataPilotState) -> str:
    """只有明确 approve 才导出；reject 和无效值都走拒绝分支。"""
    approval = state.get("approval", {})
    return "finalize" if approval.get("decision") == "approve" else "reject"


def build_graph(checkpointer: SqliteSaver) -> Any:
    """把节点和条件边编译成 LangGraph；checkpointer 负责保存每次状态快照。"""
    builder = StateGraph(DataPilotState)
    builder.add_node("audit", audit_node)
    builder.add_node("draft", draft_node)
    builder.add_node("approval", approval_node)
    builder.add_node("finalize", finalize_node)
    builder.add_node("rejected", rejected_node)
    builder.add_node("failed", failed_node)

    builder.add_edge(START, "audit")
    builder.add_conditional_edges(
        "audit",
        route_after_audit,
        {"draft": "draft", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "draft",
        route_after_draft,
        {"approval": "approval", "finalize": "finalize", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "approval",
        route_after_approval,
        {"finalize": "finalize", "reject": "rejected"},
    )
    builder.add_edge("finalize", END)
    builder.add_edge("rejected", END)
    builder.add_edge("failed", END)
    return builder.compile(checkpointer=checkpointer)


def make_config(thread_id: str) -> dict[str, Any]:
    """thread_id 是 Checkpoint 的逻辑主键；不同用户的审批不能共享线程。"""
    return {"configurable": {"thread_id": thread_id}}


def create_checkpointer(db_path: Path) -> Any:
    """打开 SQLite 检查点库；文件会在首次运行时自动初始化。

    教学环境把状态保存在本地明文文件中；生产环境若含个人信息，
    必须改用加密存储、访问审计和明确的数据保留周期。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver.from_conn_string(str(db_path))


def print_state_summary(snapshot: Any, checkpoint_path: Path) -> None:
    """打印当前执行位置、审批请求和最终产物，方便命令行排错。"""
    values = snapshot.values
    print(f"thread_id: {values.get('thread_id', '<empty>')}")
    print(f"next: {snapshot.next}")
    print(f"final_status: {values.get('final_status')}")
    print(f"risk_level: {values.get('risk_level')}")
    if values.get("audit"):
        print(f"valid_booking_count: {values['audit']['valid_booking_count']}")
        print(f"average_order_value: {values['audit']['average_order_value']}")
    if values.get("draft"):
        print(f"draft_summary: {values['draft']['summary']}")
    if values.get("approval"):
        print(f"approval: {json.dumps(values['approval'], ensure_ascii=False)}")
    if values.get("report_path"):
        print(f"report_path: {values['report_path']}")
    print(f"checkpoint: {checkpoint_path}")


def start_workflow(
    checkpoint_path: Path,
    thread_id: str,
    question: str,
    input_path: Path,
) -> str:
    """启动新线程；已结束的线程会清理旧 Checkpoint 后重新开始。"""
    config = make_config(thread_id)
    with create_checkpointer(checkpoint_path) as saver:
        graph = build_graph(saver)
        existing = graph.get_state(config)
        if existing.values:
            if existing.next:
                raise WorkflowError(
                    f"线程 {thread_id} 正在等待审批（next={existing.next}），"
                    "请使用 --resume，或换一个线程编号重新开始"
                )
            # 已结束的线程没有可恢复的工作，直接清理旧状态，保证教程命令可以重复执行。
            print(f"检测到线程 {thread_id} 的旧 Checkpoint 已结束，清理后重新开始。")
            saver.delete_thread(thread_id)

        graph.invoke(
            {
                "thread_id": thread_id,
                "question": question,
                "input_path": str(input_path.resolve()),
            },
            config,
        )
        snapshot = graph.get_state(config)
        print_state_summary(snapshot, checkpoint_path)
        if snapshot.interrupts:
            request = snapshot.interrupts[0].value
            print("approval_request:")
            print(json.dumps(request, ensure_ascii=False, indent=2))
            print("status: waiting_approval")
            return "waiting_approval"
        else:
            print("status: finished")
            return str(snapshot.values.get("final_status", "finished"))


def show_status(checkpoint_path: Path, thread_id: str) -> None:
    """读取任意进程留下的 Checkpoint，不执行节点，也不产生模型费用。"""
    config = make_config(thread_id)
    with create_checkpointer(checkpoint_path) as saver:
        graph = build_graph(saver)
        snapshot = graph.get_state(config)
        if not snapshot.values:
            raise WorkflowError(f"线程 {thread_id} 没有 Checkpoint")
        print_state_summary(snapshot, checkpoint_path)


def resume_workflow(
    checkpoint_path: Path,
    thread_id: str,
    decision: str,
    feedback: str,
) -> None:
    """向停住的 approval 节点提交审批结果，并从同一个 Checkpoint 继续。"""
    config = make_config(thread_id)
    with create_checkpointer(checkpoint_path) as saver:
        graph = build_graph(saver)
        snapshot = graph.get_state(config)
        if not snapshot.values:
            raise WorkflowError(f"线程 {thread_id} 没有 Checkpoint")
        if "approval" not in snapshot.next:
            raise WorkflowError(
                f"线程 {thread_id} 当前没有等待审批，next={snapshot.next}"
            )

        # 教学 CLI 假定审批人就是本机操作者；生产环境必须从登录会话或审批网关
        # 注入真实 reviewer 身份，不能相信调用方在请求里自报的角色。
        graph.invoke(
            Command(
                resume={
                    "decision": decision,
                    "feedback": feedback,
                    "reviewer": "local-cli",
                }
            ),
            config,
        )
        final_snapshot = graph.get_state(config)
        print("resume_result:")
        print_state_summary(final_snapshot, checkpoint_path)


def main() -> None:
    """解析命令行并执行 start、status 或 resume；退出码可用于自动化脚本。"""
    parser = argparse.ArgumentParser(description="Run the DataPilot LangGraph workflow")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--start", action="store_true", help="启动新的分析线程")
    action.add_argument("--status", action="store_true", help="只查看 Checkpoint")
    action.add_argument(
        "--resume",
        choices=("approve", "reject"),
        help="恢复等待中的审批节点",
    )
    parser.add_argument("--thread-id", default=DEFAULT_THREAD_ID)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_FILE)
    parser.add_argument("--db", type=Path, default=DEFAULT_CHECKPOINT_FILE)
    parser.add_argument("--feedback", default="", help="拒绝或批准时的说明")
    args = parser.parse_args()

    input_path = args.input.expanduser()
    if not input_path.is_absolute():
        input_path = (Path.cwd() / input_path).resolve()
    db_path = args.db.expanduser()
    if not db_path.is_absolute():
        db_path = (Path.cwd() / db_path).resolve()

    try:
        if args.start:
            status = start_workflow(db_path, args.thread_id, args.question, input_path)
            if status == "failed":
                raise SystemExit(1)
        elif args.status:
            show_status(db_path, args.thread_id)
        else:
            resume_workflow(db_path, args.thread_id, args.resume, args.feedback)
    except WorkflowError as error:
        print(f"流程错误：{error}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
