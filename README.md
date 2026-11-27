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

## 第 41 课：LangChain 模型、工具、编排与输出解析

先复用第 40 课已经验证过的 DeepSeek 配置：

```bash
cd 41-langchain-capabilities
cp ../40-deepseek-qwen-gateway/.env .env
cp ../35-data-analysis-basics/bookings.csv .
uv sync --python 3.15
```

运行真实模型分析链：

```bash
.venv/bin/python datapilot_langchain.py --output langchain-report.json
```

程序会用 `ChatOpenAI` 调用 DeepSeek，让模型选择 `audit_booking_file`，再由 pandas 计算有效订单、客单价、城市分布和异常订单。成功结果写入 `langchain-report.json`；缺失 CSV 时会停止二次生成，并把失败原因写入 `langchain-failure-report.json`。

## 第 42 课：LangGraph 状态图与人工审批

```bash
cd 42-langgraph-state-workflow
cp ../41-langchain-capabilities/.env .env
cp ../35-data-analysis-basics/bookings.csv .
uv sync --python 3.15
```

启动高风险分析线程，流程会在审批节点暂停：

```bash
.venv/bin/python datapilot_graph.py --start --thread-id demo-approval
.venv/bin/python datapilot_graph.py --status --thread-id demo-approval
```

批准或拒绝后从同一个 SQLite Checkpoint 恢复：

```bash
.venv/bin/python datapilot_graph.py --resume approve --thread-id demo-approval --feedback "仅用于内部复盘"
.venv/bin/python datapilot_graph.py --start --thread-id demo-reject
.venv/bin/python datapilot_graph.py --resume reject --thread-id demo-reject --feedback "数据口径未确认"
```

`checkpoint.sqlite` 保存可恢复状态，`analysis-demo-*.json` 保存批准、拒绝或失败记录。Checkpoint 可能包含订单与审批信息，公开分享前必须脱敏。

## 第 45 课：Agent Skill 工程化

```bash
cd 45-agent-skill-engineering
uv sync --python 3.15
.venv/bin/python skill_runtime.py --list-skills
.venv/bin/python skill_runtime.py \
  --request "检查这份 2026 年 10 月财务报表：收入是否等于支出加结余，有没有重复流水、缺失科目或异常大额支出。" \
  --input skills/financial-report-audit/assets/sample-report.csv \
  --output financial-audit-report.json \
  --run-record skill-run.json \
  --expect-status failed
```

运行时会发现 `financial-report-audit` 和 `travel-itinerary` 两个技能，只加载财务技能正文并执行审计脚本。问题报表输出 4 条发现，干净报表使用 `assets/clean-report.csv` 并通过全部检查；`missing-input-run.json` 保留缺失输入文件的失败证据。

## 第 46 课：Agent Skill 版本治理

```bash
cd 46-agent-skill-governance
uv sync --python 3.15
.venv/bin/python skill_governance.py manifest
.venv/bin/python skill_governance.py access \
  --principal alice \
  --version 1.0.0 \
  --input fixtures/sample-report.csv \
  --output governance-artifacts/acl/alice-active.json
.venv/bin/python skill_governance.py test; exit_code=$?; echo "测试退出码=$exit_code"
.venv/bin/python skill_governance.py release
```

清单会记录 1.0.0 和 1.1.0 的入口、规则、权限主体与 SHA-256。Alice 可以执行正式版，Bob 被 ACL 默认拒绝，Alice 不能提前执行候选版。正式版三条回归用例全部通过；候选版只有 2/3 通过，因为 1.1.0 把大额支出阈值提高到 70000 元后漏掉了 68000 元的 `LARGE_EXPENSE`。因此 `test` 命令返回 1，完整 `release` 门禁返回 0 并写入 `decision=blocked`、`rollback-record.json`，正式版本仍保持 1.0.0。

## 第 47 课：单 Agent 与多 Agent 路由比较

```bash
cd 47-single-vs-multi-agent
uv sync --python 3.15
.venv/bin/python agent_lab.py run \
  --request-id simple \
  --mode single \
  --output simple-single-report.json
.venv/bin/python agent_lab.py run \
  --request-id complex \
  --mode multi \
  --output complex-multi-report.json
.venv/bin/python agent_lab.py run \
  --request-id overbudget \
  --mode multi \
  --output overbudget-multi-report.json
.venv/bin/python agent_lab.py compare \
  --output comparison-report.json \
  --handoff-record handoff-record.json
```

本课不调用外部模型，所有角色都使用确定性工具，方便单独观察架构选择。简单请求复杂度为 `1/3`，推荐单 Agent；复杂请求命中五个信号，复杂度为 `5/3`，推荐多 Agent，并产生 `H001/H002/H003` 三次结构化交接。超预算方案会在独立复核阶段得到 `blocked`，`comparison-report.json` 的十项检查应全部为 `true`。
