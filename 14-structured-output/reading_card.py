"""Validate a model-generated reading card with Pydantic."""

import json
import sys

from pydantic import BaseModel, Field, ValidationError, field_validator


class ReadingCard(BaseModel):
    title: str = Field(min_length=1, max_length=40)
    summary: str = Field(min_length=10, max_length=500)
    key_points: list[str] = Field(min_length=2, max_length=6)
    tags: list[str] = Field(min_length=2, max_length=6)
    review_questions: list[str] = Field(min_length=1, max_length=5)

    @field_validator("key_points", "tags", "review_questions")
    @classmethod
    def items_must_not_be_blank(cls, values):
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("列表中的每一项都不能为空")
        return cleaned


valid_payload = {
    "title": "工具超时为什么要查询真实状态",
    "summary": "工具超时只说明结果未知，并不代表动作没有执行。系统应先查询业务状态，再决定重试或补偿。",
    "key_points": [
        "调用前必须校验权限和参数",
        "超时后不能直接重复执行",
        "关键动作需要幂等和审计",
    ],
    "tags": ["Agent", "工具调用", "幂等"],
    "review_questions": [
        "为什么超时可能已经产生副作用？",
        "如何避免重复退款或重复发送消息？",
    ],
}

invalid_payload = {
    "title": "",
    "summary": "太短",
    "key_points": ["只有一条"],
    "tags": ["Agent"],
}

payload = invalid_payload if "--bad" in sys.argv else valid_payload
raw_json = json.dumps(payload, ensure_ascii=False)

try:
    card = ReadingCard.model_validate_json(raw_json)
except ValidationError as error:
    print("模型输出未通过字段校验：")
    for item in error.errors():
        print(f"- {item['loc']}: {item['msg']}")
    raise SystemExit(1)

print("校验通过")
print(card.model_dump_json(indent=2))
