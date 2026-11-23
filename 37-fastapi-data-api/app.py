"""Expose the chapter 36 read-only query layer as a FastAPI service."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field


# 复用第 36 课的数据库、Schema 读取和安全查询实现。
STAGE_ROOT = Path(__file__).resolve().parents[1]
CHAPTER_36_DIR = STAGE_ROOT / "36-sqlite-readonly-sql"
sys.path.insert(0, str(CHAPTER_36_DIR))

from sqlite_query_demo import (  # noqa: E402
    QUERY_CATALOG,
    QueryAnalyst,
    QueryExecutionError,
    QuerySecurityError,
    create_database,
)


DB_FILE = CHAPTER_36_DIR / "datapilot-36.db"


class AnalyzeRequest(BaseModel):
    """The only input a client may send to the analysis endpoint."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        min_length=4,
        max_length=200,
        description="要问 DataPilot 的自然语言问题",
        examples=["哪些城市的有效订单最多？平均订单价格是多少？"],
    )


class ColumnSchema(BaseModel):
    """One column in the database schema response."""

    name: str
    type: str
    not_null: bool
    primary_key: bool


class TableSchema(BaseModel):
    """One table in the database schema response."""

    name: str
    row_count: int
    columns: list[ColumnSchema]


class SchemaResponse(BaseModel):
    """The schema shape returned to a client."""

    database: str
    tables: list[TableSchema]


class QueryInfo(BaseModel):
    """Metadata for one approved analysis query."""

    name: str
    question: str
    required_columns: list[str]


class AnalyzeResponse(BaseModel):
    """A checked query result returned by the API."""

    query: str
    question: str
    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int


class HealthResponse(BaseModel):
    """A small response used to check service and database availability."""

    status: str
    database: str


def open_analyst():
    """Create a fresh read-only analyst for one request."""
    try:
        return QueryAnalyst(DB_FILE)
    except QueryExecutionError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def run_query(question):
    """Run one question and always close the read-only connection."""
    analyst = open_analyst()
    try:
        result = analyst.ask(question)
    except QuerySecurityError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except QueryExecutionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        analyst.close()
    return {
        "query": result["name"],
        "question": result["question"],
        "sql": result["sql"],
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": result["row_count"],
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the demo database when the service starts if it is missing."""
    if not DB_FILE.exists():
        create_database(
            csv_file=CHAPTER_36_DIR / "bookings.csv",
            db_file=DB_FILE,
        )
    yield


app = FastAPI(
    title="DataPilot Analytics API",
    version="0.1.0",
    description=(
        "把第 36 课的安全 SQLite 查询层开放为 HTTP 接口。"
        "所有分析请求只能选择已登记的问题模板。"
    ),
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health():
    """Check that the API can see its database file."""
    if not DB_FILE.exists():
        raise HTTPException(status_code=503, detail="数据库尚未初始化")
    return {"status": "ok", "database": DB_FILE.name}


@app.get("/schema", response_model=SchemaResponse, tags=["data"])
def schema():
    """Return the table and column map exposed to the API client."""
    analyst = open_analyst()
    try:
        return analyst.schema
    finally:
        analyst.close()


@app.get("/queries", response_model=list[QueryInfo], tags=["data"])
def queries():
    """List the approved questions that /analyze can route to."""
    return [
        {
            "name": spec.name,
            "question": spec.question,
            "required_columns": list(spec.required_columns),
        }
        for spec in QUERY_CATALOG
    ]


@app.post("/analyze", response_model=AnalyzeResponse, tags=["analysis"])
def analyze(request: AnalyzeRequest):
    """Run one approved analysis question and return its checked rows."""
    return run_query(request.question)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8011, reload=False)
