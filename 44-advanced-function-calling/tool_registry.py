"""TripPilot 的 Function Calling 工具注册表。

本文件只负责三件事：
1. 用 Pydantic 校验模型传入的参数；
2. 用普通 Python 函数执行真实的业务逻辑；
3. 用 OpenAI 兼容的 JSON Schema 告诉模型有哪些工具可用。

工具不会因为“模型想调用”就直接产生副作用；所有参数都必须先通过校验。
"""

from __future__ import annotations

import math
import time
from datetime import date as date_type
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ToolError(RuntimeError):
    """所有工具错误的基类，执行器按子类型决定是否重试。"""


class TransientToolError(ToolError):
    """临时失败，例如网络超时；执行器可以重试。"""


class PermanentToolError(ToolError):
    """永久失败，例如没有资料或权限不足；重试也不会成功。"""


class ToolValidationError(ToolError):
    """参数不合法；必须在执行前拒绝，不能重试同一份错误参数。"""


class WeatherInput(BaseModel):
    """天气工具的参数。"""

    model_config = ConfigDict(extra="forbid")

    city: str = Field(min_length=1, max_length=30)
    date: str = Field(
        description="ISO 日期，例如 2026-11-28",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )


class PlacesInput(BaseModel):
    """景点检索工具的参数。"""

    model_config = ConfigDict(extra="forbid")

    city: str = Field(min_length=1, max_length=30)
    interest: str = Field(default="", max_length=30)
    limit: int = Field(default=3, ge=1, le=5)


class BudgetInput(BaseModel):
    """预算估算工具的参数。"""

    model_config = ConfigDict(extra="forbid")

    days: int = Field(ge=1, le=7)
    people: int = Field(ge=1, le=8)
    pace: Literal["轻松", "适中", "紧凑"] = "适中"


class OpeningHoursInput(BaseModel):
    """开放时间工具的参数。"""

    model_config = ConfigDict(extra="forbid")

    place: str = Field(min_length=1, max_length=50)


class TransportInput(BaseModel):
    """交通查询工具的参数。"""

    model_config = ConfigDict(extra="forbid")

    origin: str = Field(min_length=1, max_length=50)
    destination: str = Field(min_length=1, max_length=50)


# 这些是本地教学数据；生产环境应替换成数据库、地图 API 或景区官方接口。
PLACES = [
    {
        "name": "西湖音乐喷泉",
        "city": "杭州",
        "category": "景点",
        "indoor": False,
        "ticket_price": 0,
        "walk_minutes": 10,
        "tags": ["拍照", "夜景"],
        "rating": 4.7,
    },
    {
        "name": "浙江省博物馆",
        "city": "杭州",
        "category": "博物馆",
        "indoor": True,
        "ticket_price": 0,
        "walk_minutes": 18,
        "tags": ["拍照", "人文", "雨天"],
        "rating": 4.8,
    },
    {
        "name": "南宋御街",
        "city": "杭州",
        "category": "街区",
        "indoor": False,
        "ticket_price": 0,
        "walk_minutes": 22,
        "tags": ["拍照", "美食", "人文"],
        "rating": 4.5,
    },
    {
        "name": "九溪烟树",
        "city": "杭州",
        "category": "自然",
        "indoor": False,
        "ticket_price": 0,
        "walk_minutes": 35,
        "tags": ["拍照", "自然"],
        "rating": 4.6,
    },
    {
        "name": "知味观(湖滨店)",
        "city": "杭州",
        "category": "餐厅",
        "indoor": True,
        "ticket_price": 80,
        "walk_minutes": 12,
        "tags": ["美食"],
        "rating": 4.4,
    },
    {
        "name": "杭州酒家",
        "city": "杭州",
        "category": "餐厅",
        "indoor": True,
        "ticket_price": 100,
        "walk_minutes": 16,
        "tags": ["美食"],
        "rating": 4.3,
    },
]

