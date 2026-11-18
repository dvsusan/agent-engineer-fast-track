"""Assemble a bounded, inspectable context package for TripPilot."""

import json
from dataclasses import dataclass
from pathlib import Path

from .main import SAMPLE_REQUEST
from .planner import build_plan, execute_plan, validate_plan


# ContextBudgetError 表示必装上下文无法放进当前预算。
class ContextBudgetError(ValueError):
    """The required context blocks do not fit in the budget."""


# ContextBlock 是一个可以单独计数、排序和舍弃的上下文片段。
@dataclass(frozen=True)
class ContextBlock:
    # block_id 是上下文块在本次装配中的唯一编号。
    block_id: str
    # label 是读者和排障界面看到的中文名称。
    label: str
    # kind 表示规则、目标、历史或证据等类别。
    kind: str
    # priority 越大，预算不足时越应优先保留。
    priority: int
    # required 表示该块缺少时整份上下文不能交付。
    required: bool
    # content 是准备放进模型上下文的真实文本。
    content: str

    # 返回这个上下文块占用的字符预算。
    def char_cost(self):
        return len(self.content)

    # 把块的元数据转换为可写入 JSON 的字典。
    def summary(self):
        return {
            "id": self.block_id,
            "label": self.label,
            "kind": self.kind,
            "priority": self.priority,
            "required": self.required,
            "char_cost": self.char_cost(),
        }


