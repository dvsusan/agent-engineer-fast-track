"""Compare a vague prompt with a prompt that defines an output contract."""

article = (
    "Agent 每次调用外部工具前，都应该检查用户权限、参数格式和目标对象。"
    "工具超时并不等于动作失败，因为请求可能已经在远端执行。"
)


def build_weak_prompt(text):
    return f"请总结这篇文章：{text}"


def build_strong_prompt(text):
    return f"""
你是阅读卡片生成器。请阅读下面的文章，并返回一个 JSON 对象。

文章：
{text}

必须满足：
1. title：不超过 20 个字。
2. summary：3 句话以内。
3. key_points：2 到 4 条。
4. tags：2 到 5 个短语。
5. review_questions：2 个可以检查理解的问题。
6. 只返回 JSON，不要使用 Markdown 代码块。
7. 不得补充原文没有的事实。
""".strip()


weak_prompt = build_weak_prompt(article)
strong_prompt = build_strong_prompt(article)

print("弱 Prompt 字符数：", len(weak_prompt))
print("强 Prompt 字符数：", len(strong_prompt))
print("\n--- 强 Prompt ---")
print(strong_prompt)
print("\n--- Mock 期望输出 ---")
print(
    '{"title":"工具调用的安全边界",'
    '"summary":"调用工具前要校验权限、参数和目标。超时只说明结果未知，不能直接判定失败。",'
    '"key_points":["执行前做权限与参数校验","超时后先查询真实状态"],'
    '"tags":["Agent","工具调用","超时"],'
    '"review_questions":["为什么超时不等于失败？","工具调用前要检查什么？"]}'
)
