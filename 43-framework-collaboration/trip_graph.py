"""LangGraph + LangChain 层：状态、草稿、风险、审批和报告。

这一层只做三件事：
1. LangGraph 决定流程走到哪里；
2. LangChain 把模型输出解析成 TripDraft；
3. Python 负责预算和风险判断，模型不能替代业务规则。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from langchain_core.messages import AIMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from models import (
    ApprovalDecision,
    BudgetBreakdown,
    EvidenceCard,
    RiskAssessment,
    RunView,
    TripDraft,
    TripRequest,
    TripState,
)
from travel_knowledge import TravelKnowledgeBase


PROJECT_DIR = Path(__file__).resolve().parent
DOCS_DIR = PROJECT_DIR / "docs"
RUNS_DIR = PROJECT_DIR / "runs"

# .env 只用于本地开发；容器和 CI 可以用真实环境变量覆盖。
load_dotenv(PROJECT_DIR / ".env")


class FrameworkError(RuntimeError):
    """本课所有可预期错误的基类，方便 FastAPI 映射状态码。"""


class ModelConfigurationError(FrameworkError):
    """模型配置缺失或格式错误；调用方应修正 .env 后重试。"""


class DraftValidationError(FrameworkError):
    """模型草稿引用了不存在的证据，或天数、预算结构不合法。"""


class RunNotFoundError(FrameworkError):
    """请求的 thread_id 没有对应 Checkpoint。"""


class WorkflowStateError(FrameworkError):
    """当前线程状态不允许执行请求的动作，例如已完成还要审批。"""


@dataclass(frozen=True)
class ModelSettings:
    """DeepSeek 配置；密钥字段不进入 repr，避免调试输出泄露。"""

    api_key: str = field(repr=False)
    base_url: str
    model: str
    timeout_seconds: float

    def public_view(self) -> dict[str, Any]:
        """返回可安全写入 /health 和报告的脱敏配置。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "api_key_configured": bool(self.api_key),
        }


def read_model_settings() -> ModelSettings:
    """读取并校验真实模型配置；缺失密钥时在启动阶段就失败。"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ModelConfigurationError(
            "没有配置 DEEPSEEK_API_KEY，请参考 .env.example 创建 .env"
        )

    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    base_url = base_url.rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise ModelConfigurationError(
            "DEEPSEEK_BASE_URL 必须以 https:// 或 http:// 开头"
        )

    model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
    if not model:
        raise ModelConfigurationError("DEEPSEEK_MODEL 不能为空")

    raw_timeout = os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "30").strip()
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError as error:
        raise ModelConfigurationError("DEEPSEEK_TIMEOUT_SECONDS 必须是数字") from error
    if timeout_seconds <= 0:
        raise ModelConfigurationError("DEEPSEEK_TIMEOUT_SECONDS 必须大于 0")

    return ModelSettings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
    )


def build_chat_model(settings: ModelSettings) -> ChatOpenAI:
    """创建 DeepSeek 的 OpenAI 兼容客户端；重试交给外层服务决定。"""
    return ChatOpenAI(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout=settings.timeout_seconds,
        max_retries=0,
        temperature=0.1,
        max_tokens=1400,
        # DeepSeek 的思考内容会占用输出预算；本课只需要结构化 JSON 草稿。
        extra_body={"thinking": {"type": "disabled"}},
    )


def message_text(message: AIMessage) -> str:
    """兼容 LangChain 1.x 的字符串和内容块两种响应格式。"""
    if isinstance(message.content, str):
        return message.content
    return "".join(
        block.get("text", "")
        for block in message.content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def utc_now() -> str:
    """生成带时区的 ISO 时间，报告里不写本地时区猜测值。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def safe_thread_id(thread_id: str) -> str:
    """把线程编号限制为安全文件名，防止路径穿越。"""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", thread_id).strip("_")
    if not safe:
        raise FrameworkError("thread_id 不能为空")
    return safe


def report_path(thread_id: str) -> Path:
    """所有运行报告写入 runs/，文件名由服务端生成的 thread_id 决定。"""
    return RUNS_DIR / f"{safe_thread_id(thread_id)}.json"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再替换，避免进程退出留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


