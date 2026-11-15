"""Central gateway for validating, authorizing, pricing, and executing tools."""

import json
from datetime import date


# TODAY 是本课模拟环境中的当前日期。
TODAY = date(2026, 11, 15)


# TOOL_SPECS 描述模型可以选择的工具及其参数契约。
TOOL_SPECS = {
    "get_current_date": {
        "description": "获取当前日期",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "get_weather": {
        "description": "查询指定城市和日期的天气",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "minLength": 1},
                "date": {"type": "string", "format": "date"},
            },
            "required": ["city", "date"],
        },
    },
    "search_places": {
        "description": "按城市和兴趣搜索地点",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "minLength": 1},
                "keyword": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["city", "keyword", "limit"],
        },
    },
    "estimate_route": {
        "description": "估算地点之间的步行时间",
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "minLength": 1},
                "destination": {"type": "string", "minLength": 1},
            },
            "required": ["origin", "destination"],
        },
    },
}


# TOOL_COSTS 是每次工具调用的内部预算价格，单位为本课虚拟积分。
TOOL_COSTS = {
    "get_current_date": 0,
    "get_weather": 1,
    "search_places": 2,
    "estimate_route": 1,
}


# GatewayError 是所有网关拒绝原因的共同父类。
class GatewayError(Exception):
    """The gateway rejected a tool call."""


# ToolArgumentError 表示参数没有通过 Schema 校验。
class ToolArgumentError(GatewayError):
    """The tool arguments are invalid."""


# ToolPermissionError 表示当前任务没有调用该工具的权限。
class ToolPermissionError(GatewayError):
    """The tool is not allowed for this task."""


# ToolBudgetError 表示工具成本会超过剩余预算。
class ToolBudgetError(GatewayError):
    """The tool call would exceed the remaining budget."""


# ToolDateError 表示请求日期早于当前日期。
class ToolDateError(GatewayError):
    """The requested date is outside the allowed range."""


# 校验工具名、必填参数、额外字段、类型、范围和日期格式。
def validate_arguments(tool_name, arguments):
    # tool 是本次调用在 Schema 中对应的工具定义。
    tool = TOOL_SPECS.get(tool_name)
    if tool is None:
        raise ToolArgumentError(f"未注册的工具：{tool_name}")

    # schema 保存参数对象规则。
    schema = tool["parameters"]
    # required 是必须出现的参数名列表。
    required = schema.get("required", [])
    # properties 保存每个参数的详细校验规则。
    properties = schema.get("properties", {})

    # missing 记录本次调用缺少的必填参数。
    missing = [name for name in required if name not in arguments]
    if missing:
        raise ToolArgumentError(f"缺少参数：{', '.join(missing)}")

    # extra 记录 Schema 没有声明的多余参数。
    extra = [name for name in arguments if name not in properties]
    if extra:
        raise ToolArgumentError(f"存在未声明参数：{', '.join(extra)}")

    # parameter_name 是当前正在检查的参数名。
    for parameter_name, value in arguments.items():
        # rule 是当前参数的完整规则。
        rule = properties[parameter_name]
        # expected_type 是 Schema 声明的 Python 类型名称。
        expected_type = rule["type"]

        if expected_type == "string" and not isinstance(value, str):
            raise ToolArgumentError(f"{parameter_name} 必须是字符串")
        if expected_type == "integer" and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise ToolArgumentError(f"{parameter_name} 必须是整数")
        if "minLength" in rule and len(value) < rule["minLength"]:
            raise ToolArgumentError(f"{parameter_name} 不能为空")
        if "minimum" in rule and value < rule["minimum"]:
            raise ToolArgumentError(
                f"{parameter_name} 不能小于 {rule['minimum']}"
            )
        if "maximum" in rule and value > rule["maximum"]:
            raise ToolArgumentError(
                f"{parameter_name} 不能大于 {rule['maximum']}"
            )
        if rule.get("format") == "date":
            try:
                date.fromisoformat(value)
            except ValueError as error:
                raise ToolArgumentError(
                    f"{parameter_name} 必须使用 YYYY-MM-DD"
                ) from error


# 获取当前日期；参数为空，保留参数位是为了统一工具调用接口。
def get_current_date(_arguments):
    return {"date": TODAY.isoformat()}


# 返回确定性的天气结果，避免示例依赖真实天气服务。
def get_weather(arguments):
    return {
        "city": arguments["city"],
        "date": arguments["date"],
        "condition": "多云",
        "temperature": "20-25°C",
    }


# 从固定的虚拟地点库中搜索并限制返回数量。
def search_places(arguments):
    # places 是本课用于旅行规划的虚拟地点列表。
    places = [
        {"name": "西湖", "feature": "湖景与拍照"},
        {"name": "九溪烟树", "feature": "自然景观"},
        {"name": "南宋御街", "feature": "街区散步"},
        {"name": "浙江省博物馆", "feature": "室内展览"},
    ]
    return places[: arguments["limit"]]


