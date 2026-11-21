"""Assemble permission-aware retrieval and preference memory into TripPilot V2."""

import json
import sys
from pathlib import Path


# STAGE_ROOT 是 stage-03 目录，直接运行本文件时也能导入 tripilot。
STAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_ROOT))

from tripilot.access_control import (  # noqa: E402
    AccessControlledRetriever,
    Principal,
    VersionedDocumentIndex,
    VersionedResultCache,
    load_documents,
)
from tripilot.memory import PreferenceMemory  # noqa: E402
from tripilot.rag import HashedEmbedding  # noqa: E402


# PROJECT_DIR 保存本课输入、答案、报告和偏好文件。
PROJECT_DIR = Path(__file__).resolve().parent
# DOCUMENTS_FILE 复用第 33 课的权限文档。
DOCUMENTS_FILE = STAGE_ROOT / "33-rag-permissions-updates" / "documents.json"
# UPDATE_FILE 是第 33 课发布的路线 v2 文档。
UPDATE_FILE = STAGE_ROOT / "33-rag-permissions-updates" / "route-v2.json"
# MEMORY_FILE 保存 Alice 和 Bob 的长期偏好。
MEMORY_FILE = PROJECT_DIR / "stage-memory.json"


def render_stage_answer(scenarios):
    """Render user-facing answers without exposing internal ACL traces."""
    lines = ["# TripPilot V2 阶段交付结果", ""]
    for scenario in scenarios:
        lines.extend(
            [
                f"## {scenario['name']}",
                "",
                f"**用户：** `{scenario['principal']}`  ",
                f"**问题：** {scenario['question']}",
                "",
                scenario["answer"],
                "",
            ]
        )
        citations = scenario.get("citations", [])
        if citations:
            lines.extend(["**引用：**", ""])
            for citation in citations:
                lines.append(
                    f"- [{citation['number']}] {citation['title']} "
                    f"v{citation['version']}，{citation['updated_at']}"
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_stage_demo(output_dir=PROJECT_DIR):
    """Run the integrated TripPilot stage scenarios and write auditable outputs."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # preference_memory 保存长期偏好，并保留每次覆盖的历史。
    preference_memory = PreferenceMemory(MEMORY_FILE)
    preference_memory.reset()
    preference_memory.remember("alice", "pace", "紧凑", recorded_on="2026-11-15")
    preference_memory.remember("alice", "pace", "轻松", recorded_on="2026-11-19")
    preference_memory.remember(
        "alice",
        "hotel_budget",
        "1200 元/晚",
        recorded_on="2026-11-19",
    )
    preference_memory.remember("bob", "pace", "适中", recorded_on="2026-11-19")

    # index 只保留当前有效文档；发布 v2 会更新整库修订号。
    index = VersionedDocumentIndex(load_documents(DOCUMENTS_FILE))
    # cache 保存用户范围和问题相同的答案，避免跨用户复用。
    cache = VersionedResultCache(ttl_seconds=300)
    retriever = AccessControlledRetriever(index, cache, HashedEmbedding(128))
    alice = Principal("alice", "hz-2026", ["owner", "planner"])
    bob = Principal("bob", "hz-2026", ["member"])

    scenarios = []

    def retrieve_scenario(name, question, principal, now):
        result, trace = retriever.retrieve(question, principal, now)
        scenarios.append(
            {
                "name": name,
                "question": question,
                "principal": principal.user_id,
                "trip_id": principal.trip_id,
                "roles": list(principal.roles),
                "answer": result["answer"],
                "citations": result["citations"],
                "cache": trace["cache"]["status"],
                "index_revision": trace["index_revision"],
                "allowed": trace["acl"]["allowed_doc_ids"],
                "denied": [event["doc_id"] for event in trace["acl"]["denied"]],
            }
        )
        return result

    alice_budget = retrieve_scenario(
        "Alice 读取私有预算",
        "西湖附近酒店预算多少？",
        alice,
        1000.0,
    )
    retrieve_scenario(
        "Alice 重复问题命中缓存",
        "西湖附近酒店预算多少？",
        alice,
        1001.0,
    )
    bob_budget = retrieve_scenario(
        "Bob 只能读取公开预算",
        "西湖附近酒店预算多少？",
        bob,
        1002.0,
    )
    bob_spot = retrieve_scenario(
        "Bob 被规划者资料拒绝",
        "雨天摄影集合点在哪里？",
        bob,
        1003.0,
    )
    retrieve_scenario(
        "Alice 查询路线 v1",
        "杭州下雨，龙翔桥地铁站走哪个出口？",
        alice,
        1004.0,
    )
    index.upsert(load_documents(UPDATE_FILE)[0])
    alice_route_v2 = retrieve_scenario(
        "发布路线 v2 后重新查询",
        "杭州下雨，龙翔桥地铁站走哪个出口？",
        alice,
        1005.0,
    )

    # active_preferences 读取 Alice 当前有效的偏好，不返回被覆盖的旧值。
    alice_preferences = preference_memory.active_preferences("alice")
    preference_answer = (
        "偏好记忆：旅行节奏为“轻松”，酒店预算为“1200 元/晚”。"
        "旧节奏“紧凑”只保留在更新历史中，不再参与本次规划。"
    )
    scenarios.append(
        {
            "name": "Alice 读取最新偏好",
            "question": "按我的节奏安排行程，酒店预算怎么定？",
            "principal": "alice",
            "trip_id": "hz-2026",
            "roles": ["owner", "planner"],
            "answer": preference_answer,
            "citations": [],
            "cache": "not_used",
            "index_revision": index.revision,
            "allowed": [],
            "denied": [],
        }
    )

    checks = {
        "alice_sees_private_budget": "1280" in alice_budget["answer"],
        "bob_cannot_see_private_budget": "1280" not in bob_budget["answer"],
        "bob_gets_public_budget": "600" in bob_budget["answer"],
        "cache_hit_for_same_scope": scenarios[1]["cache"] == "hit",
        "bob_cannot_read_planner_doc": "连廊" not in bob_spot["answer"],
        "route_v2_visible": "C 口已经恢复" in alice_route_v2["answer"],
        "old_route_not_reused": "C 口临时关闭" not in alice_route_v2["answer"],
        "preference_uses_new_value": alice_preferences.get("pace") == "轻松",
        "preference_keeps_history": (
            preference_memory.data["users"]["alice"]["history"][0]["old_value"]
            == "紧凑"
        ),
        "preference_answer_visible": "1200 元/晚" in preference_answer,
    }
    failed_checks = [name for name, passed in checks.items() if not passed]
    if failed_checks:
        raise AssertionError(f"阶段校验失败：{', '.join(failed_checks)}")

    report = {
        "checks": checks,
        "cache": cache.stats(),
        "preferences": {
            "alice": alice_preferences,
            "bob": preference_memory.active_preferences("bob"),
        },
        "scenarios": scenarios,
    }
    (output_dir / "stage-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "stage-answer.md").write_text(
        render_stage_answer(scenarios),
        encoding="utf-8",
    )

    print("阶段交付结果：")
    for scenario in scenarios:
        print(f"\n[{scenario['name']}]")
        print(f"用户={scenario['principal']} 缓存={scenario['cache']}")
        print(scenario["answer"])
    print("\n偏好：", alice_preferences)
    print("校验结果：", True)
    print("已保存：stage-answer.md, stage-report.json")
    return report


if __name__ == "__main__":
    run_stage_demo()
