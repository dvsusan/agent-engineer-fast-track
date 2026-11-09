"""ReadDeck: turn an article into a validated reading card."""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator


class ReadingCard(BaseModel):
    title: str = Field(min_length=1, max_length=40)
    summary: str = Field(min_length=10, max_length=500)
    key_points: list[str] = Field(min_length=2, max_length=6)
    source_quote: str = Field(min_length=10, max_length=300)
    tags: list[str] = Field(min_length=2, max_length=6)
    review_questions: list[str] = Field(min_length=1, max_length=5)

    @field_validator("key_points", "tags", "review_questions")
    @classmethod
    def values_must_not_be_blank(cls, values):
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("列表项不能为空")
        return cleaned


def build_prompt(article, correction=""):
    correction_text = f"\n上次输出失败原因：{correction}\n请修正后重新输出。" if correction else ""
    return f"""
你是 ReadDeck 阅读卡片生成器。
阅读文章并只返回一个 JSON 对象。

字段要求：
- title：1 到 40 字
- summary：10 到 500 字
- key_points：2 到 6 条
- source_quote：从原文中摘录 10 到 300 字
- tags：2 到 6 个
- review_questions：1 到 5 条

不要编造原文没有的事实，不要输出 Markdown 代码块。
{correction_text}

文章：
{article}
""".strip()


def mock_model(_prompt):
    return json.dumps(
        {
            "title": "工具超时不等于任务失败",
            "summary": "工具调用超时只表示客户端没有及时收到结果，远端动作可能已经完成。系统应先核对业务状态，再决定重试或补偿。",
            "key_points": [
                "客户端超时和业务失败不是同一个概念",
                "重复执行前必须查询真实状态",
                "关键动作需要幂等键和工具回执",
            ],
            "source_quote": "一次工具调用超时，只说明客户端没有及时收到结果。请求可能已经到达业务系统并完成了写操作。",
            "tags": ["Agent", "工具调用", "幂等", "任务完成"],
            "review_questions": [
                "为什么工具超时后不能直接重试？",
                "完成任务为什么需要可验证证据？",
            ],
        },
        ensure_ascii=False,
    )


def call_model(prompt):
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if os.getenv("LLM_MOCK", "0") == "1" or not api_key:
        return mock_model(prompt), "mock"

    base_url = os.getenv("LLM_BASE_URL", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    if not base_url or not model:
        raise ValueError("真实调用需要 LLM_BASE_URL 和 LLM_MODEL。")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
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
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"模型服务返回 HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"无法连接模型服务：{error.reason}") from error

    return data["choices"][0]["message"]["content"], "remote"


def remove_code_fence(content):
    content = content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines).strip()
    return content


def fallback_card(article):
    clean_text = " ".join(article.split())
    return ReadingCard(
        title="文章结构化阅读卡片",
        summary=clean_text[:300],
        key_points=["先阅读原文", "模型输出失败时应保留可理解的降级结果"],
        source_quote=clean_text[:200],
        tags=["ReadDeck", "降级"],
        review_questions=["模型输出校验失败时，系统应怎样继续？"],
    )


def generate_card(article):
    correction = ""
    for attempt in range(1, 3):
        content, mode = call_model(build_prompt(article, correction))
        content = remove_code_fence(content)
        try:
            card = ReadingCard.model_validate_json(content)
            return card, mode, attempt
        except (ValidationError, json.JSONDecodeError) as error:
            correction = str(error)
            print(f"第 {attempt} 次输出未通过校验。")

    print("模型连续失败，使用本地降级卡片。")
    return fallback_card(article), "fallback", 2


def main():
    article_path = Path(__file__).with_name("sample_article.txt")
    output_path = Path(__file__).with_name("reading_card_output.json")
    article = article_path.read_text(encoding="utf-8")

    card, mode, attempts = generate_card(article)
    output_path.write_text(card.model_dump_json(indent=2), encoding="utf-8")

    print(f"运行模式：{mode}")
    print(f"调用次数：{attempts}")
    print(f"卡片标题：{card.title}")
    print(f"关键观点：{len(card.key_points)} 条")
    print(f"已保存：{output_path.name}")


if __name__ == "__main__":
    main()
