"""Run permission-aware RAG retrieval with versioned and expiring caches."""

import copy
import hashlib
import json
from pathlib import Path

from .evidence import query_bigrams
from .rag import HashedEmbedding, cosine_similarity


# 本课用同一个预算问题验证用户级 ACL 与缓存隔离。
BUDGET_QUESTION = "西湖附近酒店预算多少？"
# 本课用同一个出口问题验证文档版本更新和缓存失效。
ROUTE_QUESTION = "杭州下雨，龙翔桥地铁站走哪个出口？"
# 摄影集合点资料只允许负责人和规划者查看。
PLANNER_QUESTION = "雨天摄影集合点在哪里？"


# AccessControlError 表示文档 ACL、版本或索引状态不合法。
class AccessControlError(ValueError):
    """The document or access-control state is invalid."""


# Principal 表示一次请求中的用户、行程和角色。
class Principal:
    # 初始化请求主体，并把角色保存为不可变元组。
    def __init__(self, user_id, trip_id, roles):
        self.user_id = user_id
        self.trip_id = trip_id
        self.roles = tuple(sorted(set(roles)))

    # 权限范围参与缓存键，防止不同用户共享同一条缓存答案。
    def scope_key(self):
        roles = ",".join(self.roles)
        return f"{self.trip_id}|{self.user_id}|{roles}"


# TravelDocument 是带 ACL、版本和更新时间的可检索资料。
class TravelDocument:
    # 初始化文档的全部检索与安全元数据。
    def __init__(
        self,
        doc_id,
        title,
        trip_id,
        version,
        updated_at,
        acl,
        text,
    ):
        self.doc_id = doc_id
        self.title = title
        self.trip_id = trip_id
        self.version = version
        self.updated_at = updated_at
        self.acl = acl
        self.text = text

        if not isinstance(version, int) or version < 1:
            raise AccessControlError(f"文档 {doc_id} 的 version 必须是从 1 开始的正整数")
        if not text.strip():
            raise AccessControlError(f"文档 {doc_id} 没有正文")
        if not isinstance(acl, dict):
            raise AccessControlError(f"文档 {doc_id} 缺少 ACL")

        # 允许用户和允许角色都使用列表，避免把单个字符串误当成集合。
        self.allowed_users = acl.get("allowed_users", [])
        self.allowed_roles = acl.get("allowed_roles", [])
        if not isinstance(self.allowed_users, list) or not isinstance(
            self.allowed_roles,
            list,
        ):
            raise AccessControlError(f"文档 {doc_id} 的 ACL 字段必须是列表")
        if not self.allowed_users and not self.allowed_roles:
            raise AccessControlError(f"文档 {doc_id} 的 ACL 为空，默认拒绝访问")

    # 从 JSON 字典构造文档，集中校验外部输入。
    @classmethod
    def from_dict(cls, payload):
        required = {
            "doc_id",
            "title",
            "trip_id",
            "version",
            "updated_at",
            "acl",
            "text",
        }
        missing = required.difference(payload)
        if missing:
            raise AccessControlError(f"文档缺少字段：{', '.join(sorted(missing))}")
        return cls(
            doc_id=payload["doc_id"],
            title=payload["title"],
            trip_id=payload["trip_id"],
            version=payload["version"],
            updated_at=payload["updated_at"],
            acl=payload["acl"],
            text=payload["text"],
        )

    # 标题和正文一起参与向量计算，便于用标题命中主题。
    def searchable_text(self):
        return f"{self.title}\n{self.text}"


# 从 JSON 文件读取当前版本的文档集合。
def load_documents(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, list) or not raw_documents:
        raise AccessControlError("文档文件没有 documents 列表")

    documents = [TravelDocument.from_dict(item) for item in raw_documents]
    doc_ids = [document.doc_id for document in documents]
    if len(doc_ids) != len(set(doc_ids)):
        raise AccessControlError("初始文档中不能出现重复 doc_id")
    return documents


