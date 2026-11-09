"""Collect basic job-search preferences from the terminal."""

city = input("目标城市：").strip()
salary_text = input("最低月薪（元）：").strip()
# input() returns text, so parse the salary before using it as a number.
minimum_salary = int(salary_text)
skills_text = input("想匹配的技能（用逗号分隔）：").strip()
remote_answer = input("只接受远程岗位吗？(y/n)：").strip().lower()

skills = []
# Split comma-separated text and keep only non-empty cleaned items.
for skill in skills_text.split(","):
    cleaned_skill = skill.strip()
    if cleaned_skill:
        skills.append(cleaned_skill)
remote_only = remote_answer in {"y", "yes", "是"}

print()
print("你的求职偏好")
print(f"城市：{city}")
print(f"最低月薪：{minimum_salary} 元")
print(f"技能：{', '.join(skills)}")
print(f"只接受远程：{remote_only}")
