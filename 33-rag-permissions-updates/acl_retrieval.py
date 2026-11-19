"""Chapter 33 entry point for ACL-aware and versioned RAG retrieval."""

import sys
from pathlib import Path


# STAGE_ROOT 是编号目录上一级的 stage-03。
STAGE_ROOT = Path(__file__).resolve().parents[1]
# 把 stage-03 加入模块搜索路径，保证直接运行本文件时能找到 tripilot。
sys.path.insert(0, str(STAGE_ROOT))

from tripilot.access_control import main  # noqa: E402


# PROJECT_DIR 保存本课输入、答案和报告。
PROJECT_DIR = Path(__file__).resolve().parent
# DOCUMENTS_FILE 是初始有效文档。
DOCUMENTS_FILE = PROJECT_DIR / "documents.json"
# UPDATE_FILE 是路线文档的第二版。
UPDATE_FILE = PROJECT_DIR / "route-v2.json"


if __name__ == "__main__":
    main(
        documents_path=DOCUMENTS_FILE,
        update_path=UPDATE_FILE,
        output_dir=PROJECT_DIR,
    )
