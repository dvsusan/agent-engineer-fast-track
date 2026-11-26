"""高级 Function Calling：流式参数、并行工具、重试与可回放记录。

与第 41 课使用 LangChain 封装工具不同，这里直接使用 OpenAI 兼容协议：
- 模型流式返回 tool_calls 的参数片段；
- 执行器并发调用多个独立工具；
- 临时失败只重试一次，永久失败交给模型披露；
- 每一步都写入 tool-call-record.json，之后可以离线回放。
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from tool_executor import ToolCall, ToolExecutor
from tool_registry import TOOL_SPECS, public_tool_names


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_DIR / "tool-call-record.json"

# .env 只用于本地开发；生产环境应由密钥管理系统注入。
load_dotenv(PROJECT_DIR / ".env")

DEFAULT_PROMPT = (
    "我 2026-11-28 去杭州西湖，喜欢拍照和美食，预算 1500 元，2 个人。"
    "请查天气、找 3 个附近景点、估算一天预算、查九溪烟树开放时间，"
    "并给杭州东站到西湖的交通建议。"
)

SYSTEM_PROMPT = """你是 TripPilot 的旅行工具调度员。必须遵守：
1. 用户同时询问天气、景点、预算、开放时间和交通时，第一轮并行调用所有独立工具。
2. 只能使用工具返回的事实，不得编造天气、价格、开放时间或交通班次。
3. 工具 status=failed 时，必须在最终答案中说明缺少什么信息，并建议查看官方公告。
4. 天气有雨时，优先推荐室内或半室内备选。
5. 最终答案用中文，覆盖天气、推荐地点、预算、交通和不确定性。
6. 不要调用不存在的工具；参数必须符合 Schema。"""


class ModelConfigurationError(RuntimeError):
    """模型配置缺失或格式错误。"""


class ModelCallError(RuntimeError):
    """模型请求或流式响应失败。"""


class ToolArgumentParseError(RuntimeError):
    """流式参数拼接完成后不是合法 JSON。"""


@dataclass(frozen=True)
class ModelSettings:
    """DeepSeek 配置；密钥字段不进入 repr 和报告。"""

    api_key: str = field(repr=False)
    base_url: str
    model: str
    timeout_seconds: float

    def public_view(self) -> dict[str, Any]:
        """返回可安全写入记录的脱敏配置。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "api_key_configured": bool(self.api_key),
        }


def read_model_settings() -> ModelSettings:
    """读取并校验 DeepSeek 配置；缺失密钥时在启动阶段失败。"""
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


