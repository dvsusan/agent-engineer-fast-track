"""通过统一的 OpenAI 兼容网关调用 DeepSeek 与通义千问。

网关负责三条边界：
1. 配置：环境变量和密钥不进入业务代码；
2. 传输：所有 provider 都通过 POST /chat/completions 调用；
3. 策略：只有可恢复错误才允许触发故障切换。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


# 以文件所在目录为基准，保证从任意目录启动时都能读取同一份 .env 和报告路径。
PROJECT_DIR = Path(__file__).resolve().parent
REPORT_FILE = PROJECT_DIR / "gateway-report.json"
DEFAULT_PROMPT = "杭州下雨天想拍照，不想走太远，应该怎么安排？"
DEFAULT_MAX_TOKENS = 512

# .env 只用于本地开发；已经存在的环境变量优先，方便 CI 和容器覆盖配置。
load_dotenv(PROJECT_DIR / ".env")


# 异常层次把“配置错误”“单家失败”“请求被拒绝”“全部失败”分开，
# 这样 CLI、Web 层或未来的重试器可以按类型处理，而不是解析错误字符串。
class GatewayError(RuntimeError):
    """所有网关异常的基类，便于上层按类型统一处理。"""


class GatewayConfigurationError(GatewayError):
    """本地配置缺失或格式错误；调用方应修正 .env 后重试。"""


class ProviderRequestError(GatewayError):
    """单个 provider 的可分类失败；fallback_allowed 决定能否继续尝试。"""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        status_code: int | None = None,
        fallback_allowed: bool = True,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.fallback_allowed = fallback_allowed


class GatewayRejected(GatewayError):
    """请求本身无效，换一家 provider 也不能解决。"""

    def __init__(self, message: str, attempts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.attempts = attempts


class GatewayExhausted(GatewayError):
    """所有允许尝试的 provider 都失败了。"""

    def __init__(self, message: str, attempts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.attempts = attempts


@dataclass(frozen=True)
class ProviderConfig:
    """一家真实模型服务的不可变配置；密钥和请求扩展不得进入 repr 和报告。"""

    name: str
    base_url: str
    model: str
    # repr=False 是安全边界：调试时打印对象不能泄露密钥。
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 20.0
    # 供应商特有参数单独保存，避免业务层写 if provider == "deepseek"；
    # repr=False 防止调试输出把可能敏感的上游参数打印出来。
    extra_payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def public_view(self) -> dict[str, Any]:
        """返回可安全写入终端、日志和 JSON 报告的脱敏视图。"""
        return {
            "name": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "api_key_configured": bool(self.api_key),
        }


@dataclass
class ChatResult:
    """网关成功调用后的统一结果。"""

    provider: str
    model: str
    content: str
    usage: dict[str, Any]
    finish_reason: str
    elapsed_ms: float
    # 只有第二个及后续 provider 成功时才为 True；用于区分“最终答案来自备用模型”。
    fallback_used: bool
    # 成功和失败尝试都保留，便于还原调用顺序和定位故障。
    attempts: list[dict[str, Any]]


def env_float(name: str, default: float) -> float:
    """读取正数超时配置；空值用默认值，非法值立即失败。"""
    raw = os.getenv(name, "").strip()
    # 空值视为“使用默认值”，写错的值则立即失败，避免静默关闭超时保护。
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise GatewayConfigurationError(f"{name} 必须是数字") from error
    if value <= 0:
        raise GatewayConfigurationError(f"{name} 必须大于 0")
    return value


def validate_base_url(provider: str, base_url: str) -> str:
    """校验 Base URL，阻止缺失协议或占位符进入网络调用。"""
    # 统一去掉尾部斜杠，防止后面拼出 //chat/completions。
    clean_url = base_url.strip().rstrip("/")
    if not clean_url.startswith(("https://", "http://")):
        raise GatewayConfigurationError(f"{provider} 的 Base URL 必须以 https:// 或 http:// 开头")
    # 官方文档中的 {WorkspaceId} 只是占位符；带它发请求必然失败，
    # 因此必须在消耗一次网络调用之前给出可操作的配置错误。
    if "{" in clean_url or "}" in clean_url or "workspaceid" in clean_url.lower():
        raise GatewayConfigurationError(
            f"{provider} 的 Base URL 还包含 WorkspaceId 占位符，请换成控制台里的真实业务空间 ID"
        )
    return clean_url


def read_provider(
    *,
    name: str,
    key_names: tuple[str, ...],
    base_url_name: str,
    default_base_url: str,
    model_name: str,
    default_model: str,
    timeout_name: str,
    extra_payload: dict[str, Any] | None = None,
) -> ProviderConfig | None:
    """按密钥是否存在组装 provider；缺少密钥时返回 None。"""
    # 多个变量名按顺序取第一个非空值，兼容百炼官方的 DASHSCOPE_API_KEY。
    api_key = next(
        (value for key_name in key_names if (value := os.getenv(key_name, "").strip())),
        "",
    )
    if not api_key:
        return None
    base_url = validate_base_url(
        name,
        os.getenv(base_url_name, default_base_url),
    )
    model = os.getenv(model_name, default_model).strip()
    if not model:
        raise GatewayConfigurationError(f"{model_name} 不能为空")
    return ProviderConfig(
        name=name,
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_seconds=env_float(timeout_name, 20.0),
        extra_payload=extra_payload or {},
    )


def classify_status(status_code: int) -> tuple[str, bool]:
    """把 HTTP 状态码映射为错误分类和是否允许故障切换。"""
    # 400/413/422 属于同一个请求体对任何供应商都不合法，不应切换；
    # 其余分支代表某一家账号、容量、网络或服务端可能在局部出问题。
    if status_code in {400, 413, 422}:
        return "invalid_request", False
    if status_code in {401, 403}:
        return "auth", True
    if status_code == 402:
        return "balance", True
    if status_code == 408:
        return "timeout", True
    if status_code == 429:
        return "rate_limit", True
    if status_code >= 500:
        return "server", True
    return "http_error", True


class ModelGateway:
    """按顺序调用真实 provider，并保存脱敏的尝试轨迹。"""

    def __init__(self, providers: list[ProviderConfig]) -> None:
        if not providers:
            raise GatewayConfigurationError("没有可用模型提供方")
        self.providers = providers

    @classmethod
    def from_environment(
        cls, selected_providers: list[str] | None = None
    ) -> "ModelGateway":
        """从环境变量加载主备 provider；列表顺序就是故障切换顺序。"""
        # DeepSeek 是主模型，Qwen 是备用模型；顺序就是故障切换顺序。
        deepseek = read_provider(
            name="deepseek",
            key_names=("DEEPSEEK_API_KEY",),
            base_url_name="DEEPSEEK_BASE_URL",
            default_base_url="https://api.deepseek.com",
            model_name="DEEPSEEK_MODEL",
            default_model="deepseek-flash",
            timeout_name="DEEPSEEK_TIMEOUT_SECONDS",
            # DeepSeek 当前默认开启 thinking，推理 token 会先消耗 max_tokens；
            # 本课的旅行问答只需要最终正文，因此显式关闭思考模式。
            extra_payload={"thinking": {"type": "disabled"}},
        )
        # 百炼官方示例使用 DASHSCOPE_API_KEY；这里同时接受更直观的 QWEN_API_KEY。
        qwen = read_provider(
            name="qwen",
            key_names=("QWEN_API_KEY", "DASHSCOPE_API_KEY"),
            base_url_name="QWEN_BASE_URL",
            default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model_name="QWEN_MODEL",
            default_model="qwen3.7-plus",
            timeout_name="QWEN_TIMEOUT_SECONDS",
            # Qwen 当前同样默认开启 thinking；基础旅行问答关闭后延迟和费用更稳定。
            extra_payload={"enable_thinking": False},
        )
        available = {"deepseek": deepseek, "qwen": qwen}
        # 不指定时完整加载主备两家；CLI 的 --provider 可以只加载其中一家做独立验收。
        if not selected_providers:
            selected_providers = ["deepseek", "qwen"]

        providers: list[ProviderConfig] = []
        for name in selected_providers:
            provider = available.get(name)
            if provider is None:
                key_name = "DEEPSEEK_API_KEY" if name == "deepseek" else "QWEN_API_KEY（或 DASHSCOPE_API_KEY）"
                raise GatewayConfigurationError(
                    f"选择了 {name}，但没有配置 {key_name}。请先在 .env 中填写真实密钥"
                )
            providers.append(provider)
        return cls(providers)

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> ChatResult:
        """执行主备调用；失败时抛出携带 attempts 的网关异常。"""
        # 在发出网络请求前做最小输入校验，避免把明显无效的消息发给所有供应商。
        if not messages:
            raise GatewayRejected("messages 不能为空", [])
        if not any(message.get("role") == "user" for message in messages):
            raise GatewayRejected("messages 至少需要一条 user 消息", [])

        attempts: list[dict[str, Any]] = []
        # enumerate 的下标同时用于判断是否真的发生了 fallback。
        for index, provider in enumerate(self.providers):
            started = time.perf_counter()
            try:
                content, usage, finish_reason = self._call_openai_compatible(
                    provider,
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except ProviderRequestError as error:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                attempts.append(
                    {
                        "provider": provider.name,
                        "model": provider.model,
                        "outcome": "failure",
                        "category": error.category,
                        "status_code": error.status_code,
                        "fallback_allowed": error.fallback_allowed,
                        "elapsed_ms": elapsed_ms,
                        "message": str(error)[:300],
                    }
                )
                if not error.fallback_allowed:
                    # 请求本身有问题时立即停止，不能靠第二家模型掩盖客户端 bug。
                    raise GatewayRejected(
                        f"{provider.name} 拒绝了请求：{error}", attempts
                    ) from error
                continue

            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            # 成功和失败都进入 attempts，报告才能回答“最终用了谁、前面试过谁”。
            attempts.append(
                {
                    "provider": provider.name,
                    "model": provider.model,
                    "outcome": "success",
                    "category": "ok",
                    "status_code": 200,
                    "fallback_allowed": False,
                    "elapsed_ms": elapsed_ms,
                    "message": "调用成功",
                }
            )
            return ChatResult(
                provider=provider.name,
                model=provider.model,
                content=content,
                usage=usage,
                finish_reason=finish_reason,
                elapsed_ms=elapsed_ms,
                fallback_used=index > 0,
                attempts=attempts,
            )

        raise GatewayExhausted("所有模型提供方均失败", attempts)

    def _call_openai_compatible(
        self,
        provider: ProviderConfig,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, dict[str, Any], str]:
        """调用一次 /chat/completions，返回正文、usage 和结束原因。"""
        # Base URL 只保存到 /v1 或域名，具体资源路径由网关统一拼接。
        url = f"{provider.base_url}/chat/completions"
        # OpenAI 兼容协议使用 Bearer 鉴权；密钥只放在请求头，不放在 URL 或日志中。
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "model": provider.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        # 供应商特有参数由 ProviderConfig 注入，例如 DeepSeek 的 thinking 开关；
        # 这样通用 HTTP 层不需要知道哪家厂商需要什么额外字段。
        payload.update(provider.extra_payload)
        try:
            # 超时同时约束连接和读取，防止某一家卡住时占满业务线程。
            with httpx.Client(timeout=provider.timeout_seconds) as client:
                response = client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as error:
            raise ProviderRequestError(
                "timeout", f"请求 {provider.name} 超时", fallback_allowed=True
            ) from error
        except httpx.RequestError as error:
            raise ProviderRequestError(
                "network", f"连接 {provider.name} 失败：{error}", fallback_allowed=True
            ) from error

        if response.status_code >= 400:
            category, fallback_allowed = classify_status(response.status_code)
            # 错误正文只保留一行和有限长度，并主动移除密钥，避免污染日志。
            detail = response.text.strip().replace("\n", " ")[:300]
            if provider.api_key:
                detail = detail.replace(provider.api_key, "[REDACTED]")
            raise ProviderRequestError(
                category,
                f"HTTP {response.status_code}: {detail}",
                status_code=response.status_code,
                fallback_allowed=fallback_allowed,
            )

        try:
            # 严格按 OpenAI 兼容响应结构取正文；结构不符时交给备用模型或明确失败。
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
            reasoning_content = message.get("reasoning_content") or ""
            usage = body.get("usage") or {}
            finish_reason = str(choice.get("finish_reason") or "unknown")
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise ProviderRequestError(
                "invalid_response",
                f"{provider.name} 返回了无法解析的响应",
                fallback_allowed=True,
            ) from error
        if not isinstance(content, str) or not content.strip():
            # 空正文通常不是网络问题：DeepSeek 开启 thinking 且 max_tokens 太小时，
            # 推理内容可能耗尽额度，此时 finish_reason=length、content 为空。
            reasoning_chars = (
                len(reasoning_content) if isinstance(reasoning_content, str) else 0
            )
            raise ProviderRequestError(
                "invalid_response",
                f"{provider.name} 返回了空内容"
                f"（finish_reason={finish_reason}，reasoning_content={reasoning_chars} 字符）",
                fallback_allowed=True,
            )
        return content.strip(), usage if isinstance(usage, dict) else {}, finish_reason


def write_report(
    path: Path,
    *,
    status: str,
    prompt: str,
    providers: list[ProviderConfig],
    result: ChatResult | None = None,
    error: GatewayError | None = None,
) -> None:
    """把成功或失败写入 JSON；报告不得包含 API Key。"""
    # 失败对象可能来自 GatewayRejected 或 GatewayExhausted，两者都携带 attempts。
    attempts = result.attempts if result else getattr(error, "attempts", [])
    report = {
        "status": status,
        "prompt": prompt,
        "providers": [provider.public_view() for provider in providers],
        "fallback_used": result.fallback_used if result else None,
        "selected_provider": result.provider if result else None,
        "selected_model": result.model if result else None,
        "answer": result.content if result else None,
        "usage": result.usage if result else None,
        "finish_reason": result.finish_reason if result else None,
        "elapsed_ms": result.elapsed_ms if result else None,
        "attempts": attempts,
        "error": str(error) if error else None,
    }
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    """解析命令行并执行一次真实调用；配置错误返回 2，调用失败返回 1。"""
    parser = argparse.ArgumentParser(description="Call real DeepSeek and Qwen models")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="要发送给模型的用户问题")
    parser.add_argument("--output", type=Path, default=REPORT_FILE, help="调用报告路径")
    parser.add_argument(
        "--provider",
        choices=("all", "deepseek", "qwen"),
        default="all",
        help="all 表示主备顺序调用，也可以只测试某一家",
    )
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    args = parser.parse_args()

    selected = [] if args.provider == "all" else [args.provider]
    # 配置错误使用退出码 2，和一次真实调用失败（退出码 1）区分开。
    try:
        gateway = ModelGateway.from_environment(selected)
    except GatewayConfigurationError as error:
        print(f"配置错误：{error}")
        print(f"请检查 {PROJECT_DIR / '.env'}，配置模板见 .env.example")
        raise SystemExit(2) from error

    # system 约束回答风格，user 放本次问题；两者都由同一个 HTTP 契约发送。
    messages = [
        {
            "role": "system",
            "content": "你是 TripPilot 旅行助手。回答要简洁，并说明资料可能过期。",
        },
        {"role": "user", "content": args.prompt},
    ]
    print("模型提供方：", ", ".join(provider.name for provider in gateway.providers))
    try:
        result = gateway.chat(
            messages,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
        )
    except GatewayError as error:
        # 运行期失败也写报告，避免用户只看到终端文字而无法复盘调用链。
        write_report(
            args.output,
            status="failed",
            prompt=args.prompt,
            providers=gateway.providers,
            error=error,
        )
        print(f"网关调用失败：{type(error).__name__}: {error}")
        for attempt in getattr(error, "attempts", []):
            print(
                f"- {attempt['provider']} {attempt['outcome']} "
                f"{attempt['category']} {attempt['message']}"
            )
        print(f"已保存：{args.output.name}")
        raise SystemExit(1) from error

    write_report(
        args.output,
        status="ok",
        prompt=args.prompt,
        providers=gateway.providers,
        result=result,
    )
    print(f"selected_provider: {result.provider}")
    print(f"model: {result.model}")
    print(f"fallback_used: {result.fallback_used}")
    print(f"finish_reason: {result.finish_reason}")
    print("attempts:")
    for attempt in result.attempts:
        print(
            f"- {attempt['provider']} {attempt['outcome']} "
            f"{attempt['category']} {attempt['elapsed_ms']}ms"
        )
    print("\nanswer:")
    print(result.content)
    print(f"\n已保存：{args.output.name}")


if __name__ == "__main__":
    main()
