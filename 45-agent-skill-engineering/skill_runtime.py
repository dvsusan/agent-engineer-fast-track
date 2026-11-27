"""Agent Skill 运行时：发现元数据、按需加载指令、执行技能脚本。

本文件模拟一个最小 Agent Harness 的技能加载流程：
1. 扫描 `skills/*/SKILL.md`，只把名称、描述和触发词交给选择器；
2. 选中技能后才加载完整指令正文；
3. 校验入口路径必须位于技能目录内；
4. 用干净环境、超时和捕获输出执行脚本；
5. 把选择依据、加载内容和执行结果写入可检查的 JSON。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_SKILLS_DIR = PROJECT_DIR / "skills"
DEFAULT_REQUEST = (
    "检查这份 2026 年 10 月财务报表：收入是否等于支出加结余，"
    "有没有重复流水、缺失科目或异常大额支出。"
)
DEFAULT_INPUT = DEFAULT_SKILLS_DIR / "financial-report-audit" / "assets" / "sample-report.csv"
DEFAULT_REPORT = PROJECT_DIR / "financial-audit-report.json"
DEFAULT_RUN_RECORD = PROJECT_DIR / "skill-run.json"

SKILL_FILE = "SKILL.md"
SKILL_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
FRONTMATTER_PATTERN = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)

# 技能脚本只获得运行 Python 所需的最小环境变量；API Key 等秘密不会自动继承。
SAFE_ENVIRONMENT_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "SYSTEMROOT",
    "USERPROFILE",
)


class SkillRuntimeError(RuntimeError):
    """技能发现、选择或执行失败的基类。"""


class SkillFormatError(SkillRuntimeError):
    """SKILL.md 或元数据不符合约定。"""


class SkillSelectionError(SkillRuntimeError):
    """没有技能命中，或最高分出现无法裁决的并列。"""


class SkillExecutionError(SkillRuntimeError):
    """入口路径、子进程或输出文件不满足运行时要求。"""


class SkillMetadata(BaseModel):
    """SKILL.md 前置元数据的数据契约。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=SKILL_NAME_PATTERN)
    description: str = Field(min_length=20, max_length=300)
    license: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def version(self) -> str:
        """返回技能版本；缺失时不能假装它是稳定版本。"""
        value = self.metadata.get("version")
        if not isinstance(value, str) or not value.strip():
            raise SkillFormatError(f"技能 {self.name} 缺少 metadata.version")
        return value.strip()

    @property
    def triggers(self) -> list[str]:
        """返回用于本地透明选择器的触发词列表。"""
        value = self.metadata.get("triggers", [])
        if not isinstance(value, list) or not value:
            raise SkillFormatError(f"技能 {self.name} 的 metadata.triggers 必须是非空数组")
        triggers = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if len(triggers) != len(value):
            raise SkillFormatError(f"技能 {self.name} 的触发词必须全部是非空字符串")
        return triggers

    @property
    def entrypoint(self) -> str | None:
        """返回技能目录内的相对脚本路径；纯指令技能可以没有入口。"""
        value = self.metadata.get("entrypoint")
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise SkillFormatError(f"技能 {self.name} 的 metadata.entrypoint 必须是非空字符串")
        return value.strip()

    @property
    def timeout_seconds(self) -> float:
        """返回脚本超时；非法值在启动子进程前失败。"""
        value = self.metadata.get("timeout_seconds", 10)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise SkillFormatError(f"技能 {self.name} 的 timeout_seconds 必须大于 0")
        return float(value)


@dataclass(frozen=True)
class SkillDocument:
    """发现阶段保留的技能元数据；正文不进入上下文和运行记录。"""

    root: Path
    instruction_path: Path
    metadata: SkillMetadata
    instruction_chars: int
    file_sha256: str

    def public_view(self) -> dict[str, Any]:
        """返回可写入候选清单的脱敏元数据。"""
        return {
            "name": self.metadata.name,
            "description": self.metadata.description,
            "version": self.metadata.version,
            "triggers": self.metadata.triggers,
            "body_exposed": False,
            "instruction_chars_available": self.instruction_chars,
        }


