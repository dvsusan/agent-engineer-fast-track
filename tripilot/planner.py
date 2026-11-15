"""Build and execute a dependency-aware multi-step travel plan."""

import json
import sys

from .gateway import TOOL_COSTS, call_tool, create_gateway


# DEFAULT_CITY 是本课示例行程的目标城市。
DEFAULT_CITY = "杭州"
# DEFAULT_DATES 是三日行程使用的固定日期，保证输出可复现。
DEFAULT_DATES = ["2026-11-15", "2026-11-16", "2026-11-17"]
# PLAN_BUDGET 是多步骤任务可使用的虚拟工具预算。
PLAN_BUDGET = 10


# 构建一个包含日期、三日天气、地点和路线的依赖计划。
def build_plan(city=DEFAULT_CITY, dates=None):
    # trip_dates 是本次计划实际使用的一到三天日期列表。
    trip_dates = dates or DEFAULT_DATES
    # plan 按任务 ID 保存每一步的动作、参数、依赖和用途。
    plan = [
        {
            "id": "date",
            "tool": "get_current_date",
            "arguments": {},
            "depends_on": [],
            "purpose": "确认规划基准日期",
        }
    ]

    # date_text 是当前轮要添加的旅行日期。
    for date_text in trip_dates:
        # weather_task 是一天的天气查询任务。
        weather_task = {
            "id": f"weather-{date_text}",
            "tool": "get_weather",
            "arguments": {"city": city, "date": date_text},
            "depends_on": ["date"],
            "purpose": f"查询 {date_text} 的天气",
        }
        plan.append(weather_task)

    # weather_ids 是地点搜索需要等待完成的全部天气任务。
    weather_ids = [f"weather-{date_text}" for date_text in trip_dates]
    plan.append(
        {
            "id": "places",
            "tool": "search_places",
            "arguments": {"city": city, "keyword": "拍照", "limit": 3},
            "depends_on": weather_ids,
            "purpose": "根据天气准备拍照地点",
        }
    )
    plan.append(
        {
            "id": "route",
            "tool": "estimate_route",
            "arguments": {"origin": "西湖", "destination": "九溪烟树"},
            "depends_on": ["places"],
            "purpose": "确认地点之间的步行强度",
        }
    )
    return plan


# 校验工具、任务 ID、依赖引用、依赖环、预算和最大步骤数。
def validate_plan(plan, budget=PLAN_BUDGET, max_steps=8):
    # task_ids 是计划中所有任务 ID 的集合。
    task_ids = {task["id"] for task in plan}
    if len(task_ids) != len(plan):
        raise ValueError("任务 ID 不能重复")

    # unknown_tools 记录网关没有登记、无法计算成本的工具。
    unknown_tools = {
        task["tool"] for task in plan if task["tool"] not in TOOL_COSTS
    }
    if unknown_tools:
        raise ValueError(f"计划使用了未注册工具：{', '.join(sorted(unknown_tools))}")

    # task 是当前正在检查依赖关系的任务。
    for task in plan:
        # unknown_dependencies 记录依赖列表里不存在的任务。
        unknown_dependencies = [
            dependency for dependency in task["depends_on"] if dependency not in task_ids
        ]
        if unknown_dependencies:
            raise ValueError(
                f"任务 {task['id']} 依赖不存在的任务："
                f"{', '.join(unknown_dependencies)}"
            )

    # completed_ids 用于模拟逐步解锁任务，检测依赖环。
    completed_ids = set()
    # pending_ids 是还没有通过依赖检查的任务。
    pending_ids = set(task_ids)
    while pending_ids:
        # ready_ids 是当前所有依赖已经满足的任务。
        ready_ids = {
            task["id"]
            for task in plan
            if task["id"] in pending_ids
            and set(task["depends_on"]).issubset(completed_ids)
        }
        if not ready_ids:
            raise ValueError("计划存在循环依赖，无法开始执行")
        completed_ids.update(ready_ids)
        pending_ids.difference_update(ready_ids)

    # estimated_cost 是计划中所有工具调用的成本总和。
    estimated_cost = sum(TOOL_COSTS[task["tool"]] for task in plan)
    if estimated_cost > budget:
        raise ValueError(f"计划成本 {estimated_cost} 超过预算 {budget}")
    if len(plan) > max_steps:
        raise ValueError(f"计划步骤 {len(plan)} 超过上限 {max_steps}")
    return estimated_cost


