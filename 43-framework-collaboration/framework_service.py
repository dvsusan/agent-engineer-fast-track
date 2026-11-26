"""FastAPI 服务层：把四个框架组装成一条可调用的 TripPilot 链路。

职责边界：
- FastAPI：HTTP、参数校验、状态码、OpenAPI；
- LangGraph：流程状态、条件边、审批中断、Checkpoint；
- LangChain：提示词、DeepSeek 调用、Pydantic 解析；
- LlamaIndex：资料加载、切分、向量检索。
"""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel

from models import ApprovalDecision, RunView, TripRequest
from travel_knowledge import TravelKnowledgeBase
from trip_graph import (
    DOCS_DIR,
    FrameworkError,
    RunNotFoundError,
    TripWorkflow,
    WorkflowStateError,
    atomic_write_json,
    build_chat_model,
    build_draft_chain,
    read_model_settings,
)


PROJECT_DIR = Path(__file__).resolve().parent
CHECKPOINT_FILE = PROJECT_DIR / "checkpoint.sqlite"
REPORT_FILE = PROJECT_DIR / "framework-report.json"


# 这张表就是本课的“职责边界图”数据源；/architecture 会原样返回。
FRAMEWORK_BOUNDARIES = [
    {
        "framework": "FastAPI",
        "responsibility": "HTTP 路由、Pydantic 参数校验、状态码、OpenAPI 文档",
        "not_responsible": "不负责模型推理、检索算法、状态恢复和审批规则",
        "lesson_evidence": "POST /runs、GET /runs/{thread_id}、POST /runs/{thread_id}/approval",
    },
    {
        "framework": "LangGraph",
        "responsibility": "状态图、条件边、interrupt 审批、SQLite Checkpoint 恢复",
        "not_responsible": "不负责提示词细节，也不替业务计算预算",
        "lesson_evidence": "retrieve → draft → assess → approval → finalize",
    },
    {
        "framework": "LangChain",
        "responsibility": "ChatPromptTemplate、DeepSeek 模型调用、PydanticOutputParser",
        "not_responsible": "不负责 HTTP、检索索引和跨进程状态持久化",
        "lesson_evidence": "build_draft_chain() 与 TripDraft 解析",
    },
    {
        "framework": "LlamaIndex",
        "responsibility": "文档加载、Markdown 切分、向量化、相似度检索与引用编号",
        "not_responsible": "不负责最终行程决策、预算和审批",
        "lesson_evidence": "TravelKnowledgeBase.retrieve()",
    },
]


# 迁移对照表把第 28-42 课的手写能力和本课框架能力一一对应。
MIGRATION_TABLE = [
    {
        "handwritten": "第 28 课手写加载、切分、Embedding 和召回",
        "framework": "LlamaIndex SimpleDirectoryReader + MarkdownNodeParser + VectorStoreIndex",
        "kept": "引用编号、相似度阈值、无证据拒答",
    },
    {
        "handwritten": "第 41 课 LangChain 链式调用模型和解析器",
        "framework": "ChatPromptTemplate | ChatOpenAI | PydanticOutputParser",
        "kept": "结构化输出、字段校验、错误可见",
    },
    {
        "handwritten": "第 22 课任务状态机与第 42 课 LangGraph 状态图",
        "framework": "StateGraph + 条件边 + interrupt + SqliteSaver",
        "kept": "暂停、恢复、审批和跨进程 Checkpoint",
    },
    {
        "handwritten": "第 37-38 课手写 FastAPI 路由与任务接口",
        "framework": "FastAPI + Pydantic 请求/响应模型",
        "kept": "参数校验、错误状态码和 OpenAPI 文档",
    },
    {
        "handwritten": "第 33 课 ACL 与权限前置过滤",
        "framework": "本课只保留接口边界，ACL 仍应由业务层在检索前执行",
        "kept": "框架不会自动替你完成权限过滤",
    },
]


class HealthResponse(BaseModel):
    """服务健康检查的返回结构。"""

    status: str
    layers: dict[str, str]
    model: dict[str, Any]
    documents: int
    checkpoint: str


class ArchitectureResponse(BaseModel):
    """职责边界图和迁移对照表。"""

    boundaries: list[dict[str, str]]
    migration: list[dict[str, str]]


