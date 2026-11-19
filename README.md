# agent-engineer-fast-track

《Agent 工程师快速入门》配套代码，使用 Python 3.15.2。

## 第 30 课：PostgreSQL + pgvector

先确保 PostgreSQL 17 服务已启动，并让当前实例能找到 pgvector 0.8：

```bash
psql -h 127.0.0.1 -p 5432 -d postgres -Atc \
  "SELECT name, default_version FROM pg_available_extensions WHERE name = 'vector';"
```

创建专用数据库并执行建表 SQL：

```bash
createdb -h 127.0.0.1 -p 5432 tripilot_rag_30
psql -h 127.0.0.1 -p 5432 -d tripilot_rag_30 -f 30-vector-database/schema.sql
```

安装依赖并运行向量检索演示：

```bash
cd 30-vector-database
uv sync --python 3.15
source .venv/bin/activate
export PGVECTOR_DSN="postgresql://127.0.0.1:5432/tripilot_rag_30"
python3.15 pgvector_demo.py
```

Windows Docker 的数据库连接地址使用 `postgresql://postgres:postgres@127.0.0.1:5432/tripilot_rag_30`。

## 第 33 课：RAG 权限与更新

```bash
cd 33-rag-permissions-updates
python3.15 acl_retrieval.py
```

程序会验证检索前 ACL、跨行程隔离、用户范围缓存、文档版本失效和 TTL 过期，并生成 `answer.md` 与 `acl-report.json`。
