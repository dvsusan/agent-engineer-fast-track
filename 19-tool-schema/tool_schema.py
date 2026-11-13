"""Validate function-call arguments before executing three travel tools."""

import json
import sys
from datetime import date


# Schema 只描述工具契约；真正的 Python 函数注册在 TOOL_FUNCTIONS。
TOOL_SPECS = {
    "get_current_date": {
        "description": "获取今天的日期",
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
        "description": "按城市和兴趣搜索旅行地点",
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
}


# 表示 Function Call 的参数不符合注册 Schema。
class ToolArgumentError(ValueError):
    """A function call does not match the registered tool schema."""


# 执行前统一检查工具名、必填项、额外字段和参数规则。
def validate_arguments(tool_name, arguments):
    # 工具名、必填项、未声明参数都在进入具体类型检查前解决。
    if tool_name not in TOOL_SPECS:
        raise ToolArgumentError(f"未注册的工具：{tool_name}")

    # schema 保存当前工具的完整参数规则。
    schema = TOOL_SPECS[tool_name]["parameters"]
    # required 是调用时必须出现的参数名列表。
    required = schema.get("required", [])
    # properties 保存每个已声明参数的类型、范围和格式规则。
    properties = schema.get("properties", {})

    # missing 记录调用中缺少的必填参数。
    missing = [name for name in required if name not in arguments]
    if missing:
        raise ToolArgumentError(f"缺少参数：{', '.join(missing)}")

    # extra 记录 Schema 没有声明的参数，防止拼写错误被静默接受。
    extra = [name for name in arguments if name not in properties]
    if extra:
        raise ToolArgumentError(f"存在未声明参数：{', '.join(extra)}")

    # name 是参数名，value 是模型传给该参数的实际值。
    for name, value in arguments.items():
        # rule 是当前参数的完整规则字典。
        rule = properties[name]
        # expected 是规则声明的参数类型。
        expected = rule["type"]

        if expected == "string" and not isinstance(value, str):
            raise ToolArgumentError(f"{name} 必须是字符串")
        # bool 是 int 的子类，必须显式排除，避免 True 被当成整数 1。
        if expected == "integer" and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise ToolArgumentError(f"{name} 必须是整数")
        if "minLength" in rule and len(value) < rule["minLength"]:
            raise ToolArgumentError(f"{name} 不能为空")
        if "minimum" in rule and value < rule["minimum"]:
            raise ToolArgumentError(f"{name} 不能小于 {rule['minimum']}")
        if "maximum" in rule and value > rule["maximum"]:
            raise ToolArgumentError(f"{name} 不能大于 {rule['maximum']}")
        if rule.get("format") == "date":
            try:
                # fromisoformat 只接受标准日期，拒绝“明天”这类自然语言。
                date.fromisoformat(value)
            except ValueError as error:
                raise ToolArgumentError(f"{name} 必须使用 YYYY-MM-DD") from error


# 日期工具不需要参数，返回固定日期以保持示例可复现。
def get_current_date(_arguments):
    return {"date": "2026-11-15"}


# 天气工具只执行到这里时，参数已经通过 Schema 检查。
def get_weather(arguments):
    return {
        "city": arguments["city"],
        "date": arguments["date"],
        "condition": "多云",
        "temperature": "20-25°C",
    }


# 地点工具从固定的虚拟数据中返回结果，避免依赖外部服务。
def search_places(arguments):
    # places 是本课用于演示函数调用的虚拟地点列表。
    places = [
        {"name": "西湖", "feature": "湖景与拍照"},
        {"name": "九溪烟树", "feature": "自然景观"},
        {"name": "南宋御街", "feature": "街区散步"},
    ]
    return places[: arguments["limit"]]


# 工具名到真实 Python 函数的映射，只有校验通过后才会使用。
TOOL_FUNCTIONS = {
    "get_current_date": get_current_date,
    "get_weather": get_weather,
    "search_places": search_places,
}


# 生成模拟 Function Call；bad_arguments 只修改日期用于失败演示。
def mock_function_calls(bad_arguments=False):
    # 模拟模型返回的三次 Function Call，便于离线复现同一份参数。
    # calls 依次保存三个“工具名 + 参数”调用请求。
    calls = [
        {"name": "get_current_date", "arguments": {}},
        {
            "name": "get_weather",
            "arguments": {"city": "杭州", "date": "2026-11-15"},
        },
        {
            "name": "search_places",
            "arguments": {"city": "杭州", "keyword": "拍照", "limit": 2},
        },
    ]
    if bad_arguments:
        calls[1]["arguments"]["date"] = "明天"
    return calls


# Function Call 的统一入口：先校验，再分发执行。
def execute_function_call(call):
    # name 是模型请求调用的工具名。
    name = call["name"]
    # arguments 是模型为这个工具填写的参数。
    arguments = call["arguments"]
    # 校验通过后才按工具名查找函数，防止直接执行模型给出的任意名称。
    validate_arguments(name, arguments)
    return TOOL_FUNCTIONS[name](arguments)


# 程序入口：展示工具列表并逐条执行模拟 Function Call。
def main():
    # calls 是本轮要逐条处理的 Function Call 列表。
    calls = mock_function_calls(bad_arguments="--bad-args" in sys.argv)

    print("可用工具")
    # name 是工具名，spec 是该工具的完整 Schema。
    for name, spec in TOOL_SPECS.items():
        # required 只用于终端展示，方便对照 Schema 与调用结果。
        required = ", ".join(spec["parameters"]["required"]) or "无必填参数"
        print(f"- {name}：{spec['description']}；{required}")
    print()

    # index 是调用序号，call 是当前要处理的 Function Call。
    for index, call in enumerate(calls, start=1):
        print(f"[函数调用 {index}]")
        print(json.dumps(call, ensure_ascii=False))
        try:
            # result 是当前工具通过校验后的执行结果。
            result = execute_function_call(call)
        except ToolArgumentError as error:
            print(f"参数校验失败：{error}")
            raise SystemExit(1)

        print("参数校验：通过")
        print(f"执行结果：{json.dumps(result, ensure_ascii=False)}")
        print()

    print("完成：三次工具调用都通过 Schema 校验。")


if __name__ == "__main__":
    main()
