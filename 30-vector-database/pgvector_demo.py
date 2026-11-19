"""Chapter 30 entry point for the TripPilot PostgreSQL pgvector index."""

import sys
from pathlib import Path


# STAGE_ROOT 是编号目录上一级的 stage-03。
STAGE_ROOT = Path(__file__).resolve().parents[1]
# 把 stage-03 加入模块搜索路径，保证直接运行本文件时能找到 tripilot。
sys.path.insert(0, str(STAGE_ROOT))

from tripilot.pgvector_store import main  # noqa: E402


# PROJECT_DIR 是本课编号目录，数据库报告保存在这里。
PROJECT_DIR = Path(__file__).resolve().parent
# CHUNKS_FILE 复用第 29 课生成的结构化片段。
CHUNKS_FILE = STAGE_ROOT / "29-document-chunking" / "chunks.json"
# SCHEMA_FILE 是读者也能手工执行的建表 SQL。
SCHEMA_FILE = PROJECT_DIR / "schema.sql"
# SCENARIO 是命令行传入的错误演示模式。
SCENARIO = sys.argv[1] if len(sys.argv) > 1 else ""


if __name__ == "__main__":
    main(
        chunks_path=CHUNKS_FILE,
        output_dir=PROJECT_DIR,
        scenario=SCENARIO,
        schema_path=SCHEMA_FILE,
    )
