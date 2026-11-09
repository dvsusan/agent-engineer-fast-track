"""Check the tools needed for the first part of the Agent course."""

from __future__ import annotations

import platform
import shutil
import sys


def find_command(name: str) -> str:
    path = shutil.which(name)
    return path if path else "未找到"


print(f"Python 版本: {platform.python_version()}")
print(f"Python 路径: {sys.executable}")
print(f"Git: {find_command('git')}")
print(f"uv: {find_command('uv')}")
print(f"VS Code CLI: {find_command('code')}")

if sys.version_info < (3, 12):
    print("提示: 当前 Python 低于 3.12，请按照教程安装课程版本。")
else:
    print("提示: Python 主版本符合课程要求。")
