"""Validate a model-generated reading card with Pydantic."""

import sys

from pydantic import BaseModel, Field, ValidationError, field_validator


class ReadingCard(BaseModel):
    """Executable schema for one structured reading card."""
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


# This is the exact JSON text printed as the Mock result in chapter 13.
valid_model_output = (
    '{"title":"工具调用的安全边界",'
    '"summary":"调用工具前要校验权限、参数和目标。超时只说明结果未知，不能直接判定失败。",'
    '"key_points":["执行前做权限与参数校验","超时后先查询真实状态"],'
    '"tags":["Agent","工具调用","超时"],'
    '"review_questions":["为什么超时不等于失败？","工具调用前要检查什么？"]}'
)

invalid_model_output = (
    '{"title":"","summary":"太短",'
    '"key_points":["只有一条"],"tags":["Agent"]}'
)

raw_json = invalid_model_output if "--bad" in sys.argv else valid_model_output

try:
    card = ReadingCard.model_validate_json(raw_json)
except ValidationError as error:
    print("模型输出未通过字段校验：")
    for item in error.errors():
        print(f"- {item['loc']}: {item['msg']}")
    raise SystemExit(1)

print("校验通过")
print(card.model_dump_json(indent=2))
