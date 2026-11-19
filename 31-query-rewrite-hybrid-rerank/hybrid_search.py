"""Chapter 31 entry point for query rewrite, hybrid recall, and rerank."""

import sys
from pathlib import Path


# STAGE_ROOT 是编号目录上一级的 stage-03。
STAGE_ROOT = Path(__file__).resolve().parents[1]
# 把 stage-03 加入模块搜索路径，保证直接运行本文件时能找到 tripilot。
sys.path.insert(0, str(STAGE_ROOT))

from tripilot.query_optimizer import main  # noqa: E402


# PROJECT_DIR 是本课编号目录，检索报告保存在这里。
PROJECT_DIR = Path(__file__).resolve().parent
# CHUNKS_FILE 复用第 29 课的结构化片段。
CHUNKS_FILE = STAGE_ROOT / "29-document-chunking" / "chunks.json"
# SCENARIO 是命令行传入的离线或数据库模式。
SCENARIO = sys.argv[1] if len(sys.argv) > 1 else ""


if __name__ == "__main__":
    main(
        chunks_path=CHUNKS_FILE,
        output_dir=PROJECT_DIR,
        scenario=SCENARIO,
    )
