"""Store and query TripPilot RAG chunks in PostgreSQL with pgvector."""

import hashlib
import json
import math
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .rag import DEFAULT_QUERY, HashedEmbedding


# EMBEDDING_DIMENSIONS 必须与第 28 课生成的向量维度保持一致。
EMBEDDING_DIMENSIONS = 128
# DEFAULT_DSN 指向本课专用数据库，也可以通过环境变量覆盖。
DEFAULT_DSN = os.getenv(
    "PGVECTOR_DSN",
    "postgresql://127.0.0.1:5432/tripilot_rag_30",
)
# COLLECTION_VERSION_V1 是初始资料版本。
COLLECTION_VERSION_V1 = "2026-11-16"
# COLLECTION_VERSION_V2 是雨天资料更新后的版本。
COLLECTION_VERSION_V2 = "2026-11-18"


# PgVectorError 表示索引内容或数据库操作不满足本课约束。
class PgVectorError(ValueError):
    """The vector row or database operation is invalid."""


# 把浮点列表转换成 pgvector 接受的文本格式。
def vector_literal(values):
    if len(values) != EMBEDDING_DIMENSIONS:
        raise PgVectorError(
            f"向量维度必须是 {EMBEDDING_DIMENSIONS}，当前是 {len(values)}"
        )
    if any(not math.isfinite(value) for value in values):
        raise PgVectorError("向量不能包含 NaN 或无穷大")
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


# 根据片段和版本生成稳定的 PostgreSQL BIGINT 点编号。
def point_id_for(chunk_id, version):
    # digest 是片段版本组合的稳定哈希。
    digest = hashlib.blake2b(
        f"{chunk_id}:{version}".encode("utf-8"),
        digest_size=8,
    ).digest()
    # PostgreSQL BIGINT 是有符号 64 位，保留低 63 位避免溢出。
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


# 读取第 29 课输出的 chunks.json。
def load_source_chunks(chunks_path):
    # payload 是第 29 课生成的完整片段 JSON。
    payload = json.loads(Path(chunks_path).read_text(encoding="utf-8"))
    # chunks 是待写入 pgvector 的片段列表。
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise PgVectorError("chunks.json 中没有可索引片段")
    return chunks


# 读取同一份 schema.sql，先删除演示表，再创建扩展、表和索引。
def prepare_schema(connection, schema_path):
    # schema_file 是读者也能用 psql -f 手工执行的建表文件。
    schema_file = Path(schema_path)
    if not schema_file.exists():
        raise PgVectorError(f"建表文件不存在：{schema_file}")
    # schema_sql 包含扩展、表定义和两个索引。
    schema_sql = schema_file.read_text(encoding="utf-8")

    # 先删除旧演示表保证重复运行结果一致，再执行同一份建表 SQL。
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS tripilot_chunks")
        cursor.execute(schema_sql)
    connection.commit()


