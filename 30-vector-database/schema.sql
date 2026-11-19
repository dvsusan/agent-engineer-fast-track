-- 第 30 课专用数据库：tripilot_rag_30
-- 这段 SQL 可以重复执行，但已存在的旧表不会被改造成新结构。

-- pgvector 必须先安装到当前 PostgreSQL 实例，扩展才会出现在可用列表中。
CREATE EXTENSION IF NOT EXISTS vector;

-- 当前版本只保留一份旅行资料片段，point_id 由 chunk_id 和 version 稳定生成。
CREATE TABLE IF NOT EXISTS tripilot_chunks (
    point_id BIGINT PRIMARY KEY,
    chunk_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    content TEXT NOT NULL,
    city TEXT NOT NULL,
    access_group TEXT NOT NULL,
    version TEXT NOT NULL,
    metadata JSONB NOT NULL,
    embedding vector(128) NOT NULL
);

-- HNSW 用于加速余弦距离检索；维度或距离类型变化时必须重建索引。
CREATE INDEX IF NOT EXISTS tripilot_chunks_embedding_idx
ON tripilot_chunks
USING hnsw (embedding vector_cosine_ops);

-- 先按业务字段缩小候选集，再执行向量排序。
CREATE INDEX IF NOT EXISTS tripilot_chunks_filter_idx
ON tripilot_chunks (city, version, access_group, doc_id);
