"""Compare a fixed travel workflow with an adaptive agent loop."""

import sys


# CITY 是两种旅行方案共同处理的目标城市。
CITY = "杭州"
# BUDGET 是任务允许花费的总预算，单位为元。
BUDGET = 600


# 生成固定顺序的四步旅行流程；rainy 只改变地点，不改变步骤顺序。
def fixed_workflow(rainy=False):
    """Always execute the same four stages in the same order."""
    # weather 是根据 rainy 参数得到的示例天气。
    weather = "小雨" if rainy else "多云"
    # place 是根据天气选择、供后续路线步骤使用的地点。
    place = "浙江省博物馆" if rainy else "西湖"
    # steps 是按执行顺序保存的“步骤名 + 结果”列表。
    steps = [
        ("查询天气", f"{CITY}：{weather}"),
        ("搜索景点", place),
        ("估算路线", f"{place} 周边步行约 40 分钟"),
        ("生成计划", f"{CITY} 一日游：{place}，预算 {BUDGET} 元"),
    ]
    return steps


# 根据 state 中已经完成的里程碑，提出下一轮动作。
def propose_next_action(state):
    """Choose one next action from the current state."""
    # state 中的键代表已经完成的里程碑，决定下一轮还缺什么。
    if not state and "--unknown" in sys.argv:
        return {"tool": "delete_everything", "reason": "尝试一个未登记动作"}
    if "weather" not in state:
        return {"tool": "get_weather", "reason": "先根据天气决定室内或室外"}
    if "place" not in state:
        if state["weather"] == "小雨":
            return {"tool": "find_indoor_place", "reason": "下雨时优先室内地点"}
        return {"tool": "find_outdoor_place", "reason": "天气稳定，优先户外拍照"}
    if "route" not in state:
        return {"tool": "estimate_route", "reason": "确认步行强度"}
    return {"tool": "finish", "reason": "天气、地点和路线都已确认"}


# 执行白名单内的动作，并把新结果写回 state。
def execute_action(action, state, rainy=False):
    """Execute only actions explicitly supported by the controller."""
    # tool 是本轮准备执行、需要先经过白名单判断的动作名。
    tool = action["tool"]

    # 仅执行控制器登记过的动作；未知动作会落到函数末尾并抛出异常。
    if tool == "get_weather":
        state["weather"] = "小雨" if rainy else "多云"
        return f"{CITY} 天气：{state['weather']}"
    if tool == "find_indoor_place":
        state["place"] = "浙江省博物馆"
        return "找到室内地点：浙江省博物馆"
    if tool == "find_outdoor_place":
        state["place"] = "西湖"
        return "找到户外地点：西湖"
    if tool == "estimate_route":
        state["route"] = f"{state['place']} 周边步行约 40 分钟"
        return state["route"]
    if tool == "finish":
        return "任务结束"

    raise ValueError(f"不支持的工具：{tool}")


# 运行状态驱动循环，直到产生 finish 或达到最大步骤数。
def run_agent(rainy=False, max_steps=5):
    """Let the planner choose actions until it finishes or runs out of steps."""
    # state 保存天气、地点和路线等已经完成的事实。
    state = {}
    # trace 按顺序保存每一轮的决策与观察结果。
    trace = []

    # 最大步数是硬边界，避免状态判断错误时无限循环。
    for step in range(1, max_steps + 1):
        # action 是本轮规划器提出的工具名、理由和参数。
        action = propose_next_action(state)
        if action["tool"] == "finish":
            trace.append((step, action["reason"], "finish", "计划完成"))
            return state, trace

        # observation 是本轮工具执行后得到的新事实。
        observation = execute_action(action, state, rainy=rainy)
        trace.append((step, action["reason"], action["tool"], observation))

    raise RuntimeError("超过最大步骤数，任务没有结束")


# 打印固定 Workflow 的步骤列表。
def print_steps(title, steps):
    print(title)
    # index 是显示序号，name 是步骤名，result 是该步骤的结果。
    for index, (name, result) in enumerate(steps, start=1):
        print(f"{index}. {name}：{result}")


# 打印 Agent 每一轮的理由、动作和观察结果。
def print_trace(trace):
    print("自主 Agent")
    # step 是轮次，reason 是决策理由，tool 是动作，observation 是观察。
    for step, reason, tool, observation in trace:
        print(f"第 {step} 轮")
        print(f"  理由：{reason}")
        print(f"  动作：{tool}")
        print(f"  观察：{observation}")


# 程序入口：运行 Agent，再打印固定流程、轨迹和方案建议。
def main():
    # 命令行带 --rainy 时，两种方案都改用雨天条件。
    rainy = "--rainy" in sys.argv
    # 在程序入口统一处理被拒绝的动作，并返回非零退出码。
    try:
        # agent_result 是 run_agent() 返回的“最终状态 + 决策轨迹”二元组。
        agent_result = run_agent(rainy=rainy)
    except ValueError as error:
        print(f"Agent 已停止：{error}")
        raise SystemExit(1)

    # agent_state 是任务结束时积累的天气、地点和路线。
    agent_state = agent_result[0]
    # agent_trace 是用于展示和审计的完整轮次记录。
    agent_trace = agent_result[1]
    # fixed_steps 是 Workflow 的完整步骤快照，用于与 Agent 轨迹比较。
    fixed_steps = fixed_workflow(rainy=rainy)

    print_steps("固定 Workflow", fixed_steps)
    print()
    print_trace(agent_trace)
    print()
    print("方案选择")
    print("- 固定 Workflow：步骤稳定、容易审计，适合规则明确的重复任务。")
    print("- 自主 Agent：根据观察选择下一步，适合需要临场判断的任务。")
    print("- 混合方案：权限、预算和最终保存固定；查天气、选地点交给 Agent。")
    print(
        f"- 本次结果：{agent_state['place']}，"
        f"路线为 {agent_state['route']}。"
    )


if __name__ == "__main__":
    main()
