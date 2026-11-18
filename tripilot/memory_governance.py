"""Apply TTL, deletion, user isolation, and correction to travel memory."""

import json
from datetime import date, timedelta
from pathlib import Path

from .memory import PreferenceMemory, PreferenceMemoryError


# GOVERNANCE_VERSION 标记治理审计记录的格式。
GOVERNANCE_VERSION = 1
# DEMO_TODAY 是记忆生命周期演示使用的固定日期。
DEMO_TODAY = date(2026, 11, 16)


# MemoryAccessError 表示请求者试图读取或修改其他用户的数据。
class MemoryAccessError(PermissionError):
    """The requester is not allowed to access this user's memory."""


# MemoryGovernanceError 表示记忆治理操作本身不合法。
class MemoryGovernanceError(ValueError):
    """The requested memory lifecycle operation is invalid."""


# GovernedPreferenceMemory 在长期偏好存储上增加生命周期和访问控制。
class GovernedPreferenceMemory(PreferenceMemory):
    # 初始化治理存储，并补充审计列表。
    def __init__(self, storage_path):
        super().__init__(storage_path)
        # audit 保存治理操作，不保存被删除的原始值。
        self.data.setdefault("audit", [])
        self.data.setdefault("governance_version", GOVERNANCE_VERSION)

    # 清空演示数据，并建立治理审计容器。
    def reset(self):
        super().reset()
        self.data["audit"] = []
        self.data["governance_version"] = GOVERNANCE_VERSION
        self._save()

    # 检查请求者是否就是记忆所属用户。
    def _authorize(self, user_id, requester_id):
        # normalized_user_id 和 normalized_requester_id 用于精确比较身份。
        normalized_user_id = str(user_id).strip()
        normalized_requester_id = str(requester_id).strip()
        if not normalized_user_id or not normalized_requester_id:
            raise MemoryAccessError("用户 ID 和请求者 ID 不能为空")
        if normalized_user_id != normalized_requester_id:
            raise MemoryAccessError(
                f"请求者 {normalized_requester_id} 无权访问 "
                f"用户 {normalized_user_id} 的记忆"
            )

    # 追加一条不包含敏感原文的治理审计记录。
    def _record_audit(self, action, user_id, key, actor_id, detail):
        self.data["audit"].append(
            {
                "action": action,
                "user_id": user_id,
                "key": key,
                "actor_id": actor_id,
                "detail": detail,
                "recorded_on": DEMO_TODAY.isoformat(),
            }
        )

    # 写入一条带 TTL 和治理字段的长期偏好。
    def remember_governed(
        self,
        user_id,
        key,
        value,
        actor_id,
        source="explicit",
        ttl_days=None,
        recorded_on=DEMO_TODAY,
    ):
        self._authorize(user_id, actor_id)
        if ttl_days is not None and ttl_days < 1:
            raise MemoryGovernanceError("TTL 必须大于 0 天")

        # result 是父类完成普通冲突更新后的结果。
        result = self.remember(
            user_id,
            key,
            value,
            source=source,
            recorded_on=recorded_on.isoformat(),
        )
        # profile 是当前用户的长期记忆分区。
        profile = self._profile(user_id)
        # record 是刚刚写入的偏好记录。
        record = profile["preferences"][key]
        record["status"] = "active"
        if ttl_days is None:
            record["expires_on"] = None
        else:
            record["expires_on"] = (
                recorded_on + timedelta(days=ttl_days)
            ).isoformat()

        self._record_audit(
            "remember",
            user_id,
            key,
            actor_id,
            f"{result['status']}，有效期至 {record['expires_on'] or '永久'}",
        )
        self._save()
        return {
            **result,
            "expires_on": record["expires_on"],
        }

    # 清理指定用户在某个日期已经过期的偏好。
    def expire(self, user_id, requester_id, as_of):
        self._authorize(user_id, requester_id)
        # profile 是准备执行过期检查的用户档案。
        profile = self._profile(user_id)
        # expired_keys 保存本次被清理的偏好键。
        expired_keys = []

        # key 和 record 是当前要检查的偏好。
        for key, record in list(profile["preferences"].items()):
            # expires_on 为空表示永久有效。
            expires_on = record.get("expires_on")
            if expires_on and date.fromisoformat(expires_on) <= as_of:
                profile["preferences"].pop(key)
                expired_keys.append(key)
                self._record_audit(
                    "expire",
                    user_id,
                    key,
                    "system",
                    f"在 {as_of.isoformat()} 到期",
                )

        if expired_keys:
            self._save()
        return expired_keys

    # 返回当前有效偏好，同时执行身份检查和过期清理。
    def active_preferences(self, user_id, requester_id=None, as_of=DEMO_TODAY):
        # effective_requester 默认就是用户本人，方便普通读取。
        effective_requester = requester_id or user_id
        self._authorize(user_id, effective_requester)
        self.expire(user_id, effective_requester, as_of)
        # profile 是过期清理后的用户档案。
        profile = self._profile(user_id)
        return {
            key: record["value"]
            for key, record in profile["preferences"].items()
            if record.get("status", "active") == "active"
        }

    # 删除一个偏好键，并记录不可恢复的删除原因。
    def forget(self, user_id, key, requester_id, reason):
        self._authorize(user_id, requester_id)
        # profile 是当前用户的长期记忆分区。
        profile = self._profile(user_id)
        if key not in profile["preferences"]:
            raise MemoryGovernanceError(f"偏好键不存在：{key}")

        profile["preferences"].pop(key)
        self._record_audit(
            "delete",
            user_id,
            key,
            requester_id,
            reason,
        )
        self._save()
        return {"key": key, "status": "deleted", "reason": reason}

    # 在旧值仍符合预期时纠正一条错误记忆。
    def correct(
        self,
        user_id,
        key,
        expected_value,
        new_value,
        actor_id,
        reason,
        ttl_days=None,
    ):
        self._authorize(user_id, actor_id)
        # profile 是准备纠错的用户档案。
        profile = self._profile(user_id)
        # existing 是纠错前保存的当前值。
        existing = profile["preferences"].get(key)
        if existing is None:
            raise MemoryGovernanceError(f"偏好键不存在：{key}")
        if existing["value"] != expected_value:
            raise MemoryGovernanceError(
                f"记忆已被其他操作修改：期望 {expected_value}，"
                f"当前 {existing['value']}"
            )

        # result 是纠正后写回的新值。
        result = self.remember_governed(
            user_id,
            key,
            new_value,
            actor_id=actor_id,
            source="user_correction",
            ttl_days=ttl_days,
        )
        self._record_audit(
            "correct",
            user_id,
            key,
            actor_id,
            reason,
        )
        self._save()
        return result


