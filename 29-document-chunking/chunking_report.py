"""Chapter 29 entry point for document-aware TripPilot chunking."""

import sys
from pathlib import Path


# STAGE_ROOT 是编号目录上一级的 stage-03。
STAGE_ROOT = Path(__file__).resolve().parents[1]
# 把 stage-03 加入模块搜索路径，保证直接运行本文件时能找到 tripilot。
sys.path.insert(0, str(STAGE_ROOT))

from tripilot.chunking import main  # noqa: E402


# PROJECT_DIR 是本课编号目录，切分报告保存在这里。
PROJECT_DIR = Path(__file__).resolve().parent
# DOC_DIR 复用第 28 课创建的四份旅行资料。
DOC_DIR = STAGE_ROOT / "28-rag-pipeline" / "docs"
# SCENARIO 是命令行传入的切分边界演示模式。
SCENARIO = sys.argv[1] if len(sys.argv) > 1 else ""


if __name__ == "__main__":
    main(
        doc_dir=DOC_DIR,
        output_dir=PROJECT_DIR,
        scenario=SCENARIO,
    )