def build_budget(request: TripRequest) -> BudgetBreakdown:
    """用固定单价表计算预算；模型不参与任何加减乘除。"""
    rates = {
        "轻松": {"hotel": 400, "food": 180, "transport": 80, "tickets": 120},
        "适中": {"hotel": 300, "food": 140, "transport": 60, "tickets": 80},
        "紧凑": {"hotel": 220, "food": 100, "transport": 40, "tickets": 50},
    }[request.pace]

    # 两天一夜按 1 晚计算；单日行程也保留 1 晚，避免出现 0 元住宿的失真结果。
    nights = max(request.days - 1, 1)
    transport = rates["transport"] * request.days
    hotel = rates["hotel"] * nights
    food = rates["food"] * request.days
    tickets = rates["tickets"] * request.days

    return BudgetBreakdown(
        transport=transport,
        hotel=hotel,
        food=food,
        tickets=tickets,
        total=transport + hotel + food + tickets,
    )


def assess_risk(
    request: TripRequest,
    budget: BudgetBreakdown,
    draft: TripDraft,
) -> RiskAssessment:
    """把预算、节奏和置信度转成可解释的风险等级。"""
    reasons: list[str] = []

    if budget.total > request.budget:
        reasons.append(
            f"预计花费 {budget.total} 元，超过预算 {request.budget} 元"
        )
    if request.pace == "紧凑" and request.days >= 3:
        reasons.append("3 天以上紧凑行程容易疲劳，需要人工确认")
    if draft.confidence == "low":
        reasons.append("模型对证据覆盖度的置信度为 low")

    return RiskAssessment(
        level="high" if reasons else "low",
        reasons=reasons,
    )


def validate_draft(
    draft: TripDraft,
    evidence: list[EvidenceCard],
    request: TripRequest,
) -> None:
    """拒绝引用不存在证据、天数不符或证据为空的草稿。"""
    evidence_ids = {card.citation for card in evidence}
    cited_ids = set(draft.citations)
    unknown = cited_ids - evidence_ids
    if unknown:
        raise DraftValidationError(
            f"草稿引用了不存在的证据：{', '.join(sorted(unknown))}"
        )

    expected_days = list(range(1, request.days + 1))
    actual_days = sorted(day.day for day in draft.days)
    if actual_days != expected_days:
        raise DraftValidationError(
            f"草稿天数必须是 {expected_days}，实际是 {actual_days}"
        )

    for day in draft.days:
        if not day.activities:
            raise DraftValidationError(f"第 {day.day} 天没有活动")
        unknown_day_evidence = set(day.evidence) - evidence_ids
        if unknown_day_evidence:
            raise DraftValidationError(
                f"第 {day.day} 天引用了不存在的证据："
                f"{', '.join(sorted(unknown_day_evidence))}"
            )


