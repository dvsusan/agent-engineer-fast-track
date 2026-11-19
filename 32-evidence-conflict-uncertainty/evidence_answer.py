"""Chapter 32 entry point for cited and conflict-aware travel answers."""

import sys
from pathlib import Path


# STAGE_ROOT 是编号目录上一级的 stage-03。
STAGE_ROOT = Path(__file__).resolve().parents[1]
# 把 stage-03 加入模块搜索路径，保证直接运行本文件时能找到 tripilot。
sys.path.insert(0, str(STAGE_ROOT))

from tripilot.evidence import main  # noqa: E402


# PROJECT_DIR 是本课编号目录，答案和证据报告保存在这里。
PROJECT_DIR = Path(__file__).resolve().parent
# EVIDENCE_FILE 是本课证据卡 JSON。
EVIDENCE_FILE = PROJECT_DIR / "evidence-cards.json"
# SCENARIO 是命令行传入的无证据演示模式。
SCENARIO = sys.argv[1] if len(sys.argv) > 1 else ""


if __name__ == "__main__":
    main(
        evidence_path=EVIDENCE_FILE,
        output_dir=PROJECT_DIR,
        scenario=SCENARIO,
    )