# 用固定结果模拟路线服务。
def estimate_route(arguments):
    return {
        "origin": arguments["origin"],
        "destination": arguments["destination"],
        "walking_minutes": 40,
    }


# TOOL_FUNCTIONS 把通过校验的工具名映射到真实 Python 函数。
TOOL_FUNCTIONS = {
    "get_current_date": get_current_date,
    "get_weather": get_weather,
    "search_places": search_places,
    "estimate_route": estimate_route,
}


# 创建网关状态，记录权限、预算、当前日期和审计日志。
def create_gateway(allowed_tools, budget, as_of=TODAY):
    # allowed_tools_set 使用集合提高权限判断的查找速度。
    allowed_tools_set = set(allowed_tools)
    # gateway_state 是后续每次 call_tool() 共享的可变状态。
    gateway_state = {
        "allowed_tools": allowed_tools_set,
        "remaining_budget": budget,
        "as_of": as_of,
        "call_log": [],
    }
    return gateway_state


# 所有工具调用统一从这里进入，依次检查权限、Schema、日期和预算。
def call_tool(gateway_state, tool_name, arguments):
    # allowed_tools 是当前任务获准使用的工具集合。
    allowed_tools = gateway_state["allowed_tools"]
    # remaining_budget 是调用前剩余的工具预算。
    remaining_budget = gateway_state["remaining_budget"]

    try:
        if tool_name not in allowed_tools:
            raise ToolPermissionError(f"没有权限调用工具：{tool_name}")

        validate_arguments(tool_name, arguments)

        # requested_date 是参数中的日期文本，只在参数含 date 时使用。
        requested_date = (
            date.fromisoformat(arguments["date"]) if "date" in arguments else None
        )
        if requested_date is not None and requested_date < gateway_state["as_of"]:
            raise ToolDateError("不能查询今天以前的旅行天气")

        # tool_cost 是本次调用要扣除的虚拟积分。
        tool_cost = TOOL_COSTS[tool_name]
        if tool_cost > remaining_budget:
            raise ToolBudgetError(
                f"预算不足：需要 {tool_cost}，剩余 {remaining_budget}"
            )

        # tool_result 是真实工具函数的返回值。
        tool_result = TOOL_FUNCTIONS[tool_name](arguments)
        gateway_state["remaining_budget"] -= tool_cost

        # log_item 保存本次调用的审计信息。
        log_item = {
            "tool": tool_name,
            "arguments": arguments,
            "cost": tool_cost,
            "ok": True,
        }
        gateway_state["call_log"].append(log_item)

        return {
            "ok": True,
            "tool": tool_name,
            "data": tool_result,
            "remaining_budget": gateway_state["remaining_budget"],
        }
    except GatewayError as error:
        # failure 是统一格式的拒绝结果，调用方不需要解析不同异常类型。
        failure = {
            "ok": False,
            "tool": tool_name,
            "error": str(error),
            "remaining_budget": gateway_state["remaining_budget"],
        }
        gateway_state["call_log"].append(
            {"tool": tool_name, "arguments": arguments, "ok": False, "error": str(error)}
        )
        return failure


# 运行网关演示，覆盖成功、预算不足、权限不足和过期日期。
def main():
    # allowed_tools 故意不包含 estimate_route，用于演示权限拒绝。
    allowed_tools = {"get_current_date", "get_weather", "search_places"}
    # gateway_state 只给 2 个积分，用于演示预算耗尽。
    gateway_state = create_gateway(allowed_tools=allowed_tools, budget=2)

    # demo_calls 依次展示网关的不同检查分支。
    demo_calls = [
        ("get_current_date", {}),
        ("get_weather", {"city": "杭州", "date": "2026-11-15"}),
        ("search_places", {"city": "杭州", "keyword": "拍照", "limit": 2}),
        ("estimate_route", {"origin": "西湖", "destination": "九溪烟树"}),
        ("get_weather", {"city": "杭州", "date": "2026-11-14"}),
    ]

    # index 是演示序号，tool_name 和 arguments 组成当前调用。
    for index, (tool_name, arguments) in enumerate(demo_calls, start=1):
        print(f"[调用 {index}] {tool_name}")
        # result 是网关对本次调用的统一响应。
        result = call_tool(gateway_state, tool_name, arguments)
        print(json.dumps(result, ensure_ascii=False))
        print()

    print(f"剩余预算：{gateway_state['remaining_budget']}")
    print(f"审计记录：{len(gateway_state['call_log'])} 条")


if __name__ == "__main__":
    main()