# 将一批结构化片段写入 pgvector。
def upsert_chunks(connection, chunks, version, embedder):
    # rows 保存准备执行的批量参数。
    rows = []
    # chunk 是当前要写入的文本片段。
    for chunk in chunks:
        # metadata 保留切分策略、章节或字符范围。
        metadata = chunk.get("metadata", {})
        # vector 是片段正文的单位向量。
        vector = embedder.embed(chunk["content"])
        rows.append(
            (
                point_id_for(chunk["id"], version),
                chunk["id"],
                chunk["doc_id"],
                chunk["strategy"],
                chunk["content"],
                "杭州",
                "travel-public",
                version,
                Jsonb(metadata),
                vector_literal(vector),
            )
        )

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO tripilot_chunks (
                point_id, chunk_id, doc_id, strategy, content,
                city, access_group, version, metadata, embedding
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s::vector
            )
            ON CONFLICT (point_id) DO UPDATE
            SET content = EXCLUDED.content,
                metadata = EXCLUDED.metadata,
                embedding = EXCLUDED.embedding
            """,
            rows,
        )
    connection.commit()
    return len(rows)


# 计算某个过滤范围内的数据量。
def count_rows(connection, city, version, allowed_groups, doc_ids=None):
    # params 是过滤条件的参数列表。
    params = [city, version, allowed_groups]
    # sql 保存当前查询语句。
    sql = """
        SELECT count(*) AS count
        FROM tripilot_chunks
        WHERE city = %s
          AND version = %s
          AND access_group = ANY(%s)
    """
    if doc_ids is not None:
        sql += " AND doc_id = ANY(%s)"
        params.append(doc_ids)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()["count"]


# 在 PostgreSQL 中执行带元数据过滤的向量检索。
def search_chunks(
    connection,
    query,
    embedder,
    city,
    version,
    allowed_groups,
    doc_ids=None,
    top_k=3,
):
    # query_vector 是当前问题的 pgvector 文本。
    query_vector = vector_literal(embedder.embed(query))
    # where_sql 保存可选的文档范围过滤。
    where_sql = ""
    if doc_ids is not None:
        where_sql = " AND doc_id = ANY(%s)"

    # sql 使用 <=> 余弦距离，并转换成 1 - 距离的相似度。
    sql = f"""
        SELECT
            chunk_id,
            doc_id,
            strategy,
            content,
            metadata,
            1 - (embedding <=> %s::vector) AS score
        FROM tripilot_chunks
        WHERE city = %s
          AND version = %s
          AND access_group = ANY(%s)
          {where_sql}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    # reordered_params 让第一个 %s::vector 使用查询向量。
    reordered_params = [
        query_vector,
        city,
        version,
        allowed_groups,
    ]
    if doc_ids is not None:
        reordered_params.append(doc_ids)
    reordered_params.extend([query_vector, top_k])

    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(sql, reordered_params)
        return cursor.fetchall()


