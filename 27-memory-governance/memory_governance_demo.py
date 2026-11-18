"""Chapter 27 entry point for governed TripPilot memory."""

import sys
from pathlib import Path


# STAGE_ROOT 是编号目录上一级的 stage-03。
STAGE_ROOT = Path(__file__).resolve().parents[1]
# 把 stage-03 加入模块搜索路径，保证直接运行本文件时能找到 tripilot。
sys.path.insert(0, str(STAGE_ROOT))

from tripilot.memory_governance import main  # noqa: E402


# PROJECT_DIR 是本课编号目录，治理文件保存在这里。
PROJECT_DIR = Path(__file__).resolve().parent
# SCENARIO 是命令行传入的过期纠错演示模式。
SCENARIO = sys.argv[1] if len(sys.argv) > 1 else ""


if __name__ == "__main__":
    main(output_dir=PROJECT_DIR, scenario=SCENARIO)
