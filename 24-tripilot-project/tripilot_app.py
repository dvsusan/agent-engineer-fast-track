"""Chapter 24 web entry point for the interactive TripPilot workspace."""

import sys
from pathlib import Path


# STAGE_ROOT 是编号目录上一级的 stage-03。
STAGE_ROOT = Path(__file__).resolve().parents[1]
# 把 stage-03 加入模块搜索路径，保证直接运行本文件时能找到 tripilot。
sys.path.insert(0, str(STAGE_ROOT))

from tripilot.dashboard import run_dashboard  # noqa: E402


# PROJECT_DIR 是本课编号目录，行程和检查点都保存在这里。
PROJECT_DIR = Path(__file__).resolve().parent
# DASHBOARD_PORT 是本地 TripPilot 工作台使用的端口。
DASHBOARD_PORT = 8000


if __name__ == "__main__":
    run_dashboard(PROJECT_DIR, port=DASHBOARD_PORT)
