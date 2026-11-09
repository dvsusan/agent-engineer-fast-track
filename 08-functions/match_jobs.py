"""Use a reusable function to calculate job match scores."""

jobs = [
    {
        "title": "Python Agent 工程师",
        "city": "上海",
        "salary": 18000,
        "skills": ["Python", "RAG", "FastAPI"],
    },
    {
        "title": "AI 应用工程师",
        "city": "上海",
        "salary": 22000,
        "skills": ["Python", "Prompt", "FastAPI"],
    },
    {
        "title": "数据分析工程师",
        "city": "杭州",
        "salary": 16000,
        "skills": ["Python", "SQL"],
    },
]


def calculate_match_score(job, preferred_city, minimum_salary, wanted_skills):
    """Return a score from 0 to 100 for one job."""
    # Keep the scoring rule in one place so every job uses the same weights.
    score = 0

    if job["city"] == preferred_city:
        score += 30

    if job["salary"] >= minimum_salary:
        score += 30

    matched_skills = 0
    for skill in wanted_skills:
        if skill in job["skills"]:
            matched_skills += 1

    if wanted_skills:
        score += round(matched_skills / len(wanted_skills) * 40)

    return score


preferred_city = input("目标城市：").strip()
minimum_salary = int(input("最低月薪（元）：").strip())
skills_text = input("你的技能（用逗号分隔）：").strip()
wanted_skills = [skill.strip() for skill in skills_text.split(",") if skill.strip()]

print("\n岗位匹配结果")
for job in jobs:
    score = calculate_match_score(job, preferred_city, minimum_salary, wanted_skills)
    print(f"{job['title']}：{score} 分")
