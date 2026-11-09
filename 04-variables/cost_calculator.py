"""Estimate the cost of one model call with variables."""

prompt_tokens = 1200
completion_tokens = 300
# Prices are quoted per one million tokens.
input_price_per_million = 2.0
output_price_per_million = 8.0

input_cost = prompt_tokens / 1_000_000 * input_price_per_million
output_cost = completion_tokens / 1_000_000 * output_price_per_million
total_cost = input_cost + output_cost

print("prompt_tokens 类型:", type(prompt_tokens).__name__)
print("input_price_per_million 类型:", type(input_price_per_million).__name__)
print(f"输入费用: ¥{input_cost:.4f}")
print(f"输出费用: ¥{output_cost:.4f}")
print(f"总计: ¥{total_cost:.4f}")