@dataclass(frozen=True)
class LoadedInstruction:
    """选中技能后才加载的完整指令正文。"""

    name: str
    body: str
    body_chars: int
    body_sha256: str

    def public_view(self) -> dict[str, Any]:
        """只在记录中保留长度和摘要，避免把完整正文重复写进日志。"""
        return {
            "name": self.name,
            "body_chars": self.body_chars,
            "body_sha256": self.body_sha256,
        }


@dataclass(frozen=True)
class SkillSelection:
    """一次技能选择的依据，便于解释为什么没有选另一个技能。"""

    skill: SkillDocument
    score: int
    matched_triggers: list[str]
    all_scores: dict[str, int]


def utc_now() -> str:
    """返回带时区的 ISO 8601 时间戳，方便跨机器排序。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再替换，避免进程退出留下半截运行记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


def parse_skill_file(path: Path) -> tuple[SkillMetadata, str, str]:
    """解析 YAML 前置元数据和 Markdown 正文，并返回完整文件摘要。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise SkillFormatError(f"技能文件不存在：{path}") from error

    match = FRONTMATTER_PATTERN.match(text)
    if not match:
        raise SkillFormatError(f"{path} 缺少以 --- 包围的 YAML 前置元数据")

    try:
        raw_metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as error:
        raise SkillFormatError(f"{path} 的 YAML 不合法：{error}") from error
    if not isinstance(raw_metadata, dict):
        raise SkillFormatError(f"{path} 的 YAML 前置元数据必须是对象")

    try:
        metadata = SkillMetadata.model_validate(raw_metadata)
    except ValidationError as error:
        raise SkillFormatError(f"{path} 的技能元数据不合法：{error}") from error

    body = match.group(2).strip()
    if not body:
        raise SkillFormatError(f"技能 {metadata.name} 的正文为空")

    # 先访问这些属性，让缺少版本或触发词的技能在发现阶段就失败。
    _ = metadata.version
    _ = metadata.triggers
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return metadata, body, digest


class SkillCatalog:
    """扫描技能目录并提供透明、可解释的选择器。"""

    def __init__(self, root: Path, skills: dict[str, SkillDocument]):
        self.root = root
        self.skills = skills

    @classmethod
    def discover(cls, root: Path) -> SkillCatalog:
        """只读取每个 SKILL.md 的元数据，正文在选中前不暴露给执行上下文。"""
        if not root.is_dir():
            raise SkillFormatError(f"技能目录不存在：{root}")

        documents: dict[str, SkillDocument] = {}
        for instruction_path in sorted(root.glob(f"*/{SKILL_FILE}")):
            metadata, body, digest = parse_skill_file(instruction_path)
            if metadata.name in documents:
                raise SkillFormatError(f"技能名称重复：{metadata.name}")
            documents[metadata.name] = SkillDocument(
                root=instruction_path.parent.resolve(),
                instruction_path=instruction_path.resolve(),
                metadata=metadata,
                instruction_chars=len(body),
                file_sha256=digest,
            )

        if not documents:
            raise SkillFormatError(f"{root} 下没有找到任何 {SKILL_FILE}")
        return cls(root.resolve(), documents)

    def public_catalog(self) -> list[dict[str, Any]]:
        """返回只包含发现元数据的候选技能列表。"""
        return [self.skills[name].public_view() for name in sorted(self.skills)]

    def select(self, request: str) -> SkillSelection:
        """用触发词做确定性选择；生产环境可以替换为模型语义路由。"""
        normalized = request.strip().lower()
        if not normalized:
            raise SkillSelectionError("用户请求为空，无法选择技能")

        scores: dict[str, int] = {}
        matches: dict[str, list[str]] = {}
        for name, document in sorted(self.skills.items()):
            matched = [
                trigger
                for trigger in document.metadata.triggers
                if trigger.lower() in normalized
            ]
            matches[name] = matched
            scores[name] = len(matched)

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        top_score = ranked[0][1]
        if top_score == 0:
            raise SkillSelectionError("没有技能触发词命中用户请求")
        top_names = [name for name, score in ranked if score == top_score]
        if len(top_names) > 1:
            raise SkillSelectionError(f"技能选择并列：{', '.join(top_names)}")

        selected_name = top_names[0]
        return SkillSelection(
            skill=self.skills[selected_name],
            score=top_score,
            matched_triggers=matches[selected_name],
            all_scores=scores,
        )