# 把本次治理操作整理成 Markdown 生命周期表。
def build_lifecycle_table(rows):
    # lines 保存表格标题、表头和每一行操作。
    lines = [
        "# TripPilot 记忆生命周期表",
        "",
        "| 操作 | 用户 | 偏好键 | 结果 | 原因或边界 |",
        "|---|---|---|---|---|",
    ]
    # row 是一次治理动作的可读记录。
    for row in rows:
        lines.append(
            f"| {row['action']} | {row['user']} | {row['key']} | "
            f"{row['result']} | {row['detail']} |"
        )
    return "\n".join(lines) + "\n"


# 运行 TTL、越权、删除和错误记忆纠正演示。
def main(output_dir=None, scenario=None):
    # output_directory 保存治理记忆、报告和生命周期表。
    output_directory = Path(output_dir) if output_dir else Path(__file__).parent
    output_directory.mkdir(parents=True, exist_ok=True)
    # memory_path 是治理演示使用的长期记忆文件。
    memory_path = output_directory / "governed-memory.json"
    # store 是带访问控制与生命周期的偏好存储。
    store = GovernedPreferenceMemory(memory_path)
    store.reset()
    # rows 收集写进生命周期表的操作。
    rows = []

    # interests 设置两天 TTL，用来演示自动过期。
    interests = store.remember_governed(
        "alice",
        "interests",
        "拍照",
        actor_id="alice",
        ttl_days=2,
    )
    rows.append(
        {
            "action": "写入",
            "user": "alice",
            "key": "interests",
            "result": f"TTL 至 {interests['expires_on']}",
            "detail": "用户明确偏好",
        }
    )
    store.remember_governed(
        "alice",
        "travel_budget",
        "600",
        actor_id="alice",
    )
    store.remember_governed(
        "alice",
        "pace",
        "极紧凑",
        actor_id="alice",
        source="model_inference",
        ttl_days=30,
    )
    store.remember_governed(
        "bob",
        "interests",
        "美食",
        actor_id="bob",
    )

    if scenario == "--stale-correction":
        try:
            store.correct(
                "alice",
                "pace",
                expected_value="紧凑",
                new_value="轻松",
                actor_id="alice",
                reason="用过期的旧值纠错",
            )
        except MemoryGovernanceError as error:
            print(f"纠错冲突：{error}")
            raise SystemExit(1) from error

    # visibility 是两个用户在隔离条件读到的当前偏好。
    alice_view = store.active_preferences("alice", requester_id="alice")
    bob_view = store.active_preferences("bob", requester_id="bob")
    print("Alice 可见记忆：", alice_view)
    print("Bob 可见记忆：", bob_view)

    try:
        store.active_preferences("alice", requester_id="bob")
    except MemoryAccessError as error:
        print(f"越权读取：{error}")
        rows.append(
            {
                "action": "越权读取",
                "user": "alice",
                "key": "*",
                "result": "拒绝",
                "detail": "Bob 不能读取 Alice 的记忆",
            }
        )

    # correction 把模型推断出的错误节奏纠正为用户明确要求。
    correction = store.correct(
        "alice",
        "pace",
        expected_value="极紧凑",
        new_value="轻松",
        actor_id="alice",
        reason="用户明确更正",
        ttl_days=30,
    )
    print("错误记忆纠正：", correction)
    rows.append(
        {
            "action": "纠错",
            "user": "alice",
            "key": "pace",
            "result": "极紧凑 → 轻松",
            "detail": "先核对旧值，再写新版本",
        }
    )

    # expired_keys 是到达过期日期后被删除的偏好。
    expired_keys = store.expire(
        "alice",
        requester_id="alice",
        as_of=date(2026, 11, 19),
    )
    print("自动过期：", expired_keys)
    rows.append(
        {
            "action": "过期清理",
            "user": "alice",
            "key": "、".join(expired_keys) or "无",
            "result": "已移除",
            "detail": "as_of 2026-11-19",
        }
    )

    # deleted 是用户主动删除旅行预算后的结果。
    deleted = store.forget(
        "alice",
        "travel_budget",
        requester_id="alice",
        reason="用户要求删除预算记忆",
    )
    print("主动删除：", deleted)
    rows.append(
        {
            "action": "删除",
            "user": "alice",
            "key": "travel_budget",
            "result": "已删除",
            "detail": deleted["reason"],
        }
    )

    # final_view 是治理操作后真正可以进入上下文的偏好。
    final_view = store.active_preferences(
        "alice",
        requester_id="alice",
        as_of=date(2026, 11, 19),
    )
    print("Alice 最终有效记忆：", final_view)

    # report 是机器可读的治理结果。
    report = {
        "as_of": "2026-11-19",
        "alice_initial": alice_view,
        "bob_initial": bob_view,
        "expired_keys": expired_keys,
        "deleted_key": deleted,
        "corrected_pace": correction["value"],
        "alice_final": final_view,
        "audit_entries": len(store.data["audit"]),
    }
    # report_path 保存治理报告。
    report_path = output_directory / "governance-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # lifecycle_path 保存适合课程验收的生命周期表。
    lifecycle_path = output_directory / "lifecycle-table.md"
    lifecycle_path.write_text(
        build_lifecycle_table(rows),
        encoding="utf-8",
    )
    print("审计记录：", len(store.data["audit"]), "条")
    print("已保存：governed-memory.json, governance-report.json, lifecycle-table.md")


if __name__ == "__main__":
    main()
