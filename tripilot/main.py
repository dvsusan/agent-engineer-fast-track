"""Run TripPilot V1 with planning, state, gateway, and reliable delivery."""

import json
from pathlib import Path

from . import VERSION
from .executor import ResilientExecutor
from .gateway import create_gateway
from .planner import build_plan, validate_plan
from .state_machine import TripStateMachine


# PACKAGE_DIR 是 TripPilot 包所在目录，用于定位输入和输出文件。
PACKAGE_DIR = Path(__file__).parent
# PROJECT_DIR 是第 24 课编号目录，统一保存需求、行程和运行记录。
PROJECT_DIR = PACKAGE_DIR.parent / "24-tripilot-project"
# REQUEST_PATH 是 TripPilot V1 的旅行需求文件。
REQUEST_PATH = PROJECT_DIR / "sample_request.json"
# CHECKPOINT_PATH 是项目运行时的最终状态检查点。
CHECKPOINT_PATH = PROJECT_DIR / "project-checkpoint.json"
# ITINERARY_PATH 是最终三日行程文件。
ITINERARY_PATH = PROJECT_DIR / "itinerary.md"
# RUN_PATH 是本次项目运行记录。
RUN_PATH = PROJECT_DIR / "run.json"
# MAX_PROJECT_STEPS 是本项目允许执行的最大计划步骤数。
MAX_PROJECT_STEPS = 10


# 根据计划工具结果生成一份可阅读的三日行程。
def build_itinerary(request, results):
    # weather_ids 按旅行日期排序，保证行程与输入顺序一致。
    weather_ids = [f"weather-{date_text}" for date_text in request["dates"]]
    # place_names 提取地点工具返回的名称列表。
    place_names = [place["name"] for place in results["places"]]
    # route_result 保存路线工具返回的步行信息。
    route_result = results["route"]

    # lines 按 Markdown 行保存最终行程。
    lines = [
        f"# {request['city']}三日旅行计划",
        "",
        f"偏好：{request['preference']}",
        "",
        "## 天气",
    ]
    # index 是日期在行程中的序号，weather_id 对应当天天气任务。
    for index, weather_id in enumerate(weather_ids, start=1):
        # weather 是当天天气工具返回的数据。
        weather = results[weather_id]
        lines.append(
            f"- 第 {index} 天（{weather['date']}）："
            f"{weather['condition']}，{weather['temperature']}"
        )

    lines.extend(["", "## 推荐地点"])
    # place 是地点工具返回的一个推荐地点。
    for place in results["places"]:
        lines.append(f"- {place['name']}：{place['feature']}")

    lines.extend(
        [
            "",
            "## 路线",
            (
                f"- {route_result['origin']} → {route_result['destination']}："
                f"步行约 {route_result['walking_minutes']} 分钟"
            ),
            "",
            "## 执行说明",
            "- 行程由工具结果生成。",
            "- 发送使用幂等键，超时重试不会重复发送。",
        ]
    )
    return "\n".join(lines) + "\n"


# 运行 TripPilot V1，并允许编号目录入口指定输入和输出位置。
def main(request_path=REQUEST_PATH, output_dir=PROJECT_DIR):
    # request_file 是本次运行读取的旅行需求文件。
    request_file = Path(request_path)
    # output_directory 是行程、记录和检查点的保存目录。
    output_directory = Path(output_dir)
    # checkpoint_path 是本次运行使用的状态检查点。
    checkpoint_path = output_directory / "project-checkpoint.json"
    # itinerary_path 是本次运行生成的行程文件。
    itinerary_path = output_directory / "itinerary.md"
    # run_path 是本次运行记录文件。
    run_path = output_directory / "run.json"

    # request 是从 JSON 文件读取的旅行需求。
    request = json.loads(request_file.read_text(encoding="utf-8"))
    # plan 是根据城市和日期生成的多步骤任务计划。
    plan = build_plan(city=request["city"], dates=request["dates"])
    # estimated_cost 是计划预检得到的工具成本。
    estimated_cost = validate_plan(
        plan,
        budget=request["tool_budget"],
        max_steps=MAX_PROJECT_STEPS,
    )

    # gateway_state 是项目共享的工具权限、预算和审计状态。
    gateway_state = create_gateway(
        allowed_tools={"get_current_date", "get_weather", "search_places", "estimate_route"},
        budget=request["tool_budget"],
    )
    # machine 是本次项目使用的可恢复状态机。
    machine = TripStateMachine(plan, gateway_state, checkpoint_path)
    machine.start()

    while machine.status == "running":
        # step_result 是当前任务执行结果。
        step_result = machine.step()
        print(json.dumps(step_result, ensure_ascii=False))

    if machine.status != "completed":
        print(f"TripPilot 未完成：{machine.status}")
        raise SystemExit(1)

    # itinerary 是最终生成的三日行程 Markdown。
    itinerary = build_itinerary(request, machine.results)
    itinerary_path.write_text(itinerary, encoding="utf-8")

    # executor 负责用幂等键发送最终行程。
    executor = ResilientExecutor()
    # send_request 是带唯一幂等键的发送请求。
    send_request = {
        "idempotency_key": f"trip-{request['dates'][0]}-v{VERSION}",
        "content": itinerary,
    }
    # receipt 是最终发送回执。
    receipt = executor.send_itinerary(send_request)

    # run_record 汇总交付文件、预算、步骤和发送结果。
    run_record = {
        "version": VERSION,
        "status": machine.status,
        "steps": machine.current_step,
        "estimated_tool_cost": estimated_cost,
        "remaining_tool_budget": machine.gateway_state["remaining_budget"],
        "tool_calls": len(machine.gateway_state["call_log"]),
        "send_receipt": receipt,
        "files": [itinerary_path.name, run_path.name, checkpoint_path.name],
    }
    run_path.write_text(
        json.dumps(run_record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"TripPilot 状态：{machine.status}")
    print(f"完成步骤：{machine.current_step}")
    print(f"剩余预算：{machine.gateway_state['remaining_budget']}")
    print(f"发送回执：{receipt}")
    print(f"已保存行程：{itinerary_path.name}")
    print(f"已保存记录：{run_path.name}")


if __name__ == "__main__":
    main()
