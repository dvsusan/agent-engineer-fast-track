"""Persist short-term sessions and long-term travel preferences."""

import json
from dataclasses import dataclass
from pathlib import Path

from .context_engine import ContextAssembler, ContextBlock


# MEMORY_VERSION 用来识别长期记忆文件的格式。
MEMORY_VERSION = 1
# DEMO_DATE 是本课固定日期，保证记忆示例输出可以重复。
DEMO_DATE = "2026-11-16"


# PreferenceMemoryError 表示长期记忆无法安全读取或更新。
class PreferenceMemoryError(ValueError):
    """The preference store is invalid or the update is not allowed."""


# Turn 是一条已经发生的会话消息。
@dataclass(frozen=True)
class Turn:
    # role 表示消息来自 user 还是 assistant。
    role: str
    # content 是这条消息的文本内容。
    content: str

    # 把一条消息转换为可保存的字典。
    def to_dict(self):
        return {"role": self.role, "content": self.content}


# SessionMemory 保存当前会话的目标和最近消息，不跨进程长期保留。
class SessionMemory:
    # 初始化一个属于指定用户和会话的短期记忆。
    def __init__(self, session_id, user_id):
        # session_id 是当前会话的唯一编号。
        self.session_id = session_id
        # user_id 是会话所属用户。
        self.user_id = user_id
        # goal 是当前会话正在完成的目标。
        self.goal = ""
        # turns 保存按发生顺序排列的会话消息。
        self.turns = []

    # 设置当前会话目标。
    def set_goal(self, goal):
        # normalized_goal 是去掉首尾空白后的目标文本。
        normalized_goal = str(goal).strip()
        if not normalized_goal:
            raise PreferenceMemoryError("会话目标不能为空")
        self.goal = normalized_goal

    # 添加一条用户或助手消息。
    def add_turn(self, role, content):
        if role not in {"user", "assistant"}:
            raise PreferenceMemoryError(f"不支持的消息角色：{role}")
        # normalized_content 是去掉首尾空白后的消息内容。
        normalized_content = str(content).strip()
        if not normalized_content:
            raise PreferenceMemoryError("会话消息不能为空")
        self.turns.append(Turn(role, normalized_content))

    # 返回当前会话的 JSON 快照。
    def snapshot(self):
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "goal": self.goal,
            "turn_count": len(self.turns),
            "turns": [turn.to_dict() for turn in self.turns],
        }

    # 把最近的短期状态转换成上下文块。
    def to_context_block(self, max_turns=4):
        # recent_turns 只选择最近几条消息，避免短期历史无限增长。
        recent_turns = self.turns[-max_turns:]
        # lines 保存目标和最近消息的可读文本。
        lines = [f"当前目标：{self.goal}"]
        # turn 是最近发生的一条会话消息。
        for turn in recent_turns:
            lines.append(f"{turn.role}: {turn.content}")
        return ContextBlock(
            "short-term-session",
            "短期会话",
            "history",
            65,
            False,
            "\n".join(lines),
        )


# PreferenceMemory 把长期偏好保存到 JSON，并保留冲突更新历史。
class PreferenceMemory:
    # 初始化存储位置，并读取已有长期记忆。
    def __init__(self, storage_path):
        # storage_path 是长期偏好 JSON 文件的路径。
        self.storage_path = Path(storage_path)
        # data 是当前加载到内存的长期记忆数据库。
        self.data = self._load()

    # 创建一个空的长期记忆文件，方便重复运行本节演示。
    def reset(self):
        self.data = {
            "version": MEMORY_VERSION,
            "users": {},
        }
        self._save()

    # 从磁盘读取长期记忆，文件不存在时返回空结构。
    def _load(self):
        if not self.storage_path.exists():
            return {"version": MEMORY_VERSION, "users": {}}
        try:
            # loaded 是磁盘中的完整 JSON 对象。
            loaded = json.loads(
                self.storage_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise PreferenceMemoryError(
                f"长期记忆无法读取：{error}"
            ) from error

        if not isinstance(loaded, dict):
            raise PreferenceMemoryError("长期记忆顶层必须是 JSON 对象")
        if loaded.get("version") != MEMORY_VERSION:
            raise PreferenceMemoryError(
                f"不支持的记忆版本：{loaded.get('version')}"
            )
        if not isinstance(loaded.get("users"), dict):
            raise PreferenceMemoryError("长期记忆缺少 users 对象")
        return loaded

    # 先写临时文件再替换正式文件，避免留下半截 JSON。
    def _save(self):
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        # temp_path 是长期记忆写入时使用的临时文件。
        temp_path = self.storage_path.with_name(
            f"{self.storage_path.name}.tmp"
        )
        temp_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.storage_path)

    # 返回指定用户的长期记忆分区，不存在时自动创建。
    def _profile(self, user_id):
        # normalized_user_id 是用户唯一编号的文本形式。
        normalized_user_id = str(user_id).strip()
        if not normalized_user_id:
            raise PreferenceMemoryError("user_id 不能为空")
        return self.data["users"].setdefault(
            normalized_user_id,
            {"preferences": {}, "history": []},
        )

    # 写入或更新一个长期偏好。
    def remember(
        self,
        user_id,
        key,
        value,
        source="explicit",
        recorded_on=DEMO_DATE,
    ):
        # normalized_key 和 normalized_value 是验证后的记忆内容。
        normalized_key = str(key).strip()
        normalized_value = str(value).strip()
        if not normalized_key or not normalized_value:
            raise PreferenceMemoryError("偏好键和值不能为空")

        # profile 是当前用户的长期记忆分区。
        profile = self._profile(user_id)
        # preferences 保存每个偏好键的最新值。
        preferences = profile["preferences"]
        # existing 是同一个键之前保存的偏好。
        existing = preferences.get(normalized_key)
        if existing and existing["value"] == normalized_value:
            return {
                "key": normalized_key,
                "value": normalized_value,
                "status": "unchanged",
            }

        # next_version 是该偏好键下一次版本号。
        next_version = existing["version"] + 1 if existing else 1
        if existing:
            profile["history"].append(
                {
                    "key": normalized_key,
                    "old_value": existing["value"],
                    "new_value": normalized_value,
                    "source": source,
                    "recorded_on": recorded_on,
                }
            )

        preferences[normalized_key] = {
            "value": normalized_value,
            "source": source,
            "version": next_version,
            "recorded_on": recorded_on,
        }
        self._save()
        return {
            "key": normalized_key,
            "value": normalized_value,
            "previous": existing["value"] if existing else None,
            "version": next_version,
            "status": "updated" if existing else "created",
        }

    # 返回指定用户当前有效的长期偏好。
    def active_preferences(self, user_id):
        # profile 是当前用户已经保存的长期记忆。
        profile = self._profile(user_id)
        return {
            key: record["value"]
            for key, record in profile["preferences"].items()
        }

    # 把长期偏好转换成可以进入上下文的块。
    def to_context_block(self, user_id):
        # preferences 是最新有效偏好，不包含已经过期的旧值。
        preferences = self.active_preferences(user_id)
        # preference_labels 把内部键转换成中文提示。
        preference_labels = {
            "interests": "兴趣",
            "pace": "旅行节奏",
            "travel_budget": "旅行预算",
        }
        # lines 是按稳定顺序排列的偏好文本。
        lines = [
            f"{preference_labels.get(key, key)}：{value}"
            for key, value in sorted(preferences.items())
        ]
        return ContextBlock(
            "long-term-preferences",
            "长期偏好",
            "memory",
            75,
            False,
            "\n".join(lines),
        )


