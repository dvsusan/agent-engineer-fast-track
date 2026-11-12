"""Call an OpenAI-compatible chat endpoint, or run safely in Mock mode."""

import json
import os
import urllib.error
import urllib.request


def mock_chat(messages):
    """Return a deterministic response when no API key is configured."""
    user_text = messages[-1]["content"]
    return (
        "Mock 模型已收到问题："
        f"{user_text}\n"
        "这是一次本地模拟调用，不会访问网络，也不会产生费用。"
    )


def chat(messages, temperature=0.2):
    """Send messages to an OpenAI-compatible endpoint."""
    api_key = os.getenv("LLM_API_KEY", "").strip()
    # Mock mode keeps the complete exercise runnable without network or cost.
    mock_mode = os.getenv("LLM_MOCK", "0") == "1" or not api_key

    if mock_mode:
        return mock_chat(messages), "mock"

    base_url = os.getenv("LLM_BASE_URL", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()

    if not base_url or not model:
        raise ValueError("真实调用需要同时设置 LLM_BASE_URL 和 LLM_MODEL。")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"模型服务返回 HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"无法连接模型服务：{error.reason}") from error

    # A chat response wraps the model text inside a structured response object.
    content = response_data["choices"][0]["message"]["content"]
    return content, "remote"


messages = [
    {"role": "system", "content": "你是一个简洁的阅读助手。"},
    {"role": "user", "content": "用一句话解释什么是 API。"},
]

answer, mode = chat(messages)
print(f"调用模式：{mode}")
print("模型回答：")
print(answer)