# 按依赖顺序执行计划，并在预算或步骤耗尽时停止。
def execute_plan(plan, budget=PLAN_BUDGET, max_steps=8):
    # gateway_state 是整份计划共享的权限、预算和日志。
    gateway_state = create_gateway(
        allowed_tools={
            "get_current_date",
            "get_weather",
            "search_places",
            "estimate_route",
        },
        budget=budget,
    )
    # pending_ids 保存尚未执行的任务 ID。
    pending_ids = {task["id"] for task in plan}
    # completed_ids 保存已经成功执行的任务 ID。
    completed_ids = set()
    # results 保存每个任务的真实工具结果。
    results = {}
    # executed_steps 保存真正执行过的任务数量，而不是循环编号。
    executed_steps = 0

    # step 是当前执行轮次，最大轮数由 max_steps 控制。
    for step in range(1, max_steps + 1):
        if not pending_ids:
            break

        # ready_tasks 是依赖已经满足、可以执行的任务。
        ready_tasks = [
            task
            for task in plan
            if task["id"] in pending_ids
            and set(task["depends_on"]).issubset(completed_ids)
        ]
        if not ready_tasks:
            return {
                "status": "blocked",
                "step": step,
                "error": "没有可执行任务，依赖未满足",
                "results": results,
                "gateway_state": gateway_state,
            }

        # task 是本轮按计划顺序选中的任务。
        task = ready_tasks[0]
        # result 是统一网关对该任务的响应。
        result = call_tool(gateway_state, task["tool"], task["arguments"])
        print(
            f"{step}. {task['id']} -> {task['tool']}：",
            json.dumps(result, ensure_ascii=False),
        )

        if not result["ok"]:
            return {
                "status": "failed",
                "step": step,
                "error": result["error"],
                "results": results,
                "gateway_state": gateway_state,
            }

        results[task["id"]] = result["data"]
        pending_ids.remove(task["id"])
        completed_ids.add(task["id"])
        # executed_steps 更新为刚刚成功完成的真实步骤编号。
        executed_steps = step

    if pending_ids:
        return {
            "status": "stopped",
            "step": max_steps,
            "error": "达到最大步骤数，计划未完成",
            "results": results,
            "gateway_state": gateway_state,
        }

    return {
        "status": "completed",
        "step": executed_steps,
        "results": results,
        "gateway_state": gateway_state,
    }


# 打印计划，帮助学习者在执行前检查依赖和成本。
def print_plan(plan, estimated_cost):
    print(f"计划：{len(plan)} 个任务，预计成本 {estimated_cost}")
    # task 是当前要显示的任务定义。
    for task in plan:
        # dependencies 是当前任务的依赖名称文本。
        dependencies = ", ".join(task["depends_on"]) or "无"
        print(f"- {task['id']}：{task['purpose']}；依赖 {dependencies}")


# 运行默认计划和错误依赖计划，展示预检与执行差异。
def main():
    # plan 是本课要验证和执行的三日旅行计划。
    plan = build_plan()
    if "--bad-plan" in sys.argv:
        plan[1]["depends_on"] = ["missing-task"]

    try:
        # estimated_cost 是预检通过后的计划总成本。
        estimated_cost = validate_plan(plan)
    except ValueError as error:
        print(f"计划预检失败：{error}")
        raise SystemExit(1)

    # 预检后故意制造闭环，演示执行器对意外脏状态的最后一道保护。
    if "--blocked-plan" in sys.argv:
        plan[0]["depends_on"] = ["route"]

    print_plan(plan, estimated_cost)
    print("\n执行计划")
    # execution_budget 是执行器创建网关时实际获得的预算。
    execution_budget = 2 if "--runtime-budget" in sys.argv else PLAN_BUDGET
    # max_steps 是执行器允许尝试的最大任务数。
    max_steps = 3 if "--step-limit" in sys.argv else 8
    # outcome 是执行器返回的最终状态、结果和网关状态。
    outcome = execute_plan(plan, budget=execution_budget, max_steps=max_steps)
    print("\n最终状态：", outcome["status"])
    print("实际步骤：", outcome["step"])
    print("剩余预算：", outcome["gateway_state"]["remaining_budget"])
    if "error" in outcome:
        print("停止原因：", outcome["error"])


if __name__ == "__main__":
    main()