def utc_now() -> str:
    """返回带时区的时间戳，便于回放时排序。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再替换，避免进程退出留下半截记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


class StreamingToolCallAccumulator:
    """把 OpenAI 流式 tool_calls 的碎片拼成完整调用。"""

    def __init__(self) -> None:
        self._states: dict[int, dict[str, Any]] = {}
        self._chunk_index = 0

    def feed(self, tool_call_deltas: list[Any]) -> None:
        """消费一个 chunk 里的 tool_call 增量。"""
        for delta in tool_call_deltas:
            index = int(delta.index)
            state = self._states.setdefault(
                index,
                {
                    "id": "",
                    "name": "",
                    "argument_parts": [],
                    "snapshots": [],
                },
            )
            if delta.id:
                state["id"] = delta.id
            if delta.function and delta.function.name:
                state["name"] = delta.function.name
            if delta.function and delta.function.arguments:
                fragment = delta.function.arguments
                state["argument_parts"].append(fragment)
                # 每个片段都记录一次当前累计值，证明参数是流式到达的。
                state["snapshots"].append(
                    {
                        "chunk": self._chunk_index,
                        "fragment": fragment,
                        "arguments_so_far": "".join(state["argument_parts"]),
                    }
                )
        self._chunk_index += 1

    def finalize(self) -> list[ToolCall]:
        """解析完整 JSON 参数，返回可直接执行的 ToolCall 列表。"""
        calls: list[ToolCall] = []
        for index in sorted(self._states):
            state = self._states[index]
            name = state["name"]
            raw_arguments = "".join(state["argument_parts"])
            if not name:
                raise ToolArgumentParseError(f"工具调用 {index} 没有名称")
            try:
                arguments = json.loads(raw_arguments) if raw_arguments else {}
            except json.JSONDecodeError as error:
                raise ToolArgumentParseError(
                    f"工具 {name} 的参数不是合法 JSON：{raw_arguments[:300]}"
                ) from error
            if not isinstance(arguments, dict):
                raise ToolArgumentParseError(
                    f"工具 {name} 的参数必须是对象，实际是 {type(arguments).__name__}"
                )
            calls.append(
                ToolCall(
                    id=state["id"] or f"call_{index}",
                    name=name,
                    arguments=arguments,
                    raw_arguments=raw_arguments,
                    snapshots=state["snapshots"],
                )
            )
        return calls


@dataclass
class AssistantTurn:
    """一轮模型流式响应的结果。"""

    content: str
    tool_calls: list[ToolCall]
    usage: dict[str, Any] | None
    finish_reason: str | None
    chunk_count: int
    elapsed_ms: float


def stream_model_turn(
    client: OpenAI,
    settings: ModelSettings,
    messages: list[dict[str, Any]],
) -> AssistantTurn:
    """调用一次流式模型，累积文本和 tool_calls。"""
    started = time.perf_counter()
    accumulator = StreamingToolCallAccumulator()
    content_parts: list[str] = []
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    chunk_count = 0

    try:
        stream = client.chat.completions.create(
            model=settings.model,
            messages=messages,
            tools=TOOL_SPECS,
            tool_choice="auto",
            parallel_tool_calls=True,
            stream=True,
            temperature=0.1,
            max_tokens=1600,
        )
        for chunk in stream:
            chunk_count += 1
            if chunk.usage:
                usage = chunk.usage.model_dump()
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta.content:
                content_parts.append(delta.content)
            # 即使没有 tool_calls 也调用 feed，保证 chunk 序号连续。
            accumulator.feed(delta.tool_calls or [])
            if choice.finish_reason:
                finish_reason = choice.finish_reason
    except Exception as error:
        raise ModelCallError(f"{type(error).__name__}: {error}") from error

    return AssistantTurn(
        content="".join(content_parts).strip(),
        tool_calls=accumulator.finalize(),
        usage=usage,
        finish_reason=finish_reason,
        chunk_count=chunk_count,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )


def merge_usage(total: dict[str, int], usage: dict[str, Any] | None) -> None:
    """把多轮 token 用量累加到一个扁平字典。"""
    if not usage:
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


def validate_record(record: dict[str, Any]) -> dict[str, bool]:
    """检查记录是否真的包含并行、流式、重试和失败恢复证据。"""
    calls = [
        call
        for round_record in record["rounds"]
        for call in round_record.get("tool_calls", [])
    ]
    results = [
        result
        for round_record in record["rounds"]
        for result in round_record.get("tool_results", [])
    ]
    parallel_stats = [
        round_record["parallel"]
        for round_record in record["rounds"]
        if round_record.get("parallel")
    ]
    final_answer = record.get("final_answer", "")

    return {
        "tool_call_count_at_least_3": len(calls) >= 3,
        "parallel_wall_less_than_sum": any(
            stats["wall_ms"] < stats["sum_tool_ms"] for stats in parallel_stats
        ),
        "streamed_argument_snapshots": any(
            call["snapshot_count"] >= 2 for call in calls
        ),
        "retry_recorded": any(
            result["status"] == "retry_success" for result in results
        ),
        "permanent_failure_recorded": any(
            result["status"] == "failed" for result in results
        ),
        "all_tool_results_have_ids": all(
            result["tool_call_id"] for result in results
        ),
        "final_answer_present": bool(final_answer),
        "final_answer_discloses_uncertainty": (
            "九溪" in final_answer
            and any(word in final_answer for word in ["未", "确认", "官方", "公告"])
        ),
    }


def run_agent(
    client: OpenAI,
    settings: ModelSettings,
    prompt: str,
    max_rounds: int = 3,
) -> dict[str, Any]:
    """跑完整的工具调用循环，并返回可回放记录。"""
    started_at = utc_now()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    rounds: list[dict[str, Any]] = []
    executor = ToolExecutor(max_retries=1)
    usage_total: dict[str, int] = {}
    usage_seen = False
    final_answer = ""

    for round_number in range(1, max_rounds + 1):
        turn = stream_model_turn(client, settings, messages)
        if turn.usage:
            usage_seen = True
            merge_usage(usage_total, turn.usage)

        round_record: dict[str, Any] = {
            "round": round_number,
            "assistant_content": turn.content,
            "finish_reason": turn.finish_reason,
            "stream_chunk_count": turn.chunk_count,
            "model_elapsed_ms": turn.elapsed_ms,
            "tool_calls": [call.public_view() for call in turn.tool_calls],
            "tool_results": [],
            "parallel": None,
        }
        rounds.append(round_record)

        if not turn.tool_calls:
            final_answer = turn.content.strip()
            break

        # 把模型的工具意图原样追加回对话，tool_call_id 必须与结果一一对应。
        messages.append(
            {
                "role": "assistant",
                "content": turn.content or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.raw_arguments,
                        },
                    }
                    for call in turn.tool_calls
                ],
            }
        )

        results, parallel_stats = executor.execute_many(turn.tool_calls)
        round_record["tool_results"] = [result.public_view() for result in results]
        round_record["parallel"] = parallel_stats

        for result in results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "content": json.dumps(
                        result.model_payload(), ensure_ascii=False
                    ),
                }
            )

    if not final_answer:
        final_answer = "达到最大轮数，模型没有给出最终答案。"

    record: dict[str, Any] = {
        "run_id": f"run-{uuid.uuid4().hex[:10]}",
        "started_at": started_at,
        "finished_at": utc_now(),
        "model": settings.public_view(),
        "tools": public_tool_names(),
        "prompt": prompt,
        "rounds": rounds,
        "final_answer": final_answer,
        "usage_total": usage_total if usage_seen else None,
        "checks": {},
    }
    record["checks"] = validate_record(record)
    return record


def replay_record(record: dict[str, Any]) -> None:
    """离线打印记录时间线，不调用模型，也不执行工具。"""
    print(f"run_id: {record['run_id']}")
    print(f"model: {record['model']['model']}")
    print(f"prompt: {record['prompt']}")

    for round_record in record["rounds"]:
        print(
            f"\nround {round_record['round']} | "
            f"finish={round_record['finish_reason']} | "
            f"chunks={round_record['stream_chunk_count']} | "
            f"model_ms={round_record['model_elapsed_ms']}"
        )
        for call in round_record.get("tool_calls", []):
            print(
                f"  tool_call {call['name']} {call['arguments']} "
                f"snapshots={call['snapshot_count']}"
            )
        parallel = round_record.get("parallel")
        if parallel:
            print(
                f"  parallel: wall={parallel['wall_ms']}ms "
                f"sum={parallel['sum_tool_ms']}ms "
                f"saved={parallel['parallel_saved_ms']}ms"
            )
        for result in round_record.get("tool_results", []):
            print(
                f"  result {result['name']} -> {result['status']} "
                f"attempts={result['attempt_count']} "
                f"elapsed={result['elapsed_ms']}ms"
            )
        if round_record["assistant_content"]:
            print(f"  assistant: {round_record['assistant_content'][:300]}")

    print("\nfinal answer:")
    print(record["final_answer"])
    print("\nchecks:")
    print(json.dumps(record["checks"], ensure_ascii=False, indent=2))


def main() -> None:
    """命令行入口：默认跑真实模型，--replay 只回放已有记录。"""
    parser = argparse.ArgumentParser(description="Advanced TripPilot function calling")
    parser.add_argument(
        "--replay",
        type=Path,
        help="回放已有的 tool-call-record.json，不调用模型",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="运行记录输出路径",
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    args = parser.parse_args()

    if args.replay:
        record = json.loads(args.replay.read_text(encoding="utf-8"))
        replay_record(record)
        if not all(record["checks"].values()):
            raise SystemExit(1)
        return

    try:
        settings = read_model_settings()
        client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
        )
        record = run_agent(client, settings, args.prompt)
    except (ModelConfigurationError, ModelCallError, ToolArgumentParseError) as error:
        print(f"运行失败：{type(error).__name__}: {error}")
        raise SystemExit(1) from error

    atomic_write_json(args.output, record)
    replay_record(record)
    print(f"\n已保存：{args.output}")
    if not all(record["checks"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
