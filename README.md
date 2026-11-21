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

## 第 34 课：TripPilot 阶段交付

```bash
cd 34-tripilot-stage-delivery
python3.15 tripilot_stage.py
```

程序会把身份、ACL 检索、引用、文档版本和偏好记忆串进同一次运行，并生成 `stage-answer.md`、`stage-memory.json` 与 `stage-report.json`。

启动本地智能旅行规划工作台：

```bash
python3.15 stage_dashboard.py --port 8010
```

浏览器打开 `http://127.0.0.1:8010`，可以切换 Alice / Bob、输入自然语言旅行需求、选择日期天数预算与兴趣，实时查看工具调用和逐日行程。页面还会展示 ACL 检索审计、被拦截资料、长期偏好写回、工具超时降级和阶段校验结果，并支持把行程导出为 Markdown。Windows PowerShell 可将 `python3.15` 改为 `py -3.15`。

工作台默认使用第 33 课的内存索引，启动时不需要 PostgreSQL；第 30 课的 pgvector 存储可以替换这一层，规划器的输入输出契约不变。

也可以直接用命令行验证规划器：

```bash
python3.15 - <<'PY'
import json
from travel_agent import TravelPlanningAgent

result = TravelPlanningAgent().plan({
    "user_id": "alice",
    "prompt": "周末去杭州两天，喜欢拍照和美食，节奏轻松，总预算 3000 元",
    "start_date": "2026-11-22",
    "days": 2,
    "budget": 3000,
    "pace": "轻松",
    "interests": ["拍照", "美食"],
    "remember": False,
})
print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
PY
```

## 第 35 课：CSV 与 pandas 数据体检

macOS / Linux：

```bash
cd 35-data-analysis-basics
uv sync --python 3.15
.venv/bin/python data_health.py
```

Windows PowerShell：

```powershell
cd 35-data-analysis-basics
uv sync --python 3.15
.venv\Scripts\python data_health.py
```

脚本读取 `bookings.csv`，检查字段类型、缺失值、重复订单号、非法数值和四分位距（IQR）价格离群点，并生成 `data-health-report.md` 与 `data-health-report.json`。
