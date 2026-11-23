"""Run DataPilot analysis as an asynchronous FastAPI job."""

from __future__ import annotations

import asyncio
import csv
import json
import sys
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field


# 复用第 36 课已经验证过的数据库构建和只读查询层。
PROJECT_DIR = Path(__file__).resolve().parent
CHAPTER_36_DIR = PROJECT_DIR.parent / "36-sqlite-readonly-sql"
sys.path.insert(0, str(CHAPTER_36_DIR))

from sqlite_query_demo import (  # noqa: E402
    REQUIRED_COLUMNS,
    QueryAnalyst,
    QueryExecutionError,
    create_database,
)


UPLOAD_DIR = PROJECT_DIR / "uploads"
WORK_DIR = PROJECT_DIR / "work"
RESULT_DIR = PROJECT_DIR / "results"

# 1 MiB 只是本课演示上限；生产环境应结合磁盘配额、业务需求和解压风险单独设计。
MAX_UPLOAD_BYTES = 1024 * 1024
# 这些等待只是为了让人看清 SSE；真实耗时来自文件解析、数据库和模型调用。
STAGE_DELAY_SECONDS = 0.45

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL_STATUSES = {SUCCEEDED, FAILED, CANCELLED}


def utc_now() -> str:
    """Return a timezone-aware timestamp that is safe to show in JSON."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobNotFoundError(KeyError):
    """The requested job id does not exist in this process."""


class JobConflictError(RuntimeError):
    """The job exists but its current state does not allow the action."""


def safe_filename(filename: str | None) -> str:
    """Keep only the final path component supplied by the client."""
    name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not name or name in {".", ".."}:
        raise ValueError("上传文件必须有文件名")
    if Path(name).suffix.lower() != ".csv":
        raise ValueError("本课只接受 .csv 文件")
    return name


def inspect_csv(csv_path: Path) -> dict[str, Any]:
    """Check the uploaded CSV before creating a database from it."""
    with csv_path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        fieldnames = {name.strip() for name in (reader.fieldnames or [])}
        missing_columns = REQUIRED_COLUMNS.difference(fieldnames)
        if missing_columns:
            raise QueryExecutionError(
                f"CSV 缺少字段：{', '.join(sorted(missing_columns))}"
            )

        row_count = 0
        blank_booking_ids = 0
        for row in reader:
            row_count += 1
            if not (row.get("booking_id") or "").strip():
                blank_booking_ids += 1

    if row_count == 0:
        raise QueryExecutionError("CSV 只有表头，没有可分析的数据行")
    return {
        "row_count": row_count,
        "blank_booking_ids": blank_booking_ids,
        "columns": sorted(fieldnames),
    }


def analyze_database(
    database_path: Path,
    job_id: str,
    source_filename: str,
    imported_rows: int,
) -> dict[str, Any]:
    """Use the chapter 36 read-only analyst and assemble a result document."""
    analyst = QueryAnalyst(database_path)
    try:
        # 第 36 课的 schema 外层数据库名是固定示例值，这里改成当前任务的数据库名。
        schema = {**analyst.schema, "database": database_path.name}
        city_summary = analyst.ask("哪些城市的有效订单最多？平均订单价格是多少？")
        price_bands = analyst.ask("有效订单主要落在哪些价格区间？")
    finally:
        analyst.close()

    table_row_count = next(
        (
            table["row_count"]
            for table in schema["tables"]
            if table["name"] == "bookings"
        ),
        None,
    )
    city_total = sum(row["valid_orders"] for row in city_summary["rows"])
    price_total = sum(row["orders"] for row in price_bands["rows"])
    checks = {
        "schema_row_count_matches_import": table_row_count == imported_rows,
        "city_result_not_empty": bool(city_summary["rows"]),
        "price_result_not_empty": bool(price_bands["rows"]),
        "city_and_price_totals_match": city_total == price_total,
    }
    if not all(checks.values()):
        raise RuntimeError(f"结果校验失败：{checks}")

    return {
        "job_id": job_id,
        "source_filename": source_filename,
        "generated_at": utc_now(),
        "imported_rows": imported_rows,
        "schema": schema,
        "city_summary": city_summary,
        "price_bands": price_bands,
        "checks": checks,
        "summary": {
            "valid_orders": city_total,
            "cities": len(city_summary["rows"]),
            "price_bands": len(price_bands["rows"]),
        },
    }


def markdown_cell(value: Any) -> str:
    """Keep a table cell on one Markdown line."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    """Render the same verified result as a human-readable download."""
    lines = [
        "# DataPilot 异步分析结果",
        "",
        f"- 任务编号：`{report['job_id']}`",
        f"- 源文件：`{report['source_filename']}`",
        f"- 生成时间：{report['generated_at']}",
        f"- 导入原始订单：{report['imported_rows']} 行",
        f"- 有效订单：{report['summary']['valid_orders']} 笔",
        "",
        "## 城市统计",
        "",
        "| 城市 | 有效订单 | 平均价格 |",
        "|---|---:|---:|",
    ]
    for row in report["city_summary"]["rows"]:
        lines.append(
            f"| {markdown_cell(row['city'])} | {row['valid_orders']} | "
            f"{row['avg_price']} |"
        )

    lines.extend(
        [
            "",
            "## 价格区间",
            "",
            "| 价格区间 | 订单数 |",
            "|---|---:|",
        ]
    )
    for row in report["price_bands"]["rows"]:
        lines.append(f"| {markdown_cell(row['price_band'])} | {row['orders']} |")

    lines.extend(["", "## 自动检查", "", "| 检查 | 是否通过 |", "|---|---|"])
    for name, passed in report["checks"].items():
        lines.append(f"| {name} | {passed} |")
    return "\n".join(lines) + "\n"


def write_result_files(
    report: dict[str, Any], json_path: Path, markdown_path: Path
) -> None:
    """Write both downloads before the job is allowed to succeed."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_temp = json_path.with_suffix(json_path.suffix + ".tmp")
    markdown_temp = markdown_path.with_suffix(markdown_path.suffix + ".tmp")
    json_temp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_temp.write_text(render_markdown(report), encoding="utf-8")
    # 全部写完后再替换正式文件，避免客户端下载到只写了一半的结果。
    json_temp.replace(json_path)
    markdown_temp.replace(markdown_path)


