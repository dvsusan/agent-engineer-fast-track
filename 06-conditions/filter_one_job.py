"""Use conditions to decide whether one job should be recommended."""

job = {
    "title": "Python Agent 工程师",
    "city": "上海",
    "salary": 18000,
    "remote": False,
    "skills": ["Python", "RAG"],
}

preferred_city = input("目标城市：").strip()
minimum_salary = int(input("最低月薪（元）：").strip())
remote_only = input("只接受远程岗位吗？(y/n)：").strip().lower() == "y"

city_ok = job["city"] == preferred_city
salary_ok = job["salary"] >= minimum_salary
# A job passes when it is remote, or when the user does not require remote work.
remote_ok = job["remote"] or not remote_only

print()
print("岗位检查结果")
print(f"城市匹配：{city_ok}")
print(f"薪资匹配：{salary_ok}")
print(f"远程条件匹配：{remote_ok}")

if city_ok and salary_ok and remote_ok:
    print("结论：推荐投递")
elif salary_ok and remote_ok:
    print("结论：薪资合适，但城市不匹配")
else:
    print("结论：暂不推荐")