# ContextAssembler 按优先级装配上下文，并在预算边界处整块取舍。
class ContextAssembler:
    # 初始化装配器，并保存本次允许使用的字符预算。
    def __init__(self, budget):
        if not isinstance(budget, int) or budget < 1:
            raise ContextBudgetError("上下文预算必须是正整数")
        # budget 是本课使用的确定性字符预算，不是分词器结果。
        self.budget = budget

    # 装配上下文，返回选中块、省略块和最终文本。
    def assemble(self, blocks):
        if not blocks:
            raise ContextBudgetError("没有可装配的上下文块")

        # block_ids 用于确保每个上下文块只出现一次。
        block_ids = [block.block_id for block in blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ContextBudgetError("上下文块 ID 不能重复")

        # ordered_blocks 按优先级从高到低排列，同优先级保持原顺序。
        ordered_blocks = sorted(
            blocks,
            key=lambda block: block.priority,
            reverse=True,
        )
        # selected 保存最终装入上下文的块。
        selected = []
        # omitted 保存因为预算不足而整块省略的内容。
        omitted = []
        # used 记录当前已经使用的字符数量。
        used = 0

        # block 是当前准备尝试装入的上下文块。
        for block in ordered_blocks:
            # block_cost 是当前块占用的字符数。
            block_cost = block.char_cost()
            if used + block_cost <= self.budget:
                selected.append(block)
                used += block_cost
                continue

            if block.required:
                raise ContextBudgetError(
                    f"必装块“{block.label}”需要 {block_cost} 字符，"
                    f"当前只剩 {self.budget - used} 字符"
                )

            omitted.append(
                {
                    **block.summary(),
                    "reason": "超出上下文预算",
                }
            )

        # context_text 是最终交给模型阅读的有序文本。
        context_text = "\n\n".join(
            f"[{block.label}]\n{block.content}" for block in selected
        )
        return {
            "budget_unit": "characters",
            "budget": self.budget,
            "used": used,
            "remaining": self.budget - used,
            "selected": [block.summary() for block in selected],
            "omitted": omitted,
            "context_text": context_text,
        }


# 把旅行需求、工具结果和有限历史组装成候选上下文块。
def build_trip_blocks(request, results):
    # rules 是本轮对话不能违反的系统规则。
    rules = (
        "只规划城市和日期范围内的行程；天气、地点和路线必须来自工具结果；"
        "不要编造未查询到的信息；发送行程前必须得到用户确认。"
    )
    # goal 是当前这一轮用户真正要完成的目标。
    goal = (
        f"城市：{request['city']}；日期：{', '.join(request['dates'])}；"
        f"偏好：{request['preference']}；工具预算：{request['tool_budget']}。"
    )
    # weather_lines 把三天天气结果整理成一条证据块。
    weather_lines = [
        (
            f"{results[f'weather-{date_text}']['date']}："
            f"{results[f'weather-{date_text}']['condition']}，"
            f"{results[f'weather-{date_text}']['temperature']}"
        )
        for date_text in request["dates"]
    ]
    # place_lines 保存地点工具返回的名称和特点。
    place_lines = [
        f"{place['name']}：{place['feature']}"
        for place in results["places"]
    ]
    # route_result 是路线工具返回的确定性结果。
    route_result = results["route"]

    return [
        ContextBlock(
            "system-rules",
            "规则",
            "rules",
            100,
            True,
            rules,
        ),
        ContextBlock(
            "current-goal",
            "目标",
            "goal",
            90,
            True,
            goal,
        ),
        ContextBlock(
            "weather-evidence",
            "证据｜天气",
            "evidence",
            80,
            True,
            "\n".join(weather_lines),
        ),
        ContextBlock(
            "place-evidence",
            "证据｜地点",
            "evidence",
            70,
            True,
            "\n".join(place_lines),
        ),
        ContextBlock(
            "route-evidence",
            "证据｜路线",
            "evidence",
            60,
            True,
            (
                f"{route_result['origin']} → {route_result['destination']}，"
                f"步行约 {route_result['walking_minutes']} 分钟。"
            ),
        ),
        ContextBlock(
            "history-user",
            "历史｜用户请求",
            "history",
            40,
            False,
            "用户希望这次旅行以拍照为主，并尽量少走路。",
        ),
        ContextBlock(
            "history-assistant",
            "历史｜助手回复",
            "history",
            30,
            False,
            "助手已经完成天气、地点和路线工具调用，等待汇总。",
        ),
    ]


# 运行上下文装配演示，并把完整结果写入 JSON 文件。
def main(output_dir=None, scenario=None):
    # output_directory 保存上下文包，默认是共享包目录。
    output_directory = Path(output_dir) if output_dir else Path(__file__).parent
    output_directory.mkdir(parents=True, exist_ok=True)

    # request 是本课固定的三日旅行需求。
    request = dict(SAMPLE_REQUEST)
    # plan 复用第 21 课建立的依赖任务。
    plan = build_plan(city=request["city"], dates=request["dates"])
    # validate_plan() 在执行前检查依赖、预算和步骤数。
    validate_plan(plan, budget=request["tool_budget"])
    # outcome 是六个工具任务的实际执行结果。
    outcome = execute_plan(plan, budget=request["tool_budget"])
    if outcome["status"] != "completed":
        print(f"上下文装配失败：工具任务状态为 {outcome['status']}")
        raise SystemExit(1)

    # blocks 是等待预算筛选的候选上下文。
    blocks = build_trip_blocks(request, outcome["results"])
    # budget 根据演示模式选择完整、紧凑或故意过小的预算。
    budget = 60 if scenario == "--too-small" else 260 if scenario == "--tight" else 900
    # assembler 负责按优先级选出最终上下文块。
    assembler = ContextAssembler(budget)
    try:
        # package 是最终可检查的上下文包。
        package = assembler.assemble(blocks)
    except ContextBudgetError as error:
        print(f"上下文装配失败：{error}")
        raise SystemExit(1) from error

    # package_path 是保存完整上下文包的 JSON 文件。
    package_path = output_directory / "context-package.json"
    package_path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"上下文预算：{package['budget']} 字符")
    print(f"已使用：{package['used']} 字符")
    print(f"剩余：{package['remaining']} 字符")
    print("已装入：", ", ".join(item["label"] for item in package["selected"]))
    print("已省略：", ", ".join(item["label"] for item in package["omitted"]) or "无")
    print("\n最终上下文：")
    print(package["context_text"])


if __name__ == "__main__":
    main()