@dataclass
class JobRecord:
    """The in-memory state of one analysis job."""

    job_id: str
    original_filename: str
    upload_path: Path
    database_path: Path
    result_json_path: Path
    result_markdown_path: Path
    status: str = QUEUED
    progress: int = 0
    stage: str = "queued"
    message: str = "任务已创建，等待执行"
    error: str | None = None
    result_available: bool = False
    cancellation_requested: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    events: list[dict[str, Any]] = field(default_factory=list)

    def public_view(self) -> dict[str, Any]:
        """Return only fields that are safe and useful to an API client."""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "message": self.message,
            "original_filename": self.original_filename,
            "error": self.error,
            "result_available": self.result_available,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "links": {
                "events": f"/jobs/{self.job_id}/events",
                "cancel": f"/jobs/{self.job_id}/cancel",
                "result": (
                    f"/jobs/{self.job_id}/result?format=json"
                    if self.result_available
                    else None
                ),
            },
        }


class JobManager:
    """Track jobs in process memory and serialize state transitions."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    def _require(self, job_id: str) -> JobRecord:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    async def create(
        self, original_filename: str, upload_path: Path
    ) -> dict[str, Any]:
        """Register a job after its upload has been validated and stored."""
        job_id = uuid.uuid4().hex
        job = JobRecord(
            job_id=job_id,
            original_filename=original_filename,
            upload_path=upload_path,
            database_path=WORK_DIR / f"{job_id}.db",
            result_json_path=RESULT_DIR / f"{job_id}.json",
            result_markdown_path=RESULT_DIR / f"{job_id}.md",
        )
        async with self._lock:
            self._jobs[job_id] = job
            self._append_event(job, QUEUED, "queued", 0, job.message)
        return job.public_view()

    def _append_event(
        self, job: JobRecord, job_status: str, stage: str, progress: int, message: str
    ) -> None:
        """Append one SSE event; callers already hold the manager lock."""
        job.events.append(
            {
                "sequence": len(job.events) + 1,
                "job_id": job.job_id,
                "status": job_status,
                "stage": stage,
                "progress": progress,
                "message": message,
                "timestamp": utc_now(),
            }
        )

    async def attach_task(self, job_id: str, task: asyncio.Task[None]) -> None:
        """Keep the task handle so a later cancel request can stop it."""
        async with self._lock:
            job = self._require(job_id)
            self._tasks[job_id] = task
            should_cancel = job.cancellation_requested
        if should_cancel:
            task.cancel()

    async def view(self, job_id: str) -> dict[str, Any]:
        async with self._lock:
            return self._require(job_id).public_view()

    async def execution_inputs(self, job_id: str) -> dict[str, Any]:
        """Copy the paths needed by the worker before it starts waiting."""
        async with self._lock:
            job = self._require(job_id)
            return {
                "original_filename": job.original_filename,
                "upload_path": job.upload_path,
                "database_path": job.database_path,
                "result_json_path": job.result_json_path,
                "result_markdown_path": job.result_markdown_path,
            }

    async def active_count(self) -> int:
        async with self._lock:
            return sum(
                job.status not in TERMINAL_STATUSES for job in self._jobs.values()
            )

    async def transition(
        self,
        job_id: str,
        job_status: str,
        stage: str,
        progress: int,
        message: str,
        *,
        error: str | None = None,
        result_available: bool | None = None,
    ) -> dict[str, Any]:
        """Move a job forward exactly once and publish the matching event."""
        async with self._lock:
            job = self._require(job_id)
            if job.status in TERMINAL_STATUSES:
                return job.public_view()
            job.status = job_status
            job.stage = stage
            job.progress = max(0, min(100, progress))
            job.message = message
            job.error = error
            if result_available is not None:
                job.result_available = result_available
            job.updated_at = utc_now()
            self._append_event(job, job_status, stage, job.progress, message)
            return job.public_view()

    async def events_after(
        self, job_id: str, start: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return new SSE events and whether the job has reached a terminal state."""
        async with self._lock:
            job = self._require(job_id)
            events = [dict(event) for event in job.events[start:]]
            return events, job.status in TERMINAL_STATUSES

    async def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a queued or running job and wait for its terminal state."""
        async with self._lock:
            job = self._require(job_id)
            if job.status in TERMINAL_STATUSES:
                raise JobConflictError(f"任务当前状态为 {job.status}，不能取消")
            job.cancellation_requested = True
            task = self._tasks.get(job_id)

        if task is None:
            raise JobConflictError("任务尚未开始，请稍后再试")
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return await self.view(job_id)

    async def result_path(
        self, job_id: str, file_format: Literal["json", "markdown"]
    ) -> tuple[Path, str, str]:
        """Resolve a download path only from the server-side job record."""
        async with self._lock:
            job = self._require(job_id)
            if job.status != SUCCEEDED:
                raise JobConflictError(
                    f"任务当前状态为 {job.status}，只有成功任务可以下载结果"
                )
            if file_format == "markdown":
                return job.result_markdown_path, "text/markdown; charset=utf-8", "md"
            return job.result_json_path, "application/json", "json"

    async def remove_task(self, job_id: str) -> None:
        async with self._lock:
            self._tasks.pop(job_id, None)


manager = JobManager()


class JobLinks(BaseModel):
    """Relative links that a client can follow for one job."""

    events: str
    cancel: str
    result: str | None


class JobView(BaseModel):
    """The public state of one asynchronous analysis job."""

    job_id: str
    status: str
    progress: int = Field(ge=0, le=100)
    stage: str
    message: str
    original_filename: str
    error: str | None
    result_available: bool
    created_at: str
    updated_at: str
    links: JobLinks


class HealthResponse(BaseModel):
    """A small readiness response for local checks and load balancers."""

    status: str
    max_upload_mb: int
    active_jobs: int


async def run_job(job_id: str) -> None:
    """Execute one analysis pipeline and always leave a terminal state."""
    try:
        job = await manager.execution_inputs(job_id)
        await manager.transition(
            job_id, RUNNING, "validate", 10, "开始检查 CSV 表头和行数"
        )
        await asyncio.sleep(STAGE_DELAY_SECONDS)

        health = await asyncio.to_thread(inspect_csv, job["upload_path"])
        await manager.transition(
            job_id,
            RUNNING,
            "health-check",
            25,
            f"CSV 体检通过：{health['row_count']} 行，"
            f"{health['blank_booking_ids']} 个空订单号",
        )
        await asyncio.sleep(STAGE_DELAY_SECONDS)

        imported_rows = await asyncio.to_thread(
            create_database,
            csv_file=job["upload_path"],
            db_file=job["database_path"],
        )
        await manager.transition(
            job_id, RUNNING, "build-database", 42, "SQLite 数据库建立完成"
        )
        await asyncio.sleep(STAGE_DELAY_SECONDS)

        report = await asyncio.to_thread(
            analyze_database,
            job["database_path"],
            job_id,
            job["original_filename"],
            imported_rows,
        )
        await manager.transition(
            job_id, RUNNING, "schema-and-city", 68, "Schema 读取和城市统计完成"
        )
        await asyncio.sleep(STAGE_DELAY_SECONDS)

        await manager.transition(
            job_id, RUNNING, "price-bands", 84, "价格区间统计和结果校验完成"
        )
        await asyncio.sleep(STAGE_DELAY_SECONDS)

        await asyncio.to_thread(
            write_result_files,
            report,
            job["result_json_path"],
            job["result_markdown_path"],
        )
        await manager.transition(
            job_id,
            RUNNING,
            "write-result",
            95,
            "JSON 和 Markdown 结果文件已生成",
        )
        await asyncio.sleep(STAGE_DELAY_SECONDS)

        await manager.transition(
            job_id,
            SUCCEEDED,
            "completed",
            100,
            "分析完成，可以下载结果",
            result_available=True,
        )
    except asyncio.CancelledError:
        # 取消不是失败：保留独立的终态，避免客户端把用户主动取消当成系统故障。
        current = await manager.view(job_id)
        await manager.transition(
            job_id,
            CANCELLED,
            "cancelled",
            current["progress"],
            "任务已由用户取消",
        )
    except Exception as error:
        # API 只暴露可读错误，不把 Python 堆栈和服务器绝对路径交给客户端。
        current = await manager.view(job_id)
        await manager.transition(
            job_id,
            FAILED,
            "failed",
            current["progress"],
            "分析失败",
            error=f"{type(error).__name__}: {error}",
            result_available=False,
        )
    finally:
        await manager.remove_task(job_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create runtime directories without deleting files from earlier jobs."""
    for directory in (UPLOAD_DIR, WORK_DIR, RESULT_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="DataPilot Async Analysis API",
    version="0.1.0",
    description=(
        "上传 CSV 后异步执行第 36 课的只读分析，通过 SSE 查看进度，"
        "并在成功后下载 JSON 或 Markdown 结果。"
    ),
    lifespan=lifespan,
)


