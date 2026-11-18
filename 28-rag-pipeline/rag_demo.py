"""Chapter 28 entry point for the TripPilot RAG pipeline."""

import sys
from pathlib import Path


# STAGE_ROOT 是编号目录上一级的 stage-03。
STAGE_ROOT = Path(__file__).resolve().parents[1]
# 把 stage-03 加入模块搜索路径，保证直接运行本文件时能找到 tripilot。
sys.path.insert(0, str(STAGE_ROOT))

from tripilot.rag import main  # noqa: E402


# PROJECT_DIR 是本课编号目录，资料和 RAG 结果都保存在这里。
PROJECT_DIR = Path(__file__).resolve().parent
# DOC_DIR 是四份旅行资料的目录。
DOC_DIR = PROJECT_DIR / "docs"
# SCENARIO 是命令行传入的检索边界演示模式。
SCENARIO = sys.argv[1] if len(sys.argv) > 1 else ""


if __name__ == "__main__":
    main(
        doc_dir=DOC_DIR,
        output_dir=PROJECT_DIR,
        scenario=SCENARIO,
    )
