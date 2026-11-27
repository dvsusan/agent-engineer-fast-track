"""Agent Skill 治理运行时：版本清单、ACL、回归测试和候选版本回滚。

本文件把第 45 课“能发现和执行技能”推进到“团队敢复用技能”：
1. 从不可变发布目录生成带内容摘要的技能清单；
2. 在读取正文和执行脚本之前检查访问控制列表（ACL）；
3. 用同一套回归用例分别验证正式版本和候选版本；
4. 候选版本失败时阻止发布，并写出可追溯的回滚记录。

本课不调用大模型。模型可以解释测试结果，但版本比较、权限判断和发布门禁
必须由确定性程序完成，否则治理规则会被提示词绕过。
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
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


PROJECT_DIR = Path(__file__).resolve().parent
SKILLS_DIR = PROJECT_DIR / "skills"
POLICY_PATH = PROJECT_DIR / "governance-policy.json"
MANIFEST_PATH = PROJECT_DIR / "skill-manifest.json"
TEST_REPORT_PATH = PROJECT_DIR / "test-report.json"
GOVERNANCE_REPORT_PATH = PROJECT_DIR / "governance-report.json"
ROLLBACK_RECORD_PATH = PROJECT_DIR / "rollback-record.json"
TEST_ARTIFACT_DIR = PROJECT_DIR / "test-artifacts"
ACL_ARTIFACT_DIR = PROJECT_DIR / "governance-artifacts" / "acl"

SKILL_FILE = "SKILL.md"
FRONTMATTER_PATTERN = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
VERSION_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+$"
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


class GovernanceError(RuntimeError):
    """策略、发布包、ACL 或治理产物不满足约定。"""


class ACLGrant(BaseModel):
    """一条 ACL 白名单：某个主体可以执行哪个技能版本。"""

    model_config = ConfigDict(extra="forbid")

    principal: str = Field(min_length=1, max_length=64)
    skill: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=VERSION_PATTERN)
    actions: list[Literal["execute"]] = Field(min_length=1)


class SkillTestCase(BaseModel):
    """一个回归用例的输入、预期进程结果和预期业务发现。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=64)
    role: Literal["active", "candidate"]
    input_path: str = Field(min_length=1)
    expected_process: Literal["success", "failure"]
    expected_exit_code: int
    expected_report: bool
    expected_status: Literal["passed", "failed"] | None = None
    expected_finding_codes: list[str] = Field(default_factory=list)


class GovernancePolicy(BaseModel):
    """发布治理策略：正式版本、候选版本、服务账号、ACL 和回归测试集。"""

    model_config = ConfigDict(extra="forbid")

    skill_name: str = Field(min_length=1, max_length=64)
    owner: str = Field(min_length=1, max_length=64)
    active_version: str = Field(pattern=VERSION_PATTERN)
    candidate_version: str = Field(pattern=VERSION_PATTERN)
    release_bot: str = Field(min_length=1, max_length=64)
    grants: list[ACLGrant] = Field(default_factory=list)
    tests: list[SkillTestCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_policy(self) -> GovernancePolicy:
        """在读取发布文件之前拒绝自相矛盾或无法区分的测试配置。"""
        if self.active_version == self.candidate_version:
            raise ValueError("active_version 与 candidate_version 必须不同")

        seen: set[tuple[str, str]] = set()
        for test in self.tests:
            key = (test.role, test.case_id)
            if key in seen:
                raise ValueError(f"测试用例重复：{key}")
            seen.add(key)

        roles = {test.role for test in self.tests}
        if roles != {"active", "candidate"}:
            raise ValueError("测试集必须同时覆盖 active 和 candidate 两个发布角色")
        return self


class SkillFrontmatter(BaseModel):
    """SKILL.md 前置元数据；只校验治理需要的稳定字段。"""

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=20, max_length=500)
    license: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def version(self) -> str:
        """返回发布版本；缺失时禁止进入清单。"""
        value = self.metadata.get("version")
        if not isinstance(value, str) or not re.fullmatch(VERSION_PATTERN, value):
            raise GovernanceError("SKILL.md 的 metadata.version 必须是 x.y.z 格式")
        return value

    @property
    def owner(self) -> str:
        """返回技能负责人；负责人必须与治理策略一致。"""
        value = self.metadata.get("owner")
        if not isinstance(value, str) or not value.strip():
            raise GovernanceError("SKILL.md 缺少 metadata.owner")
        return value.strip()

    @property
    def entrypoint(self) -> str:
        """返回发布目录内的唯一脚本入口。"""
        value = self.metadata.get("entrypoint")
        if not isinstance(value, str) or not value.strip():
            raise GovernanceError("SKILL.md 缺少 metadata.entrypoint")
        return value.strip()

    @property
    def rules_path(self) -> str:
        """返回发布目录内的规则文件路径。"""
        value = self.metadata.get("rules_path")
        if not isinstance(value, str) or not value.strip():
            raise GovernanceError("SKILL.md 缺少 metadata.rules_path")
        return value.strip()

    @property
    def timeout_seconds(self) -> float:
        """返回脚本超时；非法值会让版本在清单阶段失败。"""
        value = self.metadata.get("timeout_seconds", 10)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise GovernanceError("metadata.timeout_seconds 必须大于 0")
        return float(value)