WEATHER_BY_DATE = {
    "2026-11-28": {
        "condition": "中雨",
        "temperature_c": 14,
        "advice": "优先安排室内景点，携带雨具",
    },
    "2026-11-29": {
        "condition": "多云",
        "temperature_c": 16,
        "advice": "适合户外拍照，早晚加外套",
    },
}

OPENING_HOURS = {
    "浙江省博物馆": {
        "hours": "09:00-17:00",
        "closed_days": ["周一"],
        "source": "博物馆公告 2026-11-20",
    },
    "西湖音乐喷泉": {
        "hours": "19:00-19:30",
        "closed_days": [],
        "source": "景区公告 2026-11-18",
    },
    "南宋御街": {
        "hours": "全天开放",
        "closed_days": [],
        "source": "街区公告 2026-11-15",
    },
}

TRANSPORT_TABLE = {
    ("杭州东站", "西湖"): {"method": "地铁 1 号线 + 步行", "minutes": 40, "cost": 5},
    ("杭州东站", "浙江省博物馆"): {"method": "地铁 + 步行", "minutes": 45, "cost": 6},
    ("西湖", "浙江省博物馆"): {"method": "步行", "minutes": 18, "cost": 0},
    ("西湖", "南宋御街"): {"method": "步行", "minutes": 22, "cost": 0},
    ("西湖", "九溪烟树"): {"method": "公交", "minutes": 35, "cost": 4},
}

BUDGET_RATES = {
    "轻松": {"transport": 80, "food": 180, "tickets": 120, "hotel": 400},
    "适中": {"transport": 60, "food": 140, "tickets": 80, "hotel": 300},
    "紧凑": {"transport": 40, "food": 100, "tickets": 50, "hotel": 220},
}

# 进程内计数器用于模拟“第一次超时、第二次成功”；每次 CLI 运行都会从 0 开始。
_weather_attempts = 0


def get_weather(payload: WeatherInput) -> dict[str, Any]:
    """查询天气；2026-11-28 第一次调用会模拟超时，用来验证重试。"""
    global _weather_attempts
    _weather_attempts += 1

    # 故意保留一段可观察的延迟，让并行执行的时间线更清楚。
    time.sleep(0.35)
    if payload.date == "2026-11-28" and _weather_attempts == 1:
        raise TransientToolError("天气服务第一次请求超时（模拟）")

    try:
        date_type.fromisoformat(payload.date)
    except ValueError as error:
        raise ToolValidationError(f"日期格式不合法：{payload.date}") from error

    weather = WEATHER_BY_DATE.get(
        payload.date,
        {"condition": "晴", "temperature_c": 18, "advice": "适合户外活动"},
    )
    return {
        "city": payload.city,
        "date": payload.date,
        "condition": weather["condition"],
        "temperature_c": weather["temperature_c"],
        "advice": weather["advice"],
        "source": "weather-api",
        "attempt": _weather_attempts,
    }


def search_places(payload: PlacesInput) -> dict[str, Any]:
    """按城市和兴趣检索景点/餐厅，返回室内外标签和步行时间。"""
    time.sleep(0.2)
    interest = payload.interest.strip()
    candidates = [
        place
        for place in PLACES
        if place["city"] == payload.city
        and (not interest or interest in place["tags"])
    ]
    # 先按评分降序，再按步行时间升序，保证同样的输入得到同样的顺序。
    candidates.sort(key=lambda item: (-item["rating"], item["walk_minutes"]))
    return {
        "city": payload.city,
        "interest": interest or "全部",
        "count": len(candidates[: payload.limit]),
        "places": candidates[: payload.limit],
    }


