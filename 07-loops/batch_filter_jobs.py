"""Filter a batch of jobs with while and for loops."""

jobs = [
    {"title": "Python Agent 工程师", "city": "上海", "salary": 18000},
    {"title": "AI 应用工程师", "city": "杭州", "salary": 22000},
    {"title": "数据分析工程师", "city": "上海", "salary": 16000},
    {"title": "RAG 工程师", "city": "上海", "salary": 25000},
    {"title": "Python 开发工程师", "city": "深圳", "salary": 15000},
]

preferred_city = input("目标城市：").strip()

attempts = 0
minimum_salary = 0
# Stop asking after three invalid inputs.
while attempts < 3:
    salary_text = input("最低月薪（元）：").strip()
    if salary_text.isdigit():
        minimum_salary = int(salary_text)
        break
    attempts += 1
    print("请输入纯数字，例如 15000。")
else:
    print("输入次数已用完，本次使用最低月薪 0。")

matched_count = 0
print("\n符合条件的岗位")

# matched_count must live outside the loop so results accumulate across jobs.
for job in jobs:
    if job["city"] == preferred_city and job["salary"] >= minimum_salary:
        matched_count += 1
        print(f"- {job['title']}｜{job['city']}｜{job['salary']} 元")

print(f"\n共找到 {matched_count} 个岗位。")
