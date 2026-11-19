"""Rewrite, hybrid-retrieve, and rerank TripPilot travel evidence."""

import json
import re
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .pgvector_store import DEFAULT_DSN, vector_literal
from .rag import HashedEmbedding


# RAW_QUERY 是用户真实会说的模糊问题。
RAW_QUERY = "下雨了想拍照，但腿不想太累，去哪？"
# REWRITE_RULES 把口语线索扩展成更适合检索的词项。
REWRITE_RULES = {
    "雨": ["雨天", "室内", "博物馆"],
    "拍照": ["摄影", "景点"],
    "累": ["少步行", "短距离", "公交"],
}


# 把用户问句整理成统一空格形式。
def normalize_query(query):
    # normalized 是去掉多余空白后的问句。
    normalized = re.sub(r"\s+", " ", str(query)).strip()
    if not normalized:
        raise ValueError("查询问题不能为空")
    return normalized


# 根据规则扩展查询，并保留原始问题和扩展词。
def rewrite_query(query):
    # original 是用户输入的原问题。
    original = normalize_query(query)
    # expansions 保存命中的检索扩展词。
    expansions = []
    # trigger 和 words 是规则表中的口语触发词与扩展词。
    for trigger, words in REWRITE_RULES.items():
        if trigger in original:
            expansions.extend(
                word for word in words if word not in expansions
            )
    # rewritten 是最终用于向量与关键词检索的问句。
    rewritten = " ".join([original, *expansions])
    return {
        "original": original,
        "rewritten": rewritten,
        "expansions": expansions,
    }


# 只保留有检索意义的词项，避免单字噪声主导结果。
def meaningful_tokens(text, embedder):
    # tokens 是原始词项。
    tokens = embedder.tokenize(text)
    # filtered 去掉单字中文和空白，保留二元组与英文数字。
    filtered = [
        token
        for token in tokens
        if len(token) > 1 and token.strip()
    ]
    return set(filtered)


# 计算查询词项在片段中的覆盖比例。
def keyword_coverage(query, content, embedder):
    # query_tokens 是改写后问题的有效词项。
    query_tokens = meaningful_tokens(query, embedder)
    # content_tokens 是资料片段中的有效词项。
    content_tokens = meaningful_tokens(content, embedder)
    if not query_tokens:
        return 0.0
    return len(query_tokens & content_tokens) / len(query_tokens)


# 从 PostgreSQL 读取每个文档的当前版本，并计算 pgvector 相似度。
def load_current_rows(
    connection,
    rewritten_query,
    embedder,
    city="杭州",
    allowed_groups=None,
):
    # groups 是服务端授权后的权限组。
    groups = allowed_groups or ["travel-public"]
    # query_vector 是改写问题的 pgvector 文本。
    query_vector = vector_literal(embedder.embed(rewritten_query))
    # SQL 的 CTE 为每个文档保留最新版本，再参与向量比较。
    sql = """
        WITH current_docs AS (
            SELECT doc_id, max(version) AS max_version
            FROM tripilot_chunks
            WHERE city = %s AND access_group = ANY(%s)
            GROUP BY doc_id
        )
        SELECT
            c.chunk_id,
            c.doc_id,
            c.strategy,
            c.content,
            c.metadata,
            c.version,
            1 - (c.embedding <=> %s::vector) AS vector_score
        FROM tripilot_chunks c
        JOIN current_docs current
          ON current.doc_id = c.doc_id
         AND current.max_version = c.version
        WHERE c.city = %s
          AND c.access_group = ANY(%s)
        ORDER BY c.embedding <=> %s::vector
    """
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            sql,
            [
                city,
                groups,
                query_vector,
                city,
                groups,
                query_vector,
            ],
        )
        return cursor.fetchall()


# 用本地片段和同一哈希向量重建离线候选，便于验证排序算法。
def load_offline_rows(chunks_path, rewritten_query, embedder):
    # payload 是第 29 课切分结果。
    payload = json.loads(Path(chunks_path).read_text(encoding="utf-8"))
    # query_vector 是改写问题的单位向量。
    query_vector = embedder.embed(rewritten_query)
    # rows 模拟第 30 课数据库中的当前版本。
    rows = []
    # chunk 是当前准备评分的片段。
    for chunk in payload["chunks"]:
        # content 是当前版本正文；雨天资料追加第 30 课更新说明。
        content = chunk["content"]
        version = "2026-11-16"
        if chunk["doc_id"] == "rainy-day":
            version = "2026-11-18"
            content += (
                "\n2026-11-18 更新：浙江省博物馆周一闭馆，出发前请预约。"
            )
        # chunk_vector 是片段正文向量。
        chunk_vector = embedder.embed(content)
        # vector_score 是两个单位向量的余弦相似度。
        vector_score = sum(
            left * right
            for left, right in zip(query_vector, chunk_vector)
        )
        rows.append(
            {
                "chunk_id": chunk["id"],
                "doc_id": chunk["doc_id"],
                "strategy": chunk["strategy"],
                "content": content,
                "metadata": chunk.get("metadata", {}),
                "version": version,
                "vector_score": vector_score,
            }
        )
    rows.sort(key=lambda row: row["vector_score"], reverse=True)
    return rows


