"""Inspect an OpenAI-compatible request without sending real data."""

import json
import os
from http import HTTPStatus
from urllib.parse import urlparse

base_url = os.getenv("LLM_BASE_URL", "https://api.example.com/v1")
endpoint = base_url.rstrip("/") + "/chat/completions"
parsed = urlparse(endpoint)
api_key = os.getenv("LLM_API_KEY", "")

payload = {
    "model": os.getenv("LLM_MODEL", "course-model"),
    "messages": [
        {"role": "system", "content": "你是一个简洁的阅读助手。"},
        {"role": "user", "content": "用一句话解释什么是 API。"},
    ],
    "temperature": 0.2,
}

print("请求方法：POST")
print(f"协议：{parsed.scheme}")
print(f"主机：{parsed.netloc}")
print(f"路径：{parsed.path}")
print(f"鉴权头：{'Bearer 已设置（不显示具体值）' if api_key else '尚未设置'}")
print(f"成功状态示例：{HTTPStatus.OK.value} {HTTPStatus.OK.phrase}")
print("\n请求体：")
print(json.dumps(payload, ensure_ascii=False, indent=2))

mock_response = {
    "id": "mock-response-001",
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": "API 是程序之间约定好的请求和响应接口。",
            }
        }
    ],
}

print("\n模拟响应中的正文：")
print(mock_response["choices"][0]["message"]["content"])
