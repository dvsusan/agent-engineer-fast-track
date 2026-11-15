"""Chapter 24 entry point for the complete TripPilot V1 project."""

import sys
from pathlib import Path


# STAGE_ROOT 是编号目录上一级的 stage-03。
STAGE_ROOT = Path(__file__).resolve().parents[1]
# 把 stage-03 加入模块搜索路径，保证直接运行本文件时能找到 tripilot。
sys.path.insert(0, str(STAGE_ROOT))

from tripilot.main import main  # noqa: E402


# PROJECT_DIR 是本课编号目录，输入与输出文件都放在这里。
PROJECT_DIR = Path(__file__).resolve().parent
# REQUEST_FILE 是本课入口读取的旅行需求文件。
REQUEST_FILE = PROJECT_DIR / "sample_request.json"


if __name__ == "__main__":
    main(request_path=REQUEST_FILE, output_dir=PROJECT_DIR)
