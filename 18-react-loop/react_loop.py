"""Run a minimal structured ReAct loop with a whitelist of tools."""

import json
import sys


# CITY 是本课旅行任务的目标城市。
CITY = "杭州"
# DATE 是固定查询日期，保证 Mock 输出可复现。
DATE = "2026-11-15"


# 表示模型提出了控制器不允许执行的动作。
class RejectedActionError(Exception):
    """The model proposed an action the controller will not execute."""


# 天气工具：把输入参数整理成结构化天气结果。
def get_weather(arguments):
    return {
        "city": arguments["city"],
        "date": arguments["date"],
        "condition": "多云",
        "temperature": "20-25°C",
    }


# 地点工具：按 limit 截取让学习者容易观察的少量结果。
def search_places(arguments):
    return [
        {"name": "西湖", "reason": "适合拍照，步行路线成熟"},
        {"name": "九溪烟树", "reason": "自然景观适合拍照"},
        {"name": "南宋御街", "reason": "适合傍晚散步"},
    ][: arguments["limit"]]


# 路线工具：用固定步行时间模拟外部路线服务。
def estimate_route(arguments):
    return f"{arguments['place']} 周边建议步行约 40 分钟"


# 白名单同时决定模型“能提议什么”和代码“允许执行什么”。
TOOLS = {
    "get_weather": get_weather,
    "search_places": search_places,
    "estimate_route": estimate_route,
}


# Mock 模型只根据 observation 判断已完成工具，再提出下一步。
def mock_model(messages, bad_tool=False):
    """Choose one action from the conversation history."""
    # 只读取由代码追加的 observation，避免把模型自己说的话当成工具结果。
    # observations 是代码执行工具后留下的结构化观察记录。
    observations = [
        json.loads(message["content"])
        for message in messages
        if message["role"] == "observation"
    ]
    # completed_tools 用集合记录哪些工具已经成功执行。
    completed_tools = {item["tool"] for item in observations}

    if not observations:
        if bad_tool:
            return {
                "thought": "尝试执行一个未登记的动作",
                "action": "delete_everything",
                "arguments": {},
            }
        return {
            "thought": "先确认杭州当天的天气",
            "action": "get_weather",
            "arguments": {"city": CITY, "date": DATE},
        }

    if "get_weather" in completed_tools and "search_places" not in completed_tools:
        return {
            "thought": "天气适合户外活动，再搜索拍照地点",
            "action": "search_places",
            "arguments": {"city": CITY, "limit": 2},
        }

    if "search_places" in completed_tools and "estimate_route" not in completed_tools:
        return {
            "thought": "地点已确定，还要确认步行强度",
            "action": "estimate_route",
            "arguments": {"place": "西湖"},
        }

    return {
        "thought": "天气、地点和路线信息已经足够",
        "action": "finish",
        "arguments": {},
    }


# 把模型提议转换成真实 Python 工具调用。
def execute_action(proposal):
    # action 是模型提议的工具名，执行前必须检查白名单。
    action = proposal["action"]
    # arguments 是模型为工具填写的参数。
    arguments = proposal["arguments"]

    # 先检查白名单，再查找 Python 函数；模型输出不能直接成为执行权限。
    if action not in TOOLS:
        raise RejectedActionError(f"工具不在白名单：{action}")

    # result 是工具真实返回值，不是模型自己声称的结果。
    result = TOOLS[action](arguments)
    return result


# 控制器维护消息历史，并负责每一轮的执行顺序和停止条件。
def run_react(task, bad_tool=False, max_steps=5):
    # messages 是跨轮次状态：包含原任务、模型提议和工具观察。
    messages = [{"role": "user", "content": task}]

    # step 是当前轮次编号，从 1 开始增长。
    for step in range(1, max_steps + 1):
        # proposal 是本轮的结构化 Thought + Action，不会自行执行。
        proposal = mock_model(messages, bad_tool=bad_tool)
        print(f"[第 {step} 轮]")
        print(f"Thought: {proposal['thought']}")
        print(
            "Action: "
            + json.dumps(
                {"tool": proposal["action"], "arguments": proposal["arguments"]},
                ensure_ascii=False,
            )
        )

        # finish 只结束流程，不进入工具执行。
        if proposal["action"] == "finish":
            print("Final: 西湖半日拍照路线，步行约 40 分钟，预算控制在 600 元内。")
            print(f"状态: completed；总轮数: {step}")
            return

        # result 是本轮工具执行后的真实返回值。
        result = execute_action(proposal)
        # 把“调用了哪个工具”和“工具返回什么”一起写回历史。
        observation = {
            "tool": proposal["action"],
            "result": result,
        }
        # observation_text 是追加到消息历史中的 JSON 文本。
        observation_text = json.dumps(observation, ensure_ascii=False)
        print(f"Observation: {observation_text}")
        print()

        messages.append(
            {"role": "assistant", "content": json.dumps(proposal, ensure_ascii=False)}
        )
        messages.append({"role": "observation", "content": observation_text})

    raise RuntimeError("达到最大轮数，Agent 仍未结束")


# 程序入口：创建任务并统一处理非法工具导致的中止。
def main():
    # task 是本课所有轮次共同服务的原始用户目标。
    task = f"{CITY} 一日游，预算 600 元，喜欢拍照，不想走太多路。"
    print(f"任务: {task}\n")

    # 在入口处把拒绝动作转成可被脚本识别的失败状态。
    try:
        run_react(task, bad_tool="--bad-tool" in sys.argv)
    except RejectedActionError as error:
        print(f"已拒绝: {error}")
        print("状态: stopped；未执行任何外部动作。")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