def create_checkpointer(path: Path):
    """打开 SQLite Checkpoint；FastAPI 生命周期结束时由 ExitStack 关闭。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver.from_conn_string(str(path))


def create_app() -> FastAPI:
    """创建 FastAPI 应用；工厂函数让测试可以独立启动生命周期。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """启动时建索引、建图；关闭时释放 SQLite 连接。"""
        stack = ExitStack()
        try:
            # 配置错误在启动阶段暴露，避免服务已经监听端口却每次请求才失败。
            settings = read_model_settings()
            model = build_chat_model(settings)
            chain, parser = build_draft_chain(model)
            knowledge = TravelKnowledgeBase(DOCS_DIR)

            saver = stack.enter_context(create_checkpointer(CHECKPOINT_FILE))
            workflow = TripWorkflow(knowledge, chain, parser)
            workflow.compile(saver)

            app.state.workflow = workflow
            app.state.lock = threading.Lock()
            app.state.model_public = settings.public_view()
            yield
        finally:
            # 即使索引或图编译失败，也要关闭已经打开的 SQLite 连接。
            stack.close()

    app = FastAPI(
        title="TripPilot Framework Service",
        version="0.1.0",
        description=(
            "第 43 课：FastAPI 负责接口，LangGraph 负责状态，"
            "LangChain 负责模型草稿，LlamaIndex 负责证据检索。"
        ),
        lifespan=lifespan,
    )

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> dict[str, Any]:
        """检查四层是否已经装配完成。"""
        if not hasattr(app.state, "workflow"):
            raise HTTPException(status_code=503, detail="服务尚未初始化")
        return {
            "status": "ok",
            "layers": {
                "api": "FastAPI",
                "workflow": "LangGraph",
                "model": "LangChain",
                "retrieval": "LlamaIndex",
            },
            "model": app.state.model_public,
            "documents": len(list(DOCS_DIR.glob("*.md"))),
            "checkpoint": CHECKPOINT_FILE.name,
        }

    @app.get(
        "/architecture",
        response_model=ArchitectureResponse,
        tags=["system"],
    )
    def architecture() -> dict[str, Any]:
        """返回职责边界图和迁移对照表，方便前端或文档直接展示。"""
        return {
            "boundaries": FRAMEWORK_BOUNDARIES,
            "migration": MIGRATION_TABLE,
        }

    @app.post("/runs", response_model=RunView, status_code=201, tags=["runs"])
    def start_run(request: TripRequest) -> RunView:
        """启动一条新线程；高风险行程会返回 waiting_approval。"""
        thread_id = f"trip-{uuid.uuid4().hex[:10]}"
        try:
            # 本地 SQLite Checkpoint 不适合高并发写入；教学服务用一把锁串行化图执行。
            with app.state.lock:
                return app.state.workflow.start(thread_id, request)
        except (FrameworkError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/runs/{thread_id}", response_model=RunView, tags=["runs"])
    def get_run(thread_id: str) -> RunView:
        """读取线程状态，不重新执行节点。"""
        try:
            with app.state.lock:
                return app.state.workflow.view(thread_id)
        except RunNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post(
        "/runs/{thread_id}/approval",
        response_model=RunView,
        tags=["runs"],
    )
    def approve_run(thread_id: str, decision: ApprovalDecision) -> RunView:
        """提交审批决定；只有等待审批的线程才允许恢复。"""
        try:
            with app.state.lock:
                return app.state.workflow.resume(thread_id, decision)
        except RunNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except WorkflowStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/runs/{thread_id}/report", tags=["runs"])
    def get_report(thread_id: str) -> dict[str, Any]:
        """返回最终报告；没有完成或已拒绝的线程不会伪造通过报告。"""
        try:
            with app.state.lock:
                view = app.state.workflow.view(thread_id)
        except RunNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

        if view.report is None:
            raise HTTPException(
                status_code=409,
                detail=f"线程 {thread_id} 还没有最终报告，当前状态：{view.status}",
            )
        return view.report

    return app


app = create_app()


LOW_RISK_REQUEST = {
    "user_id": "alice",
    "prompt": "周末去杭州两天，喜欢拍照和美食，节奏轻松，总预算 3000 元",
    "destination": "杭州",
    "days": 2,
    "budget": 3000,
    "pace": "轻松",
    "interests": ["拍照", "美食"],
}

HIGH_RISK_REQUEST = {
    "user_id": "bob",
    "prompt": "杭州三天紧凑打卡，预算有限，想把主要景点都走完",
    "destination": "杭州",
    "days": 3,
    "budget": 800,
    "pace": "紧凑",
    "interests": ["拍照", "人文"],
}

NO_EVIDENCE_REQUEST = {
    "user_id": "carol",
    "prompt": "火星上的酒店怎么预订？",
    "destination": "火星",
    "days": 2,
    "budget": 5000,
    "pace": "适中",
    "interests": ["酒店"],
}


def response_payload(response) -> dict[str, Any]:
    """把 TestClient 响应整理成报告条目，保留状态码和错误正文。"""
    try:
        body = response.json()
    except Exception:
        body = {"text": response.text}
    return {"status_code": response.status_code, "body": body}


def run_report() -> None:
    """通过 ASGI 直接调用应用，生成一份可检查的四层协作报告。"""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        health = response_payload(client.get("/health"))
        architecture = response_payload(client.get("/architecture"))
        low_risk = response_payload(client.post("/runs", json=LOW_RISK_REQUEST))
        high_risk = response_payload(client.post("/runs", json=HIGH_RISK_REQUEST))
        no_evidence = response_payload(client.post("/runs", json=NO_EVIDENCE_REQUEST))

        approval = None
        high_body = high_risk["body"]
        if isinstance(high_body, dict) and high_body.get("status") == "waiting_approval":
            thread_id = high_body["thread_id"]
            approval = response_payload(
                client.post(
                    f"/runs/{thread_id}/approval",
                    json={
                        "decision": "approve",
                        "feedback": "预算已确认，允许生成最终行程",
                    },
                )
            )
            high_report = response_payload(client.get(f"/runs/{thread_id}/report"))
        else:
            high_report = {"status_code": None, "body": None}

        payload = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "health": health,
            "architecture": architecture,
            "low_risk_run": low_risk,
            "high_risk_run": high_risk,
            "approval": approval,
            "high_risk_report": high_report,
            "no_evidence_run": no_evidence,
        }
        atomic_write_json(REPORT_FILE, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"已保存：{REPORT_FILE}")


def main() -> None:
    """命令行入口：默认启动服务，--report 生成自检报告。"""
    parser = argparse.ArgumentParser(description="TripPilot framework service")
    parser.add_argument(
        "--report",
        action="store_true",
        help="通过 TestClient 跑一遍四层协作并写入 framework-report.json",
    )
    args = parser.parse_args()

    if args.report:
        run_report()
        return

    import uvicorn

    uvicorn.run("framework_service:app", host="127.0.0.1", port=8043, reload=False)


if __name__ == "__main__":
    main()
