"""Show how one model task spends time, retries failures, and degrades safely."""

import sys
import time


class RateLimitError(Exception):
    """The service is rejecting requests temporarily because they arrive too fast."""


class ModelTimeoutError(Exception):
    """The client stopped waiting before the model returned a result."""


class AuthenticationError(Exception):
    """The request credential is missing, invalid, or lacks permission."""


FALLBACK_TEXT = "服务暂时繁忙：已保留原文，暂不生成自动摘要，请稍后重试。"


def estimate_cost(prompt_tokens, completion_tokens, input_price, output_price):
    """Estimate one completed call from token usage and per-million-token prices."""
    input_cost = prompt_tokens / 1_000_000 * input_price
    output_cost = completion_tokens / 1_000_000 * output_price
    return input_cost + output_cost


def call_with_retry(operation, max_attempts=3, max_wait_seconds=1.0):
    """Retry temporary failures, enforce a wait budget, and return a fallback on exhaustion."""
    started_at = time.perf_counter()
    attempts = 0

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            return operation(attempt), "primary", attempts
        except AuthenticationError:
            print("鉴权失败：密钥或权限不对，重试不会解决问题，立即停止。")
            raise
        except (RateLimitError, ModelTimeoutError) as error:
            if attempt == max_attempts:
                print(f"第 {attempt} 次仍失败，尝试次数预算已用完。")
                break

            delay = 0.1 * (2 ** (attempt - 1))
            elapsed = time.perf_counter() - started_at
            if elapsed + delay > max_wait_seconds:
                print("继续等待会超过任务的总时间预算，停止重试。")
                break

            print(
                f"第 {attempt} 次调用遇到 {type(error).__name__}，"
                f"等待 {delay:.1f} 秒后重试。"
            )
            time.sleep(delay)

    return FALLBACK_TEXT, "fallback", attempts


def fake_model(attempt):
    """Simulate a slow read-only model call with different temporary failures."""
    time.sleep(0.05)

    if "--fallback" in sys.argv:
        raise RateLimitError("mock rate limit")
    if "--auth" in sys.argv:
        raise AuthenticationError("mock invalid key")
    if attempt == 1:
        raise RateLimitError("mock rate limit")
    if attempt == 2:
        raise ModelTimeoutError("mock timeout")
    return "第三次调用成功"


if __name__ == "__main__":
    started_at = time.perf_counter()
    try:
        result, mode, attempts = call_with_retry(fake_model)
    except AuthenticationError:
        raise SystemExit(2)

    elapsed = time.perf_counter() - started_at

    print("模型返回：", result)
    print("调用模式：", mode)
    print("实际尝试：", attempts, "次")
    print(f"任务耗时：约 {elapsed:.1f} 秒")

    if mode == "primary":
        cost = estimate_cost(1500, 300, 2.0, 8.0)
        print(f"单次成功调用估算成本：¥{cost:.4f}")
    else:
        print("降级结果不生成新的模型内容，不应把它当成成功卡片。")
