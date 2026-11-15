"""Chapter 23 entry point for resilient side-effect execution."""

import sys
from pathlib import Path


# STAGE_ROOT 是编号目录上一级的 stage-03。
STAGE_ROOT = Path(__file__).resolve().parents[1]
# 把 stage-03 加入模块搜索路径，保证直接运行本文件时能找到 tripilot。
sys.path.insert(0, str(STAGE_ROOT))

from tripilot.executor import main  # noqa: E402


# SCENARIO 是命令行传入的演示模式，例如 --conflict。
SCENARIO = sys.argv[1] if len(sys.argv) > 1 else ""


if __name__ == "__main__":
    main(scenario=SCENARIO)
