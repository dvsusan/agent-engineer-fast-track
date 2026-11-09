"""JobSift: a small job filter built with the Stage 1 Python skills."""

import json
from pathlib import Path


def load_jobs(file_path):
    """Load valid job records and count records with missing fields."""
    with file_path.open("r", encoding="utf-8") as file:
        raw_jobs = json.load(file)

    if not isinstance(raw_jobs, list):
        raise ValueError("JSON 顶层必须是列表。")

    jobs = []
    skipped = 0

    # Invalid records stop at this boundary and never enter the filter pipeline.
    for raw_job in raw_jobs:
        try:
            job = {
                "title": str(raw_job["title"]).strip(),
                "city": str(raw_job["city"]).strip(),
                "salary": int(raw_job["salary"]),
                "remote": bool(raw_job["remote"]),
                "skills": [str(skill) for skill in raw_job["skills"]],
            }
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue

        if not job["title"] or not job["city"]:
            skipped += 1
            continue

        jobs.append(job)

    return jobs, skipped


def read_minimum_salary():
    """Read a positive integer, retrying up to three times."""
    for _ in range(3):
        salary_text = input("最低月薪（元）：").strip()
        if salary_text.isdigit() and int(salary_text) > 0:
            return int(salary_text)
        print("请输入大于 0 的整数。")

    print("连续输入无效，本次使用最低月薪 0。")
    return 0


def calculate_skill_score(job_skills, wanted_skills):
    """Return the percentage of wanted skills found in the job."""
    if not wanted_skills:
        return 0

    matched = 0
    for skill in wanted_skills:
        if skill in job_skills:
            matched += 1

    return round(matched / len(wanted_skills) * 100)


def filter_jobs(jobs, city, minimum_salary, remote_only, wanted_skills):
    """Return jobs that satisfy the user's hard constraints."""
    results = []

    # Hard constraints are checked before the softer skill score is calculated.
    for job in jobs:
        if city and job["city"] != city:
            continue
        if job["salary"] < minimum_salary:
            continue
        if remote_only and not job["remote"]:
            continue

        skill_score = calculate_skill_score(job["skills"], wanted_skills)
        results.append({**job, "skill_score": skill_score})

    return sorted(results, key=lambda job: (job["skill_score"], job["salary"]), reverse=True)


def main():
    data_file = Path(__file__).with_name("jobs.json")

    try:
        jobs, skipped = load_jobs(data_file)
    except FileNotFoundError:
        print(f"找不到数据文件：{data_file}")
        return
    except json.JSONDecodeError as error:
        print(f"JSON 格式错误：第 {error.lineno} 行，第 {error.colno} 列")
        return
    except ValueError as error:
        print(f"数据格式错误：{error}")
        return

    print("JobSift 求职岗位筛选器")
    print(f"已读取 {len(jobs)} 条有效岗位，跳过 {skipped} 条异常记录。")

    city = input("目标城市（直接回车表示不限）：").strip()
    minimum_salary = read_minimum_salary()
    remote_only = input("只接受远程岗位吗？(y/n)：").strip().lower() == "y"
    skills_text = input("希望匹配的技能（用逗号分隔）：").strip()
    wanted_skills = [skill.strip() for skill in skills_text.split(",") if skill.strip()]

    results = filter_jobs(jobs, city, minimum_salary, remote_only, wanted_skills)

    print(f"\n找到 {len(results)} 个符合条件的岗位")
    for index, job in enumerate(results, start=1):
        print(
            f"{index}. {job['title']}｜{job['city']}｜{job['salary']} 元"
            f"｜技能匹配 {job['skill_score']}%"
        )


if __name__ == "__main__":
    main()