# 删除指定文档的旧版本，保留同一文档的新版本。
def delete_document_version(connection, doc_id, version):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM tripilot_chunks
            WHERE doc_id = %s AND version = %s
            """,
            (doc_id, version),
        )
        # deleted 是本次删除的行数。
        deleted = cursor.rowcount
    connection.commit()
    return deleted


# 读取数据库版本、pgvector 版本和当前索引统计。
def database_info(connection):
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT version() AS postgresql_version"
        )
        # database_version 是 PostgreSQL 完整版本文本。
        database_version = cursor.fetchone()["postgresql_version"]
        cursor.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
        # vector_version 是当前 pgvector 扩展版本。
        vector_version = cursor.fetchone()["extversion"]
        cursor.execute(
            """
            SELECT version, count(*) AS count
            FROM tripilot_chunks
            GROUP BY version
            ORDER BY version
            """
        )
        # version_counts 是各资料版本的记录数。
        version_counts = cursor.fetchall()
    return {
        "postgresql": database_version.split(",")[0],
        "pgvector": vector_version,
        "version_counts": version_counts,
    }


# 运行 pgvector 索引、过滤和版本更新演示。
def main(
    chunks_path,
    output_dir=None,
    dsn=DEFAULT_DSN,
    scenario=None,
    schema_path=None,
):
    # source_path 是第 29 课生成的结构化片段。
    source_path = Path(chunks_path)
    # output_directory 保存本次数据库运行报告。
    output_directory = (
        Path(output_dir) if output_dir else source_path.parent
    )
    output_directory.mkdir(parents=True, exist_ok=True)

    if scenario == "--bad-vector":
        try:
            vector_literal([0.1, 0.2])
        except PgVectorError as error:
            print(f"写入失败：{error}")
            raise SystemExit(1) from error

    # source_chunks 是第 29 课的 12 个结构感知片段。
    source_chunks = load_source_chunks(source_path)
    # embedder 复用第 28 课的确定性向量生成器。
    embedder = HashedEmbedding(EMBEDDING_DIMENSIONS)

    # schema_file 默认使用第 30 课目录中的建表 SQL。
    schema_file = (
        Path(schema_path)
        if schema_path
        else Path(__file__).resolve().parent.parent
        / "30-vector-database"
        / "schema.sql"
    )

    with psycopg.connect(dsn, connect_timeout=5) as connection:
        prepare_schema(connection, schema_file)
        # inserted_v1 是初始版本写入数量。
        inserted_v1 = upsert_chunks(
            connection,
            source_chunks,
            COLLECTION_VERSION_V1,
            embedder,
        )
        # filter_doc_ids 把检索限制在雨天资料和地图说明。
        filter_doc_ids = ["rainy-day", "west-lake-map"]
        # before_hits 是 v1 版本经过元数据过滤后的召回结果。
        before_hits = search_chunks(
            connection,
            DEFAULT_QUERY,
            embedder,
            city="杭州",
            version=COLLECTION_VERSION_V1,
            allowed_groups=["travel-public"],
            doc_ids=filter_doc_ids,
        )
        # candidate_count 是执行文档范围过滤后的候选数量。
        candidate_count = count_rows(
            connection,
            "杭州",
            COLLECTION_VERSION_V1,
            ["travel-public"],
            filter_doc_ids,
        )

        # updated_chunks 只复制并更新雨天资料。
        updated_chunks = []
        # source_chunk 是当前准备发布新版的片段。
        for source_chunk in source_chunks:
            if source_chunk["doc_id"] != "rainy-day":
                continue
            # new_chunk 是新版本片段副本。
            new_chunk = {
                **source_chunk,
                "content": (
                    source_chunk["content"]
                    + "\n2026-11-18 更新：浙江省博物馆周一闭馆，"
                    "出发前请预约。"
                ),
            }
            updated_chunks.append(new_chunk)

        # inserted_v2 是雨天资料新版写入数量。
        inserted_v2 = upsert_chunks(
            connection,
            updated_chunks,
            COLLECTION_VERSION_V2,
            embedder,
        )
        # deleted_v1 是删除的雨天资料旧版本行数。
        deleted_v1 = delete_document_version(
            connection,
            "rainy-day",
            COLLECTION_VERSION_V1,
        )
        # after_hits 是只查询新版雨天资料的召回结果。
        after_hits = search_chunks(
            connection,
            DEFAULT_QUERY,
            embedder,
            city="杭州",
            version=COLLECTION_VERSION_V2,
            allowed_groups=["travel-public"],
            doc_ids=["rainy-day"],
        )
        # database 保存版本与数量统计。
        database = database_info(connection)

    print(f"PostgreSQL：{database['postgresql']}")
    print(f"pgvector：{database['pgvector']}")
    print(f"初始写入：{inserted_v1} 条，版本 {COLLECTION_VERSION_V1}")
    print(
        f"元数据过滤：city=杭州, doc_id=rainy-day/west-lake-map, "
        f"候选 {candidate_count} 条"
    )
    print("更新前 Top-3：")
    # rank 是当前检索名次。
    for rank, hit in enumerate(before_hits, start=1):
        print(
            f"{rank}. score={hit['score']:.4f} "
            f"source={hit['doc_id']}#{hit['chunk_id']}"
        )
    print(
        f"发布新版：rainy-day {COLLECTION_VERSION_V2} "
        f"写入 {inserted_v2} 条，删除旧版 {deleted_v1} 条"
    )
    print("更新后 Top-3：")
    for rank, hit in enumerate(after_hits, start=1):
        print(
            f"{rank}. score={hit['score']:.4f} "
            f"source={hit['doc_id']}#{hit['chunk_id']}"
        )
    if after_hits:
        print("新版证据包含更新说明：", "周一闭馆" in after_hits[0]["content"])
    print("版本统计：", database["version_counts"])

    # report 是本次 pgvector 实验的机器可读证据。
    report = {
        "database": database,
        "dsn_database": "tripilot_rag_30",
        "initial_version": COLLECTION_VERSION_V1,
        "initial_rows": inserted_v1,
        "metadata_filter": {
            "city": "杭州",
            "doc_ids": filter_doc_ids,
            "access_group": "travel-public",
            "candidate_count": candidate_count,
        },
        "before_hits": before_hits,
        "updated_version": COLLECTION_VERSION_V2,
        "updated_rows": inserted_v2,
        "deleted_old_rows": deleted_v1,
        "after_hits": after_hits,
    }
    # report_path 保存数据库检索与版本更新结果。
    report_path = output_directory / "pgvector-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("已保存：pgvector-report.json")


if __name__ == "__main__":
    main(
        Path(__file__).parent.parent
        / "29-document-chunking"
        / "chunks.json"
    )