# 在打分前判断文档是否允许当前请求主体读取。
def check_access(document, principal):
    # 跨行程访问必须先被拒绝，不能依赖后面的相关性排序。
    if document.trip_id != principal.trip_id:
        return False, "different_trip"

    if document.allowed_users:
        if principal.user_id not in document.allowed_users:
            return False, "user_not_allowed"

    if document.allowed_roles:
        if not set(principal.roles).intersection(document.allowed_roles):
            return False, "role_not_allowed"

    return True, "allowed"


# VersionedDocumentIndex 只保留每个 doc_id 的最新版本。
class VersionedDocumentIndex:
    # 初始化索引并写入当前有效版本。
    def __init__(self, documents):
        self._documents = {}
        self.revision = 0
        for document in documents:
            self.upsert(document)

    # 发布新版本；版本号必须严格递增。
    def upsert(self, document):
        current = self._documents.get(document.doc_id)
        if current is not None and document.version <= current.version:
            raise AccessControlError(
                f"文档 {document.doc_id} 的新版本必须大于 {current.version}"
            )
        self._documents[document.doc_id] = document
        self.revision += 1

    # 为一次检索创建稳定快照，避免读到一半时索引被更新。
    def snapshot(self):
        documents = tuple(
            sorted(
                self._documents.values(),
                key=lambda document: document.doc_id,
            )
        )
        return self.revision, documents


# 把查询和权限范围组合成不可逆的稳定缓存键。
def build_cache_key(question, principal):
    raw_key = f"{principal.scope_key()}\n{question.strip()}"
    return hashlib.blake2b(raw_key.encode("utf-8"), digest_size=16).hexdigest()


# VersionedResultCache 同时检查 TTL 和索引修订号。
class VersionedResultCache:
    # ttl_seconds 是答案允许被复用的最长秒数。
    def __init__(self, ttl_seconds=300):
        self.ttl_seconds = ttl_seconds
        self.entries = {}
        self.hits = 0
        self.misses = 0
        self.invalidations = 0
        self.expirations = 0

    # 只有用户范围、索引版本和 TTL 都匹配时才返回缓存。
    def get(self, key, index_revision, now):
        entry = self.entries.get(key)
        if entry is None:
            self.misses += 1
            return None, "miss"

        if entry["index_revision"] != index_revision:
            del self.entries[key]
            self.invalidations += 1
            self.misses += 1
            return None, "index_revision_changed"

        age = now - entry["stored_at"]
        if age > self.ttl_seconds:
            del self.entries[key]
            self.expirations += 1
            self.misses += 1
            return None, "ttl_expired"

        self.hits += 1
        return copy.deepcopy(entry["payload"]), "hit"

    # 缓存保存答案和内部追踪，调用方不得直接修改原对象。
    def put(self, key, index_revision, now, payload):
        self.entries[key] = {
            "index_revision": index_revision,
            "stored_at": now,
            "payload": copy.deepcopy(payload),
        }

    # 返回可写入报告的缓存统计。
    def stats(self):
        return {
            "ttl_seconds": self.ttl_seconds,
            "site_hits": self.hits,
            "site_misses": self.misses,
            "revision_invalidations": self.invalidations,
            "ttl_expirations": self.expirations,
        }


