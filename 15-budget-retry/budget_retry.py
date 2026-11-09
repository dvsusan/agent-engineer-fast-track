"""Demonstrate retry, backoff, stop rules, and cost estimation."""

import time


class RateLimitError(Exception):
    pass


class AuthenticationError(Exception):
    pass


def estimate_cost(prompt_tokens, completion_tokens, input_price, output_price):
    return (
        prompt_tokens / 1_000_000 * input_price
        + completion_tokens / 1_000_000 * output_price
    )


def call_with_retry(operation, max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        try:
            return operation(attempt)
        except AuthenticationError:
            print("鉴权失败：重试不会解决问题，立即停止。")
            raise
        except RateLimitError:
            if attempt == max_attempts:
                print(f"第 {attempt} 次仍被限流，停止重试。")
                raise
            delay = 0.1 * (2 ** (attempt - 1))
            print(f"第 {attempt} 次被限流，等待 {delay:.1f} 秒后重试。")
            time.sleep(delay)


def fake_model(attempt):
    if attempt < 3:
        raise RateLimitError("mock rate limit")
    return "第三次调用成功"


result = call_with_retry(fake_model)
cost = estimate_cost(1500, 300, 2.0, 8.0)

print("任务结果：", result)
print(f"本次估算成本：¥{cost:.4f}")
