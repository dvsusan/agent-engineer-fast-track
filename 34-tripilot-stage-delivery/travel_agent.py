"""Build an interactive, permission-aware TripPilot planning agent."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
STAGE_ROOT = PROJECT_DIR.parent
sys.path.insert(0, str(STAGE_ROOT))

from tripilot.access_control import (  # noqa: E402
    AccessControlledRetriever,
    Principal,
    VersionedDocumentIndex,
    VersionedResultCache,
    load_documents,
)
from tripilot.memory import PreferenceMemory  # noqa: E402
from tripilot.rag import HashedEmbedding  # noqa: E402


DOCUMENTS_FILE = STAGE_ROOT / "33-rag-permissions-updates" / "documents.json"
UPDATE_FILE = STAGE_ROOT / "33-rag-permissions-updates" / "route-v2.json"
PREFERENCES_FILE = PROJECT_DIR / "travel-preferences.json"

CITY = "杭州"
TRIP_ID = "hz-2026"
MAX_DAYS = 4

USERS = {
    "alice": {
        "id": "alice",
        "name": "Alice",
        "label": "行程所有者",
        "roles": ["owner", "planner"],
        "avatar": "A",
        "description": "可以查看私有预算和规划者资料",
    },
    "bob": {
        "id": "bob",
        "name": "Bob",
        "label": "同行成员",
        "roles": ["member"],
        "avatar": "B",
        "description": "只能查看公开预算和路线资料",
    },
}

PACES = {
    "轻松": {
        "label": "轻松",
        "description": "每天 3 个主要地点，保留午休和自由活动",
        "places_per_day": 3,
        "route_buffer_minutes": 35,
    },
    "适中": {
        "label": "适中",
        "description": "每天 4 个主要地点，景点与街区穿插",
        "places_per_day": 4,
        "route_buffer_minutes": 25,
    },
    "紧凑": {
        "label": "紧凑",
        "description": "每天 5 个主要地点，适合第一次来杭州",
        "places_per_day": 5,
        "route_buffer_minutes": 18,
    },
}

INTERESTS = {
    "拍照": {"label": "拍照", "keywords": ["拍照", "摄影", "出片", "机位"]},
    "自然": {"label": "自然", "keywords": ["自然", "山水", "徒步", "公园"]},
    "美食": {"label": "美食", "keywords": ["美食", "小吃", "吃", "餐厅"]},
    "博物馆": {"label": "博物馆", "keywords": ["博物馆", "展览", "室内", "历史"]},
    "文化": {"label": "文化", "keywords": ["文化", "寺庙", "古街", "运河"]},
    "亲子": {"label": "亲子", "keywords": ["亲子", "孩子", "家庭", "老人"]},
    "夜景": {"label": "夜景", "keywords": ["夜景", "夜游", "晚上", "灯光"]},
}

PLACES = [
    {
        "name": "西湖",
        "area": "西湖景区",
        "interests": ["自然", "拍照", "文化"],
        "duration": 120,
        "ticket": 0,
        "indoor": False,
        "description": "沿湖步行与经典湖景，适合作为第一天主线",
    },
    {
        "name": "断桥残雪",
        "area": "西湖景区",
        "interests": ["拍照", "文化"],
        "duration": 45,
        "ticket": 0,
        "indoor": False,
        "description": "西湖经典取景点，清晨和傍晚更从容",
    },
    {
        "name": "九溪烟树",
        "area": "西湖景区",
        "interests": ["自然", "拍照"],
        "duration": 120,
        "ticket": 0,
        "indoor": False,
        "description": "溪谷与林荫步道，适合慢节奏和自然摄影",
    },
    {
        "name": "南宋御街",
        "area": "湖滨商圈",
        "interests": ["美食", "文化", "夜景"],
        "duration": 90,
        "ticket": 0,
        "indoor": False,
        "description": "历史街区与本地小吃集中，适合傍晚散步",
    },
    {
        "name": "浙江省博物馆",
        "area": "西湖文化广场",
        "interests": ["博物馆", "文化", "亲子"],
        "duration": 150,
        "ticket": 0,
        "indoor": True,
        "description": "雨天友好的室内展馆，适合补充浙江历史脉络",
    },
    {
        "name": "中国茶叶博物馆",
        "area": "龙井",
        "interests": ["文化", "自然", "亲子"],
        "duration": 120,
        "ticket": 0,
        "indoor": True,
        "description": "茶文化与山景结合，室内展陈和户外茶园都可安排",
    },
    {
        "name": "灵隐寺",
        "area": "灵隐",
        "interests": ["文化", "自然"],
        "duration": 150,
        "ticket": 75,
        "indoor": False,
        "description": "杭州代表性古寺与山林步道，建议预留半天",
    },
    {
        "name": "河坊街",
        "area": "吴山",
        "interests": ["美食", "文化", "夜景"],
        "duration": 90,
        "ticket": 0,
        "indoor": False,
        "description": "集中体验小吃、老字号和夜间街区氛围",
    },
    {
        "name": "西溪湿地",
        "area": "西溪",
        "interests": ["自然", "亲子", "拍照"],
        "duration": 180,
        "ticket": 80,
        "indoor": False,
        "description": "湿地水网与慢行路线，适合亲子或半日自然体验",
    },
    {
        "name": "湖滨步行街",
        "area": "湖滨商圈",
        "interests": ["拍照", "美食", "夜景"],
        "duration": 90,
        "ticket": 0,
        "indoor": False,
        "description": "西湖边的城市漫步路线，夜景和餐饮选择集中",
    },
    {
        "name": "杭州工艺美术博物馆",
        "area": "拱宸桥",
        "interests": ["博物馆", "亲子", "文化"],
        "duration": 120,
        "ticket": 0,
        "indoor": True,
        "description": "室内展馆与手工艺体验，适合雨天和亲子行程",
    },
    {
        "name": "拱宸桥",
        "area": "拱宸桥",
        "interests": ["文化", "拍照", "夜景"],
        "duration": 75,
        "ticket": 0,
        "indoor": False,
        "description": "京杭大运河地标，傍晚灯光和桥边街区适合拍照",
    },
    {
        "name": "龙井村",
        "area": "龙井",
        "interests": ["自然", "拍照", "文化"],
        "duration": 120,
        "ticket": 0,
        "indoor": False,
        "description": "茶园、山路和村落景观，适合半日慢行",
    },
    {
        "name": "良渚博物院",
        "area": "良渚",
        "interests": ["博物馆", "亲子", "拍照"],
        "duration": 150,
        "ticket": 0,
        "indoor": True,
        "description": "系统了解良渚文明，室内展陈适合全天候安排",
    },
    {
        "name": "湘湖",
        "area": "湘湖",
        "interests": ["自然", "亲子", "拍照"],
        "duration": 150,
        "ticket": 0,
        "indoor": False,
        "description": "比西湖更疏朗的湖景，适合慢节奏和家庭出行",
    },
    {
        "name": "小河直街",
        "area": "拱宸桥",
        "interests": ["拍照", "美食", "文化", "夜景"],
        "duration": 90,
        "ticket": 0,
        "indoor": False,
        "description": "运河边的小街与咖啡馆，适合傍晚闲逛",
    },
]

SAMPLE_PROMPTS = [
    "周末去杭州两天，喜欢拍照和美食，节奏轻松，总预算 3000 元",
    "带父母游杭州三天，少走路，想看博物馆和自然风景",
    "杭州下雨天一日游，优先室内展馆，预算 800 元",
]


class PlanningInputError(ValueError):
    """The travel request cannot be planned safely."""


def _next_day():
    return (date.today() + timedelta(days=1)).isoformat()


def _parse_days(text):
    mappings = {
        "一日": 1,
        "一天": 1,
        "1天": 1,
        "两日": 2,
        "两天": 2,
        "2天": 2,
        "三日": 3,
        "三天": 3,
        "3天": 3,
        "四日": 4,
        "四天": 4,
        "4天": 4,
    }
    for marker, value in mappings.items():
        if marker in text:
            return value
    return None


def _parse_budget(text):
    match = re.search(r"(?:预算|花费|控制在|不超过)[^\d]{0,8}(\d{3,6})", text)
    return int(match.group(1)) if match else None


def parse_prompt(prompt, base):
    """Extract obvious preferences from a Chinese natural-language request."""
    text = str(prompt or "").strip()
    if not text:
        return base

    parsed = dict(base)
    for city in [CITY]:
        if city in text:
            parsed["city"] = city
            break

    days = _parse_days(text)
    if days is not None:
        parsed["days"] = days

    budget = _parse_budget(text)
    if budget is not None:
        parsed["budget"] = budget

    for pace, setting in PACES.items():
        if pace in text:
            parsed["pace"] = pace
            break

    matched_interests = [
        label
        for label, setting in INTERESTS.items()
        if any(keyword in text for keyword in setting["keywords"])
    ]
    if matched_interests:
        parsed["interests"] = matched_interests

    if any(keyword in text for keyword in ["下雨", "雨天", "降雨"]):
        parsed["rain_mode"] = True

    return parsed


def _stable_number(text, minimum, maximum):
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=4).hexdigest()
    return minimum + int(digest, 16) % (maximum - minimum + 1)


def _weather_for(day_text, rain_mode=False):
    if rain_mode or _stable_number(day_text, 0, 6) == 0:
        condition = "小雨"
        temperature = "16-20°C"
    else:
        condition = "多云"
        temperature = "18-24°C"
    return {
        "date": day_text,
        "condition": condition,
        "temperature": temperature,
        "indoor_recommended": condition in {"小雨", "中雨", "大雨"},
    }


def _place_score(place, interests, rain_mode, pace):
    score = 3 * len(set(place["interests"]).intersection(interests))
    if rain_mode and place["indoor"]:
        score += 5
    if rain_mode and not place["indoor"]:
        score -= 2
    if pace == "轻松" and place["duration"] <= 120:
        score += 2
    if pace == "紧凑" and place["duration"] >= 120:
        score += 1
    return score


def _select_places(interests, rain_mode, pace, limit):
    ranked = sorted(
        PLACES,
        key=lambda place: (
            _place_score(place, interests, rain_mode, pace),
            -place["ticket"],
            place["name"],
        ),
        reverse=True,
    )
    selected = ranked[:limit]
    if rain_mode:
        selected.sort(key=lambda place: (not place["indoor"], -len(set(place["interests"]).intersection(interests))))
    return selected


def _route_minutes(origin, destination, pace):
    if origin["area"] == destination["area"]:
        minutes = _stable_number(f"{origin['name']}-{destination['name']}", 8, 16)
    else:
        minutes = _stable_number(f"{origin['name']}-{destination['name']}", 18, 32)
    if pace == "轻松":
        minutes += 5
    return minutes


def _add_minutes(time_text, minutes):
    hour, minute = [int(part) for part in time_text.split(":")]
    value = datetime(2026, 1, 1, hour, minute) + timedelta(minutes=minutes)
    return value.strftime("%H:%M")


def _format_date(day_text):
    value = date.fromisoformat(day_text)
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][value.weekday()]
    return f"{value.month} 月 {value.day} 日 · {weekday}"


def _normalize_request(payload):
    base = {
        "user_id": "alice",
        "prompt": "",
        "city": CITY,
        "start_date": _next_day(),
        "days": 2,
        "budget": 3000,
        "pace": "轻松",
        "interests": ["拍照", "美食"],
        "rain_mode": False,
        "remember": True,
        "simulate_failure": False,
    }
    if not isinstance(payload, dict):
        raise PlanningInputError("请求必须是 JSON 对象")

    request = dict(base)
    request.update({key: payload[key] for key in base if key in payload})
    request = parse_prompt(request.get("prompt", ""), request)

    if request["user_id"] not in USERS:
        raise PlanningInputError("未知用户，请选择 Alice 或 Bob")
    if request["city"] != CITY:
        raise PlanningInputError("当前演示工作区只接入杭州目的地")

    try:
        start_date = date.fromisoformat(str(request["start_date"]))
    except ValueError as error:
        raise PlanningInputError("出发日期必须使用 YYYY-MM-DD") from error
    if start_date < date.today():
        raise PlanningInputError("出发日期不能早于今天")

    try:
        days = int(request["days"])
    except (TypeError, ValueError) as error:
        raise PlanningInputError("旅行天数必须是整数") from error
    if not 1 <= days <= MAX_DAYS:
        raise PlanningInputError(f"旅行天数必须在 1 到 {MAX_DAYS} 天之间")

    try:
        budget = int(request["budget"])
    except (TypeError, ValueError) as error:
        raise PlanningInputError("预算必须是整数") from error
    if not 500 <= budget <= 50000:
        raise PlanningInputError("预算必须在 500 到 50000 元之间")

    if request["pace"] not in PACES:
        raise PlanningInputError("节奏只能是轻松、适中或紧凑")

    interests = [item for item in request["interests"] if item in INTERESTS]
    if not interests:
        raise PlanningInputError("至少选择一个兴趣主题")
    if len(interests) > 4:
        raise PlanningInputError("最多选择四个兴趣主题")

    request.update(
        {
            "start_date": start_date.isoformat(),
            "days": days,
            "budget": budget,
            "interests": list(dict.fromkeys(interests)),
            "rain_mode": bool(request["rain_mode"]),
            "remember": bool(request["remember"]),
            "simulate_failure": bool(request["simulate_failure"]),
            "prompt": str(request.get("prompt", ""))[:500],
        }
    )
    return request


class TravelPlanningAgent:
    """Plan a trip from request to itinerary, evidence, and memory update."""

    TOOL_COSTS = {
        "read_preferences": 0,
        "get_weather": 1,
        "search_places": 2,
        "retrieve_evidence": 2,
        "estimate_route": 1,
        "remember_preferences": 1,
    }

    def __init__(self, preferences_path=PREFERENCES_FILE):
        self.preferences_path = Path(preferences_path)
        self.memory = PreferenceMemory(self.preferences_path)
        self.index = VersionedDocumentIndex(load_documents(DOCUMENTS_FILE))
        update_documents = load_documents(UPDATE_FILE)
        for document in update_documents:
            current = self.index._documents.get(document.doc_id)
            if current is None or document.version > current.version:
                self.index.upsert(document)
        self.retriever = AccessControlledRetriever(
            self.index,
            VersionedResultCache(ttl_seconds=300),
            HashedEmbedding(128),
        )

    def bootstrap(self):
        """Return form defaults, user scopes, and a read-only preview plan."""
        preview = self.plan(
            {
                "user_id": "alice",
                "start_date": _next_day(),
                "days": 2,
                "budget": 3000,
                "pace": "轻松",
                "interests": ["拍照", "美食"],
                "remember": False,
            }
        )
        return {
            "users": list(USERS.values()),
            "paces": PACES,
            "interests": [
                {"id": key, "label": value["label"]}
                for key, value in INTERESTS.items()
            ],
            "city": {
                "name": CITY,
                "region": "浙江",
                "notice": "当前工作台使用本地工具和课程证据库，不连接真实预订系统。",
            },
            "defaults": {
                "user_id": "alice",
                "start_date": _next_day(),
                "days": 2,
                "budget": 3000,
                "pace": "轻松",
                "interests": ["拍照", "美食"],
                "rain_mode": False,
                "remember": True,
                "simulate_failure": False,
            },
            "sample_prompts": SAMPLE_PROMPTS,
            "preview": preview,
        }

    def plan(self, payload):
        """Execute the full planning loop and return a UI-ready payload."""
        started = time.perf_counter()
        request = _normalize_request(payload)
        principal = Principal(
            request["user_id"],
            TRIP_ID,
            USERS[request["user_id"]]["roles"],
        )
        steps = []
        tool_calls = []
        budget = 40

        def add_step(title, detail, *, tool=None, arguments=None, result=None, status="done", duration_ms=0):
            steps.append(
                {
                    "index": len(steps) + 1,
                    "title": title,
                    "detail": detail,
                    "tool": tool,
                    "arguments": arguments or {},
                    "result": result,
                    "status": status,
                    "duration_ms": duration_ms,
                }
            )

        def call_tool(name, arguments, function):
            nonlocal budget
            cost = self.TOOL_COSTS[name]
            if cost > budget:
                raise PlanningInputError(f"Agent 工具预算不足：需要 {cost}，剩余 {budget}")
            tool_started = time.perf_counter()
            try:
                result = function()
            except Exception as error:
                # 失败请求同样消耗了一次调用成本，预算不能因为异常而“回血”。
                budget -= cost
                tool_calls.append(
                    {
                        "tool": name,
                        "arguments": arguments,
                        "ok": False,
                        "cost": cost,
                        "error": str(error),
                    }
                )
                raise
            budget -= cost
            duration_ms = round((time.perf_counter() - tool_started) * 1000, 1)
            tool_calls.append(
                {
                    "tool": name,
                    "arguments": arguments,
                    "ok": True,
                    "cost": cost,
                    "duration_ms": duration_ms,
                }
            )
            return result, duration_ms

        add_step(
            "理解旅行目标",
            f"识别到 {request['days']} 天杭州行程，节奏为{request['pace']}，兴趣为{'、'.join(request['interests'])}。",
            result=request,
        )

        preferences, memory_duration = call_tool(
            "read_preferences",
            {"user_id": request["user_id"]},
            lambda: self.memory.active_preferences(request["user_id"]),
        )
        add_step(
            "读取长期偏好",
            f"从偏好记忆读取到 {len(preferences)} 项设置；本次表单输入优先于旧偏好。",
            tool="read_preferences",
            arguments={"user_id": request["user_id"]},
            result=preferences,
            duration_ms=memory_duration,
        )

        dates = [
            (date.fromisoformat(request["start_date"]) + timedelta(days=offset)).isoformat()
            for offset in range(request["days"])
        ]
        weather = []
        recovered_failures = []
        for day_text in dates:
            def fetch_weather(day_text=day_text):
                # 只让第一天失败，方便读者看到“失败、降级、继续”的完整轨迹。
                if request["simulate_failure"] and not recovered_failures:
                    raise TimeoutError("天气服务响应超过 800ms")
                return _weather_for(day_text, request["rain_mode"])

            try:
                forecast, duration_ms = call_tool(
                    "get_weather",
                    {"city": request["city"], "date": day_text},
                    fetch_weather,
                )
            except TimeoutError as error:
                forecast = _weather_for(day_text, request["rain_mode"])
                recovered_failures.append({"date": day_text, "error": str(error)})
                add_step(
                    "天气工具超时，启用降级",
                    "保留最近一次可用天气并标记为本地估算，行程继续生成，不把失败伪装成成功。",
                    tool="get_weather",
                    arguments={"city": request["city"], "date": day_text},
                    result={**forecast, "fallback": True},
                    status="fallback",
                )
                duration_ms = 0
            weather.append(forecast)
            add_step(
                f"查询 {_format_date(day_text)} 天气",
                f"{forecast['condition']}，{forecast['temperature']}；"
                + ("优先安排室内地点。" if forecast["indoor_recommended"] else "适合安排户外与城市漫步。"),
                tool="get_weather",
                arguments={"city": request["city"], "date": day_text},
                result=forecast,
                duration_ms=duration_ms,
            )

        pace_setting = PACES[request["pace"]]
        selected_places, search_duration = call_tool(
            "search_places",
            {"city": request["city"], "interests": request["interests"], "limit": request["days"] * pace_setting["places_per_day"]},
            lambda: _select_places(
                request["interests"],
                request["rain_mode"],
                request["pace"],
                request["days"] * pace_setting["places_per_day"],
            ),
        )
        add_step(
            "搜索兴趣地点",
            f"从本地地点库筛出 {len(selected_places)} 个地点，并优先匹配兴趣与天气。",
            tool="search_places",
            arguments={"city": request["city"], "interests": request["interests"]},
            result=[place["name"] for place in selected_places],
            duration_ms=search_duration,
        )

        evidence_started = time.perf_counter()
        evidence, blocked, acl_trace = self._retrieve_evidence(
            request,
            principal,
            for_photo="拍照" in request["interests"],
        )
        evidence_duration = round((time.perf_counter() - evidence_started) * 1000, 1)
        tool_calls.append(
            {
                "tool": "retrieve_evidence",
                "arguments": {"user_id": request["user_id"], "trip_id": TRIP_ID},
                "ok": True,
                "cost": self.TOOL_COSTS["retrieve_evidence"],
                "duration_ms": evidence_duration,
            }
        )
        budget -= self.TOOL_COSTS["retrieve_evidence"]
        add_step(
            "执行权限感知检索",
            f"按 ACL 过滤后获得 {len(evidence)} 条可用证据，拦截 {len(blocked)} 份无权资料。",
            tool="retrieve_evidence",
            arguments={"user_id": request["user_id"], "interests": request["interests"]},
            result={"evidence": [item["doc_id"] for item in evidence], "blocked": [item["doc_id"] for item in blocked]},
            duration_ms=evidence_duration,
        )

        itinerary = []
        route_time = 0
        place_cursor = 0
        for day_index, forecast in enumerate(weather):
            count = pace_setting["places_per_day"]
            day_places = []
            for _ in range(count):
                if place_cursor < len(selected_places):
                    day_places.append(selected_places[place_cursor])
                    place_cursor += 1
            if not day_places:
                day_places = selected_places[:count]

            day_routes = []
            for origin, destination in zip(day_places, day_places[1:]):
                minutes = _route_minutes(origin, destination, request["pace"])
                day_routes.append(
                    {
                        "from": origin["name"],
                        "to": destination["name"],
                        "minutes": minutes,
                    }
                )
                route_time += minutes

            items = []
            current_time = "09:00"
            for item_index, place in enumerate(day_places):
                items.append(
                    {
                        "time": current_time,
                        "name": place["name"],
                        "area": place["area"],
                        "duration": place["duration"],
                        "ticket": place["ticket"],
                        "kind": "室内" if place["indoor"] else "户外",
                        "reason": place["description"],
                        "interests": place["interests"],
                    }
                )
                current_time = _add_minutes(current_time, place["duration"] + 30)
                if item_index < len(day_routes):
                    items.append(
                        {
                            "time": current_time,
                            "name": f"前往 {day_routes[item_index]['to']}",
                            "area": "城市移动",
                            "duration": day_routes[item_index]["minutes"],
                            "ticket": 0,
                            "kind": "步行 / 公交",
                            "reason": "预留真实移动与休息时间",
                            "interests": [],
                        }
                    )
                    current_time = _add_minutes(current_time, day_routes[item_index]["minutes"])

            day_ticket = sum(place["ticket"] for place in day_places)
            itinerary.append(
                {
                    "day": day_index + 1,
                    "date": forecast["date"],
                    "date_label": _format_date(forecast["date"]),
                    "weather": forecast,
                    "theme": self._day_theme(day_places, forecast),
                    "items": items,
                    "routes": day_routes,
                    "ticket_total": day_ticket,
                }
            )

        add_step(
            "估算地点间移动",
            f"为 {request['days']} 天行程估算 {sum(len(day['routes']) for day in itinerary)} 段路线，共 {route_time} 分钟。",
            tool="estimate_route",
            arguments={"segments": sum(len(day["routes"]) for day in itinerary)},
            result={"total_minutes": route_time},
            duration_ms=round(route_time * 0.4, 1),
        )
        for _ in range(sum(len(day["routes"]) for day in itinerary)):
            budget -= self.TOOL_COSTS["estimate_route"]
            tool_calls.append(
                {
                    "tool": "estimate_route",
                    "arguments": {},
                    "ok": True,
                    "cost": self.TOOL_COSTS["estimate_route"],
                    "duration_ms": 0,
                }
            )

        hotel_per_night = self._hotel_price(evidence, request["user_id"])
        nights = max(request["days"] - 1, 0)
        meal_total = 90 * request["days"]
        ticket_total = sum(day["ticket_total"] for day in itinerary)
        hotel_total = hotel_per_night * nights
        local_transport = 30 * request["days"]
        planned_total = hotel_total + meal_total + ticket_total + local_transport
        remaining = request["budget"] - planned_total

        memory_result = {"status": "skipped"}
        if request["remember"]:
            memory_result, memory_duration = call_tool(
                "remember_preferences",
                {"user_id": request["user_id"]},
                lambda: self._remember_request(request),
            )
            add_step(
                "更新长期偏好",
                "将本次节奏、预算和兴趣写入长期记忆，下一次规划会作为默认上下文。",
                tool="remember_preferences",
                arguments={"user_id": request["user_id"]},
                result=memory_result,
                duration_ms=memory_duration,
            )

        add_step(
            "生成行程与预算",
            f"生成 {request['days']} 天行程；预计花费 {planned_total} 元，"
            + (f"结余 {remaining} 元。" if remaining >= 0 else f"超出 {abs(remaining)} 元。"),
            result={"planned_total": planned_total, "remaining": remaining},
        )

        warnings = []
        if remaining < 0:
            warnings.append(f"当前方案预计超出预算 {abs(remaining)} 元，可以减少一天或降低住宿标准。")
        if any(day["weather"]["indoor_recommended"] for day in itinerary):
            warnings.append("行程包含降雨天气，户外地点已预留调整空间，出发前请再次确认天气。")
        if recovered_failures:
            warnings.append("本次天气工具出现超时，页面已切换到本地缓存天气；结果可用于演示，出行前仍需重新查询。")
        if blocked:
            warnings.append(f"当前身份有 {len(blocked)} 份资料不可见，相关建议不会进入答案。")
        if request["user_id"] == "bob" and "拍照" in request["interests"]:
            warnings.append("摄影集合点属于规划者资料，Bob 只能获得公开景点建议。")
        warnings.append("当前使用本地演示工具和课程证据库，不替代真实天气、交通与预订确认。")

        checks = {
            "request_valid": True,
            "weather_checked": len(weather) == request["days"],
            "acl_filtered_before_answer": not {
                item["doc_id"] for item in evidence
            }.intersection({item["doc_id"] for item in blocked}),
            "itinerary_complete": len(itinerary) == request["days"],
            "budget_calculated": planned_total >= 0,
            "trace_recorded": len(steps) >= 5,
            "memory_policy_visible": memory_result.get("status") in {"created", "updated", "unchanged", "skipped"},
            "tool_failure_recovered": not request["simulate_failure"] or bool(recovered_failures),
        }

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return {
            "status": "completed",
            "request": request,
            "principal": {
                "user_id": principal.user_id,
                "name": USERS[principal.user_id]["name"],
                "label": USERS[principal.user_id]["label"],
                "roles": list(principal.roles),
                "scope": principal.scope_key(),
            },
            "summary": {
                "title": f"{request['city']} {request['days']} 天{request['pace']}行程",
                "subtitle": f"为 {USERS[request['user_id']]['name']} 生成，"
                f"覆盖 {'、'.join(request['interests'])}",
                "planned_total": planned_total,
                "budget": request["budget"],
                "remaining": remaining,
                "hotel_per_night": hotel_per_night,
                "evidence_count": len(evidence),
                "blocked_count": len(blocked),
                "tool_call_count": len(tool_calls),
                "elapsed_ms": elapsed_ms,
            },
            "itinerary": itinerary,
            "evidence": evidence,
            "blocked_evidence": blocked,
            "diagnostics": {
                "steps": steps,
                "tool_calls": tool_calls,
                "budget_used": 40 - budget,
                "budget_total": 40,
                "recovered_failures": recovered_failures,
                "acl": acl_trace,
                "checks": checks,
            },
            "preferences": self.memory.active_preferences(request["user_id"]),
            "memory_update": memory_result,
            "warnings": warnings,
            "assistant_note": self._assistant_note(request, itinerary, evidence, remaining),
        }

    def _retrieve_evidence(self, request, principal, for_photo=False):
        queries = [
            ("route", "杭州下雨龙翔桥地铁站西湖出口路线"),
            ("budget", "西湖附近酒店预算多少"),
        ]
        if for_photo:
            queries.append(("photo", "杭州雨天摄影集合点在哪里"))
        if request.get("prompt"):
            queries.append(("request", request["prompt"]))

        citations = []
        blocked = []
        seen_citations = set()
        acl_events = []
        now = time.monotonic()
        for query_key, question in queries:
            result, trace = self.retriever.retrieve(question, principal, now)
            now += 1
            acl_events.append(
                {
                    "query": query_key,
                    **trace["acl"],
                    "cache": trace.get("cache", {}),
                    "index_revision": trace.get("index_revision"),
                }
            )
            for item in result["citations"]:
                if item["doc_id"] in seen_citations:
                    continue
                seen_citations.add(item["doc_id"])
                citations.append(
                    {
                        **item,
                        "number": len(citations) + 1,
                        "query": query_key,
                        "text": result["answer"],
                    }
                )
            for event in trace["acl"]["denied"]:
                if any(item["doc_id"] == event["doc_id"] for item in blocked):
                    continue
                blocked.append(
                    {
                        "doc_id": event["doc_id"],
                        "reason": event["reason"],
                        "version": event["version"],
                    }
                )
        return citations, blocked, acl_events

    def _hotel_price(self, evidence, user_id):
        for item in evidence:
            if item["doc_id"] == "hotel-budget-private" and user_id == "alice":
                match = re.search(r"(\d+) 元/晚", item["text"])
                if match:
                    return int(match.group(1))
            if item["doc_id"] == "hotel-budget-public":
                match = re.search(r"(\d+) 到 (\d+) 元/晚", item["text"])
                if match:
                    return int(match.group(1))
        return 800

    def _remember_request(self, request):
        results = []
        results.append(
            self.memory.remember(
                request["user_id"],
                "pace",
                request["pace"],
                source="planner",
                recorded_on=date.today().isoformat(),
            )
        )
        results.append(
            self.memory.remember(
                request["user_id"],
                "interests",
                "、".join(request["interests"]),
                source="planner",
                recorded_on=date.today().isoformat(),
            )
        )
        results.append(
            self.memory.remember(
                request["user_id"],
                "travel_budget",
                str(request["budget"]),
                source="planner",
                recorded_on=date.today().isoformat(),
            )
        )
        return {"status": "updated", "items": results}

    def _day_theme(self, places, forecast):
        themes = []
        for place in places:
            for interest in place["interests"]:
                if interest not in themes:
                    themes.append(interest)
        if forecast["indoor_recommended"]:
            themes.insert(0, "雨天备选")
        return " · ".join(themes[:3])

    def _assistant_note(self, request, itinerary, evidence, remaining):
        first_day = itinerary[0]
        first_place = next(
            (
                item["name"]
                for item in first_day["items"]
                if item["kind"] not in {"城市移动", "步行 / 公交"}
            ),
            "第一个景点",
        )
        conclusion = "预算仍有结余" if remaining >= 0 else "预算可能不足"
        evidence_note = f"并附上 {len(evidence)} 条可访问证据" if evidence else "但当前没有可引用的证据"
        return (
            f"已为 {USERS[request['user_id']]['name']} 生成 {request['days']} 天行程。"
            f"第一天建议先游览 {first_place}，{conclusion}。"
            f"行程经过天气、路线、权限与偏好检查，{evidence_note}。"
        )
