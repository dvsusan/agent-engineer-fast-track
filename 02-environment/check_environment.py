"""Check the tools needed for the first part of the Agent course."""

from __future__ import annotations

import platform
import shutil
import sys


COURSE_PYTHON = (3, 15, 2)


def find_command(name: str) -> str:
    path = shutil.which(name)
    return path if path else "未找到"


print(f"Python 版本: {platform.python_version()}")
print(f"Python 路径: {sys.executable}")
print(f"Git: {find_command('git')}")
print(f"uv: {find_command('uv')}")
print(f"VS Code CLI: {find_command('code')}")

if sys.version_info[:3] != COURSE_PYTHON:
    required = ".".join(str(part) for part in COURSE_PYTHON)
    print(f"提示: 当前 Python 不是课程基线 {required}，请安装对应版本。")
else:
    print("提示: Python 版本符合课程基线 3.15.2。")