# 运行短期会话、长期偏好和冲突更新演示。
def main(output_dir=None, scenario=None):
    # output_directory 保存会话、长期记忆和最终上下文。
    output_directory = Path(output_dir) if output_dir else Path(__file__).parent
    output_directory.mkdir(parents=True, exist_ok=True)
    # memory_path 是本课长期偏好文件。
    memory_path = output_directory / "memory-store.json"
    # preferences 是长期偏好存储。
    preferences = PreferenceMemory(memory_path)

    if scenario == "--bad-memory":
        try:
            preferences.remember("demo-user", "", "")
        except PreferenceMemoryError as error:
            print(f"记忆写入失败：{error}")
            raise SystemExit(1) from error

    preferences.reset()
    # first_result 模拟上一个会话创建“轻松节奏”的偏好。
    first_result = preferences.remember(
        "demo-user",
        "pace",
        "轻松",
    )
    print("上一会话写入：", first_result)
    preferences.remember("demo-user", "interests", "拍照")

    # reloaded_preferences 模拟新进程重新读取长期记忆。
    reloaded_preferences = PreferenceMemory(memory_path)
    # session 是这次新会话的短期状态。
    session = SessionMemory("session-2026-11-16", "demo-user")
    session.set_goal("规划杭州三日拍照行程")
    session.add_turn(
        "user",
        "这次还是去杭州，预算 600，想多走几个拍照点。",
    )
    session.add_turn(
        "assistant",
        "已记住预算 600 和拍照偏好。",
    )
    session.add_turn("user", "这次节奏改成紧凑一点。")

    # conflict_result 记录同一个 pace 键从“轻松”更新为“紧凑”。
    conflict_result = reloaded_preferences.remember(
        "demo-user",
        "pace",
        "紧凑",
        source="explicit_correction",
    )
    reloaded_preferences.remember(
        "demo-user",
        "travel_budget",
        "600",
        source="current_session",
    )
    print("冲突更新：", conflict_result)

    # rules_block 和 goal_block 是本轮上下文中的硬边界。
    rules_block = ContextBlock(
        "system-rules",
        "规则",
        "rules",
        100,
        True,
        "只使用已保存偏好和工具证据；偏好冲突时采用用户最新明确要求。",
    )
    goal_block = ContextBlock(
        "current-goal",
        "目标",
        "goal",
        90,
        True,
        "为 demo-user 规划杭州三日拍照行程，预算 600，节奏紧凑。",
    )
    # assembler 把规则、目标、长期偏好和短期会话装配在一起。
    assembler = ContextAssembler(500)
    package = assembler.assemble(
        [
            rules_block,
            goal_block,
            reloaded_preferences.to_context_block("demo-user"),
            session.to_context_block(),
        ]
    )

    # active 是冲突更新后的长期偏好。
    active = reloaded_preferences.active_preferences("demo-user")
    # session_path 保存本次短期会话状态。
    session_path = output_directory / "session-state.json"
    session_path.write_text(
        json.dumps(session.snapshot(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # package_path 保存记忆参与上下文装配后的结果。
    package_path = output_directory / "memory-context.json"
    package_path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"当前会话轮次：{len(session.turns)}")
    print("当前长期偏好：", active)
    print(f"上下文已使用：{package['used']} / {package['budget']} 字符")
    print("\n记忆上下文：")
    print(package["context_text"])
    print("\n已保存：memory-store.json, session-state.json, memory-context.json")


if __name__ == "__main__":
    main()