def build_report(
    state: TripState,
    status: str,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    """把图状态整理成可下载、可审计的最终报告。"""
    request = state.get("request", {})
    return {
        "thread_id": state.get("thread_id"),
        "status": status,
        "generated_at": utc_now(),
        "request": request,
        "evidence": state.get("evidence", []),
        "draft": state.get("draft"),
        "draft_elapsed_ms": state.get("draft_elapsed_ms"),
        "budget": state.get("budget"),
        "risk": {
            "level": state.get("risk_level"),
            "reasons": state.get("risk_reasons", []),
        },
        "approval": state.get("approval"),
        "error": error,
    }


class TripWorkflow:
    """把 LangGraph 的状态流封装成 FastAPI 可调用的三个动作。"""

    def __init__(
        self,
        knowledge: TravelKnowledgeBase,
        chain: Runnable,
        parser: PydanticOutputParser,
    ) -> None:
        self.knowledge = knowledge
        self.chain = chain
        self.parser = parser
        self.graph = None

    def compile(self, checkpointer: Any):
        """编译状态图；Checkpointer 由调用方决定用 SQLite 还是其他存储。"""
        builder = StateGraph(TripState)
        builder.add_node("retrieve", self.retrieve_node)
        builder.add_node("draft", self.draft_node)
        builder.add_node("assess", self.assess_node)
        builder.add_node("approval", self.approval_node)
        builder.add_node("finalize", self.finalize_node)
        builder.add_node("rejected", self.rejected_node)
        builder.add_node("failed", self.failed_node)

        builder.add_edge(START, "retrieve")
        builder.add_conditional_edges(
            "retrieve",
            self.route_after_retrieve,
            {"draft": "draft", "failed": "failed"},
        )
        builder.add_conditional_edges(
            "draft",
            self.route_after_draft,
            {"assess": "assess", "failed": "failed"},
        )
        builder.add_conditional_edges(
            "assess",
            self.route_after_assess,
            {"approval": "approval", "finalize": "finalize"},
        )
        builder.add_conditional_edges(
            "approval",
            self.route_after_approval,
            {"finalize": "finalize", "rejected": "rejected"},
        )
        builder.add_edge("finalize", END)
        builder.add_edge("rejected", END)
        builder.add_edge("failed", END)

        self.graph = builder.compile(checkpointer=checkpointer)
        return self.graph

    def start(self, thread_id: str, request: TripRequest) -> RunView:
        """启动一条新线程，并立即返回当前状态。"""
        if self.graph is None:
            raise WorkflowStateError("状态图尚未编译")
        config = {"configurable": {"thread_id": thread_id}}
        self.graph.invoke(
            {"thread_id": thread_id, "request": request.model_dump()},
            config,
        )
        return self.view(thread_id)

    def view(self, thread_id: str) -> RunView:
        """读取线程快照，不执行节点，也不产生模型费用。"""
        if self.graph is None:
            raise WorkflowStateError("状态图尚未编译")
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = self.graph.get_state(config)
        values = snapshot.values or {}
        if not values:
            raise RunNotFoundError(f"线程 {thread_id} 没有 Checkpoint")

        approval = snapshot.interrupts[0].value if snapshot.interrupts else None
        if approval is not None:
            status = "waiting_approval"
        else:
            status = str(values.get("final_status", "running"))

        return RunView(
            thread_id=thread_id,
            status=status,
            next=list(snapshot.next),
            risk_level=values.get("risk_level"),
            risk_reasons=values.get("risk_reasons", []),
            approval=approval,
            report=values.get("report"),
            error=values.get("error"),
        )

    def resume(
        self,
        thread_id: str,
        decision: ApprovalDecision,
        reviewer: str = "api-client",
    ) -> RunView:
        """向等待中的审批节点提交决定，并返回恢复后的状态。"""
        if self.graph is None:
            raise WorkflowStateError("状态图尚未编译")
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = self.graph.get_state(config)
        if not snapshot.values:
            raise RunNotFoundError(f"线程 {thread_id} 没有 Checkpoint")
        if "approval" not in snapshot.next:
            raise WorkflowStateError(
                f"线程 {thread_id} 当前没有等待审批，next={snapshot.next}"
            )

        self.graph.invoke(
            Command(
                resume={
                    "decision": decision.decision,
                    "feedback": decision.feedback,
                    # 生产环境必须从登录会话或审批网关注入真实身份，不能相信请求体。
                    "reviewer": reviewer,
                }
            ),
            config,
        )
        return self.view(thread_id)

    def retrieve_node(self, state: TripState) -> dict[str, Any]:
        """LangGraph 节点：调用 LlamaIndex 检索，失败时直接进入 failed。"""
        request = TripRequest.model_validate(state["request"])
        query = (
            f"{request.prompt} 兴趣：{'、'.join(request.interests)} "
            f"节奏：{request.pace}"
        )
        try:
            cards = self.knowledge.retrieve(query, request.destination)
        except Exception as error:
            return {"error": f"检索失败：{type(error).__name__}: {error}"}

        if not cards:
            # 没有证据时不让模型自由发挥，直接由业务层拒绝生成行程。
            return {"error": "没有检索到足够可靠的旅行证据"}

        return {"evidence": [card.model_dump() for card in cards]}

    def draft_node(self, state: TripState) -> dict[str, Any]:
        """LangGraph 节点：调用 LangChain 链生成结构化行程草稿。"""
        started = time.perf_counter()
        try:
            request = TripRequest.model_validate(state["request"])
            evidence = [
                EvidenceCard.model_validate(card) for card in state["evidence"]
            ]
            message = self.chain.invoke(
                {
                    "request_json": json.dumps(
                        request.model_dump(), ensure_ascii=False, indent=2
                    ),
                    "evidence_json": json.dumps(
                        [card.model_dump() for card in evidence],
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "format_instructions": self.parser.get_format_instructions(),
                }
            )
            draft = self.parser.parse(message_text(message))
            validate_draft(draft, evidence, request)
        except Exception as error:
            # 模型、解析或引用校验失败都不能进入审批，否则审批人会被错误草稿误导。
            return {
                "error": f"{type(error).__name__}: {str(error)[:600]}",
                "draft_elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }

        return {
            "draft": draft.model_dump(),
            "draft_elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def assess_node(self, state: TripState) -> dict[str, Any]:
        """LangGraph 节点：用确定性规则计算预算和审批风险。"""
        request = TripRequest.model_validate(state["request"])
        draft = TripDraft.model_validate(state["draft"])
        budget = build_budget(request)
        risk = assess_risk(request, budget, draft)
        return {
            "budget": budget.model_dump(),
            "risk_level": risk.level,
            "risk_reasons": risk.reasons,
        }

    def approval_node(self, state: TripState) -> dict[str, Any]:
        """LangGraph 节点：高风险行程在写报告前暂停，等待人工决定。"""
        request = TripRequest.model_validate(state["request"])
        approval_request = {
            "kind": "trip_plan_approval",
            "thread_id": state["thread_id"],
            "prompt": request.prompt,
            "risk_level": state["risk_level"],
            "risk_reasons": state.get("risk_reasons", []),
            "budget": state.get("budget"),
            "draft": state.get("draft"),
        }

        # interrupt 必须在任何写文件、发通知或扣费动作之前调用；
        # 恢复时节点会从头执行，提前产生的副作用会被重复执行。
        decision = interrupt(approval_request)
        if not isinstance(decision, dict):
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
                "reviewer": str(decision.get("reviewer", "api-client")),
                "feedback": str(decision.get("feedback", "")),
                "valid": True,
            }
        }

    def finalize_node(self, state: TripState) -> dict[str, Any]:
        """LangGraph 节点：写入通过报告；自动通过和人工通过都落到这里。"""
        approval = state.get("approval")
        status = "approved" if approval else "auto_approved"
        report = build_report(state, status)
        atomic_write_json(report_path(state["thread_id"]), report)
        return {"final_status": status, "report": report}

    def rejected_node(self, state: TripState) -> dict[str, Any]:
        """LangGraph 节点：写入拒绝报告，不生成可执行的最终行程。"""
        report = build_report(state, "rejected")
        atomic_write_json(report_path(state["thread_id"]), report)
        return {"final_status": "rejected", "report": report}

    def failed_node(self, state: TripState) -> dict[str, Any]:
        """LangGraph 节点：写入失败报告，保留错误供调用方定位。"""
        report = build_report(
            state,
            "failed",
            error=state.get("error", "未知错误"),
        )
        atomic_write_json(report_path(state["thread_id"]), report)
        return {
            "final_status": "failed",
            "report": report,
            "error": state.get("error", "未知错误"),
        }

    @staticmethod
    def route_after_retrieve(state: TripState) -> Literal["draft", "failed"]:
        """检索失败时不调用模型。"""
        return "failed" if state.get("error") else "draft"

    @staticmethod
    def route_after_draft(state: TripState) -> Literal["assess", "failed"]:
        """草稿解析失败时不进入预算和审批。"""
        return "failed" if state.get("error") else "assess"

    @staticmethod
    def route_after_assess(state: TripState) -> Literal["approval", "finalize"]:
        """只有高风险才进入人工审批。"""
        return "approval" if state.get("risk_level") == "high" else "finalize"

    @staticmethod
    def route_after_approval(state: TripState) -> Literal["finalize", "rejected"]:
        """审批值只有明确 approve 才写通过报告。"""
        approval = state.get("approval", {})
        return "finalize" if approval.get("decision") == "approve" else "rejected"


def build_draft_chain(model: Runnable) -> tuple[Runnable, PydanticOutputParser]:
    """组装 LangChain 提示词和 Pydantic 解析器。"""
    parser = PydanticOutputParser(pydantic_object=TripDraft)
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "你是 TripPilot 的旅行规划员。只能使用提供的证据，不得补造景点、"
                "交通、价格或营业时间。每一天至少引用一条证据，citation 必须来自"
                "证据列表。预算由业务系统计算，你不要输出预算数字。只返回符合格式"
                "说明的 JSON。\n{format_instructions}",
            ),
            (
                "human",
                "用户需求：\n{request_json}\n\n可用证据：\n{evidence_json}",
            ),
        ]
    )
    return prompt | model, parser
