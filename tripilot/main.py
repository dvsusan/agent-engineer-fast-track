"""Run TripPilot V1 with planning, state, gateway, and reliable delivery."""

import json
from datetime import date
from pathlib import Path

from . import VERSION
from .executor import ResilientExecutor
from .gateway import create_gateway
from .planner import build_plan, validate_plan
from .state_machine import TripStateMachine


# PACKAGE_DIR 是 TripPilot 包所在目录，用于定位默认输入和输出位置。
PACKAGE_DIR = Path(__file__).parent
# PROJECT_DIR 是第 24 课编号目录，统一保存需求、行程和运行记录。
PROJECT_DIR = PACKAGE_DIR.parent / "24-tripilot-project"
# SAMPLE_REQUEST 是命令行和网页共用的默认旅行需求。
SAMPLE_REQUEST = {
    "city": "杭州",
    "dates": ["2026-11-15", "2026-11-16", "2026-11-17"],
    "preference": "拍照，不想走太多路",
    "tool_budget": 10,
}
# ALLOWED_TOOLS 是第 24 课 TripPilot V1 获准使用的全部工具。
ALLOWED_TOOLS = {
    "get_current_date",
    "get_weather",
    "search_places",
    "estimate_route",
}
# MAX_PROJECT_STEPS 是本项目允许执行的最大计划步骤数。
MAX_PROJECT_STEPS = 10


# ProjectError 表示项目请求或执行状态不允许继续。
class ProjectError(ValueError):
    """The project request cannot be completed safely."""


# 校验网页或命令行传入的旅行需求，并返回规范化副本。
def validate_request(payload):
    if not isinstance(payload, dict):
        raise ProjectError("旅行需求必须是 JSON 对象")

    # city 是用户希望游览的城市名称。
    city = str(payload.get("city", "")).strip()
    if not city:
        raise ProjectError("城市不能为空")

    # dates 是固定为三天的旅行日期列表。
    dates = payload.get("dates")
    if not isinstance(dates, list) or len(dates) != 3:
        raise ProjectError("请提供三个旅行日期")
    if len(set(dates)) != 3:
        raise ProjectError("旅行日期不能重复")

    # date_text 是当前正在校验的日期文本。
    for date_text in dates:
        try:
            date.fromisoformat(date_text)
        except (TypeError, ValueError) as error:
            raise ProjectError(
                f"日期格式不正确：{date_text}"
            ) from error

    # preference 是用户的旅行偏好，会写入最终行程。
    preference = str(payload.get("preference", "")).strip()
    if not preference:
        raise ProjectError("旅行偏好不能为空")

    # tool_budget 是本次项目允许消耗的虚拟工具积分。
    tool_budget = payload.get("tool_budget")
    if (
        not isinstance(tool_budget, int)
        or isinstance(tool_budget, bool)
        or not 1 <= tool_budget <= 50
    ):
        raise ProjectError("工具预算必须是 1 到 50 的整数")

    return {
        "city": city,
        "dates": dates,
        "preference": preference,
        "tool_budget": tool_budget,
    }


# 根据计划工具结果生成一份可阅读的三日行程。
def build_itinerary(request, results):
    # weather_ids 按旅行日期排序，保证行程与输入顺序一致。
    weather_ids = [f"weather-{date_text}" for date_text in request["dates"]]
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