@dataclass(frozen=True)
class ReleaseBundle:
    """一个不可变技能发布版本及其内容指纹。"""

    skill_name: str
    version: str
    role: Literal["active", "candidate"]
    root: Path
    skill_doc_path: Path
    entrypoint_path: Path
    rules_path: Path
    timeout_seconds: float
    file_hashes: dict[str, str]
    content_sha256: str

    def public_view(self, principals: list[str]) -> dict[str, Any]:
        """返回可写入清单的发布证据，不包含正文和规则内容。"""
        return {
            "role": self.role,
            "root": display_path(self.root),
            "skill_doc": {
                "path": display_path(self.skill_doc_path),
                "sha256": self.file_hashes["skill_doc"],
            },
            "entrypoint": {
                "path": display_path(self.entrypoint_path),
                "sha256": self.file_hashes["entrypoint"],
            },
            "rules": {
                "path": display_path(self.rules_path),
                "sha256": self.file_hashes["rules"],
            },
            "timeout_seconds": self.timeout_seconds,
            "principals": sorted(principals),
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class AccessDecision:
    """一次 ACL 判断的完整依据，便于解释为什么允许或拒绝。"""

    principal: str
    skill: str
    version: str
    action: str
    allowed: bool
    reason: str
    matched_grant: dict[str, Any] | None

    def public_view(self) -> dict[str, Any]:
        """返回可审计的授权结论，不包含用户凭据或密钥。"""
        return {
            "principal": self.principal,
            "skill": self.skill,
            "version": self.version,
            "action": self.action,
            "allowed": self.allowed,
            "reason": self.reason,
            "matched_grant": self.matched_grant,
        }


def utc_now() -> str:
    """返回带时区的 ISO 8601 时间戳，保证不同机器上的记录可排序。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def display_path(path: Path) -> str:
    """项目内文件使用相对路径，外部输入保留绝对路径。"""
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_DIR).as_posix()
    except ValueError:
        return str(resolved)


def sha256_path(path: Path) -> str:
    """计算文件内容指纹；任何字节变化都会改变版本摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再替换，避免中途退出留下半截治理记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


def resolve_project_path(value: str | Path) -> Path:
    """把策略中的相对路径统一绑定到项目根目录。"""
    path = Path(value)
    return path.resolve() if path.is_absolute() else (PROJECT_DIR / path).resolve()


def parse_skill_file(path: Path) -> tuple[SkillFrontmatter, int]:
    """解析 SKILL.md 前置元数据，并返回正文长度供清单记录。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise GovernanceError(f"技能说明不存在：{path}") from error

    match = FRONTMATTER_PATTERN.match(text)
    if not match:
        raise GovernanceError(f"{path} 缺少以 --- 包围的 YAML 前置元数据")

    try:
        raw_metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as error:
        raise GovernanceError(f"{path} 的 YAML 不合法：{error}") from error

    try:
        metadata = SkillFrontmatter.model_validate(raw_metadata)
    except ValidationError as error:
        raise GovernanceError(f"{path} 的元数据不合法：{error}") from error
    return metadata, len(match.group(2).strip())


def ensure_inside_root(path: Path, root: Path, label: str) -> Path:
    """拒绝 ../ 或绝对路径把入口、规则引到发布目录之外。"""
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise GovernanceError(f"{label} 必须位于发布目录内：{path}") from error
    return resolved


def load_policy(path: Path = POLICY_PATH) -> GovernancePolicy:
    """读取治理策略，并拒绝指向未知技能或版本的 ACL 授权。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise GovernanceError(f"治理策略不存在：{path}") from error
    except json.JSONDecodeError as error:
        raise GovernanceError(f"治理策略不是合法 JSON：{error}") from error

    try:
        policy = GovernancePolicy.model_validate(payload)
    except ValidationError as error:
        raise GovernanceError(f"治理策略不合法：{error}") from error

    known_versions = {policy.active_version, policy.candidate_version}
    for grant in policy.grants:
        if grant.skill != policy.skill_name:
            raise GovernanceError(
                f"ACL 授权引用了本课未治理的技能：{grant.skill}"
            )
        if grant.version not in known_versions:
            raise GovernanceError(
                f"ACL 授权引用了不存在的发布版本：{grant.version}"
            )
    return policy


def load_release(
    policy: GovernancePolicy,
    version: str,
    role: Literal["active", "candidate"],
) -> ReleaseBundle:
    """加载并校验一个发布版本，生成不可变的内容指纹。"""
    root = (SKILLS_DIR / policy.skill_name / "releases" / version).resolve()
    if not root.is_dir():
        raise GovernanceError(f"发布目录不存在：{root}")

    skill_doc_path = (root / SKILL_FILE).resolve()
    metadata, body_chars = parse_skill_file(skill_doc_path)
    if metadata.name != policy.skill_name:
        raise GovernanceError(
            f"技能名称不一致：策略为 {policy.skill_name}，说明为 {metadata.name}"
        )
    if metadata.version != version:
        raise GovernanceError(
            f"版本不一致：目录为 {version}，SKILL.md 为 {metadata.version}"
        )
    if metadata.owner != policy.owner:
        raise GovernanceError(
            f"负责人不一致：策略为 {policy.owner}，SKILL.md 为 {metadata.owner}"
        )
    if body_chars == 0:
        raise GovernanceError(f"技能 {version} 的正文为空")

    entrypoint_path = ensure_inside_root(
        root / metadata.entrypoint,
        root,
        "metadata.entrypoint",
    )
    rules_path = ensure_inside_root(
        root / metadata.rules_path,
        root,
        "metadata.rules_path",
    )
    if entrypoint_path.suffix != ".py" or not entrypoint_path.is_file():
        raise GovernanceError(f"脚本入口不存在或不是 Python 文件：{entrypoint_path}")
    if not rules_path.is_file():
        raise GovernanceError(f"规则文件不存在：{rules_path}")

    try:
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise GovernanceError(f"规则文件不是合法 JSON：{error}") from error
    required_rule_keys = {
        "currency",
        "period",
        "balance_tolerance",
        "large_expense_threshold",
        "allowed_categories",
        "required_fields",
    }
    missing = sorted(required_rule_keys - set(rules))
    if missing:
        raise GovernanceError(f"规则文件缺少字段：{', '.join(missing)}")

    file_hashes = {
        "skill_doc": sha256_path(skill_doc_path),
        "entrypoint": sha256_path(entrypoint_path),
        "rules": sha256_path(rules_path),
    }
    canonical = "\n".join(
        f"{name}:{file_hashes[name]}"
        for name in ("skill_doc", "entrypoint", "rules")
    )
    content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ReleaseBundle(
        skill_name=metadata.name,
        version=version,
        role=role,
        root=root,
        skill_doc_path=skill_doc_path,
        entrypoint_path=entrypoint_path,
        rules_path=rules_path,
        timeout_seconds=metadata.timeout_seconds,
        file_hashes=file_hashes,
        content_sha256=content_sha256,
    )


def load_releases(policy: GovernancePolicy) -> dict[str, ReleaseBundle]:
    """加载正式版和候选版，键为版本号。"""
    return {
        policy.active_version: load_release(
            policy,
            policy.active_version,
            "active",
        ),
        policy.candidate_version: load_release(
            policy,
            policy.candidate_version,
            "candidate",
        ),
    }


def principals_for(policy: GovernancePolicy, version: str) -> list[str]:
    """列出某个版本当前可以执行的主体，用于清单展示。"""
    return sorted(
        {
            grant.principal
            for grant in policy.grants
            if grant.skill == policy.skill_name
            and grant.version == version
            and "execute" in grant.actions
        }
    )


def build_manifest(
    policy: GovernancePolicy,
    releases: dict[str, ReleaseBundle],
) -> dict[str, Any]:
    """把版本、路径、摘要、负责人和授权主体固化成技能清单。"""
    active = releases[policy.active_version]
    candidate = releases[policy.candidate_version]
    checks = {
        "active_release_present": active.role == "active",
        "candidate_release_present": candidate.role == "candidate",
        "versions_distinct": active.version != candidate.version,
        "release_bot_can_test_active": policy.release_bot
        in principals_for(policy, policy.active_version),
        "release_bot_can_test_candidate": policy.release_bot
        in principals_for(policy, policy.candidate_version),
        "content_digests_present": all(
            len(release.content_sha256) == 64 for release in releases.values()
        ),
    }
    checks["all"] = all(checks.values())
    return {
        "manifest_version": 1,
        "generated_at": utc_now(),
        "skill": policy.skill_name,
        "owner": policy.owner,
        "active_version": policy.active_version,
        "candidate_version": policy.candidate_version,
        "release_bot": policy.release_bot,
        "releases": {
            version: release.public_view(principals_for(policy, version))
            for version, release in sorted(releases.items())
        },
        "checks": checks,
    }


def authorize(
    policy: GovernancePolicy,
    principal: str,
    version: str,
    action: str = "execute",
) -> AccessDecision:
    """按 ACL 做默认拒绝判断；没有精确授权就绝不启动技能脚本。"""
    matched_grant: dict[str, Any] | None = None
    for grant in policy.grants:
        if (
            grant.principal == principal
            and grant.skill == policy.skill_name
            and grant.version == version
            and action in grant.actions
        ):
            matched_grant = grant.model_dump()
            break

    allowed = matched_grant is not None
    if allowed:
        reason = "exact_grant"
    elif not any(grant.principal == principal for grant in policy.grants):
        reason = "default_deny"
    else:
        reason = "no_matching_skill_version_action"

    return AccessDecision(
        principal=principal,
        skill=policy.skill_name,
        version=version,
        action=action,
        allowed=allowed,
        reason=reason,
        matched_grant=matched_grant,
    )


def build_safe_environment() -> dict[str, str]:
    """只向技能脚本传递运行 Python 所需变量，避免密钥随子进程泄漏。"""
    environment = {
        key: os.environ[key]
        for key in SAFE_ENVIRONMENT_KEYS
        if key in os.environ
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def denied_execution(
    decision: AccessDecision,
    output_path: Path,
) -> dict[str, Any]:
    """构造 ACL 拒绝结果；不读取发布正文，也不创建任何技能输出。"""
    if decision.allowed:
        raise GovernanceError("denied_execution 只能用于 ACL 拒绝结果")
    return {
        "allowed": False,
        "access": decision.public_view(),
        "command": None,
        "environment_keys": [],
        "cwd": None,
        "timeout_seconds": None,
        "timed_out": False,
        "elapsed_ms": 0.0,
        "exit_code": None,
        "stdout": "",
        "stderr": "ACL 拒绝：未加载技能发布包，未启动技能脚本",
        "output_path": str(output_path.resolve()),
        "output_created": False,
        "report_error": None,
        "report": None,
        "report_version_matches": False,
    }


def execute_request(
    policy: GovernancePolicy,
    principal: str,
    version: str,
    input_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """在单次用户请求中先查 ACL，只有授权后才加载发布包并执行。"""
    if version not in {policy.active_version, policy.candidate_version}:
        raise GovernanceError(f"未知发布版本：{version}")
    decision = authorize(policy, principal, version)
    if not decision.allowed:
        return denied_execution(decision, output_path)

    role: Literal["active", "candidate"] = (
        "active" if version == policy.active_version else "candidate"
    )
    release = load_release(policy, version, role)
    return execute_release(policy, release, principal, input_path, output_path)


def execute_release(
    policy: GovernancePolicy,
    release: ReleaseBundle,
    principal: str,
    input_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """先检查 ACL，再在受限子进程中执行指定版本，并收集运行证据。"""
    decision = authorize(policy, principal, release.version)
    if not decision.allowed:
        # 拒绝发生在清理旧产物和启动子进程之前，不能留下“执行了一半”的文件。
        return denied_execution(decision, output_path)

    resolved_input = input_path.resolve()
    resolved_output = output_path.resolve()
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    # 每次测试前删除旧输出，否则失败进程可能被上一次成功产物伪装成成功。
    resolved_output.unlink(missing_ok=True)
    command = [
        sys.executable,
        str(release.entrypoint_path),
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
            cwd=release.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=release.timeout_seconds,
            check=False,
        )
        exit_code = process.returncode
        stdout = process.stdout.strip()
        stderr = process.stderr.strip()
    except subprocess.TimeoutExpired as error:
        # 超时后不信任可能只写了一半的报告，测试会把它判为失败。
        timed_out = True
        exit_code = 124
        stdout = (error.stdout or "").strip() if isinstance(error.stdout, str) else ""
        stderr = f"技能执行超过 {release.timeout_seconds} 秒"

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    report: dict[str, Any] | None = None
    report_error: str | None = None
    if resolved_output.is_file():
        try:
            report = json.loads(resolved_output.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            report_error = f"输出不是合法 JSON：{error}"

    report_version = report.get("skill_version") if isinstance(report, dict) else None
    return {
        "allowed": True,
        "access": decision.public_view(),
        "command": command,
        "environment_keys": sorted(environment),
        "cwd": str(release.root),
        "timeout_seconds": release.timeout_seconds,
        "timed_out": timed_out,
        "elapsed_ms": elapsed_ms,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "output_path": str(resolved_output),
        "output_created": resolved_output.is_file(),
        "report_error": report_error,
        "report": report,
        "report_version_matches": report_version == release.version,
    }


def evaluate_case(test: SkillTestCase, execution: dict[str, Any]) -> dict[str, Any]:
    """逐项比较进程、报告、业务状态、发现代码和版本证据。"""
    report = execution["report"]
    exit_code = execution["exit_code"]
    actual_process = "success" if exit_code == 0 else "failure"
    actual_report = isinstance(report, dict)
    actual_status = report.get("status") if actual_report else None
    actual_codes = sorted(
        finding.get("code", "")
        for finding in (report.get("findings", []) if actual_report else [])
    )
    expected_codes = sorted(test.expected_finding_codes)

    checks = {
        "authorized": execution["allowed"],
        "process_matches": actual_process == test.expected_process,
        "exit_code_matches": exit_code == test.expected_exit_code,
        "report_matches": actual_report == test.expected_report,
        "status_matches": actual_status == test.expected_status,
        "finding_codes_match": actual_codes == expected_codes,
        "report_version_matches": (
            execution["report_version_matches"] if actual_report else True
        ),
    }
    checks["all"] = all(checks.values())
    return {
        "case_id": test.case_id,
        "role": test.role,
        "input_path": test.input_path,
        "expected": {
            "process": test.expected_process,
            "exit_code": test.expected_exit_code,
            "report": test.expected_report,
            "status": test.expected_status,
            "finding_codes": expected_codes,
        },
        "actual": {
            "process": actual_process,
            "exit_code": exit_code,
            "report": actual_report,
            "status": actual_status,
            "finding_codes": actual_codes,
            "findings_count": len(report.get("findings", [])) if actual_report else 0,
        },
        "checks": checks,
        "passed": checks["all"],
        "execution": {
            "access": execution["access"],
            "command": execution["command"],
            "environment_keys": execution["environment_keys"],
            "cwd": execution["cwd"],
            "timeout_seconds": execution["timeout_seconds"],
            "timed_out": execution["timed_out"],
            "elapsed_ms": execution["elapsed_ms"],
            "stdout": execution["stdout"],
            "stderr": execution["stderr"],
            "output_path": execution["output_path"],
            "output_created": execution["output_created"],
            "report_error": execution["report_error"],
        },
    }


def summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """按正式版与候选版汇总用例数量和失败用例。"""
    summary: dict[str, Any] = {}
    for role in ("active", "candidate"):
        role_runs = [run for run in runs if run["role"] == role]
        failed = [run["case_id"] for run in role_runs if not run["passed"]]
        summary[role] = {
            "total": len(role_runs),
            "passed": len(role_runs) - len(failed),
            "failed": len(failed),
            "failed_cases": failed,
            "all_passed": not failed,
        }
    return summary


def run_test_suite(
    policy: GovernancePolicy,
    releases: dict[str, ReleaseBundle],
    report_path: Path = TEST_REPORT_PATH,
) -> dict[str, Any]:
    """用发布机器人身份跑完整回归集，并保留每个用例的证据。"""
    runs: list[dict[str, Any]] = []
    for test in policy.tests:
        release = releases[policy.active_version if test.role == "active" else policy.candidate_version]
        input_path = resolve_project_path(test.input_path)
        output_path = TEST_ARTIFACT_DIR / release.version / f"{test.case_id}.json"
        execution = execute_release(
            policy,
            release,
            policy.release_bot,
            input_path,
            output_path,
        )
        runs.append(evaluate_case(test, execution))

    summary = summarize_runs(runs)
    report = {
        "report_version": 1,
        "generated_at": utc_now(),
        "skill": policy.skill_name,
        "principal": policy.release_bot,
        "active_version": policy.active_version,
        "candidate_version": policy.candidate_version,
        "runs": runs,
        "summary": summary,
        "checks": {
            "release_bot_authorized_for_all_runs": all(
                run["execution"]["access"]["allowed"] for run in runs
            ),
            "active_version_passed": summary["active"]["all_passed"],
            "candidate_version_passed": summary["candidate"]["all_passed"],
            "suite_completed": len(runs) == len(policy.tests),
        },
    }
    atomic_write_json(report_path, report)
    return report


def run_acl_checks(policy: GovernancePolicy) -> list[dict[str, Any]]:
    """真实执行允许、默认拒绝和候选版隔离三条 ACL 路径。"""
    sample = resolve_project_path("fixtures/sample-report.csv")
    ACL_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    cases = [
        {
            "check_id": "alice_active_allowed",
            "principal": "alice",
            "version": policy.active_version,
            "expect_allowed": True,
            "expect_output": True,
            "output": ACL_ARTIFACT_DIR / "alice-active.json",
        },
        {
            "check_id": "bob_default_denied",
            "principal": "bob",
            "version": policy.active_version,
            "expect_allowed": False,
            "expect_output": False,
            "output": ACL_ARTIFACT_DIR / "bob-denied.json",
        },
        {
            "check_id": "alice_candidate_denied",
            "principal": "alice",
            "version": policy.candidate_version,
            "expect_allowed": False,
            "expect_output": False,
            "output": ACL_ARTIFACT_DIR / "alice-candidate-denied.json",
        },
    ]

    results: list[dict[str, Any]] = []
    for case in cases:
        output_path: Path = case["output"]
        output_path.unlink(missing_ok=True)
        execution = execute_request(
            policy,
            case["principal"],
            case["version"],
            sample,
            output_path,
        )
        report = execution["report"]
        finding_count = len(report.get("findings", [])) if isinstance(report, dict) else 0
        checks = {
            "allowed_matches": execution["allowed"] == case["expect_allowed"],
            "output_matches": output_path.is_file() == case["expect_output"],
            "authorization_precedes_execution": (
                execution["allowed"]
                or (execution["exit_code"] is None and not output_path.is_file())
            ),
        }
        checks["all"] = all(checks.values())
        results.append(
            {
                "check_id": case["check_id"],
                "access": execution["access"],
                "exit_code": execution["exit_code"],
                "output_created": output_path.is_file(),
                "findings_count": finding_count,
                "checks": checks,
                "passed": checks["all"],
            }
        )
    return results


def write_manifest(policy: GovernancePolicy, releases: dict[str, ReleaseBundle]) -> tuple[dict[str, Any], str]:
    """写出技能清单并返回其内容摘要。"""
    manifest = build_manifest(policy, releases)
    atomic_write_json(MANIFEST_PATH, manifest)
    return manifest, sha256_path(MANIFEST_PATH)


def build_rollback_record(
    policy: GovernancePolicy,
    releases: dict[str, ReleaseBundle],
    test_report: dict[str, Any],
    test_report_sha256: str,
    manifest_sha256: str,
) -> dict[str, Any]:
    """记录候选版本为何被撤销，以及正式版本仍然可用的证据。"""
    candidate = releases[policy.candidate_version]
    active = releases[policy.active_version]
    failed_cases = [
        {
            "case_id": run["case_id"],
            "expected": run["expected"],
            "actual": run["actual"],
            "checks": run["checks"],
        }
        for run in test_report["runs"]
        if run["role"] == "candidate" and not run["passed"]
    ]
    return {
        "record_version": 1,
        "record_id": f"rollback-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}",
        "generated_at": utc_now(),
        "skill": policy.skill_name,
        "action": "candidate_release_reverted",
        "from_version": policy.candidate_version,
        "to_version": policy.active_version,
        "triggered_by": policy.release_bot,
        "reason": "候选版本未通过全部回归用例，发布门禁拒绝提升",
        "failed_cases": failed_cases,
        "evidence": {
            "candidate_content_sha256": candidate.content_sha256,
            "active_content_sha256": active.content_sha256,
            "manifest": display_path(MANIFEST_PATH),
            "manifest_sha256": manifest_sha256,
            "test_report": display_path(TEST_REPORT_PATH),
            "test_report_sha256": test_report_sha256,
        },
        "active_version_after": policy.active_version,
        "active_release_verified": active.version == policy.active_version,
    }


def run_release_gate(policy: GovernancePolicy) -> dict[str, Any]:
    """执行清单、ACL、回归测试、发布门禁和回滚记录的完整链路。"""
    releases = load_releases(policy)
    manifest, manifest_sha256 = write_manifest(policy, releases)
    acl_results = run_acl_checks(policy)
    test_report = run_test_suite(policy, releases)
    test_report_sha256 = sha256_path(TEST_REPORT_PATH)

    active_summary = test_report["summary"]["active"]
    candidate_summary = test_report["summary"]["candidate"]
    acl_all = all(item["passed"] for item in acl_results)
    promotion_ready = (
        manifest["checks"]["all"]
        and acl_all
        and active_summary["all_passed"]
        and candidate_summary["all_passed"]
    )
    decision = "ready_for_promotion" if promotion_ready else "blocked"

    rollback_sha256: str | None = None
    if decision == "blocked":
        rollback = build_rollback_record(
            policy,
            releases,
            test_report,
            test_report_sha256,
            manifest_sha256,
        )
        atomic_write_json(ROLLBACK_RECORD_PATH, rollback)
        rollback_sha256 = sha256_path(ROLLBACK_RECORD_PATH)
    else:
        # 成功发布时清理旧候选的失败记录，避免下一次操作误读历史文件。
        ROLLBACK_RECORD_PATH.unlink(missing_ok=True)

    checks = {
        "manifest_all": manifest["checks"]["all"],
        "acl_all": acl_all,
        "active_version_tests_passed": active_summary["all_passed"],
        "candidate_gate_applied": (
            candidate_summary["all_passed"]
            if decision == "ready_for_promotion"
            else decision == "blocked" and ROLLBACK_RECORD_PATH.is_file()
        ),
        "active_version_unchanged": policy.active_version
        == releases[policy.active_version].version,
        "test_report_written": TEST_REPORT_PATH.is_file(),
        "manifest_written": MANIFEST_PATH.is_file(),
        "rollback_recorded": (
            ROLLBACK_RECORD_PATH.is_file()
            if decision == "blocked"
            else not ROLLBACK_RECORD_PATH.exists()
        ),
    }
    checks["all"] = all(checks.values())
    governance_report = {
        "report_version": 1,
        "generated_at": utc_now(),
        "skill": policy.skill_name,
        "owner": policy.owner,
        "active_version": policy.active_version,
        "candidate_version": policy.candidate_version,
        "decision": decision,
        "reason": (
            "候选版本通过全部门禁，可以进入后续人工发布"
            if promotion_ready
            else "候选版本未通过全部回归用例，保持正式版本不变"
        ),
        "manifest": {
            "path": display_path(MANIFEST_PATH),
            "sha256": manifest_sha256,
            "checks": manifest["checks"],
        },
        "acl_checks": acl_results,
        "tests": {
            "report_path": display_path(TEST_REPORT_PATH),
            "report_sha256": test_report_sha256,
            "summary": test_report["summary"],
        },
        "release_gate": {
            "decision": decision,
            "promotion_ready": promotion_ready,
            "active_before": policy.active_version,
            "active_after": policy.active_version,
            "rollback_record": (
                {
                    "path": display_path(ROLLBACK_RECORD_PATH),
                    "sha256": rollback_sha256,
                }
                if rollback_sha256
                else None
            ),
        },
        "checks": checks,
    }
    atomic_write_json(GOVERNANCE_REPORT_PATH, governance_report)
    return governance_report


def print_manifest(manifest: dict[str, Any]) -> None:
    """打印清单摘要，便于读者确认版本和摘要不是口头声明。"""
    print(f"技能：{manifest['skill']}")
    print(f"正式版本：{manifest['active_version']}")
    print(f"候选版本：{manifest['candidate_version']}")
    for version, release in manifest["releases"].items():
        print(
            f"  {version}: role={release['role']} "
            f"digest={release['content_sha256'][:12]}... "
            f"principals={release['principals']}"
        )
    print(f"清单检查：{manifest['checks']}")
    print(f"已保存：{MANIFEST_PATH}")


def command_manifest(policy: GovernancePolicy) -> int:
    """只生成技能清单，不执行技能。"""
    releases = load_releases(policy)
    manifest, _ = write_manifest(policy, releases)
    print_manifest(manifest)
    return 0 if manifest["checks"]["all"] else 1


def command_access(args: argparse.Namespace, policy: GovernancePolicy) -> int:
    """验证一次授权；允许时真实执行技能，拒绝时不创建输出文件。"""
    output_path = resolve_project_path(args.output)
    execution = execute_request(
        policy,
        args.principal,
        args.version,
        resolve_project_path(args.input),
        output_path,
    )
    print(json.dumps(execution["access"], ensure_ascii=False, indent=2))
    if not execution["allowed"]:
        print("ACL 已拒绝：没有启动技能脚本，也没有生成输出文件。")
        return 3

    report = execution["report"]
    print(f"脚本退出码：{execution['exit_code']}")
    print(f"输出文件：{execution['output_path']}")
    if isinstance(report, dict):
        print(
            f"业务状态：{report.get('status')} "
            f"发现数量：{len(report.get('findings', []))}"
        )
    return 0 if execution["exit_code"] == 0 else 1


def command_test(policy: GovernancePolicy) -> int:
    """运行正式版与候选版回归集，并写出测试报告。"""
    releases = load_releases(policy)
    report = run_test_suite(policy, releases)
    for role in ("active", "candidate"):
        summary = report["summary"][role]
        print(
            f"{role}: {summary['passed']}/{summary['total']} 通过 "
            f"failed_cases={summary['failed_cases']}"
        )
    failed = [
        run for run in report["runs"] if not run["passed"]
    ]
    for run in failed:
        print(
            f"失败用例：{run['role']}/{run['case_id']} "
            f"expected_codes={run['expected']['finding_codes']} "
            f"actual_codes={run['actual']['finding_codes']}"
        )
    print(f"测试报告：{TEST_REPORT_PATH}")
    print(f"测试检查：{report['checks']}")
    return 0 if report["summary"]["active"]["all_passed"] and report["summary"]["candidate"]["all_passed"] else 1


def command_release(policy: GovernancePolicy) -> int:
    """运行完整发布门禁；候选失败时写出回滚记录而不是继续发布。"""
    report = run_release_gate(policy)
    print(f"发布决策：{report['decision']}")
    print(f"正式版本：{report['release_gate']['active_after']}")
    for role in ("active", "candidate"):
        summary = report["tests"]["summary"][role]
        print(
            f"{role}: {summary['passed']}/{summary['total']} 通过 "
            f"failed_cases={summary['failed_cases']}"
        )
    for item in report["acl_checks"]:
        print(
            f"ACL {item['check_id']}: allowed={item['access']['allowed']} "
            f"reason={item['access']['reason']} output={item['output_created']}"
        )
    rollback = report["release_gate"]["rollback_record"]
    if rollback:
        print(f"回滚记录：{ROLLBACK_RECORD_PATH}")
    print(f"治理检查：{report['checks']}")
    print(f"治理报告：{GOVERNANCE_REPORT_PATH}")
    return 0 if report["checks"]["all"] else 1


def parse_args() -> argparse.Namespace:
    """定义清单、单次授权、回归测试和发布门禁四个命令。"""
    parser = argparse.ArgumentParser(description="Agent Skill 版本、权限、测试与回滚治理")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("manifest", help="生成技能版本清单")

    access = subparsers.add_parser("access", help="检查 ACL 并按需执行技能")
    access.add_argument("--principal", required=True)
    access.add_argument("--version", required=True)
    access.add_argument("--input", required=True)
    access.add_argument("--output", required=True)

    subparsers.add_parser("test", help="运行正式版与候选版回归集")
    subparsers.add_parser("release", help="执行完整发布门禁和回滚记录")
    return parser.parse_args()


def main() -> int:
    """加载策略并分发命令；配置错误以非零状态退出。"""
    args = parse_args()
    try:
        policy = load_policy()
        if args.command == "manifest":
            return command_manifest(policy)
        if args.command == "access":
            return command_access(args, policy)
        if args.command == "test":
            return command_test(policy)
        if args.command == "release":
            return command_release(policy)
        raise GovernanceError(f"未知命令：{args.command}")
    except GovernanceError as error:
        print(f"治理失败：{type(error).__name__}: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
