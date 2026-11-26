"""并行执行工具调用，并记录每一次尝试的时间线。

执行器不理解业务，只负责：
- 把模型给出的多个 tool call 并发执行；
- 对临时失败做有限重试；
- 对永久失败和参数错误立即停止；
- 把结果整理成模型能继续消费的 tool 消息。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tool_registry import (
    PermanentToolError,
    ToolValidationError,
    TransientToolError,
    call_tool,
)


def utc_now() -> str:
    """返回带时区的时间戳，便于重放时排序。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class ToolCall:
    """模型请求的一次工具调用；snapshots 保存流式参数的中间状态。"""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str
    snapshots: list[dict[str, Any]] = field(default_factory=list)

    def public_view(self) -> dict[str, Any]:
        """返回可写入 JSON 的工具调用视图。"""
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "raw_arguments": self.raw_arguments,
            "snapshot_count": len(self.snapshots),
            "snapshots": self.snapshots,
        }


@dataclass
class ToolAttempt:
    """一次实际执行尝试；重试时会追加新记录，不覆盖旧记录。"""

    attempt: int
    started_at: str
    finished_at: str
    elapsed_ms: float
    status: str
    error_type: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None

    def public_view(self) -> dict[str, Any]:
        """返回可写入报告的尝试记录。"""
        return {
            "attempt": self.attempt,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_ms": self.elapsed_ms,
            "status": self.status,
            "error_type": self.error_type,
            "error": self.error,
            "result": self.result,
        }


@dataclass
class ToolResult:
    """一次工具调用的最终结果，包含成功、重试成功或失败。"""

    tool_call_id: str
    name: str
    status: str
    elapsed_ms: float
    attempts: list[ToolAttempt]
    result: dict[str, Any] | None = None
    error: str | None = None

    def public_view(self) -> dict[str, Any]:
        """返回可写入报告的完整结果。"""
        return {
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "attempt_count": len(self.attempts),
            "attempts": [attempt.public_view() for attempt in self.attempts],
            "result": self.result,
            "error": self.error,
        }

    def model_payload(self) -> dict[str, Any]:
        """返回给模型的消息载荷；隐藏内部时间戳，保留状态和失败原因。"""
        return {
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "status": self.status,
            "attempt_count": len(self.attempts),
            "result": self.result,
            "error": self.error,
        }


class ToolExecutor:
    """并发执行工具，并对可恢复错误做有限重试。"""

    def __init__(self, max_retries: int = 1, retry_delay_seconds: float = 0.15):
        if max_retries < 0:
            raise ValueError("max_retries 不能小于 0")
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds

    def execute_many(
        self,
        calls: list[ToolCall],
        max_workers: int = 4,
    ) -> tuple[list[ToolResult], dict[str, Any]]:
        """并发执行多个独立工具调用，并返回并行统计。"""
        if not calls:
            return [], {
                "tool_count": 0,
                "wall_ms": 0.0,
                "sum_tool_ms": 0.0,
                "parallel_saved_ms": 0.0,
            }

        started = time.perf_counter()
        started_at = utc_now()
        results_by_index: dict[int, ToolResult] = {}

        # 独立工具之间没有共享状态，可以并发；有副作用的工具不应放进这里。
        with ThreadPoolExecutor(max_workers=min(max_workers, len(calls))) as pool:
            futures = {
                pool.submit(self._execute_one, call): index
                for index, call in enumerate(calls)
            }
            for future in as_completed(futures):
                index = futures[future]
                results_by_index[index] = future.result()

        finished_at = utc_now()
        wall_ms = round((time.perf_counter() - started) * 1000, 1)
        ordered = [results_by_index[index] for index in range(len(calls))]
        sum_tool_ms = round(sum(result.elapsed_ms for result in ordered), 1)

        return ordered, {
            "tool_count": len(calls),
            "started_at": started_at,
            "finished_at": finished_at,
            "wall_ms": wall_ms,
            "sum_tool_ms": sum_tool_ms,
            "parallel_saved_ms": round(max(0.0, sum_tool_ms - wall_ms), 1),
        }

    def _execute_one(self, call: ToolCall) -> ToolResult:
        """执行单个工具；临时错误重试，永久错误立即返回。"""
        started = time.perf_counter()
        attempts: list[ToolAttempt] = []

        for attempt_number in range(1, self.max_retries + 2):
            attempt_started = time.perf_counter()
            attempt_started_at = utc_now()
            try:
                result = call_tool(call.name, call.arguments)
            except TransientToolError as error:
                attempt = ToolAttempt(
                    attempt=attempt_number,
                    started_at=attempt_started_at,
                    finished_at=utc_now(),
                    elapsed_ms=round((time.perf_counter() - attempt_started) * 1000, 1),
                    status="error",
                    error_type=type(error).__name__,
                    error=str(error),
                )
                attempts.append(attempt)

                if attempt_number <= self.max_retries:
                    # 只有临时错误才等待后重试；永久错误重试只会浪费时间。
                    time.sleep(self.retry_delay_seconds)
                    continue

                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    status="failed",
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                    attempts=attempts,
                    error=str(error),
                )
            except (PermanentToolError, ToolValidationError) as error:
                attempts.append(
                    ToolAttempt(
                        attempt=attempt_number,
                        started_at=attempt_started_at,
                        finished_at=utc_now(),
                        elapsed_ms=round(
                            (time.perf_counter() - attempt_started) * 1000, 1
                        ),
                        status="error",
                        error_type=type(error).__name__,
                        error=str(error),
                    )
                )
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    status="failed",
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                    attempts=attempts,
                    error=str(error),
                )
            except Exception as error:
                attempts.append(
                    ToolAttempt(
                        attempt=attempt_number,
                        started_at=attempt_started_at,
                        finished_at=utc_now(),
                        elapsed_ms=round(
                            (time.perf_counter() - attempt_started) * 1000, 1
                        ),
                        status="error",
                        error_type=type(error).__name__,
                        error=str(error),
                    )
                )
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    status="failed",
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                    attempts=attempts,
                    error=f"{type(error).__name__}: {error}",
                )

            attempts.append(
                ToolAttempt(
                    attempt=attempt_number,
                    started_at=attempt_started_at,
                    finished_at=utc_now(),
                    elapsed_ms=round((time.perf_counter() - attempt_started) * 1000, 1),
                    status="ok",
                    result=result,
                )
            )
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                status="ok" if attempt_number == 1 else "retry_success",
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                attempts=attempts,
                result=result,
            )

        # 正常流程不会走到这里；保留一个防御性返回值，避免类型检查歧义。
        return ToolResult(
            tool_call_id=call.id,
            name=call.name,
            status="failed",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            attempts=attempts,
            error="工具执行器没有产生结果",
        )