# TripPilotService 保存一次规划、发送和补偿的完整会话状态。
class TripPilotService:
    # 初始化服务，并指定行程文件、检查点和运行记录的保存目录。
    def __init__(self, output_dir):
        # output_directory 是本项目所有交付文件所在的目录。
        self.output_directory = Path(output_dir)
        # reset() 建立一次尚未开始的空会话。
        self.reset()

    # 清空内存中的项目状态，但保留已经写出的文件。
    def reset(self):
        # request 是当前会话使用的规范化旅行需求。
        self.request = None
        # plan 是当前会话生成的任务列表。
        self.plan = []
        # machine 是推进计划并保存检查点的状态机。
        self.machine = None
        # executor 是处理超时、幂等和补偿的可靠执行器。
        self.executor = None
        # estimated_cost 是计划预检得到的工具成本。
        self.estimated_cost = 0
        # itinerary 是当前会话生成的三日行程 Markdown。
        self.itinerary = ""
        # receipt 是最近一次发送得到的回执。
        self.receipt = None
        # compensation 是最近一次补偿结果。
        self.compensation = None
        # events 记录状态机每一步执行结果，供界面时间线展示。
        self.events = []

    # 执行一次旅行规划；此时只生成行程，不向外部发送。
    def start(self, payload):
        # normalized_request 先校验输入，失败时不会清空已有会话。
        normalized_request = validate_request(payload)
        self.reset()
        # request 是校验后的旅行需求。
        self.request = normalized_request
        self.output_directory.mkdir(parents=True, exist_ok=True)

        # plan 是根据城市和日期生成的多步骤任务计划。
        self.plan = build_plan(
            city=self.request["city"],
            dates=self.request["dates"],
        )
        try:
            # estimated_cost 是计划预检通过后的工具成本。
            self.estimated_cost = validate_plan(
                self.plan,
                budget=self.request["tool_budget"],
                max_steps=MAX_PROJECT_STEPS,
            )
        except (KeyError, ValueError) as error:
            raise ProjectError(f"计划预检失败：{error}") from error

        # gateway_state 是项目共享的工具权限、预算和审计状态。
        gateway_state = create_gateway(
            allowed_tools=ALLOWED_TOOLS,
            budget=self.request["tool_budget"],
        )
        # checkpoint_path 是本次项目运行保存的状态检查点。
        checkpoint_path = self.output_directory / "project-checkpoint.json"
        # self.machine 是推进六个旅行任务的状态机。
        self.machine = TripStateMachine(
            self.plan,
            gateway_state,
            checkpoint_path,
        )
        self.machine.start()

        while self.machine.status == "running":
            # result 是状态机当前步骤的返回结果。
            result = self.machine.step()
            self.events.append(
                {
                    "step": self.machine.current_step,
                    "task": result.get("task"),
                    "status": result["status"],
                    "ok": result["ok"],
                    "remaining_budget": self.machine.gateway_state[
                        "remaining_budget"
                    ],
                    "error": result.get("error", ""),
                }
            )

        if self.machine.status != "completed":
            raise ProjectError(
                f"TripPilot 未完成，当前状态：{self.machine.status}"
            )

        # self.itinerary 是最终生成的三日行程 Markdown。
        self.itinerary = build_itinerary(
            self.request,
            self.machine.results,
        )
        # itinerary_path 是本次行程的保存位置。
        itinerary_path = self.output_directory / "itinerary.md"
        itinerary_path.write_text(self.itinerary, encoding="utf-8")

        # self.executor 为后续发送和补偿保存幂等状态。
        self.executor = ResilientExecutor()
        return self.snapshot()

    # 使用固定幂等键发送当前行程。
    def send(self):
        if self.machine is None or self.machine.status != "completed":
            raise ProjectError("请先完成旅行规划")
        if self.executor is None:
            raise ProjectError("发送执行器尚未初始化")

        # send_request 是一个业务动作对应的稳定发送请求。
        send_request = {
            "idempotency_key": (
                f"trip-{self.request['dates'][0]}-ui-v{VERSION}"
            ),
            "content": self.itinerary,
        }
        # self.receipt 是超时重试后得到的发送回执。
        self.receipt = self.executor.send_itinerary(send_request)
        self.compensation = None
        self._write_run_record()
        return self.snapshot()

    # 补偿已经发送的行程；重复调用会返回已经取消的标记。
    def compensate(self):
        if self.receipt is None:
            raise ProjectError("当前没有可以补偿的发送回执")

        # self.compensation 是本次补偿操作的返回结果。
        self.compensation = self.executor.compensate(
            self.receipt["receipt_id"]
        )
        self.receipt["status"] = self.compensation["status"]
        self._write_run_record()
        return self.snapshot()

    # 生成界面需要的纯 JSON 状态。
    def snapshot(self):
        if self.machine is None:
            # request 在未开始时使用默认样例，方便页面预填表单。
            request = self.request or dict(SAMPLE_REQUEST)
            return {
                "status": "idle",
                "request": request,
                "tasks": [],
                "events": [],
                "estimated_cost": 0,
                "remaining_budget": request["tool_budget"],
                "current_step": 0,
                "itinerary": "",
                "receipt": None,
                "compensation": None,
                "send_attempts": 0,
                "tool_calls": 0,
                "results": {},
            }

        # tasks 把计划定义和当前完成状态合并给界面。
        tasks = [
            {
                "id": task["id"],
                "purpose": task["purpose"],
                "tool": task["tool"],
                "depends_on": task["depends_on"],
                "status": (
                    "completed"
                    if task["id"] in self.machine.completed_ids
                    else "pending"
                ),
            }
            for task in self.plan
        ]
        return {
            "status": self.machine.status,
            "request": self.request,
            "tasks": tasks,
            "events": self.events,
            "estimated_cost": self.estimated_cost,
            "remaining_budget": self.machine.gateway_state[
                "remaining_budget"
            ],
            "current_step": self.machine.current_step,
            "itinerary": self.itinerary,
            "receipt": self.receipt,
            "compensation": self.compensation,
            "send_attempts": (
                self.executor.send_attempts if self.executor else 0
            ),
            "tool_calls": len(self.machine.gateway_state["call_log"]),
            "results": self.machine.results,
        }

    # 把发送和补偿结果写入项目运行记录。
    def _write_run_record(self):
        # run_record 是界面、命令行和验收共同使用的证据文件。
        run_record = {
            "version": VERSION,
            "status": self.machine.status,
            "steps": self.machine.current_step,
            "estimated_tool_cost": self.estimated_cost,
            "remaining_tool_budget": self.machine.gateway_state[
                "remaining_budget"
            ],
            "tool_calls": len(self.machine.gateway_state["call_log"]),
            "send_attempts": self.executor.send_attempts,
            "send_receipt": self.receipt,
            "compensation": self.compensation,
            "files": [
                "itinerary.md",
                "run.json",
                "project-checkpoint.json",
            ],
        }
        # run_path 是本次项目运行记录文件。
        run_path = self.output_directory / "run.json"
        run_path.write_text(
            json.dumps(run_record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# 运行命令行版 TripPilot V1，并允许入口指定需求文件和输出目录。
def main(request_path=PROJECT_DIR / "sample_request.json", output_dir=PROJECT_DIR):
    # request_file 是命令行本次读取的旅行需求文件。
    request_file = Path(request_path)
    # request 是从 JSON 文件读取的旅行需求。
    request = json.loads(request_file.read_text(encoding="utf-8"))
    # service 负责执行计划、生成行程、发送并保存记录。
    service = TripPilotService(output_dir)
    try:
        state = service.start(request)
        state = service.send()
    except ProjectError as error:
        print(f"TripPilot 失败：{error}")
        raise SystemExit(1) from error

    # event 是命令行回放的一条任务执行记录。
    for event in state["events"]:
        print(json.dumps(event, ensure_ascii=False))
    print(f"TripPilot 状态：{state['status']}")
    print(f"完成步骤：{state['current_step']}")
    print(f"剩余预算：{state['remaining_budget']}")
    print(f"发送回执：{state['receipt']}")
    print("已保存行程：itinerary.md")
    print("已保存记录：run.json")


if __name__ == "__main__":
    main()
