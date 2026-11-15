"""Run a travel plan as a checkpointed, resumable state machine."""

import json
from datetime import date
from pathlib import Path

from .gateway import call_tool, create_gateway
from .planner import PLAN_BUDGET, build_plan


# CHECKPOINT_VERSION 用来识别检查点格式，未来字段变化时可以安全升级。
CHECKPOINT_VERSION = 1

# VALID_STATES 是任务状态机允许出现的全部状态。
VALID_STATES = {
    "new",
    "running",
    "paused",
    "completed",
    "failed",
    "blocked",
    "cancelled",
}

# ALLOWED_TRANSITIONS 规定每个状态下一步可以迁移到哪里。
ALLOWED_TRANSITIONS = {
    "new": {"running", "cancelled"},
    "running": {"paused", "completed", "failed", "blocked", "cancelled"},
    "paused": {"running", "cancelled"},
    "failed": {"cancelled"},
    "blocked": {"cancelled"},
    "completed": set(),
    "cancelled": set(),
}


# CheckpointError 表示检查点无法安全恢复。
class CheckpointError(ValueError):
    """The checkpoint is missing, invalid, or uses an unsupported format."""


# 状态机负责推进计划、保存检查点、恢复和取消。
class TripStateMachine:
    # 初始化状态机，并保存计划、网关状态和检查点位置。
    def __init__(self, plan, gateway_state, checkpoint_path):
        # plan 是有依赖关系的任务列表。
        self.plan = plan
        # gateway_state 保存工具权限、预算和审计日志。
        self.gateway_state = gateway_state
        # checkpoint_path 是恢复任务时读取的 JSON 文件。
        self.checkpoint_path = Path(checkpoint_path)
        # status 是状态机当前所处阶段。
        self.status = "new"
        # completed_ids 保存已经成功完成的任务 ID。
        self.completed_ids = set()
        # results 保存每个已完成任务的工具结果。
        self.results = {}
        # current_step 保存已经执行的步骤数量。
        self.current_step = 0
        # cancel_reason 保存用户取消任务时给出的原因。
        self.cancel_reason = ""

    # 把当前状态序列化，供暂停、恢复和排障使用。
    def to_checkpoint(self):
        # checkpoint 是写入磁盘的完整状态快照。
        checkpoint = {
            "version": CHECKPOINT_VERSION,
            "status": self.status,
            "current_step": self.current_step,
            "completed_ids": sorted(self.completed_ids),
            "cancel_reason": self.cancel_reason,
            "plan": self.plan,
            "results": self.results,
            "gateway": {
                "allowed_tools": sorted(self.gateway_state["allowed_tools"]),
                "remaining_budget": self.gateway_state["remaining_budget"],
                "as_of": self.gateway_state["as_of"].isoformat(),
                "call_log": self.gateway_state["call_log"],
            },
        }
        return checkpoint

    # 先写临时文件再替换目标文件，避免留下写了一半的 JSON。
    def save(self):
        # temp_path 是本次检查点写入使用的临时文件。
        temp_path = self.checkpoint_path.with_name(
            f"{self.checkpoint_path.name}.tmp"
        )
        temp_path.write_text(
            json.dumps(self.to_checkpoint(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.checkpoint_path)

    # 检查状态迁移是否合法，更新状态后立即保存检查点。
    def transition(self, next_status):
        if next_status not in VALID_STATES:
            raise ValueError(f"未知状态：{next_status}")

        # allowed_states 是当前状态允许迁移到的状态集合。
        allowed_states = ALLOWED_TRANSITIONS[self.status]
        if next_status not in allowed_states:
            raise ValueError(f"不允许从 {self.status} 迁移到 {next_status}")

        self.status = next_status
        self.save()

    # 从 new 状态进入 running。
    def start(self):
        self.transition("running")

    # 暂停当前任务，保留已完成结果和剩余预算。
    def pause(self):
        self.transition("paused")

    # 从 paused 状态继续执行。
    def resume(self):
        self.transition("running")

    # 取消任务，并记录取消原因。
    def cancel(self, reason):
        if self.status in {"completed", "cancelled"}:
            raise ValueError(f"{self.status} 状态不能取消")
        self.cancel_reason = reason
        self.transition("cancelled")

    # 找到依赖已满足且尚未完成的下一个任务。
    def next_task(self):
        # pending_tasks 保留尚未完成但依赖已满足的任务。
        pending_tasks = [
            task
            for task in self.plan
            if task["id"] not in self.completed_ids
            and set(task["depends_on"]).issubset(self.completed_ids)
        ]
        if not pending_tasks:
            return None
        return pending_tasks[0]

    # 执行一个任务，并把新状态写入检查点。
    def step(self):
        if self.status == "new":
            self.start()
        if self.status != "running":
            return {"ok": False, "status": self.status, "error": "状态机未运行"}

        # task 是本轮依赖已满足的任务。
        task = self.next_task()
        if task is None:
            # remaining_ids 记录尚未完成的任务 ID。
            remaining_ids = {
                item["id"]
                for item in self.plan
                if item["id"] not in self.completed_ids
            }
            if remaining_ids:
                self.transition("blocked")
                return {
                    "ok": False,
                    "status": self.status,
                    "task": None,
                    "error": "还有任务，但没有就绪任务",
                }

            self.transition("completed")
            return {"ok": True, "status": self.status, "task": None}

        # result 是网关对当前任务的响应。
        result = call_tool(
            self.gateway_state,
            task["tool"],
            task["arguments"],
        )
        self.current_step += 1

        if not result["ok"]:
            self.transition("failed")
            return {
                "ok": False,
                "status": self.status,
                "task": task["id"],
                "error": result["error"],
            }

        self.results[task["id"]] = result["data"]
        self.completed_ids.add(task["id"])

        # remaining_ids 判断是否还有未完成任务。
        remaining_ids = {
            item["id"] for item in self.plan if item["id"] not in self.completed_ids
        }
        if not remaining_ids:
            self.transition("completed")
        else:
            self.save()

        return {
            "ok": True,
            "status": self.status,
            "task": task["id"],
            "data": result["data"],
            "remaining_budget": self.gateway_state["remaining_budget"],
        }


# 从检查点文件恢复状态机；这是“进程重启后继续”的关键入口。
def load_state_machine(checkpoint_path):
    # saved_path 是本次要读取的检查点文件。
    saved_path = Path(checkpoint_path)
    try:
        # checkpoint 是从磁盘读取出来的 JSON 字典。
        checkpoint = json.loads(saved_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckpointError(f"检查点无法读取或不是合法 JSON：{error}") from error

    if not isinstance(checkpoint, dict):
        raise CheckpointError("检查点顶层必须是 JSON 对象")

    # required_fields 是恢复状态机时必须存在的顶层字段。
    required_fields = {
        "version",
        "status",
        "current_step",
        "completed_ids",
        "cancel_reason",
        "plan",
        "results",
        "gateway",
    }
    # missing_fields 记录当前检查点缺少的字段。
    missing_fields = required_fields.difference(checkpoint)
    if missing_fields:
        raise CheckpointError(
            f"检查点缺少字段：{', '.join(sorted(missing_fields))}"
        )
    if checkpoint["version"] != CHECKPOINT_VERSION:
        raise CheckpointError(
            f"检查点版本 {checkpoint['version']} 不受支持，"
            f"当前版本是 {CHECKPOINT_VERSION}"
        )
    if checkpoint["status"] not in VALID_STATES:
        raise CheckpointError(f"检查点包含未知状态：{checkpoint['status']}")

    # gateway_data 是检查点中保存的权限、预算、日期边界和日志。
    gateway_data = checkpoint["gateway"]
    if not isinstance(gateway_data, dict):
        raise CheckpointError("检查点的 gateway 字段必须是 JSON 对象")
    # required_gateway_fields 是重建工具网关必须保存的数据。
    required_gateway_fields = {
        "allowed_tools",
        "remaining_budget",
        "as_of",
        "call_log",
    }
    # missing_gateway_fields 记录网关数据中缺少的字段。
    missing_gateway_fields = required_gateway_fields.difference(gateway_data)
    if missing_gateway_fields:
        raise CheckpointError(
            "检查点 gateway 缺少字段："
            f"{', '.join(sorted(missing_gateway_fields))}"
        )

    try:
        # saved_as_of 是恢复网关时继续使用的日期边界。
        saved_as_of = date.fromisoformat(gateway_data["as_of"])
    except (TypeError, ValueError) as error:
        raise CheckpointError("检查点的 gateway.as_of 不是合法日期") from error

    # gateway_state 用保存的数据重建网关运行状态。
    gateway_state = create_gateway(
        allowed_tools=gateway_data["allowed_tools"],
        budget=gateway_data["remaining_budget"],
        as_of=saved_as_of,
    )
    gateway_state["call_log"] = gateway_data["call_log"]

    # machine 是根据检查点重建出来的状态机对象。
    machine = TripStateMachine(
        plan=checkpoint["plan"],
        gateway_state=gateway_state,
        checkpoint_path=saved_path,
    )
    machine.status = checkpoint["status"]
    machine.current_step = checkpoint["current_step"]
    machine.completed_ids = set(checkpoint["completed_ids"])
    machine.results = checkpoint["results"]
    machine.cancel_reason = checkpoint["cancel_reason"]
    return machine


# 故意写入半截 JSON，展示恢复入口怎样拒绝损坏检查点。
def demo_bad_checkpoint(output_directory):
    # broken_path 是本次错误演示使用的临时检查点。
    broken_path = output_directory / "broken-checkpoint.json"
    broken_path.write_text('{"version": 1, "status": "paused",', encoding="utf-8")
    try:
        load_state_machine(broken_path)
    except CheckpointError as error:
        print(f"检查点恢复失败：{error}")
    finally:
        broken_path.unlink(missing_ok=True)


# 运行本课演示；scenario 用于选择正常流程或损坏检查点诊断。
def main(output_dir=None, scenario=None):
    # output_directory 是编号课程目录，检查点文件统一保存在这里。
    output_directory = Path(output_dir) if output_dir else Path(__file__).parent
    if scenario == "--bad-checkpoint":
        demo_bad_checkpoint(output_directory)
        return

    # checkpoint_path 是暂停恢复演示使用的检查点。
    checkpoint_path = output_directory / "checkpoint.json"
    # plan 是第 21 课已经创建的三日旅行计划。
    plan = build_plan()
    # gateway_state 是初始网关状态，预算中留出了路线调用成本。
    gateway_state = create_gateway(
        allowed_tools={
            "get_current_date",
            "get_weather",
            "search_places",
            "estimate_route",
        },
        budget=PLAN_BUDGET,
    )
    # machine 是第一次运行的状态机。
    machine = TripStateMachine(plan, gateway_state, checkpoint_path)
    machine.start()

    print("初始运行")
    # step_number 表示本次演示要执行的步骤序号。
    for step_number in range(2):
        # result 是当前单步执行结果。
        result = machine.step()
        print(json.dumps(result, ensure_ascii=False))
    machine.pause()
    print(f"已暂停，检查点：{checkpoint_path.name}")

    # resumed_machine 模拟新进程从检查点恢复。
    resumed_machine = load_state_machine(checkpoint_path)
    resumed_machine.resume()
    print("恢复运行")
    while resumed_machine.status == "running":
        # step_result 是恢复后每一步的执行结果。
        step_result = resumed_machine.step()
        print(json.dumps(step_result, ensure_ascii=False))

    # cancel_path 是第二个状态机使用的独立检查点。
    cancel_path = output_directory / "cancel-checkpoint.json"
    # cancel_gateway 是取消演示使用的独立网关状态。
    cancel_gateway = create_gateway(
        allowed_tools={
            "get_current_date",
            "get_weather",
            "search_places",
            "estimate_route",
        },
        budget=PLAN_BUDGET,
    )
    # cancel_machine 用于演示中途取消。
    cancel_machine = TripStateMachine(plan, cancel_gateway, cancel_path)
    cancel_machine.step()
    cancel_machine.cancel("用户改变出行计划")
    print(f"取消演示状态：{cancel_machine.status}")
    print(f"取消原因：{cancel_machine.cancel_reason}")
    print(f"最终运行状态：{resumed_machine.status}")
    print(f"最终完成步骤：{resumed_machine.current_step}")


if __name__ == "__main__":
    main()