# AccessControlledRetriever 将 ACL、检索、版本和缓存串成一条调用链。
class AccessControlledRetriever:
    # min_score 和 min_term_overlap 共同阻止弱相关文档被拿来凑答案。
    def __init__(
        self,
        index,
        cache,
        embedder,
        min_score=0.25,
        min_term_overlap=2,
        top_k=2,
    ):
        self.index = index
        self.cache = cache
        self.embedder = embedder
        self.min_score = min_score
        self.min_term_overlap = min_term_overlap
        self.top_k = top_k

    # 对单次请求执行缓存查询或完整的安全检索。
    def retrieve(self, question, principal, now):
        key = build_cache_key(question, principal)
        cached, cache_status = self.cache.get(key, self.index.revision, now)
        if cached is not None:
            cached["trace"]["cache"] = {
                "status": cache_status,
                "ttl_seconds": self.cache.ttl_seconds,
            }
            return cached["result"], cached["trace"]

        result, trace = self._retrieve_uncached(question, principal)
        trace["cache"] = {
            "status": cache_status,
            "ttl_seconds": self.cache.ttl_seconds,
        }
        payload = {"result": result, "trace": trace}
        self.cache.put(key, trace["index_revision"], now, payload)
        return result, trace

    # 未命中缓存时，先过滤 ACL，再给允许的文档打分。
    def _retrieve_uncached(self, question, principal):
        index_revision, documents = self.index.snapshot()
        question_vector = self.embedder.embed(question)
        question_terms = query_bigrams(question)

        allowed_documents = []
        acl_events = []
        for document in documents:
            allowed, reason = check_access(document, principal)
            event = {
                "doc_id": document.doc_id,
                "version": document.version,
                "decision": "allow" if allowed else "deny",
                "reason": reason,
            }
            acl_events.append(event)
            if allowed:
                allowed_documents.append(document)

        hits = []
        for document in allowed_documents:
            document_terms = query_bigrams(document.searchable_text())
            overlap = len(question_terms.intersection(document_terms))
            if overlap < self.min_term_overlap:
                continue

            score = cosine_similarity(
                question_vector,
                self.embedder.embed(document.searchable_text()),
            )
            if score < self.min_score:
                continue
            hits.append(
                {
                    "document": document,
                    "score": round(score, 4),
                    "term_overlap": overlap,
                }
            )

        hits.sort(
            key=lambda hit: (hit["score"], hit["document"].updated_at),
            reverse=True,
        )
        selected_hits = hits[: self.top_k]

        if not selected_hits:
            result = {
                "answer": "没有检索到你有权限查看、且足够可靠的旅行资料，暂时无法回答。",
                "citations": [],
            }
        else:
            primary = selected_hits[0]["document"]
            result = {
                "answer": f"{primary.text} [1]",
                "citations": [
                    {
                        "number": 1,
                        "doc_id": primary.doc_id,
                        "title": primary.title,
                        "version": primary.version,
                        "updated_at": primary.updated_at,
                    }
                ],
            }

        trace = {
            "question": question,
            "principal": {
                "user_id": principal.user_id,
                "trip_id": principal.trip_id,
                "roles": list(principal.roles),
            },
            "index_revision": index_revision,
            "acl": {
                "evaluated": len(documents),
                "allowed_doc_ids": [document.doc_id for document in allowed_documents],
                "denied": [event for event in acl_events if event["decision"] == "deny"],
            },
            "retrieval": {
                "candidate_doc_ids": [hit["document"].doc_id for hit in selected_hits],
                "scores": {
                    hit["document"].doc_id: {
                        "score": hit["score"],
                        "term_overlap": hit["term_overlap"],
                    }
                    for hit in selected_hits
                },
            },
        }
        return result, trace


