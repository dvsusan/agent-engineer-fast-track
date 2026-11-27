"""单 Agent 与多 Agent 的路线调度实验。

本课用同一组杭州旅行请求对比两种组织方式：
1. 单 Agent：一个角色持有全部工具，在同一个上下文中串行完成研究、预算和复核；
2. 多 Agent：主管 Agent 先分析复杂度，再按职责把任务移交给专业角色。

为了让路由和交接逻辑可以稳定复现，本文件不调用大模型。角色内部采用确定性
工具和规则，模型调用可以在后续章节替换进来，但路由、预算、权限和交接契约
仍应由程序控制。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_POLICY = PROJECT_DIR / "agent-policy.json"
DEFAULT_DATA = PROJECT_DIR / "travel-data.json"
DEFAULT_REQUESTS = PROJECT_DIR / "requests.json"
DEFAULT_COMPARISON = PROJECT_DIR / "comparison-report.json"
DEFAULT_HANDOFF_RECORD = PROJECT_DIR / "handoff-record.json"

Mode = Literal["single", "multi"]
Requirement = Literal["research", "budget", "review"]

# 路由键与工具名的绑定放在代码里，策略文件只决定角色和权重，避免配置拼接出未知工具。
TOOL_FOR_REQUIREMENT: dict[Requirement, str] = {
    "research": "search_hangzhou",
    "budget": "estimate_costs",
    "review": "review_plan",
}


class AgentLabError(RuntimeError):
    """策略、请求、工具或路由不满足实验契约。"""


class PolicyError(AgentLabError):
    """策略文件缺少必需角色、工具或路由。"""


class ToolPermissionError(AgentLabError):
    """角色尝试调用未授权工具。"""


class RoutingError(AgentLabError):
    """主管无法把任务分配给合法角色或交接次数超限。"""


@dataclass(frozen=True)
class Complexity:
    """一次任务的复杂度判断及可解释信号。"""

    signals: dict[str, bool]
    weighted_score: int
    threshold: int
    recommended_mode: Mode

    def public_view(self) -> dict[str, Any]:
        """返回报告需要的复杂度信息。"""
        return {
            "signals": self.signals,
            "score": self.weighted_score,
            "threshold": self.threshold,
            "recommended_mode": self.recommended_mode,
        }


def utc_now() -> str:
    """返回带时区的 ISO 8601 时间戳。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再替换，避免中途退出留下半截报告。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    """读取 JSON 对象；文件名和顶层类型错误都在启动前失败。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AgentLabError(f"{label}不存在：{path}") from error
    except json.JSONDecodeError as error:
        raise AgentLabError(f"{label}不是合法 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise AgentLabError(f"{label}顶层必须是 JSON 对象")
    return payload


def money(value: Decimal) -> str:
    """统一输出两位小数，避免报告里混有不同精度。"""
    return format(value.quantize(Decimal("0.01")), "f")


def decimal_value(value: Any, label: str) -> Decimal:
    """把金额配置或请求转换成 Decimal，拒绝空值和非法数字。"""
    try:
        parsed = Decimal(str(value))
    except Exception as error:  # Decimal 的异常类型随输入类型变化
        raise AgentLabError(f"{label}不是合法金额：{value!r}") from error
    if not parsed.is_finite() or parsed < 0:
        raise AgentLabError(f"{label}必须是非负有限金额：{value!r}")
    return parsed


def validate_policy(policy: dict[str, Any]) -> None:
    """检查角色、工具、路由和预算上限，防止策略把任务交给不存在的角色。"""
    if policy.get("policy_version") != 1:
        raise PolicyError("只支持 policy_version=1")
    for key in ("max_tool_calls", "max_handoffs", "multi_agent_threshold"):
        value = policy.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise PolicyError(f"{key} 必须是大于 0 的整数")

    roles = policy.get("roles")
    if not isinstance(roles, dict):
        raise PolicyError("roles 必须是对象")
    required_roles = {
        "generalist",
        "supervisor",
        "research_agent",
        "budget_agent",
        "review_agent",
    }
    missing_roles = sorted(required_roles - set(roles))
    if missing_roles:
        raise PolicyError(f"缺少角色：{', '.join(missing_roles)}")
    for role_id, role in roles.items():
        if not isinstance(role, dict) or not isinstance(role.get("tools"), list):
            raise PolicyError(f"角色 {role_id} 缺少 tools 数组")

    routing = policy.get("routing")
    expected_requirements = set(TOOL_FOR_REQUIREMENT)
    if not isinstance(routing, dict) or set(routing) != expected_requirements:
        raise PolicyError("routing 必须覆盖 research、budget、review")
    for requirement, role_id in routing.items():
        if role_id not in roles:
            raise PolicyError(f"路由 {requirement} 指向不存在的角色：{role_id}")
        if TOOL_FOR_REQUIREMENT[requirement] not in roles[role_id]["tools"]:
            raise PolicyError(f"角色 {role_id} 没有执行 {requirement} 所需工具")

    tasks = policy.get("tasks")
    if not isinstance(tasks, dict) or set(tasks) != expected_requirements:
        raise PolicyError("tasks 必须覆盖 research、budget、review")
    for requirement, task in tasks.items():
        if not isinstance(task, dict) or not task.get("output_key"):
            raise PolicyError(f"任务 {requirement} 缺少 output_key")


def validate_data(data: dict[str, Any]) -> None:
    """检查旅行数据和价格字段，避免运行到预算阶段才发现缺价格。"""
    attractions = data.get("attractions")
    if not isinstance(attractions, list) or not attractions:
        raise AgentLabError("travel-data.json 缺少 attractions")
    seen_ids: set[str] = set()
    for attraction in attractions:
        required = {
            "place_id",
            "name",
            "tags",
            "duration_hours",
            "ticket_per_person",
            "indoor",
        }
        if not isinstance(attraction, dict) or not required.issubset(attraction):
            raise AgentLabError("地点记录字段不完整")
        if attraction["place_id"] in seen_ids:
            raise AgentLabError(f"地点 ID 重复：{attraction['place_id']}")
        seen_ids.add(attraction["place_id"])

    prices = data.get("prices")
    required_prices = {
        "currency",
        "hotel_per_room_night",
        "food_per_person_day",
        "local_transport_per_day",
        "contingency_rate",
    }
    if not isinstance(prices, dict) or not required_prices.issubset(prices):
        raise AgentLabError("travel-data.json 缺少价格字段")


def validate_request(request: dict[str, Any]) -> None:
    """检查一份旅行请求的必填字段和基本类型。"""
    required = {
        "request_id",
        "user_id",
        "prompt",
        "days",
        "travelers",
        "budget",
        "interests",
        "constraints",
    }
    missing = sorted(required - set(request))
    if missing:
        raise AgentLabError(f"请求缺少字段：{', '.join(missing)}")
    if not isinstance(request["days"], int) or request["days"] <= 0:
        raise AgentLabError("days 必须是大于 0 的整数")
    if not isinstance(request["travelers"], int) or request["travelers"] <= 0:
        raise AgentLabError("travelers 必须是大于 0 的整数")
    if not isinstance(request["interests"], list) or not request["interests"]:
        raise AgentLabError("interests 必须是非空数组")
    if not isinstance(request["constraints"], list):
        raise AgentLabError("constraints 必须是数组")
    decimal_value(request["budget"], "budget")


def analyze_complexity(request: dict[str, Any], policy: dict[str, Any]) -> Complexity:
    """用可解释信号判断是否值得承担多 Agent 的协调成本。"""
    prompt_and_constraints = f"{request['prompt']} {' '.join(request['constraints'])}"
    signals = {
        "multi_day": request["days"] >= 2,
        "multiple_interests": len(request["interests"]) >= 2,
        "budget_cross_check": request.get("budget") is not None,
        "independent_review": any(
            keyword in prompt_and_constraints
            for keyword in ("核对", "复核", "审核")
        ),
        "multiple_travelers": request["travelers"] >= 2,
    }
    signal_policy = policy["complexity_signals"]
    score = sum(
        int(signal_policy[name]["weight"])
        for name, active in signals.items()
        if active
    )
    threshold = int(policy["multi_agent_threshold"])
    recommended: Mode = "multi" if score >= threshold else "single"
    return Complexity(
        signals=signals,
        weighted_score=score,
        threshold=threshold,
        recommended_mode=recommended,
    )


def requirements_for(request: dict[str, Any], complexity: Complexity) -> list[Requirement]:
    """把任务拆成研究、预算和复核三段；复核只在复杂任务中启用。"""
    requirements: list[Requirement] = ["research"]
    if request.get("budget") is not None:
        requirements.append("budget")
    if complexity.recommended_mode == "multi":
        requirements.append("review")
    return requirements


class ToolRegistry:
    """确定性工具集；每次调用都先校验角色权限，再执行工具。"""

    def __init__(self, policy: dict[str, Any], data: dict[str, Any]):
        """保存策略和数据，不在这里执行任何工具或修改输入。"""
        self.policy = policy
        self.data = data

    def invoke(
        self,
        agent_id: str,
        tool_name: str,
        request: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """执行一个工具；角色未获得工具授权时直接拒绝。"""
        # 工具白名单在多 Agent 中承担权限边界，不能只依赖 Prompt 提醒角色“不要越权”。
        allowed_tools = set(self.policy["roles"][agent_id]["tools"])
        if tool_name not in allowed_tools:
            raise ToolPermissionError(f"角色 {agent_id} 无权调用工具 {tool_name}")

        if tool_name == "search_hangzhou":
            return self.search_hangzhou(request)
        if tool_name == "estimate_costs":
            return self.estimate_costs(request, context["research"])
        if tool_name == "review_plan":
            return self.review_plan(request, context["research"], context["budget"])
        raise AgentLabError(f"未知工具：{tool_name}")

    def search_hangzhou(self, request: dict[str, Any]) -> dict[str, Any]:
        """按兴趣匹配户外地点，并准备一个室内雨天备选。"""
        interests = set(request["interests"])
        outdoor = [item for item in self.data["attractions"] if not item["indoor"]]
        scored = [
            (len(interests.intersection(item["tags"])), item)
            for item in outdoor
        ]
        matched = [item for score, item in scored if score > 0]
        if not matched:
            matched = outdoor[:2]

        # 日程越长允许多放一站，但最多三个，避免把一天塞成赶场。
        limit = min(3, max(2, request["days"] + 1))
        selected = matched[:limit]
        fallback = next(
            (item for item in self.data["attractions"] if item["indoor"]),
            None,
        )
        if fallback is None:
            raise AgentLabError("旅行数据中没有雨天备选")

        return {
            "city": self.data["city"],
            "places": [
                {
                    "place_id": item["place_id"],
                    "name": item["name"],
                    "tags": item["tags"],
                    "duration_hours": item["duration_hours"],
                    "ticket_per_person": item["ticket_per_person"],
                }
                for item in selected
            ],
            "rain_fallback": {
                "place_id": fallback["place_id"],
                "name": fallback["name"],
                "tags": fallback["tags"],
            },
            "evidence": [
                f"travel-data:place:{item['place_id']}" for item in selected
            ]
            + [f"travel-data:place:{fallback['place_id']}"],
        }

    def estimate_costs(
        self,
        request: dict[str, Any],
        research: dict[str, Any],
    ) -> dict[str, Any]:
        """根据人数、天数和地点生成可解释的费用拆分。"""
        prices = self.data["prices"]
        days = int(request["days"])
        travelers = int(request["travelers"])
        budget = decimal_value(request["budget"], "budget")
        # 两天行程通常只住一晚，房间按每间两人计算；这套简化假设必须写入报告，不能让用户猜。
        nights = max(0, days - 1)
        rooms = max(1, (travelers + 1) // 2)

        hotel = decimal_value(prices["hotel_per_room_night"], "hotel") * nights * rooms
        food = decimal_value(prices["food_per_person_day"], "food") * travelers * days
        transport = decimal_value(prices["local_transport_per_day"], "transport") * days
        tickets = sum(
            decimal_value(item["ticket_per_person"], "ticket") * travelers
            for item in research["places"]
        )
        subtotal = hotel + food + transport + tickets
        contingency = subtotal * decimal_value(prices["contingency_rate"], "contingency")
        total = subtotal + contingency

        return {
            "currency": prices["currency"],
            "breakdown": {
                "hotel": money(hotel),
                "food": money(food),
                "local_transport": money(transport),
                "tickets": money(tickets),
                "contingency": money(contingency),
            },
            "subtotal": money(subtotal),
            "total": money(total),
            "budget": money(budget),
            "within_budget": total <= budget,
            "evidence": ["travel-data:prices", "request:budget"],
        }

    def review_plan(
        self,
        request: dict[str, Any],
        research: dict[str, Any],
        budget: dict[str, Any],
    ) -> dict[str, Any]:
        """独立检查预算、偏好覆盖、雨天备选、时长和证据完整性。"""
        available_tags = {
            tag
            for item in research["places"]
            for tag in item["tags"]
        }
        interest_coverage = {
            interest: interest in available_tags for interest in request["interests"]
        }
        total_hours = sum(item["duration_hours"] for item in research["places"])
        checks = {
            "budget_within_limit": bool(budget["within_budget"]),
            "rain_fallback_present": bool(research["rain_fallback"]),
            "interests_covered": all(interest_coverage.values()),
            "duration_reasonable": total_hours <= request["days"] * 8,
            "evidence_present": bool(research["evidence"]),
        }
        issues: list[str] = []
        if not checks["budget_within_limit"]:
            issues.append(
                f"预估费用 {budget['total']} 元超过预算 {budget['budget']} 元"
            )
        if not checks["rain_fallback_present"]:
            issues.append("缺少雨天备选")
        if not checks["interests_covered"]:
            missing = [name for name, covered in interest_coverage.items() if not covered]
            issues.append(f"未覆盖偏好：{', '.join(missing)}")
        if not checks["duration_reasonable"]:
            issues.append("地点总时长超过每日可用时间")
        if not checks["evidence_present"]:
            issues.append("缺少地点证据")

        return {
            "status": "passed" if all(checks.values()) else "blocked",
            "checks": checks,
            "interest_coverage": interest_coverage,
            "issues": issues,
            "evidence": research["evidence"] + budget["evidence"],
        }


def context_for(requirement: Requirement, state: dict[str, Any], mode: Mode) -> dict[str, Any]:
    """构造角色可见上下文；多 Agent 只传当前任务需要的前序产物。"""
    if mode == "single":
        # 单 Agent 共享完整工作区，因此研究和预算之间的中间状态天然可见。
        return dict(state)
    if requirement == "research":
        return {}
    if requirement == "budget":
        # 预算角色只需要地点结果，不需要看到主管的完整状态或后续复核草稿。
        return {"research": state["research"]}
    # 复核角色拿到研究结果和预算结果，但不拿到其他角色的推理过程，降低被生成角色带偏的风险。
    return {"research": state["research"], "budget": state["budget"]}


def compose_final(
    request: dict[str, Any],
    state: dict[str, Any],
    mode: Mode,
) -> dict[str, Any]:
    """根据已生成产物拼出面向用户的答案；复核失败时不伪造可执行行程。"""
    research = state["research"]
    budget = state["budget"]
    review = state.get("review")
    status = review["status"] if review else (
        "passed" if budget["within_budget"] else "blocked"
    )

    if status == "blocked":
        issues = review["issues"] if review else ["预算不足"]
        return {
            "status": "blocked",
            "mode": mode,
            "answer": "当前方案不能直接执行：" + "；".join(issues),
            "issues": issues,
            "evidence": research["evidence"] + budget["evidence"],
        }

    places = research["places"]
    day_groups: list[list[dict[str, Any]]] = [[] for _ in range(request["days"])]
    group_size = max(1, (len(places) + request["days"] - 1) // request["days"])
    for index, place in enumerate(places):
        day_index = min(index // group_size, request["days"] - 1)
        day_groups[day_index].append(place)

    lines = []
    for day_index, group in enumerate(day_groups, start=1):
        names = "、".join(place["name"] for place in group) or "自由活动"
        lines.append(f"第 {day_index} 天：{names}")
    lines.append(f"雨天备选：{research['rain_fallback']['name']}")
    lines.append(f"预估总费用：{budget['total']} {budget['currency']}，预算 {budget['budget']} 元")

    return {
        "status": "passed",
        "mode": mode,
        "answer": "\n".join(lines),
        "issues": [],
        "evidence": research["evidence"] + budget["evidence"],
    }


def build_run_record(
    request: dict[str, Any],
    mode: Mode,
    complexity: Complexity,
    requirements: list[Requirement],
    agents_used: list[str],
    agent_steps: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    handoffs: list[dict[str, Any]],
    state: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """整理一次运行的决策、交接、工具和最终状态证据。"""
    final = compose_final(request, state, mode)
    output_keys = {
        policy["tasks"][requirement]["output_key"]
        for requirement in requirements
    }
    routing_ok = True
    if mode == "multi":
        routing_ok = all(
            handoff["to"] == policy["routing"][handoff["requirement"]]
            for handoff in handoffs
        )
    review_consistent = (
        final["status"] == state["review"]["status"]
        if "review" in state
        else True
    )
    checks = {
        "routing_within_policy": routing_ok,
        "tool_calls_within_budget": len(tool_calls) <= policy["max_tool_calls"],
        "handoffs_within_budget": len(handoffs) <= policy["max_handoffs"],
        "required_artifacts_present": output_keys.issubset(state),
        "final_status_consistent": review_consistent,
    }
    checks["all"] = all(checks.values())
    return {
        "run_id": f"{request['request_id']}-{mode}",
        "generated_at": utc_now(),
        "request_id": request["request_id"],
        "mode": mode,
        "request": request,
        "complexity": complexity.public_view(),
        "requirements": requirements,
        "agents_used": agents_used,
        "agent_steps": agent_steps,
        "tool_calls": tool_calls,
        "handoffs": handoffs,
        "artifacts": state,
        "final": final,
        "checks": checks,
    }


def run_single_agent(
    request: dict[str, Any],
    complexity: Complexity,
    requirements: list[Requirement],
    policy: dict[str, Any],
    tools: ToolRegistry,
) -> dict[str, Any]:
    """一个角色串行调用全部工具，所有中间产物共享同一个上下文。"""
    state: dict[str, Any] = {}
    tool_calls: list[dict[str, Any]] = []
    steps = [
        {
            "step": 1,
            "agent": "generalist",
            "action": "plan",
            "detail": f"复杂度 {complexity.weighted_score}/{complexity.threshold}，使用单 Agent",
        }
    ]

    for requirement in requirements:
        # 所有工具调用都由同一个 generalist 完成，步骤之间共享 state，因此这里不会产生 handoff。
        tool_name = TOOL_FOR_REQUIREMENT[requirement]
        context = context_for(requirement, state, "single")
        artifact = tools.invoke("generalist", tool_name, request, context)
        output_key = policy["tasks"][requirement]["output_key"]
        state[output_key] = artifact
        tool_calls.append(
            {
                "role": "generalist",
                "requirement": requirement,
                "tool": tool_name,
                "input_keys": sorted(context),
                "output_key": output_key,
                "status": "completed",
            }
        )
        steps.append(
            {
                "step": len(steps) + 1,
                "agent": "generalist",
                "action": requirement,
                "detail": f"调用 {tool_name} 并把结果写入 {output_key}",
            }
        )

    steps.append(
        {
            "step": len(steps) + 1,
            "agent": "generalist",
            "action": "compose",
            "detail": "在同一个上下文中汇总研究、预算和复核结果",
        }
    )
    return build_run_record(
        request,
        "single",
        complexity,
        requirements,
        ["generalist"],
        steps,
        tool_calls,
        [],
        state,
        policy,
    )


def run_multi_agent(
    request: dict[str, Any],
    complexity: Complexity,
    requirements: list[Requirement],
    policy: dict[str, Any],
    tools: ToolRegistry,
) -> dict[str, Any]:
    """主管路由给专业角色，并用结构化交接记录限制上下文和调用预算。"""
    state: dict[str, Any] = {}
    tool_calls: list[dict[str, Any]] = []
    handoffs: list[dict[str, Any]] = []
    agents_used = ["supervisor"]
    steps = [
        {
            "step": 1,
            "agent": "supervisor",
            "action": "route",
            "detail": f"复杂度 {complexity.weighted_score}/{complexity.threshold}，选择多 Agent",
        }
    ]

    for requirement in requirements:
        # 交接预算是硬约束；超过上限时直接失败，避免主管无限转派任务。
        if len(handoffs) >= policy["max_handoffs"]:
            raise RoutingError("交接次数超过策略预算")
        role_id = policy["routing"][requirement]
        if role_id not in policy["roles"]:
            raise RoutingError(f"路由目标不存在：{role_id}")
        if role_id not in agents_used:
            agents_used.append(role_id)

        context = context_for(requirement, state, "multi")
        handoff = {
            "handoff_id": f"H{len(handoffs) + 1:03d}",
            "requirement": requirement,
            "from": "supervisor",
            "to": role_id,
            "task": policy["tasks"][requirement]["title"],
            "input_keys": sorted(context),
            "status": "in_progress",
        }
        handoffs.append(handoff)

        tool_name = TOOL_FOR_REQUIREMENT[requirement]
        artifact = tools.invoke(role_id, tool_name, request, context)
        output_key = policy["tasks"][requirement]["output_key"]
        state[output_key] = artifact
        handoff["status"] = "completed"
        handoff["output_keys"] = [output_key]
        tool_calls.append(
            {
                "role": role_id,
                "requirement": requirement,
                "tool": tool_name,
                "input_keys": sorted(context),
                "output_key": output_key,
                "status": "completed",
            }
        )
        steps.append(
            {
                "step": len(steps) + 1,
                "agent": role_id,
                "action": requirement,
                "detail": f"接收 {handoff['handoff_id']}，调用 {tool_name}",
            }
        )

    steps.append(
        {
            "step": len(steps) + 1,
            "agent": "supervisor",
            "action": "aggregate",
            "detail": "收齐各角色产物并生成最终答案",
        }
    )
    return build_run_record(
        request,
        "multi",
        complexity,
        requirements,
        agents_used,
        steps,
        tool_calls,
        handoffs,
        state,
        policy,
    )


def run_agent(
    request: dict[str, Any],
    mode: Mode,
    policy: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any]:
    """分析复杂度后运行指定组织模式。"""
    validate_request(request)
    complexity = analyze_complexity(request, policy)
    requirements = requirements_for(request, complexity)
    tools = ToolRegistry(policy, data)
    if mode == "single":
        return run_single_agent(request, complexity, requirements, policy, tools)
    return run_multi_agent(request, complexity, requirements, policy, tools)


def load_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    """加载策略、旅行数据和按请求编号索引的请求集。"""
    policy = load_json_object(DEFAULT_POLICY, "策略文件")
    data = load_json_object(DEFAULT_DATA, "旅行数据")
    request_set = load_json_object(DEFAULT_REQUESTS, "请求文件")
    validate_policy(policy)
    validate_data(data)

    raw_requests = request_set.get("requests")
    if not isinstance(raw_requests, list) or not raw_requests:
        raise AgentLabError("requests.json 缺少 requests 数组")
    requests: dict[str, dict[str, Any]] = {}
    for request in raw_requests:
        if not isinstance(request, dict):
            raise AgentLabError("每个请求必须是对象")
        validate_request(request)
        request_id = request["request_id"]
        if request_id in requests:
            raise AgentLabError(f"请求编号重复：{request_id}")
        requests[request_id] = request
    return policy, data, requests


def metrics(run: dict[str, Any]) -> dict[str, Any]:
    """提取比较表需要的最小指标。"""
    return {
        "agents": len(run["agents_used"]),
        "tool_calls": len(run["tool_calls"]),
        "handoffs": len(run["handoffs"]),
        "agent_steps": len(run["agent_steps"]),
        "status": run["final"]["status"],
        "complexity_score": run["complexity"]["score"],
        "recommended_mode": run["complexity"]["recommended_mode"],
    }


def recommendation_reason(run: dict[str, Any], recommended: Mode) -> str:
    """把复杂度信号翻译成一条便于读者检查的决策理由。"""
    score = run["complexity"]["score"]
    threshold = run["complexity"]["threshold"]
    if recommended == "single":
        return f"复杂度 {score} 低于阈值 {threshold}，一个角色共享上下文即可完成，额外交接没有足够收益"
    active = [
        name for name, enabled in run["complexity"]["signals"].items() if enabled
    ]
    return f"复杂度 {score} 达到阈值 {threshold}，且需要 {', '.join(active)} 对应的职责隔离"


def build_comparison(policy: dict[str, Any], data: dict[str, Any], requests: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """运行简单任务和复杂任务的两种模式，并加上一次超预算演练。"""
    runs = [
        run_agent(requests["simple"], "single", policy, data),
        run_agent(requests["simple"], "multi", policy, data),
        run_agent(requests["complex"], "single", policy, data),
        run_agent(requests["complex"], "multi", policy, data),
    ]
    overbudget = run_agent(requests["overbudget"], "multi", policy, data)
    runs.append(overbudget)
    by_id = {(run["request_id"], run["mode"]): run for run in runs}

    comparisons = []
    for request_id in ("simple", "complex"):
        single = by_id[(request_id, "single")]
        multi = by_id[(request_id, "multi")]
        recommended = single["complexity"]["recommended_mode"]
        comparisons.append(
            {
                "request_id": request_id,
                "recommended_mode": recommended,
                "single": metrics(single),
                "multi": metrics(multi),
                "difference": {
                    "extra_agents": len(multi["agents_used"]) - len(single["agents_used"]),
                    "extra_handoffs": len(multi["handoffs"]) - len(single["handoffs"]),
                    "same_tool_count": len(single["tool_calls"]) == len(multi["tool_calls"]),
                },
                "reason": recommendation_reason(multi, recommended),
            }
        )

    complex_multi = by_id[("complex", "multi")]
    handoff_record = {
        "record_version": 1,
        "generated_at": utc_now(),
        "request_id": complex_multi["request_id"],
        "mode": complex_multi["mode"],
        "route_decision": complex_multi["complexity"],
        "agents_used": complex_multi["agents_used"],
        "chain": complex_multi["handoffs"],
        "guard": {
            "max_handoffs": policy["max_handoffs"],
            "used_handoffs": len(complex_multi["handoffs"]),
            "within_budget": len(complex_multi["handoffs"]) <= policy["max_handoffs"],
        },
    }
    handoff_record["checks"] = {
        "chain_not_empty": bool(handoff_record["chain"]),
        "all_handoffs_completed": all(
            handoff["status"] == "completed" for handoff in handoff_record["chain"]
        ),
        "within_budget": handoff_record["guard"]["within_budget"],
    }
    handoff_record["checks"]["all"] = all(handoff_record["checks"].values())

    failure_drill = {
        "request_id": overbudget["request_id"],
        "mode": overbudget["mode"],
        "status": overbudget["final"]["status"],
        "review_checks": overbudget["artifacts"]["review"]["checks"],
        "issues": overbudget["final"]["issues"],
        "budget": overbudget["artifacts"]["budget"],
    }
    checks = {
        "simple_recommended_single": comparisons[0]["recommended_mode"] == "single",
        "complex_recommended_multi": comparisons[1]["recommended_mode"] == "multi",
        "simple_single_has_no_handoff": comparisons[0]["single"]["handoffs"] == 0,
        "simple_multi_adds_overhead": comparisons[0]["multi"]["handoffs"] > 0,
        "complex_multi_uses_three_handoffs": comparisons[1]["multi"]["handoffs"] == 3,
        "overbudget_blocked": failure_drill["status"] == "blocked",
        "overbudget_has_review_issue": bool(failure_drill["issues"]),
        "handoff_record_valid": handoff_record["checks"]["all"],
        "all_runs_valid": all(run["checks"]["all"] for run in runs),
    }
    checks["all"] = all(checks.values())
    return {
        "report_version": 1,
        "generated_at": utc_now(),
        "policy_version": policy["policy_version"],
        "runs": runs,
        "comparison": comparisons,
        "failure_drill": failure_drill,
        "handoff_record": handoff_record,
        "checks": checks,
    }


def print_run(run: dict[str, Any], output_path: Path) -> None:
    """打印一份运行的决策摘要，便于逐步验证。"""
    complexity = run["complexity"]
    print(f"请求：{run['request_id']}")
    print(f"模式：{run['mode']}")
    print(
        f"复杂度：{complexity['score']}/{complexity['threshold']} "
        f"推荐={complexity['recommended_mode']}"
    )
    print(f"角色：{run['agents_used']}")
    print(
        f"Agent 步骤={len(run['agent_steps'])} "
        f"工具调用={len(run['tool_calls'])} 交接={len(run['handoffs'])}"
    )
    print(f"最终状态：{run['final']['status']}")
    if run["final"]["issues"]:
        print(f"阻塞原因：{run['final']['issues']}")
    print(f"运行检查：{run['checks']}")
    print(f"运行报告：{output_path}")


def print_comparison(report: dict[str, Any], output_path: Path) -> None:
    """打印单 Agent 与多 Agent 的实际步骤对照。"""
    print("Request      Mode    Agents  Tools  Handoffs  Steps  Status")
    for run in report["runs"]:
        print(
            f"{run['request_id']:<13}{run['mode']:<8}"
            f"{len(run['agents_used']):>6}{len(run['tool_calls']):>7}"
            f"{len(run['handoffs']):>10}{len(run['agent_steps']):>7}"
            f"  {run['final']['status']}"
        )
    print("推荐：")
    for item in report["comparison"]:
        print(f"  {item['request_id']}: {item['recommended_mode']} - {item['reason']}")
    print(f"超预算演练：{report['failure_drill']['status']} - {report['failure_drill']['issues']}")
    print(f"比较检查：{report['checks']}")
    print(f"比较报告：{output_path}")


def parse_args() -> argparse.Namespace:
    """定义单次运行和完整比较两个命令。"""
    parser = argparse.ArgumentParser(description="单 Agent 与多 Agent 路由实验")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="运行一个请求")
    run.add_argument("--request-id", required=True)
    run.add_argument("--mode", choices=("single", "multi"), required=True)
    run.add_argument("--output", type=Path, required=True)

    compare = subparsers.add_parser("compare", help="运行完整对照和失败演练")
    compare.add_argument("--output", type=Path, default=DEFAULT_COMPARISON)
    compare.add_argument("--handoff-record", type=Path, default=DEFAULT_HANDOFF_RECORD)
    return parser.parse_args()


def resolve_output(path: Path) -> Path:
    """相对输出路径绑定到项目目录，避免依赖当前 shell 的位置。"""
    return path if path.is_absolute() else (PROJECT_DIR / path).resolve()


def main() -> int:
    """加载输入并执行命令；配置错误返回 2，运行检查失败返回 1。"""
    args = parse_args()
    try:
        policy, data, requests = load_inputs()
        if args.command == "run":
            if args.request_id not in requests:
                raise AgentLabError(f"未知请求编号：{args.request_id}")
            run = run_agent(requests[args.request_id], args.mode, policy, data)
            output_path = resolve_output(args.output)
            atomic_write_json(output_path, run)
            print_run(run, output_path)
            return 0 if run["checks"]["all"] else 1

        report = build_comparison(policy, data, requests)
        output_path = resolve_output(args.output)
        handoff_path = resolve_output(args.handoff_record)
        atomic_write_json(output_path, report)
        atomic_write_json(handoff_path, report["handoff_record"])
        print_comparison(report, output_path)
        print(f"交接记录：{handoff_path}")
        return 0 if report["checks"]["all"] else 1
    except AgentLabError as error:
        print(f"实验失败：{type(error).__name__}: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
