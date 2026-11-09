"""Read job records from a JSON file stored beside this script."""

import json
from pathlib import Path

data_file = Path(__file__).with_name("jobs.json")

try:
    # UTF-8 keeps Chinese text readable; json.load parses the file contents.
    with data_file.open("r", encoding="utf-8") as file:
        jobs = json.load(file)
except FileNotFoundError:
    print(f"找不到数据文件：{data_file}")
    raise SystemExit(1)
except json.JSONDecodeError as error:
    print(f"JSON 格式错误：第 {error.lineno} 行，第 {error.colno} 列")
    raise SystemExit(1)

print(f"数据文件：{data_file.name}")
print(f"共读取 {len(jobs)} 个岗位")

for job in jobs:
    print(f"- {job['title']}｜{job['city']}｜{job['salary']} 元")