def load_instruction(document: SkillDocument) -> LoadedInstruction:
    """在选中后重新读取正文，并记录长度和摘要供审计。"""
    _, body, _ = parse_skill_file(document.instruction_path)
    return LoadedInstruction(
        name=document.metadata.name,
        body=body,
        body_chars=len(body),
        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )


def build_safe_environment() -> dict[str, str]:
    """构造最小环境变量集合，阻止 API Key 等秘密自动传给技能脚本。"""
    environment = {
        key: os.environ[key]
        for key in SAFE_ENVIRONMENT_KEYS
        if key in os.environ
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


class SkillExecutor:
    """校验并执行技能入口，捕获退出码、输出和生成的报告。"""

    def execute(self, document: SkillDocument, input_path: Path, output_path: Path) -> dict[str, Any]:
        """执行一个已选技能；业务失败由报告表达，运行失败由检查项表达。"""
        root = document.root.resolve()
        entrypoint_value = document.metadata.entrypoint
        if entrypoint_value is None:
            raise SkillExecutionError(f"技能 {document.metadata.name} 没有可执行入口")
        entrypoint = (root / entrypoint_value).resolve()

        # 防止 SKILL.md 用 ../ 或绝对路径把执行器引到技能目录之外。
        try:
            entrypoint.relative_to(root)
        except ValueError as error:
            raise SkillExecutionError(f"入口不在技能目录内：{entrypoint_value}") from error
        if not entrypoint.is_file():
            raise SkillExecutionError(f"技能入口不存在：{entrypoint_value}")
        if entrypoint.suffix != ".py":
            raise SkillExecutionError("本课运行时只允许 Python 脚本入口")

        resolved_input = input_path.resolve()
        resolved_output = output_path.resolve()
        command = [
            sys.executable,
            str(entrypoint),
            "--input",
            str(resolved_input),
            "--output",
            str(resolved_output),
        ]
        environment = build_safe_environment()
        started = time.perf_counter()
        timed_out = False

        try:
            process = subprocess.run(
                command,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=document.metadata.timeout_seconds,
                check=False,
            )
            exit_code = process.returncode
            stdout = process.stdout.strip()
            stderr = process.stderr.strip()
        except subprocess.TimeoutExpired as error:
            # 超时后不继续解析可能只写了一半的输出文件。
            timed_out = True
            exit_code = 124
            stdout = (error.stdout or "").strip() if isinstance(error.stdout, str) else ""
            stderr = f"技能执行超过 {document.metadata.timeout_seconds} 秒"

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        report: dict[str, Any] | None = None
        report_error: str | None = None
        if resolved_output.is_file():
            try:
                report = json.loads(resolved_output.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                report_error = f"输出不是合法 JSON：{error}"

        return {
            "entrypoint": entrypoint_value,
            "entrypoint_inside_root": True,
            "command": command,
            "cwd": str(root),
            "environment_keys": sorted(environment),
            "timeout_seconds": document.metadata.timeout_seconds,
            "timed_out": timed_out,
            "elapsed_ms": elapsed_ms,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "report_path": str(resolved_output),
            "report_error": report_error,
            "report": report,
        }


def build_run_record(
    request: str,
    catalog: SkillCatalog,
    selection: SkillSelection,
    instruction: LoadedInstruction,
    execution: dict[str, Any],
    expected_status: str | None,
) -> dict[str, Any]:
    """把发现、选择、加载和执行证据整理成一份可检查记录。"""
    report = execution["report"]
    business_status = report.get("status") if isinstance(report, dict) else None
    version_matches = (
        isinstance(report, dict)
        and report.get("skill_version") == selection.skill.metadata.version
    )
    status_matches = expected_status is None or business_status == expected_status
    runtime_checks = {
        "entrypoint_inside_root": execution["entrypoint_inside_root"],
        "only_selected_body_loaded": instruction.name == selection.skill.metadata.name,
        "process_completed": not execution["timed_out"],
        "exit_code_zero": execution["exit_code"] == 0,
        "report_readable": isinstance(report, dict) and execution["report_error"] is None,
        "version_matches": version_matches,
    }
    runtime_checks["all"] = all(runtime_checks.values())
    if expected_status is not None:
        runtime_checks["business_status_matches"] = status_matches
        runtime_checks["all"] = runtime_checks["all"] and status_matches

    return {
        "run_id": f"skill-run-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}",
        "generated_at": utc_now(),
        "request": request,
        "catalog": catalog.public_catalog(),
        "selection": {
            "selected": selection.skill.metadata.name,
            "score": selection.score,
            "matched_triggers": selection.matched_triggers,
            "all_scores": selection.all_scores,
            "body_exposed_during_discovery": False,
        },
        "loaded_instructions": [instruction.public_view()],
        "execution": {
            key: execution[key]
            for key in (
                "entrypoint",
                "entrypoint_inside_root",
                "command",
                "cwd",
                "environment_keys",
                "timeout_seconds",
                "timed_out",
                "elapsed_ms",
                "exit_code",
                "stdout",
                "stderr",
                "report_path",
                "report_error",
            )
        },
        "business_status": business_status,
        "expected_status": expected_status,
        "resources_used": report.get("resources_used", []) if isinstance(report, dict) else [],
        "findings": report.get("findings", []) if isinstance(report, dict) else [],
        "checks": report.get("checks", {}) if isinstance(report, dict) else {},
        "runtime_checks": runtime_checks,
    }


def resolve_path_from_project(value: Path) -> Path:
    """教程命令允许使用相对路径，但内部统一解析为绝对路径。"""
    return value if value.is_absolute() else (PROJECT_DIR / value).resolve()


def parse_args() -> argparse.Namespace:
    """定义技能运行时的命令行接口。"""
    parser = argparse.ArgumentParser(description="Agent Skill 发现与执行运行时")
    parser.add_argument("--skills-dir", type=Path, default=DEFAULT_SKILLS_DIR)
    parser.add_argument("--request", default=DEFAULT_REQUEST)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--run-record", type=Path, default=DEFAULT_RUN_RECORD)
    parser.add_argument("--expect-status", choices=("passed", "failed"))
    parser.add_argument("--list-skills", action="store_true")
    return parser.parse_args()


def main() -> int:
    """执行发现、选择、加载和运行；任何证据不足都返回非零状态。"""
    args = parse_args()
    try:
        catalog = SkillCatalog.discover(resolve_path_from_project(args.skills_dir))
        if args.list_skills:
            print(json.dumps(catalog.public_catalog(), ensure_ascii=False, indent=2))
            return 0

        selection = catalog.select(args.request)
        instruction = load_instruction(selection.skill)
        executor = SkillExecutor()
        execution = executor.execute(
            selection.skill,
            resolve_path_from_project(args.input),
            resolve_path_from_project(args.output),
        )
        record = build_run_record(
            args.request,
            catalog,
            selection,
            instruction,
            execution,
            args.expect_status,
        )
        atomic_write_json(resolve_path_from_project(args.run_record), record)
    except SkillRuntimeError as error:
        print(f"技能运行失败：{type(error).__name__}: {error}")
        return 2

    print(f"候选技能：{selection.all_scores}")
    print(
        f"已选择：{selection.skill.metadata.name} "
        f"score={selection.score} triggers={selection.matched_triggers}"
    )
    print(f"已加载指令：{instruction.body_chars} 字符，sha256={instruction.body_sha256[:12]}...")
    print(
        f"脚本退出码：{execution['exit_code']} 用时：{execution['elapsed_ms']}ms "
        f"业务状态：{record['business_status']}"
    )
    print(f"发现的问题：{len(record['findings'])} 个")
    print(f"运行检查：{record['runtime_checks']}")
    print(f"运行记录：{resolve_path_from_project(args.run_record)}")

    if not record["runtime_checks"]["all"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