# 对每个候选计算关键词覆盖、混合分和重排分。
def score_candidates(rows, rewritten_query, embedder):
    # scored_rows 保存带三个分数的候选。
    scored_rows = []
    # row 是当前候选片段。
    for row in rows:
        # coverage 是有效查询词在片段中的覆盖率。
        coverage = keyword_coverage(
            rewritten_query,
            row["content"],
            embedder,
        )
        # hybrid_score 同时考虑向量分数和关键词覆盖。
        hybrid_score = 0.7 * row["vector_score"] + 0.3 * coverage
        # strategy_boost 对更适合当前问题的结构给少量加分。
        strategy_boost = {
            "overlap_window": 0.10,
            "map_landmark": 0.08,
            "markdown_section": 0.04,
            "table_row": 0.02,
        }.get(row["strategy"], 0.0)
        # rerank_score 是最终规则重排分。
        rerank_score = (
            0.75 * hybrid_score
            + 0.15 * coverage
            + 0.10 * strategy_boost
        )
        scored_rows.append(
            {
                **row,
                "keyword_coverage": coverage,
                "hybrid_score": hybrid_score,
                "rerank_score": rerank_score,
            }
        )
    return scored_rows


# 合并向量候选和关键词候选，并按重排分输出 Top-k。
def hybrid_rerank(rows, rewritten_query, embedder, recall_k=6, top_k=3):
    # scored_rows 是所有带分数的候选。
    scored_rows = score_candidates(rows, rewritten_query, embedder)
    # vector_candidates 保存向量召回的前六名。
    vector_candidates = sorted(
        scored_rows,
        key=lambda row: row["vector_score"],
        reverse=True,
    )[:recall_k]
    # lexical_candidates 按关键词覆盖召回前六名。
    lexical_candidates = sorted(
        scored_rows,
        key=lambda row: row["keyword_coverage"],
        reverse=True,
    )[:recall_k]
    # candidate_map 按片段 ID 去重合并两路召回。
    candidate_map = {}
    for row in [*vector_candidates, *lexical_candidates]:
        candidate_map[row["chunk_id"]] = row
    # merged 是去重后的混合候选。
    merged = list(candidate_map.values())
    # reranked 是最终按重排分排序的结果。
    reranked = sorted(
        merged,
        key=lambda row: row["rerank_score"],
        reverse=True,
    )
    return {
        "vector_top": vector_candidates,
        "lexical_top": lexical_candidates,
        "merged_count": len(merged),
        "reranked": reranked[:top_k],
    }


# 运行查询改写、混合召回和重排实验。
def main(
    chunks_path,
    output_dir=None,
    dsn=DEFAULT_DSN,
    scenario=None,
):
    # output_directory 保存本次检索实验报告。
    output_directory = (
        Path(output_dir) if output_dir else Path(chunks_path).parent
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    # embedder 与第 28、30 课保持相同维度。
    embedder = HashedEmbedding(128)
    # rewrite 保存原始问题、改写结果与扩展词。
    rewrite = rewrite_query(RAW_QUERY)

    if scenario == "--offline":
        # rows 从本地 chunks.json 重建同一批当前片段。
        rows = load_offline_rows(
            chunks_path,
            rewrite["rewritten"],
            embedder,
        )
    else:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            rows = load_current_rows(
                connection,
                rewrite["rewritten"],
                embedder,
            )

    # result 是混合召回和重排后的候选组。
    result = hybrid_rerank(rows, rewrite["rewritten"], embedder)
    print("原始问题：", rewrite["original"])
    print("改写问题：", rewrite["rewritten"])
    print("扩展词：", ", ".join(rewrite["expansions"]))
    print("向量候选：", len(result["vector_top"]), "条")
    print("关键词候选：", len(result["lexical_top"]), "条")
    print("合并去重：", result["merged_count"], "条")
    print(
        "向量原始 Top-3：",
        ", ".join(
            f"{row['doc_id']}#{row['chunk_id']}"
            for row in result["vector_top"][:3]
        ),
    )
    print("\n重排 Top-3：")
    # rank 是最终结果名次。
    for rank, row in enumerate(result["reranked"], start=1):
        print(
            f"{rank}. {row['doc_id']}#{row['chunk_id']} "
            f"vector={row['vector_score']:.4f} "
            f"coverage={row['keyword_coverage']:.4f} "
            f"rerank={row['rerank_score']:.4f}"
        )
    print("\nTop-1 证据：")
    print(result["reranked"][0]["content"])

    # report 保存可复核的改写和候选明细。
    report = {
        "mode": "offline" if scenario == "--offline" else "postgresql",
        "query_rewrite": rewrite,
        "vector_top": result["vector_top"],
        "lexical_top": result["lexical_top"],
        "merged_count": result["merged_count"],
        "reranked": result["reranked"],
    }
    # report_path 是本课最终结果文件。
    report_path = output_directory / "hybrid-search-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\n已保存：hybrid-search-report.json")


if __name__ == "__main__":
    main(
        Path(__file__).parent.parent
        / "29-document-chunking"
        / "chunks.json"
    )
