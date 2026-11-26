"""四个框架共享的数据契约。

FastAPI、LangGraph、LangChain 和 LlamaIndex 不直接传彼此的私有对象，
而是只交换这里定义的请求、证据、草稿和报告结构。这样替换任意一层时，
其他层仍然能按同一份契约工作。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class TripRequest(BaseModel):
    """客户端唯一允许提交的旅行规划参数。"""

    # extra="forbid" 让拼错的字段直接返回 422，而不是被静默忽略。
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(
        min_length=1,
        max_length=64,
        description="发起规划的用户标识；生产环境应由登录会话注入",
    )
    prompt: str = Field(
        min_length=4,
        max_length=300,
        description="用户的自然语言旅行需求",
        examples=["周末去杭州两天，喜欢拍照和美食，节奏轻松，总预算 3000 元"],
    )
    destination: str = Field(
        default="杭州",
        min_length=1,
        max_length=40,
        description="目的地；检索层只查询有资料覆盖的城市",
    )
    days: int = Field(ge=1, le=7, description="行程天数，本课限制为 1-7 天")
    budget: int = Field(ge=500, le=100000, description="总预算，单位为人民币元")
    pace: Literal["轻松", "适中", "紧凑"] = Field(
        default="适中",
        description="行程节奏；紧凑节奏会参与风险判断",
    )
    interests: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="兴趣标签，例如拍照、美食、博物馆",
    )


class EvidenceCard(BaseModel):
    """LlamaIndex 检索层返回的一条可引用证据。"""

    citation: str = Field(description="稳定引用编号，例如 hangzhou-guide#02")
    title: str = Field(description="证据所属文档标题")
    content: str = Field(description="去掉标题后的证据正文")
    score: float = Field(description="相似度分数；低于阈值的证据不会进入摘要")


class DayPlan(BaseModel):
    """模型生成的单日行程草稿。"""

    day: int = Field(ge=1, le=7)
    theme: str = Field(min_length=2, max_length=40)
    activities: list[str] = Field(min_length=2, max_length=5)
    # 每一天至少引用一条证据，避免模型写出没有资料支撑的行程。
    evidence: list[str] = Field(min_length=1)


class TripDraft(BaseModel):
    """LangChain 解析后的结构化草稿；预算数字不在这里生成。"""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=15, max_length=600)
    days: list[DayPlan] = Field(min_length=1, max_length=7)
    citations: list[str] = Field(min_length=1, max_length=12)
    confidence: Literal["high", "medium", "low"]


class BudgetBreakdown(BaseModel):
    """由 Python 计算的确定性预算，避免让模型做算术。"""

    transport: int = Field(ge=0)
    hotel: int = Field(ge=0)
    food: int = Field(ge=0)
    tickets: int = Field(ge=0)
    total: int = Field(ge=0)


class RiskAssessment(BaseModel):
    """预算、节奏和证据置信度共同决定是否需要人工审批。"""

    level: Literal["low", "high"]
    reasons: list[str] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    """审批接口唯一接受的请求体。"""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    feedback: str = Field(default="", max_length=300)


class RunView(BaseModel):
    """FastAPI 返回给客户端的运行视图。"""

    thread_id: str
    status: str
    next: list[str] = Field(default_factory=list)
    risk_level: str | None = None
    risk_reasons: list[str] = Field(default_factory=list)
    approval: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    error: str | None = None


class TripState(TypedDict, total=False):
    """LangGraph 节点之间传递的共享状态。"""

    thread_id: str
    request: dict[str, Any]
    evidence: list[dict[str, Any]]
    draft: dict[str, Any]
    budget: dict[str, Any]
    risk_level: Literal["low", "high"]
    risk_reasons: list[str]
    draft_elapsed_ms: float
    approval: dict[str, Any]
    final_status: str
    report: dict[str, Any]
    error: str