async def get_job_or_404(job_id: str) -> dict[str, Any]:
    try:
        return await manager.view(job_id)
    except JobNotFoundError as error:
        raise HTTPException(status_code=404, detail="任务不存在") from error


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "active_jobs": await manager.active_count(),
    }


@app.post(
    "/jobs",
    response_model=JobView,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["jobs"],
)
async def create_job(file: UploadFile = File(...)) -> dict[str, Any]:
    """Accept one CSV, persist it, then start a background analysis task."""
    try:
        original_filename = safe_filename(file.filename)
    except ValueError as error:
        raise HTTPException(status_code=415, detail=str(error)) from error

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="CSV 超过 1 MiB 上限")

    upload_id = uuid.uuid4().hex
    upload_path = UPLOAD_DIR / f"{upload_id}.csv"
    await asyncio.to_thread(upload_path.write_bytes, content)
    view = await manager.create(original_filename, upload_path)
    task = asyncio.create_task(run_job(view["job_id"]))
    await manager.attach_task(view["job_id"], task)
    return view


@app.get("/jobs/{job_id}", response_model=JobView, tags=["jobs"])
async def get_job(job_id: str) -> dict[str, Any]:
    return await get_job_or_404(job_id)


@app.get("/jobs/{job_id}/events", tags=["jobs"])
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    """Stream saved and future progress events until the job is terminal."""
    await get_job_or_404(job_id)

    async def stream():
        sent = 0
        while True:
            try:
                events, terminal = await manager.events_after(job_id, sent)
            except JobNotFoundError:
                return
            for event in events:
                payload = json.dumps(event, ensure_ascii=False)
                yield (
                    f"id: {event['sequence']}\n"
                    "event: progress\n"
                    f"data: {payload}\n\n"
                )
            sent += len(events)
            if terminal:
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 避免 Nginx 等反向代理攒满缓冲区后才把进度发给浏览器。
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/jobs/{job_id}/cancel", response_model=JobView, tags=["jobs"])
async def cancel_job(job_id: str) -> dict[str, Any]:
    try:
        return await manager.cancel(job_id)
    except JobNotFoundError as error:
        raise HTTPException(status_code=404, detail="任务不存在") from error
    except JobConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/jobs/{job_id}/result", tags=["jobs"])
async def download_result(
    job_id: str,
    file_format: Literal["json", "markdown"] = Query("json", alias="format"),
) -> FileResponse:
    try:
        path, media_type, suffix = await manager.result_path(job_id, file_format)
    except JobNotFoundError as error:
        raise HTTPException(status_code=404, detail="任务不存在") from error
    except JobConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if not path.exists():
        raise HTTPException(status_code=500, detail="结果文件不存在，请重新运行任务")
    return FileResponse(
        path,
        media_type=media_type,
        filename=f"datapilot-{job_id[:8]}.{suffix}",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8012, reload=False)
