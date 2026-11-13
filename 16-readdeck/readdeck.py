"""ReadDeck: turn one article into a validated reading card and run record."""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator


class ModelServiceError(Exception):
    """The model service could not complete the request temporarily."""


class ModelRateLimitError(ModelServiceError):
    """The model service is receiving requests too quickly."""


class ModelTimeoutError(ModelServiceError):
    """The client stopped waiting before the model returned a result."""


class AuthenticationError(Exception):
    """The API key is missing, invalid, or does not have permission."""


class ReadingCard(BaseModel):
    title: str = Field(min_length=1, max_length=40, description="卡片标题")
    summary: str = Field(
        min_length=10,
        max_length=500,
        description="对文章核心内容的简短概括",
    )
    key_points: list[str] = Field(
        min_length=2,
        max_length=6,
        description="文章的主要观点",
    )
    source_quote: str = Field(
        min_length=10,
        max_length=300,
        description="必须来自输入文章的原文",
    )
    tags: list[str] = Field(
        min_length=2,
        max_length=6,
        description="用于分类和检索的关键词",
    )
    review_questions: list[str] = Field(
        min_length=1,
        max_length=5,
        description="检查读者是否理解文章的问题",
    )

    @field_validator("key_points", "tags", "review_questions")
    @classmethod
    def values_must_not_be_blank(cls, values):
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("列表项不能为空")
        return cleaned


def build_prompt(article, correction=""):
    correction_text = (
        f"\n上次输出失败原因：{correction}\n请修正后重新输出。"
        if correction
        else ""
    )
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
    if "--bad" in sys.argv:
        return '{"title":"","summary":"太短"}'

    payload = {
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
    }
    if "--bad-quote" in sys.argv:
        payload["source_quote"] = "这是一段原文中并不存在的引用。"

    return json.dumps(payload, ensure_ascii=False)


def call_model_once(prompt):
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
        if error.code == 429:
            raise ModelRateLimitError("模型服务返回 HTTP 429（限流）") from error
        if error.code in {401, 403}:
            raise AuthenticationError(f"模型鉴权失败：HTTP {error.code}") from error
        raise ModelServiceError(f"模型服务返回 HTTP {error.code}") from error
    except TimeoutError as error:
        raise ModelTimeoutError("等待模型响应超时") from error
    except urllib.error.URLError as error:
        raise ModelServiceError(f"无法连接模型服务：{error.reason}") from error

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


def validate_against_article(card, article):
    """Check one cross-field claim that Pydantic cannot verify on its own."""
    clean_article = " ".join(article.split())
    clean_quote = " ".join(card.source_quote.split())
    if clean_quote not in clean_article:
        raise ValueError("source_quote 不在文章中")


def validate_model_output(content, article):
    """Run both structure validation and the cross-field source check."""
    card = ReadingCard.model_validate_json(content)
    validate_against_article(card, article)
    return card


def validate_final_card(card, article):
    """Re-run both checks against the exact card that will be written to disk."""
    return validate_model_output(card.model_dump_json(), article)


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
    failure_reason = "无"
    attempts = 0

    for attempt in range(1, 3):
        attempts = attempt
        try:
            content, mode = call_model_once(build_prompt(article, correction))
        except AuthenticationError as error:
            failure_reason = str(error)
            print(f"鉴权失败，停止重试：{error}")
            return fallback_card(article), "fallback", attempt, failure_reason
        except ModelServiceError as error:
            failure_reason = str(error)
            print(f"第 {attempt} 次模型调用失败：{error}")
            if attempt == 2:
                break
            delay = 0.1 * (2 ** (attempt - 1))
            print(f"等待 {delay:.1f} 秒后重试。")
            time.sleep(delay)
            continue
        except ValueError as error:
            failure_reason = str(error)
            print(f"模型配置错误：{error}")
            break

        content = remove_code_fence(content)
        try:
            card = validate_model_output(content, article)
            return card, mode, attempt, failure_reason
        except ValidationError as error:
            failure_reason = f"字段校验失败：{len(error.errors())} 项"
            correction = str(error)
            print(f"第 {attempt} 次输出未通过校验。")
        except json.JSONDecodeError as error:
            failure_reason = f"JSON 解析失败：{error.msg}"
            correction = str(error)
            print(f"第 {attempt} 次输出不是合法 JSON。")
        except ValueError as error:
            failure_reason = f"原文引用核对失败：{error}"
            correction = str(error)
            print(f"第 {attempt} 次输出没有通过原文核对。")

        if attempt == 2:
            break

    print("模型连续失败，使用本地降级卡片。")
    return fallback_card(article), "fallback", attempts, failure_reason


def main():
    article_path = Path(__file__).with_name("sample_article.txt")
    output_path = Path(__file__).with_name("reading_card_output.json")
    run_path = Path(__file__).with_name("reading_card_run.json")

    try:
        article = article_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"找不到文章文件：{article_path.name}")
        print("请确认当前目录是 stage-02/16-readdeck，并创建 sample_article.txt。")
        raise SystemExit(1)

    card, mode, attempts, failure_reason = generate_card(article)
    try:
        card = validate_final_card(card, article)
    except (ValidationError, ValueError) as error:
        print(f"最终卡片校验失败：{error}")
        raise SystemExit(1)

    output_path.write_text(card.model_dump_json(indent=2), encoding="utf-8")
    run_record = {
        "mode": mode,
        "attempts": attempts,
        "failure_reason": failure_reason,
        "final_card_validation": ["schema", "source_quote"],
        "summary_length": len(card.summary),
        "tag_count": len(card.tags),
        "output_file": output_path.name,
    }
    run_path.write_text(
        json.dumps(run_record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"运行模式：{mode}")
    print(f"调用次数：{attempts}")
    print(f"失败原因：{failure_reason}")
    print(f"卡片标题：{card.title}")
    print(f"卡片摘要：{card.summary}")
    print(f"卡片标签：{'、'.join(card.tags)}")
    print("最终卡片校验：字段结构通过；原文引用通过")
    print(f"关键观点：{len(card.key_points)} 条")
    print(f"已保存：{output_path.name}")
    print(f"运行记录：{run_path.name}")


if __name__ == "__main__":
    main()