# 渲染面向用户的答案；内部 ACL 事件不进入这段文本。
def render_answer_markdown(scenarios):
    lines = ["# TripPilot 权限感知检索结果", ""]
    for scenario in scenarios:
        result = scenario["result"]
        lines.extend(
            [
                f"## {scenario['name']}",
                "",
                f"**用户：** `{scenario['principal']}`  ",
                f"**问题：** {scenario['question']}",
                "",
                result["answer"],
                "",
            ]
        )
        if result["citations"]:
            lines.extend(["**引用：**", ""])
            for citation in result["citations"]:
                lines.append(
                    f"- [{citation['number']}] {citation['title']} "
                    f"v{citation['version']}，{citation['updated_at']}"
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# 运行固定场景，并校验权限、版本和缓存行为。
def run_demo(documents_path, update_path, output_dir):
    index = VersionedDocumentIndex(load_documents(documents_path))
    cache = VersionedResultCache(ttl_seconds=300)
    retriever = AccessControlledRetriever(index, cache, HashedEmbedding(128))
    alice = Principal("alice", "hz-2026", ["owner", "planner"])
    bob = Principal("bob", "hz-2026", ["member"])

    scenarios = []

    def run(name, question, principal, now):
        result, trace = retriever.retrieve(question, principal, now)
        scenarios.append(
            {
                "name": name,
                "principal": principal.user_id,
                "question": question,
                "result": result,
                "trace": trace,
            }
        )
        return result, trace

    # 1. Alice 能读取自己的预算单，首次查询写入私有作用域缓存。
    alice_budget, _ = run("Alice 读取私有预算", BUDGET_QUESTION, alice, 0)
    # 2. Bob 使用同一问题，但缓存键不同，不能复用 Alice 的答案。
    bob_budget, _ = run("Bob 被 ACL 拦截私有预算", BUDGET_QUESTION, bob, 1)
    # 3. 角色 ACL 同样在召回前生效。
    alice_spot, _ = run("Alice 读取规划者资料", PLANNER_QUESTION, alice, 2)
    bob_spot, _ = run("Bob 被角色 ACL 拦截", PLANNER_QUESTION, bob, 3)
    # 4. 第一次查询路线时缓存 v1 答案。
    route_v1, _ = run("路线 v1 首次查询", ROUTE_QUESTION, alice, 4)
    route_hit, _ = run("路线 v1 缓存命中", ROUTE_QUESTION, alice, 14)
    # 5. 发布 v2 后索引修订号变化，旧缓存必须失效。
    index.upsert(load_documents(update_path)[0])
    route_v2, _ = run("发布 v2 后重新查询", ROUTE_QUESTION, alice, 20)
    # 6. 即使索引不变，超过 TTL 也不能继续返回旧答案。
    route_expired, _ = run("超过 TTL 后重新查询", ROUTE_QUESTION, alice, 321)

    checks = {
        "alice_sees_private_budget": "1280" in alice_budget["answer"],
        "bob_cannot_see_private_budget": "1280" not in bob_budget["answer"],
        "bob_gets_public_budget": "600" in bob_budget["answer"],
        "planner_doc_allowed_for_alice": "连廊" in alice_spot["answer"],
        "planner_doc_denied_for_bob": "连廊" not in bob_spot["answer"],
        "same_scope_cache_hits": scenarios[5]["trace"]["cache"]["status"] == "hit",
        "version_update_invalidates_cache": (
            scenarios[6]["trace"]["cache"]["status"] == "index_revision_changed"
        ),
        "updated_route_visible": "C 口已经恢复" in route_v2["answer"],
        "old_route_not_reused": "C 口临时关闭" not in route_v2["answer"],
        "ttl_expires_cache": (
            scenarios[7]["trace"]["cache"]["status"] == "ttl_expired"
        ),
        "v1_answer_stable_before_update": "C 口临时关闭" in route_v1["answer"],
        "cached_answer_stable": route_hit["answer"] == route_v1["answer"],
        "expired_answer_refreshed": route_expired["answer"] == route_v2["answer"],
    }
    failed_checks = [name for name, passed in checks.items() if not passed]
    if failed_checks:
        raise AssertionError(f"演示校验失败：{', '.join(failed_checks)}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "checks": checks,
        "cache": cache.stats(),
        "scenarios": scenarios,
    }
    (output_dir / "acl-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "answer.md").write_text(
        render_answer_markdown(scenarios),
        encoding="utf-8",
    )
    return report


# 打印关键场景，便于读者在终端直接核对结果。
def main(documents_path, update_path, output_dir):
    report = run_demo(documents_path, update_path, output_dir)
    for scenario in report["scenarios"]:
        print(f"\n[{scenario['name']}]")
        print(
            f"用户={scenario['principal']} "
            f"缓存={scenario['trace']['cache']['status']} "
            f"允许={scenario['trace']['acl']['allowed_doc_ids']} "
            f"拒绝={[item['doc_id'] for item in scenario['trace']['acl']['denied']]}"
        )
        print(scenario["result"]["answer"])

    print("\n缓存统计：", report["cache"])
    print("校验结果：", all(report["checks"].values()))
    print("已保存：answer.md, acl-report.json")