def estimate_budget(payload: BudgetInput) -> dict[str, Any]:
    """按人数、天数和节奏估算预算；模型不参与这里的算术。"""
    time.sleep(0.15)
    rates = BUDGET_RATES[payload.pace]
    nights = max(payload.days - 1, 1)
    rooms = math.ceil(payload.people / 2)

    transport = rates["transport"] * payload.days * payload.people
    food = rates["food"] * payload.days * payload.people
    tickets = rates["tickets"] * payload.days * payload.people
    hotel = rates["hotel"] * nights * rooms
    total = transport + food + tickets + hotel

    return {
        "days": payload.days,
        "people": payload.people,
        "pace": payload.pace,
        "breakdown": {
            "transport": transport,
            "food": food,
            "tickets": tickets,
            "hotel": hotel,
        },
        "total": total,
        "currency": "CNY",
        "note": "教学估算，不含购物和临时打车",
    }


def check_opening_hours(payload: OpeningHoursInput) -> dict[str, Any]:
    """查询开放时间；没有可靠资料时返回永久失败，而不是编造时间。"""
    time.sleep(0.1)
    if payload.place not in OPENING_HOURS:
        raise PermanentToolError(
            f"开放时间服务没有 {payload.place} 的可靠数据，请查看官方公告"
        )
    return {
        "place": payload.place,
        **OPENING_HOURS[payload.place],
        "status": "verified",
    }


def get_transport(payload: TransportInput) -> dict[str, Any]:
    """查询两点交通；没有精确记录时返回可解释的估算值。"""
    time.sleep(0.15)
    record = TRANSPORT_TABLE.get((payload.origin, payload.destination))
    if record:
        return {
            "origin": payload.origin,
            "destination": payload.destination,
            **record,
            "status": "verified",
        }
    return {
        "origin": payload.origin,
        "destination": payload.destination,
        "method": "打车或公交",
        "minutes": 30,
        "cost": 20,
        "status": "estimated",
        "note": "没有精确线路记录，按同城平均时间估算",
    }


TOOL_INPUT_MODELS: dict[str, type[BaseModel]] = {
    "get_weather": WeatherInput,
    "search_places": PlacesInput,
    "estimate_budget": BudgetInput,
    "check_opening_hours": OpeningHoursInput,
    "get_transport": TransportInput,
}

TOOL_FUNCTIONS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "get_weather": get_weather,
    "search_places": search_places,
    "estimate_budget": estimate_budget,
    "check_opening_hours": check_opening_hours,
    "get_transport": get_transport,
}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """校验参数并执行工具；未知工具和非法参数都按永久失败处理。"""
    if name not in TOOL_FUNCTIONS:
        raise PermanentToolError(f"未知工具：{name}")

    model = TOOL_INPUT_MODELS[name]
    try:
        payload = model.model_validate(arguments)
    except ValidationError as error:
        raise ToolValidationError(f"工具 {name} 参数不合法：{error}") from error

    return TOOL_FUNCTIONS[name](payload)


# OpenAI 兼容的工具 Schema；模型只会看到名称、描述和参数结构，看不到实现。
TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询城市某天的天气；安排户外或室内行程前使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名称"},
                    "date": {
                        "type": "string",
                        "description": "ISO 日期，例如 2026-11-28",
                    },
                },
                "required": ["city", "date"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_places",
            "description": "按城市和兴趣检索景点或餐厅，返回室内外标签、门票和步行时间。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "interest": {"type": "string", "description": "例如拍照、美食、人文"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_budget",
            "description": "按天数、人数和节奏估算交通、餐饮、门票和住宿预算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 7},
                    "people": {"type": "integer", "minimum": 1, "maximum": 8},
                    "pace": {"type": "string", "enum": ["轻松", "适中", "紧凑"]},
                },
                "required": ["days", "people", "pace"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_opening_hours",
            "description": "查询景点开放时间；没有可靠资料时会失败，不要编造时间。",
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "景点或场馆名称"},
                },
                "required": ["place"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_transport",
            "description": "查询两个地点之间的交通方式、预计时间和费用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string"},
                    "destination": {"type": "string"},
                },
                "required": ["origin", "destination"],
                "additionalProperties": False,
            },
        },
    },
]


def public_tool_names() -> list[str]:
    """返回工具名列表，方便教程和报告展示。"""
    return [spec["function"]["name"] for spec in TOOL_SPECS]
