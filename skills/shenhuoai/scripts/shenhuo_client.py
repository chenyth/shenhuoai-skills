#!/usr/bin/env python3
"""神火 AI 通用 Agent Skill 的无依赖 External API 客户端。"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import http.client
import json
import mimetypes
import os
import re
import secrets
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO
from uuid import uuid4


CLIENT_VERSION = "0.3.0"
SKILL_NAME = "shenhuo-ai"
SKILL_VERSION = "0.3.1"
SKILL_ROOT = Path(__file__).resolve().parents[1]
SKILL_ENV_PATH = SKILL_ROOT / ".env"
SKILL_CHANNEL_PATH = SKILL_ROOT / "channel.json"
SKILL_RUNTIME_PROFILE_PATH = SKILL_ROOT / "runtime-profile.json"
SKILL_PACKAGE_INTEGRITY_PATH = SKILL_ROOT / "package-integrity.json"
SKILL_DESIGNER_CATALOG_CURRENT_PATH = (
    SKILL_ROOT / "references" / "designer-catalog.current.md"
)
TRIAL_BOOTSTRAP_PATH = SKILL_ROOT / ".trial-bootstrap.json"
SKILL_ENV_KEYS = frozenset(
    {"SHENHUO_API_BASE_URL", "SHENHUO_API_KEY", "SHENHUO_TRIAL_INSTALLATION_ID"}
)
DEFAULT_BASE_URL = "https://api.shenhuoai.com/external/v1"
OFFICIAL_TRIAL_API_HOST = "api.shenhuoai.com"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
MAX_HTTP_ATTEMPTS = 3
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_JSON_INPUT_BYTES = 2 * 1024 * 1024
MAX_API_KEY_LENGTH = 4096
MAX_DESIGNER_CATALOG_ITEMS = 500
MAX_DESIGNER_CATALOG_BYTES = 1 * 1024 * 1024
MAX_AUTOMATIC_RETRY_DELAY_SECONDS = 60.0
ACTIVE_STATUSES = {"queued", "processing"}
STOP_ROUTE_STATUSES = {"succeeded", "failed", "canceled"}
STOP_REQUEST_STATUSES = {"requires_action", "ready", "succeeded", "failed", "canceled"}
STOP_GENERATION_STATUSES = {"succeeded", "failed", "canceled"}
RETRYABLE_TRANSPORT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    ConnectionError,
    http.client.HTTPException,
    ssl.SSLEOFError,
    ssl.SSLError,
)


class ClientUsageError(Exception):
    """本地命令参数无效。"""

    code = "CLIENT_USAGE_ERROR"


class TrialChannelNotConfiguredError(ClientUsageError):
    code = "TRIAL_CHANNEL_NOT_CONFIGURED"


class ProcessEnvironmentOverrideError(ClientUsageError):
    code = "PROCESS_ENVIRONMENT_OVERRIDE"


class UntrustedTrialApiOriginError(ClientUsageError):
    code = "UNTRUSTED_TRIAL_API_ORIGIN"


class UntrustedApiOriginError(ClientUsageError):
    code = "UNTRUSTED_API_ORIGIN"


class ApiKeyAlreadyConfiguredError(ClientUsageError):
    code = "SHENHUO_API_KEY_ALREADY_CONFIGURED"


class RuntimeProfileError(ClientUsageError):
    code = "INVALID_RUNTIME_PROFILE"


class PackageIntegrityError(ClientUsageError):
    code = "PACKAGE_INTEGRITY_ERROR"


class PollTimeoutError(Exception):
    """轮询超时，但远端资源可能仍在运行。"""

    code = "POLL_TIMEOUT"

    def __init__(self, resource_id: str, last_data: Mapping[str, Any] | None = None):
        super().__init__(f"等待 {resource_id} 超时。")
        self.resource_id = resource_id
        self.last_data = dict(last_data or {})


@dataclass
class ApiError(Exception):
    code: str
    message: str
    status: int | None = None
    retryable: bool = False
    details: Any = None
    retry_after: float | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class DesignerProfile:
    id: str
    slug: str
    name: str

    def as_api_value(self) -> dict[str, str]:
        return {"id": self.id, "slug": self.slug, "name": self.name}


@dataclass(frozen=True)
class RuntimeProfile:
    schema_version: int
    skill_name: str
    skill_version: str
    routing_enabled: bool
    fixed_designer: DesignerProfile | None


GENERIC_RUNTIME_PROFILE = RuntimeProfile(
    schema_version=1,
    skill_name=SKILL_NAME,
    skill_version=SKILL_VERSION,
    routing_enabled=True,
    fixed_designer=None,
)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ClientUsageError(message)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝重定向，避免 Bearer 凭证离开配置的 External API 源。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def load_runtime_profile(path: Path) -> RuntimeProfile:
    """加载发布包中的公开约束；拒绝扩展字段，避免垂直 Skill 绕过固定设计师。"""

    if not path.exists() or not path.is_file() or path.is_symlink():
        raise RuntimeProfileError("Skill runtime profile 必须是普通文件。")
    if path.stat().st_size > 8 * 1024:
        raise RuntimeProfileError("Skill runtime profile 不能超过 8 KB。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeProfileError("Skill runtime profile 必须是 UTF-8 JSON。") from exc
    expected_keys = {
        "schema_version",
        "skill_name",
        "skill_version",
        "routing_enabled",
        "fixed_designer",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise RuntimeProfileError("Skill runtime profile 字段不受支持。")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise RuntimeProfileError("Skill runtime profile schema_version 必须为 1。")
    skill_name = str(payload["skill_name"] or "").strip()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", skill_name):
        raise RuntimeProfileError("Skill runtime profile 的 skill_name 无效。")
    skill_version = str(payload["skill_version"] or "").strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", skill_version):
        raise RuntimeProfileError("Skill runtime profile 的 skill_version 无效。")
    if type(payload["routing_enabled"]) is not bool:
        raise RuntimeProfileError("Skill runtime profile 的 routing_enabled 必须为布尔值。")
    routing_enabled = payload["routing_enabled"]

    raw_designer = payload["fixed_designer"]
    fixed_designer: DesignerProfile | None = None
    if raw_designer is not None:
        if not isinstance(raw_designer, dict) or set(raw_designer) != {"id", "slug", "name"}:
            raise RuntimeProfileError("固定设计师必须且只能包含 id、slug、name。")
        values = {key: str(raw_designer[key] or "").strip() for key in ("id", "slug", "name")}
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,200}", values["id"]):
            raise RuntimeProfileError("固定设计师 id 无效。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,200}", values["slug"]):
            raise RuntimeProfileError("固定设计师 slug 无效。")
        if not values["name"] or len(values["name"]) > 200 or any(
            character in values["name"] for character in "\r\n"
        ):
            raise RuntimeProfileError("固定设计师 name 无效。")
        fixed_designer = DesignerProfile(**values)

    if fixed_designer is None and not routing_enabled:
        raise RuntimeProfileError("禁用匹配的 runtime profile 必须固定设计师。")
    if fixed_designer is not None and routing_enabled:
        raise RuntimeProfileError("固定设计师的 runtime profile 必须禁用匹配。")
    return RuntimeProfile(
        schema_version=1,
        skill_name=skill_name,
        skill_version=skill_version,
        routing_enabled=routing_enabled,
        fixed_designer=fixed_designer,
    )


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正数")
    return parsed


def _target_image_count(value: str) -> str | int:
    return value if value == "auto" else _positive_int(value)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _stable_idempotency_key(
    command: str,
    payload: Any,
    override: str | None = None,
    *,
    namespace: str = SKILL_NAME,
) -> str:
    if override is not None:
        value = override.strip()
        if not value or len(value) > 160 or "\r" in value or "\n" in value:
            raise ClientUsageError("Idempotency-Key 必须为 1–160 个字符且不能包含换行。")
        return value
    digest = hashlib.sha256(_canonical_json(payload)).hexdigest()[:40]
    return f"{namespace}:{command}:{digest}"


def _safe_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ClientUsageError("SHENHUO_API_BASE_URL 必须是绝对 URL。")
    if parsed.username is not None or parsed.password is not None:
        raise ClientUsageError("SHENHUO_API_BASE_URL 不能包含账号或密码。")
    is_local = (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_local):
        raise ClientUsageError("除 localhost 测试外，SHENHUO_API_BASE_URL 必须使用 HTTPS。")
    if parsed.query or parsed.fragment:
        raise ClientUsageError("SHENHUO_API_BASE_URL 不能包含 query 或 fragment。")
    return base_url


def _safe_trial_base_url(value: str) -> str:
    return _safe_official_api_base_url(value, error_type=UntrustedTrialApiOriginError)


def _safe_authenticated_base_url(value: str) -> str:
    return _safe_official_api_base_url(value, error_type=UntrustedApiOriginError)


def _safe_official_api_base_url(
    value: str,
    *,
    error_type: type[ClientUsageError],
) -> str:
    """只允许官方 External API，另为显式本地开发保留 loopback origin。"""

    base_url = _safe_base_url(value)
    parsed = urllib.parse.urlsplit(base_url)
    host = (parsed.hostname or "").lower()
    is_local = host in {"127.0.0.1", "localhost", "::1"}
    if host != OFFICIAL_TRIAL_API_HOST and not is_local:
        raise error_type(
            "携带神火凭证的请求只能访问官方 API 或显式 localhost 开发环境。"
        )
    if not is_local and parsed.port not in {None, 443}:
        raise error_type("神火官方 API 只能使用标准 HTTPS 端口。")
    if parsed.path.rstrip("/") != "/external/v1":
        raise error_type("神火 API 地址必须指向 /external/v1。")
    return base_url


def _read_channel_code(path: Path) -> str:
    if not path.exists():
        raise TrialChannelNotConfiguredError("当前 Skill 包尚未配置试用渠道。")
    if not path.is_file() or path.is_symlink():
        raise TrialChannelNotConfiguredError("Skill 渠道 manifest 必须是普通文件。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrialChannelNotConfiguredError("Skill 渠道 manifest 必须是 UTF-8 JSON。") from exc
    if not isinstance(payload, dict) or set(payload) != {"channel_code"}:
        raise TrialChannelNotConfiguredError("Skill 渠道 manifest 只能包含 channel_code。")
    channel_code = str(payload.get("channel_code") or "").strip()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", channel_code):
        raise TrialChannelNotConfiguredError("Skill 渠道 code 无效。")
    return channel_code


def verify_package_integrity(skill_root: Path) -> dict[str, str] | None:
    """校验打包器生成的公开摘要；用于防误配/篡改检测，不是签名安全边界。"""

    integrity_path = skill_root / "package-integrity.json"
    channel_path = skill_root / "channel.json"
    if not integrity_path.exists():
        if channel_path.exists() or channel_path.is_symlink():
            raise PackageIntegrityError(
                "检测到未带完整性摘要的 channel.json；正式渠道 Skill 必须由打包器生成。"
            )
        return None
    if not integrity_path.is_file() or integrity_path.is_symlink():
        raise PackageIntegrityError("Skill package integrity manifest 必须是普通文件。")
    if integrity_path.stat().st_size > 16 * 1024:
        raise PackageIntegrityError("Skill package integrity manifest 不能超过 16 KB。")
    try:
        payload = json.loads(integrity_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageIntegrityError(
            "Skill package integrity manifest 必须是 UTF-8 JSON。"
        ) from exc
    expected_keys = {
        "schema_version",
        "algorithm",
        "skill_name",
        "skill_version",
        "channel_code",
        "files",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise PackageIntegrityError("Skill package integrity manifest 字段不受支持。")
    schema_version = payload["schema_version"]
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise PackageIntegrityError("Skill package integrity schema_version 必须为 1 或 2。")
    if payload["algorithm"] != "sha256":
        raise PackageIntegrityError("Skill package integrity 只支持 sha256。")

    skill_name = str(payload["skill_name"] or "").strip()
    skill_version = str(payload["skill_version"] or "").strip()
    channel_code = str(payload["channel_code"] or "").strip()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", skill_name):
        raise PackageIntegrityError("Skill package integrity 的 skill_name 无效。")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", skill_version):
        raise PackageIntegrityError("Skill package integrity 的 skill_version 无效。")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", channel_code):
        raise PackageIntegrityError("Skill package integrity 的 channel_code 无效。")

    expected_files = {
        "channel.json",
        "runtime-profile.json",
        "scripts/shenhuo_client.py",
    }
    if schema_version == 2:
        if skill_name == "shenhuo-ai":
            expected_files.add("references/designer-catalog.md")
        elif skill_name == "shenhuo-hair-preview":
            expected_files.add("references/designer-profile.md")
        else:
            raise PackageIntegrityError("Skill package integrity 的 skill_name 不受支持。")
    files = payload["files"]
    if not isinstance(files, dict) or set(files) != expected_files:
        raise PackageIntegrityError("Skill package integrity 文件集合不完整。")
    for relative_name in sorted(expected_files):
        expected_digest = files.get(relative_name)
        if not isinstance(expected_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_digest
        ):
            raise PackageIntegrityError(
                f"Skill package integrity 的 {relative_name} 摘要无效。"
            )
        file_path = skill_root / relative_name
        if not file_path.is_file() or file_path.is_symlink():
            raise PackageIntegrityError(f"Skill 发布包缺少普通文件 {relative_name}。")
        try:
            actual_digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise PackageIntegrityError(f"无法读取 Skill 发布文件 {relative_name}。") from exc
        if not secrets.compare_digest(actual_digest, expected_digest):
            raise PackageIntegrityError(f"Skill 发布文件 {relative_name} 完整性校验失败。")

    profile = load_runtime_profile(skill_root / "runtime-profile.json")
    if profile.skill_name != skill_name or profile.skill_version != skill_version:
        raise PackageIntegrityError("Skill runtime profile 与 package integrity 不一致。")
    if _read_channel_code(skill_root / "channel.json") != channel_code:
        raise PackageIntegrityError("Skill channel manifest 与 package integrity 不一致。")
    return {
        "skill_name": skill_name,
        "skill_version": skill_version,
        "channel_code": channel_code,
    }


def _clean_installation_id(value: str) -> str:
    clean = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", clean):
        raise ClientUsageError("试用安装 ID 无效。")
    return clean


def _env_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = str(environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ClientUsageError(f"{name} 必须是正数。") from exc
    if value <= 0:
        raise ClientUsageError(f"{name} 必须是正数。")
    return value


def _read_skill_env(path: Path, *, require_private_permissions: bool = True) -> dict[str, str]:
    if not path.exists():
        return {}
    if not path.is_file() or path.is_symlink():
        raise ClientUsageError("Skill .env 必须是普通文件。")
    if require_private_permissions and os.name != "nt" and path.stat().st_mode & 0o077:
        raise ClientUsageError("Skill .env 权限必须为 600。")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ClientUsageError("无法读取 Skill .env。") from exc
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ClientUsageError(f"Skill .env 第 {line_number} 行必须使用 KEY=VALUE 格式。")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in SKILL_ENV_KEYS:
            raise ClientUsageError(f"Skill .env 包含不支持的配置：{key}。")
        values[key] = value.strip()
    return values


def _runtime_environment(path: Path | None = None) -> dict[str, str]:
    values = _read_skill_env(path or SKILL_ENV_PATH)
    values.update(os.environ)
    return values


def _clean_api_key(value: str) -> str:
    if not value:
        raise ClientUsageError("API Key 不能为空。")
    if len(value) > MAX_API_KEY_LENGTH:
        raise ClientUsageError(f"API Key 不能超过 {MAX_API_KEY_LENGTH} 个字符。")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise ClientUsageError("API Key 不能包含空白、控制字符或非 ASCII 字符。")
    return value


def _read_api_key(stdin: TextIO, stderr: TextIO) -> str:
    try:
        if stdin is sys.stdin and stdin.isatty():
            raw_value = getpass.getpass("请输入神火 API Key：", stream=stderr)
        else:
            raw_value = stdin.readline(MAX_API_KEY_LENGTH + 2)
    except (EOFError, KeyboardInterrupt, OSError) as exc:
        raise ClientUsageError("未能从标准输入读取 API Key。") from exc
    if raw_value.endswith("\n"):
        raw_value = raw_value[:-1]
        if raw_value.endswith("\r"):
            raw_value = raw_value[:-1]
    return _clean_api_key(raw_value)


def _configure_skill_api_key(
    api_key: str,
    env_path: Path,
    *,
    trial_installation_id: str | None = None,
) -> dict[str, Any]:
    if env_path.exists() or env_path.is_symlink():
        if not env_path.is_file() or env_path.is_symlink():
            raise ClientUsageError("Skill .env 必须是普通文件，不能是符号链接。")
        _read_skill_env(env_path, require_private_permissions=False)
        try:
            existing_lines = env_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ClientUsageError("无法读取 Skill .env。") from exc
    else:
        existing_lines = []

    output_lines: list[str] = []
    base_url_present = False
    key_written = False
    installation_written = False
    for raw_line in existing_lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            output_lines.append(raw_line)
            continue
        config_key = line.split("=", 1)[0].strip()
        if config_key == "SHENHUO_API_KEY":
            if not key_written:
                output_lines.append(f"SHENHUO_API_KEY={api_key}")
                key_written = True
            continue
        if config_key == "SHENHUO_TRIAL_INSTALLATION_ID" and trial_installation_id:
            if not installation_written:
                output_lines.append(
                    f"SHENHUO_TRIAL_INSTALLATION_ID={trial_installation_id}"
                )
                installation_written = True
            continue
        if config_key == "SHENHUO_API_BASE_URL":
            base_url_present = True
        output_lines.append(raw_line)

    if not base_url_present:
        output_lines.insert(0, f"SHENHUO_API_BASE_URL={DEFAULT_BASE_URL}")
    if not key_written:
        output_lines.append(f"SHENHUO_API_KEY={api_key}")
    if trial_installation_id and not installation_written:
        output_lines.append(f"SHENHUO_TRIAL_INSTALLATION_ID={trial_installation_id}")

    _atomic_replace_private_text(env_path, "\n".join(output_lines) + "\n")
    return {
        "configured": True,
        "env_path": str(env_path.resolve()),
        "mode": "0600",
    }


def _read_or_create_trial_bootstrap(
    path: Path,
    installation_id: str | None = None,
) -> tuple[str, str]:
    if path.exists() or path.is_symlink():
        if not path.is_file() or path.is_symlink():
            raise ClientUsageError("试用 bootstrap 状态必须是普通文件。")
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise ClientUsageError("试用 bootstrap 状态权限必须为 600。")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClientUsageError("无法读取试用 bootstrap 状态。") from exc
        if not isinstance(payload, dict) or set(payload) != {"installation_id", "installation_token"}:
            raise ClientUsageError("试用 bootstrap 状态结构无效。")
        stored_id = _clean_installation_id(str(payload.get("installation_id") or ""))
        token = str(payload.get("installation_token") or "").strip()
        if installation_id and stored_id != _clean_installation_id(installation_id):
            raise ClientUsageError("试用 bootstrap 状态与当前安装不匹配。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{24,160}", token):
            raise ClientUsageError("试用 bootstrap 状态与当前安装不匹配。")
        return stored_id, token

    resolved_installation_id = _clean_installation_id(installation_id or str(uuid4()))
    token = secrets.token_urlsafe(32)
    _atomic_replace_private_text(
        path,
        json.dumps(
            {
                "installation_id": resolved_installation_id,
                "installation_token": token,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )
    return resolved_installation_id, token


def _remove_private_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise ClientUsageError("无法清理试用 bootstrap 状态。") from exc


def _json_from_bytes(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("INVALID_API_RESPONSE", "神火返回了非 JSON 响应。") from exc


def _read_response_bytes(response: Any, max_response_bytes: int | None) -> bytes:
    if max_response_bytes is None:
        return response.read()
    response_headers = getattr(response, "headers", None)
    raw_content_length = (
        response_headers.get("Content-Length") if response_headers is not None else None
    )
    if raw_content_length is not None:
        try:
            content_length = int(raw_content_length)
        except (TypeError, ValueError) as exc:
            raise ApiError(
                "INVALID_API_RESPONSE",
                "神火响应的 Content-Length 无效。",
            ) from exc
        if content_length < 0:
            raise ApiError(
                "INVALID_API_RESPONSE",
                "神火响应的 Content-Length 无效。",
            )
        if content_length > max_response_bytes:
            raise ApiError(
                "DESIGNER_CATALOG_TOO_LARGE",
                "设计师目录响应不能超过 1 MiB。",
            )
    raw = response.read(max_response_bytes + 1)
    if len(raw) > max_response_bytes:
        raise ApiError(
            "DESIGNER_CATALOG_TOO_LARGE",
            "设计师目录响应不能超过 1 MiB。",
        )
    return raw


def _error_from_payload(payload: Any, *, status: int | None) -> ApiError:
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ApiError(
            code=f"HTTP_{status}" if status else "API_ERROR",
            message="神火返回了 API 错误。",
            status=status,
            retryable=bool(status and (status == 429 or status >= 500)),
        )
    return ApiError(
        code=str(error.get("code") or (f"HTTP_{status}" if status else "API_ERROR")),
        message=str(error.get("message") or "神火返回了 API 错误。"),
        status=status,
        retryable=bool(error.get("retryable", status and (status == 429 or status >= 500))),
        details=error.get("details"),
    )


def _unwrap_envelope(payload: Any, *, status: int | None = None) -> Any:
    if not isinstance(payload, dict):
        raise ApiError("INVALID_API_RESPONSE", "神火返回了无效响应结构。", status=status)
    if payload.get("error"):
        raise _error_from_payload(payload, status=status)
    if "data" not in payload:
        raise ApiError("INVALID_API_RESPONSE", "神火响应缺少 data。", status=status)
    data = payload["data"]
    meta = payload.get("meta")
    if isinstance(data, dict) and isinstance(meta, dict):
        data = dict(data)
        if "poll_after_ms" in meta and "poll_after_ms" not in data:
            data["poll_after_ms"] = meta["poll_after_ms"]
        if "idempotent_replay" in meta and "idempotent_replay" not in data:
            data["idempotent_replay"] = meta["idempotent_replay"]
    return data


def _raise_for_non_active_trial_credential(account: Any) -> None:
    """把 External account 的试用状态转为稳定语义，但不拦截其他历史 GET。"""

    if not isinstance(account, dict):
        return
    credential = account.get("credential")
    if not isinstance(credential, dict) or str(credential.get("kind") or "").strip() != "trial":
        return
    status = str(credential.get("status") or "").strip().lower()
    if status == "active":
        return
    if status == "expired":
        raise ApiError(
            "TRIAL_KEY_EXPIRED",
            "试用 Key 已过期，不能创建新任务；保留期内仍可用原 Key 轮询或下载既有结果。",
        )
    if status == "revoked":
        raise ApiError("TRIAL_KEY_REVOKED", "试用 Key 已撤销，不能再访问任务或结果。")
    if status in {"channel_paused", "paused"}:
        raise ApiError("TRIAL_CHANNEL_PAUSED", "试用渠道已暂停；请改用个人 Key。")
    if status in {"channel_not_active", "pending_activation", "not_active"}:
        raise ApiError("TRIAL_CHANNEL_NOT_ACTIVE", "试用渠道尚未激活；请改用个人 Key。")
    raise ApiError("INVALID_API_RESPONSE", "神火返回了不支持的试用 Key 状态。")


def _safe_trial_account_projection(account: Any) -> dict[str, Any]:
    """显式投影 configure-trial 输出，避免服务端新增字段被意外透传。"""

    if not isinstance(account, dict):
        raise ApiError("INVALID_API_RESPONSE", "试用账户响应必须是对象。")
    credential = account.get("credential")
    if not isinstance(credential, dict):
        raise ApiError("INVALID_API_RESPONSE", "试用账户响应缺少 credential。")
    kind = str(credential.get("kind") or "").strip()
    status = str(credential.get("status") or "").strip().lower()
    expires_at = credential.get("expires_at")
    if kind != "trial" or status != "active":
        _raise_for_non_active_trial_credential(account)
        raise ApiError("INVALID_API_RESPONSE", "新签发凭证必须是 active 试用 Key。")
    if not isinstance(expires_at, str) or not expires_at.strip():
        raise ApiError("INVALID_API_RESPONSE", "试用账户响应缺少 Key 到期时间。")

    weekly = account.get("weekly")
    if not isinstance(weekly, dict):
        raise ApiError("INVALID_API_RESPONSE", "试用账户响应缺少个人周额度。")
    safe_weekly: dict[str, Any] = {}
    for field_name in (
        "limit_credits",
        "consumed_credits",
        "reserved_credits",
        "remaining_credits",
    ):
        value = weekly.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ApiError("INVALID_API_RESPONSE", "试用账户个人周额度字段无效。")
        safe_weekly[field_name] = value
    reset_at = weekly.get("reset_at")
    if not isinstance(reset_at, str) or not reset_at.strip():
        raise ApiError("INVALID_API_RESPONSE", "试用账户响应缺少个人周额度重置时间。")
    safe_weekly["reset_at"] = reset_at.strip()

    safe_percentages: dict[str, int] = {}
    for field_name in ("proposal_remaining_percent", "image_remaining_percent"):
        value = account.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise ApiError("INVALID_API_RESPONSE", "试用共享池百分比必须是 0–100 的整数。")
        safe_percentages[field_name] = value
    return {
        "credential": {
            "kind": "trial",
            "status": "active",
            "expires_at": expires_at.strip(),
        },
        "weekly": safe_weekly,
        **safe_percentages,
    }


def _catalog_text(value: Any, label: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise ApiError("INVALID_API_RESPONSE", f"设计师目录的 {label} 必须是字符串。")
    clean = value.strip()
    if (
        not clean
        or len(clean) > max_length
        or clean != value
        or any(ord(character) < 32 or ord(character) == 127 for character in clean)
        or any(character in "\u0085\u2028\u2029" for character in clean)
    ):
        raise ApiError("INVALID_API_RESPONSE", f"设计师目录的 {label} 无效。")
    return clean


def _safe_designer_catalog_projection(catalog: Any) -> dict[str, Any]:
    """严格校验公开目录契约，拒绝新增字段被写入本地 Markdown。"""

    if not isinstance(catalog, dict) or set(catalog) != {"items", "generated_at"}:
        raise ApiError(
            "INVALID_API_RESPONSE",
            "设计师目录必须且只能包含 items、generated_at。",
        )
    generated_at = _catalog_text(catalog["generated_at"], "generated_at", max_length=80)
    try:
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError("INVALID_API_RESPONSE", "设计师目录的 generated_at 无效。") from exc

    raw_items = catalog["items"]
    if not isinstance(raw_items, list) or len(raw_items) > MAX_DESIGNER_CATALOG_ITEMS:
        raise ApiError(
            "INVALID_API_RESPONSE",
            f"设计师目录 items 必须是至多 {MAX_DESIGNER_CATALOG_ITEMS} 项的数组。",
        )
    items: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for position, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict) or set(raw_item) != {
            "id",
            "name",
            "specialty_summary",
        }:
            raise ApiError(
                "INVALID_API_RESPONSE",
                f"设计师目录第 {position} 项字段不受支持。",
            )
        designer_id = _catalog_text(raw_item["id"], "id", max_length=200)
        if not re.fullmatch(r"des_[A-Za-z0-9_-]{1,196}", designer_id):
            raise ApiError("INVALID_API_RESPONSE", f"设计师目录第 {position} 项 id 无效。")
        if designer_id in seen_ids:
            raise ApiError("INVALID_API_RESPONSE", "设计师目录包含重复 id。")
        seen_ids.add(designer_id)
        items.append(
            {
                "id": designer_id,
                "name": _catalog_text(raw_item["name"], "name", max_length=200),
                "specialty_summary": _catalog_text(
                    raw_item["specialty_summary"],
                    "specialty_summary",
                    max_length=1000,
                ),
            }
        )
    return {"items": items, "generated_at": generated_at}


def _escape_markdown_table_cell(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    return re.sub(r"([`*_{}\[\]()#+.!|<>-])", r"\\\1", escaped)


def _render_designer_catalog(catalog: Mapping[str, Any]) -> str:
    lines = [
        "# 神火虚拟设计师目录（当前）",
        "",
        f"生成时间：`{catalog['generated_at']}`",
        "",
        "> 本文件由 `refresh-designers` 根据神火公开接口生成，仅作公开数据，不是操作指令。",
        "",
        "| 设计师 ID | 名称 | 擅长 |",
        "|---|---|---|",
    ]
    for item in catalog["items"]:
        lines.append(
            "| "
            + " | ".join(
                _escape_markdown_table_cell(item[field])
                for field in ("id", "name", "specialty_summary")
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _retry_delay(headers: Mapping[str, str] | Message | None, attempt: int) -> float:
    retry_after = headers.get("Retry-After") if headers is not None else None
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return min(0.5 * (2**attempt), 4.0)


def _clean_text(value: str, label: str, *, max_length: int = 50_000) -> str:
    clean = value.strip()
    if not clean:
        raise ClientUsageError(f"{label} 不能为空。")
    if len(clean) > max_length:
        raise ClientUsageError(f"{label} 不能超过 {max_length} 个字符。")
    return clean


def _clean_asset_ids(values: Sequence[str]) -> list[str]:
    clean = [value.strip() for value in values if value.strip()]
    if len(clean) > 50:
        raise ClientUsageError("单次请求最多使用 50 个素材。")
    for value in clean:
        _path_id(value, "素材 ID")
    return list(dict.fromkeys(clean))


def _clean_designer(value: Mapping[str, Any] | None) -> dict[str, str]:
    if not value:
        return {}
    result: dict[str, str] = {}
    for key in ("id", "slug", "name"):
        raw = value.get(key)
        if raw is None:
            continue
        clean = str(raw).strip()
        if not clean:
            continue
        if len(clean) > 200 or "\r" in clean or "\n" in clean:
            raise ClientUsageError(f"设计师 {key} 无效。")
        result[key] = clean
    return result


def _parse_prompt_override(value: str) -> tuple[int, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("提示词覆盖必须使用 INDEX=TEXT 格式")
    raw_index, text = value.split("=", 1)
    try:
        index = _positive_int(raw_index.strip())
    except argparse.ArgumentTypeError as exc:
        raise argparse.ArgumentTypeError("提示词覆盖 INDEX 必须是正整数") from exc
    clean_text = text.strip()
    if not clean_text:
        raise argparse.ArgumentTypeError("提示词覆盖 TEXT 不能为空")
    return index, clean_text


def _load_json_list(path: Path, label: str) -> list[Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ClientUsageError(f"{label}文件不存在：{resolved}")
    if resolved.stat().st_size > MAX_JSON_INPUT_BYTES:
        raise ClientUsageError(f"{label}文件不能超过 2 MB。")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientUsageError(f"{label}文件必须是 UTF-8 JSON。") from exc
    if not isinstance(value, list):
        raise ClientUsageError(f"{label}文件顶层必须是 JSON 数组。")
    return value


def _prompt_overrides_from_json(path: Path) -> list[tuple[int, str]]:
    values = _load_json_list(path, "提示词覆盖")
    result: list[tuple[int, str]] = []
    for position, value in enumerate(values, start=1):
        if not isinstance(value, dict) or set(value) != {"index", "text"}:
            raise ClientUsageError(f"提示词覆盖第 {position} 项必须且只能包含 index、text。")
        if type(value["index"]) is not int or value["index"] <= 0:
            raise ClientUsageError(f"提示词覆盖第 {position} 项的 index 必须是正整数。")
        if not isinstance(value["text"], str):
            raise ClientUsageError(f"提示词覆盖第 {position} 项的 text 必须是字符串。")
        result.append((value["index"], _clean_text(value["text"], "提示词覆盖")))
    return result


def _standalone_entries_from_json(path: Path) -> list[dict[str, Any]]:
    values = _load_json_list(path, "最终提示词")
    if not values or len(values) > 50:
        raise ClientUsageError("最终提示词文件必须包含 1–50 项。")
    entries: list[dict[str, Any]] = []
    allowed = {"text", "asset_ids", "negative_prompt"}
    for position, value in enumerate(values, start=1):
        if not isinstance(value, dict) or "text" not in value or set(value) - allowed:
            raise ClientUsageError(
                f"最终提示词第 {position} 项必须包含 text，且只能包含 text、asset_ids、negative_prompt。"
            )
        if not isinstance(value["text"], str):
            raise ClientUsageError(f"最终提示词第 {position} 项的 text 必须是字符串。")
        raw_assets = value["asset_ids"] if "asset_ids" in value else []
        if not isinstance(raw_assets, list) or not all(isinstance(item, str) for item in raw_assets):
            raise ClientUsageError(f"最终提示词第 {position} 项的 asset_ids 必须是字符串数组。")
        raw_negative = value["negative_prompt"] if "negative_prompt" in value else ""
        if not isinstance(raw_negative, str) or len(raw_negative) > 50_000:
            raise ClientUsageError(f"最终提示词第 {position} 项的 negative_prompt 无效。")
        entries.append(
            {
                "text": _clean_text(value["text"], "最终提示词"),
                "asset_ids": _clean_asset_ids(raw_assets),
                "negative_prompt": raw_negative.strip(),
            }
        )
    return entries


class ApiClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        runtime_profile: RuntimeProfile = GENERIC_RUNTIME_PROFILE,
        allow_anonymous: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        diagnostic: Callable[[str], None] | None = None,
    ) -> None:
        clean_api_key = str(api_key or "").strip()
        if not clean_api_key and not allow_anonymous:
            raise ClientUsageError("SHENHUO_API_KEY 未配置。")
        self._api_key = clean_api_key
        self.runtime_profile = runtime_profile
        self.base_url = (
            _safe_authenticated_base_url(base_url)
            if clean_api_key
            else _safe_base_url(base_url)
        )
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(NoRedirectHandler())
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.diagnostic = diagnostic or (lambda _message: None)

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": (
                f"{self.runtime_profile.skill_name}/{self.runtime_profile.skill_version} "
                f"client/{CLIENT_VERSION}"
            ),
            "X-Shenhuo-Client-Version": CLIENT_VERSION,
            "X-Shenhuo-Skill-Name": self.runtime_profile.skill_name,
            "X-Shenhuo-Skill-Version": self.runtime_profile.skill_version,
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if extra:
            headers.update(extra)
        return headers

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Any | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        idempotency_key: str | None = None,
        max_response_bytes: int | None = None,
    ) -> Any:
        method = method.upper()
        if method == "POST" and not idempotency_key:
            raise ClientUsageError("POST 请求必须包含 Idempotency-Key。")
        if payload is not None and body is not None:
            raise ClientUsageError("请求不能同时包含 JSON 与原始 body。")
        request_body = _canonical_json(payload) if payload is not None else body
        headers: dict[str, str] = {}
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        elif content_type:
            headers["Content-Type"] = content_type
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        for attempt in range(MAX_HTTP_ATTEMPTS):
            request = urllib.request.Request(
                self._url(path), data=request_body, headers=self._headers(headers), method=method
            )
            try:
                response = self.opener.open(request, timeout=self.timeout)
                raw = _read_response_bytes(response, max_response_bytes)
                status = getattr(response, "status", None) or response.getcode()
                return _unwrap_envelope(_json_from_bytes(raw), status=status)
            except ApiError as exc:
                if exc.code == "INVALID_API_RESPONSE":
                    if attempt + 1 < MAX_HTTP_ATTEMPTS:
                        self.diagnostic("神火响应不完整；保持幂等键重试。")
                        self.sleeper(_retry_delay(None, attempt))
                        continue
                    exc.retryable = True
                raise
            except urllib.error.HTTPError as exc:
                try:
                    raw = _read_response_bytes(exc, max_response_bytes)
                except RETRYABLE_TRANSPORT_ERRORS as read_exc:
                    delay = _retry_delay(exc.headers, attempt)
                    if attempt + 1 < MAX_HTTP_ATTEMPTS:
                        if delay > MAX_AUTOMATIC_RETRY_DELAY_SECONDS:
                            error = ApiError(
                                "NETWORK_ERROR",
                                "读取神火错误响应时连接中断。",
                                status=exc.code,
                                retryable=True,
                                retry_after=delay,
                            )
                            raise error from read_exc
                        self.diagnostic("读取神火错误响应时连接中断；保持幂等键重试。")
                        self.sleeper(delay)
                        continue
                    error = ApiError(
                        "NETWORK_ERROR",
                        "读取神火错误响应时连接中断。",
                        status=exc.code,
                        retryable=True,
                    )
                    if exc.headers is not None and exc.headers.get("Retry-After"):
                        error.retry_after = delay
                    raise error from read_exc
                try:
                    api_error = _error_from_payload(_json_from_bytes(raw), status=exc.code)
                except ApiError:
                    api_error = ApiError(
                        f"HTTP_{exc.code}",
                        f"神火返回 HTTP {exc.code}。",
                        status=exc.code,
                        retryable=exc.code == 429 or exc.code >= 500,
                    )
                delay = _retry_delay(exc.headers, attempt)
                if exc.headers is not None and exc.headers.get("Retry-After"):
                    api_error.retry_after = delay
                if api_error.retryable and attempt + 1 < MAX_HTTP_ATTEMPTS:
                    if delay > MAX_AUTOMATIC_RETRY_DELAY_SECONDS:
                        self.diagnostic(f"临时错误 {api_error.code}；服务端等待时间过长，停止自动重试。")
                        raise api_error from None
                    self.diagnostic(f"临时错误 {api_error.code}；保持幂等键重试。")
                    self.sleeper(delay)
                    continue
                raise api_error from None
            except RETRYABLE_TRANSPORT_ERRORS as exc:
                if attempt + 1 < MAX_HTTP_ATTEMPTS:
                    self.diagnostic("网络暂时不可用；保持幂等键重试。")
                    self.sleeper(_retry_delay(None, attempt))
                    continue
                raise ApiError("NETWORK_ERROR", "无法连接神火。", retryable=True) from exc
        raise ApiError("NETWORK_ERROR", "无法连接神火。", retryable=True)

    def account(self) -> Any:
        account = self.request_json("GET", "/account")
        _raise_for_non_active_trial_credential(account)
        return account

    def designer_catalog(self) -> dict[str, Any]:
        if not self.runtime_profile.routing_enabled:
            raise ClientUsageError("当前垂直 Skill 禁止匹配或搜索其他设计师。")
        return _safe_designer_catalog_projection(
            self.request_json(
                "GET",
                "/designers",
                max_response_bytes=MAX_DESIGNER_CATALOG_BYTES,
            )
        )

    def issue_trial_api_key(
        self,
        *,
        channel_code: str,
        installation_id: str,
        installation_token: str,
    ) -> Any:
        payload = {
            "channel_code": channel_code,
            "installation_id": installation_id,
            "installation_token": installation_token,
        }
        return self.request_json(
            "POST",
            "/trial-api-keys",
            payload=payload,
            idempotency_key=_stable_idempotency_key(
                "configure-trial",
                {"channel_code": channel_code, "installation_id": installation_id},
                namespace=self.runtime_profile.skill_name,
            ),
        )

    def upload(
        self,
        *,
        file_path: Path,
        role: str,
        label: str | None,
        idempotency_key: str | None,
    ) -> Any:
        resolved = file_path.expanduser().resolve()
        if not resolved.is_file():
            raise ClientUsageError(f"上传文件不存在：{resolved}")
        if resolved.stat().st_size > MAX_UPLOAD_BYTES:
            raise ClientUsageError("上传文件超过神火 20 MB 单文件限制。")
        clean_role = _clean_text(role, "素材角色", max_length=64)
        if "\r" in clean_role or "\n" in clean_role:
            raise ClientUsageError("素材角色不能包含换行。")
        clean_label = label.strip() if label else None
        if clean_label and (len(clean_label) > 255 or "\r" in clean_label or "\n" in clean_label):
            raise ClientUsageError("素材标签无效。")
        file_bytes = resolved.read_bytes()
        content_hash = hashlib.sha256(file_bytes).hexdigest()
        key_payload = {
            "role": clean_role,
            "label": clean_label,
            "file_name": resolved.name,
            "sha256": content_hash,
            "size": len(file_bytes),
        }
        fields = {"role": clean_role}
        if clean_label:
            fields["label"] = clean_label
        body, content_type = _multipart_body(
            fields=fields,
            field_name="file",
            file_name=resolved.name,
            file_bytes=file_bytes,
            mime_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
            boundary_seed=content_hash,
        )
        return self.request_json(
            "POST",
            "/assets",
            body=body,
            content_type=content_type,
            idempotency_key=_stable_idempotency_key(
                "upload",
                key_payload,
                idempotency_key,
                namespace=self.runtime_profile.skill_name,
            ),
        )

    def route(
        self,
        *,
        brief: str,
        asset_ids: Sequence[str],
        target_image_count: str | int,
        resolution: str,
        aspect_ratio: str,
        idempotency_key: str | None,
    ) -> Any:
        if not self.runtime_profile.routing_enabled:
            raise ClientUsageError("当前垂直 Skill 禁止匹配或搜索其他设计师。")
        payload = {
            "brief": _clean_text(brief, "Brief"),
            "asset_ids": _clean_asset_ids(asset_ids),
            "target_image_count": target_image_count,
            "image_spec": {
                "resolution": _clean_text(resolution, "分辨率", max_length=30),
                "aspect_ratio": _clean_text(aspect_ratio, "宽高比", max_length=30),
            },
        }
        return self.request_json(
            "POST",
            "/routing-evaluations",
            payload=payload,
            idempotency_key=_stable_idempotency_key(
                "route",
                payload,
                idempotency_key,
                namespace=self.runtime_profile.skill_name,
            ),
        )

    def get_route(self, routing_id: str) -> Any:
        if not self.runtime_profile.routing_enabled:
            raise ClientUsageError("当前垂直 Skill 禁止读取匹配结果。")
        return self.request_json("GET", f"/routing-evaluations/{_path_id(routing_id, '匹配 ID')}")

    def create_request(
        self,
        *,
        brief: str,
        asset_ids: Sequence[str],
        designer: Mapping[str, Any] | None,
        routing_evaluation_id: str | None,
        interaction_mode: str,
        target_image_count: str | int,
        resolution: str,
        aspect_ratio: str,
        idempotency_key: str | None,
    ) -> Any:
        clean_designer = _clean_designer(designer)
        fixed_designer = self.runtime_profile.fixed_designer
        if fixed_designer is not None:
            fixed_value = fixed_designer.as_api_value()
            if clean_designer and clean_designer != fixed_value:
                raise ClientUsageError("当前垂直 Skill 不允许覆盖固定设计师。")
            clean_designer = fixed_value
            if routing_evaluation_id:
                raise ClientUsageError("当前垂直 Skill 不允许使用匹配结果创建请求。")
        clean_routing_id = (
            _path_id(routing_evaluation_id, "匹配 ID") if routing_evaluation_id else None
        )
        if not clean_designer.get("id"):
            raise ClientUsageError(
                "创建设计请求必须提供目录或设计师匹配返回的稳定设计师 ID。"
            )
        clean_designer["id"] = _path_id(clean_designer["id"], "设计师 ID")
        if interaction_mode not in {"clarify_if_needed", "complete_brief"}:
            raise ClientUsageError("interaction_mode 无效。")
        payload: dict[str, Any] = {
            "brief": _clean_text(brief, "Brief"),
            "asset_ids": _clean_asset_ids(asset_ids),
            "interaction_mode": interaction_mode,
            "target_image_count": target_image_count,
            "image_spec": {
                "resolution": _clean_text(resolution, "分辨率", max_length=30),
                "aspect_ratio": _clean_text(aspect_ratio, "宽高比", max_length=30),
            },
        }
        if clean_designer:
            payload["designer"] = clean_designer
        if clean_routing_id:
            payload["routing_evaluation_id"] = clean_routing_id
        return self.request_json(
            "POST",
            "/design-requests",
            payload=payload,
            idempotency_key=_stable_idempotency_key(
                "create-request",
                payload,
                idempotency_key,
                namespace=self.runtime_profile.skill_name,
            ),
        )

    def get_request(self, request_id: str) -> Any:
        return self.request_json("GET", f"/design-requests/{_path_id(request_id, '设计请求 ID')}")

    def answer(
        self,
        *,
        request_id: str,
        action_id: str,
        answer: str,
        asset_ids: Sequence[str],
        idempotency_key: str | None,
    ) -> Any:
        clean_id = _path_id(request_id, "设计请求 ID")
        clean_action = _clean_text(action_id, "追问操作 ID", max_length=200)
        if "\r" in clean_action or "\n" in clean_action:
            raise ClientUsageError("追问操作 ID 无效。")
        payload: dict[str, Any] = {"answer": _clean_text(answer, "追问回答", max_length=20_000)}
        clean_assets = _clean_asset_ids(asset_ids)
        if clean_assets:
            payload["asset_ids"] = clean_assets
        return self.request_json(
            "POST",
            f"/design-requests/{clean_id}/clarifications",
            payload=payload,
            idempotency_key=_stable_idempotency_key(
                f"answer:{clean_id}",
                {"required_action_id": clean_action, **payload},
                idempotency_key,
                namespace=self.runtime_profile.skill_name,
            ),
        )

    def generate(
        self,
        *,
        request_id: str,
        prompt_overrides: Sequence[tuple[int, str]],
        idempotency_key: str | None,
    ) -> Any:
        clean_id = _path_id(request_id, "设计请求 ID")
        if len(prompt_overrides) > 50:
            raise ClientUsageError("单次生成最多支持 50 个提示词覆盖。")
        for index, _text in prompt_overrides:
            if type(index) is not int or index <= 0:
                raise ClientUsageError("提示词覆盖 index 必须是正整数。")
        indexes = [index for index, _text in prompt_overrides]
        if len(indexes) != len(set(indexes)):
            raise ClientUsageError("提示词覆盖 index 不能重复。")
        overrides = [
            {"index": index, "text": _clean_text(text, "提示词覆盖")}
            for index, text in prompt_overrides
        ]
        payload = {
            "source": {
                "type": "design_request",
                "design_request_id": clean_id,
                "prompt_overrides": overrides,
            }
        }
        return self.request_json(
            "POST",
            "/image-generations",
            payload=payload,
            idempotency_key=_stable_idempotency_key(
                f"generate:{clean_id}",
                payload,
                idempotency_key,
                namespace=self.runtime_profile.skill_name,
            ),
        )

    def generate_standalone(
        self,
        *,
        prompts: Sequence[str],
        asset_ids: Sequence[str],
        negative_prompt: str,
        designer: Mapping[str, Any] | None,
        resolution: str,
        aspect_ratio: str,
        idempotency_key: str | None,
        prompt_entries: Sequence[Mapping[str, Any]] | None = None,
    ) -> Any:
        if prompt_entries is None:
            clean_prompts = [_clean_text(value, "最终提示词") for value in prompts]
            if not clean_prompts:
                raise ClientUsageError("Standalone 生图至少需要一个最终提示词。")
            if len(clean_prompts) > 50:
                raise ClientUsageError("Standalone 生图最多支持 50 个最终提示词。")
            clean_assets = _clean_asset_ids(asset_ids)
            clean_negative = negative_prompt.strip()
            if len(clean_negative) > 50_000:
                raise ClientUsageError("负向提示词不能超过 50000 个字符。")
            entries = [
                {
                    "text": text,
                    "asset_ids": clean_assets,
                    "negative_prompt": clean_negative,
                }
                for text in clean_prompts
            ]
        else:
            if asset_ids or negative_prompt:
                raise ClientUsageError(
                    "使用 prompt_entries 时，每项素材和负向提示词必须写在条目中；"
                    "不能同时传 asset_ids 或 negative_prompt。"
                )
            entries = [dict(value) for value in prompt_entries]
            if not entries or len(entries) > 50:
                raise ClientUsageError("Standalone 生图必须包含 1–50 个最终提示词。")
        source: dict[str, Any] = {
            "type": "standalone",
            "prompts": entries,
            "image_spec": {
                "resolution": _clean_text(resolution, "分辨率", max_length=30),
                "aspect_ratio": _clean_text(aspect_ratio, "宽高比", max_length=30),
            },
        }
        clean_designer = _clean_designer(designer)
        fixed_designer = self.runtime_profile.fixed_designer
        if fixed_designer is not None:
            fixed_value = fixed_designer.as_api_value()
            if clean_designer and clean_designer != fixed_value:
                raise ClientUsageError("当前垂直 Skill 不允许覆盖固定设计师。")
            clean_designer = fixed_value
        if clean_designer:
            source["designer"] = clean_designer
        payload = {"source": source}
        return self.request_json(
            "POST",
            "/image-generations",
            payload=payload,
            idempotency_key=_stable_idempotency_key(
                "generate-standalone",
                payload,
                idempotency_key,
                namespace=self.runtime_profile.skill_name,
            ),
        )

    def get_generation(self, generation_id: str) -> Any:
        return self.request_json("GET", f"/image-generations/{_path_id(generation_id, '生图 ID')}")

    def poll(
        self,
        *,
        resource_id: str,
        getter: Callable[[str], Any],
        stop_statuses: set[str],
        timeout: float,
        poll_interval: float,
    ) -> Any:
        deadline = self.monotonic() + timeout
        last_status: str | None = None
        last_data: Mapping[str, Any] | None = None
        while True:
            data = getter(resource_id)
            if not isinstance(data, dict):
                raise ApiError("INVALID_API_RESPONSE", "异步资源响应必须是对象。")
            last_data = data
            status = str(data.get("status") or "").strip().lower()
            if not status:
                raise ApiError("INVALID_API_RESPONSE", "异步资源响应缺少 status。")
            if status != last_status:
                self.diagnostic(
                    f"{resource_id}: status={status} stage={str(data.get('stage') or 'unknown')}"
                )
                last_status = status
            if status in stop_statuses:
                return data
            if status not in ACTIVE_STATUSES:
                raise ApiError("UNKNOWN_OPERATION_STATUS", f"不支持的异步状态：{status}")
            if self.monotonic() >= deadline:
                raise PollTimeoutError(resource_id, last_data)
            hint_ms = data.get("poll_after_ms")
            try:
                delay = max(0.1, float(hint_ms) / 1000.0) if hint_ms is not None else poll_interval
            except (TypeError, ValueError):
                delay = poll_interval
            self.sleeper(min(delay, max(0.0, deadline - self.monotonic())))

    def download_variants(
        self,
        *,
        variant_ids: Sequence[str],
        output_dir: Path,
    ) -> list[dict[str, Any]]:
        clean_ids = list(dict.fromkeys(_path_id(item, "结果 ID") for item in variant_ids))
        if not clean_ids:
            raise ClientUsageError("当前生图结果中没有可下载的图片。")
        destination = output_dir.expanduser().resolve()
        destination_existed = destination.exists()
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not destination_existed:
            destination.chmod(0o700)
        downloaded = [self._download_variant(variant_id, destination) for variant_id in clean_ids]
        manifest_path = _unique_path(destination / "manifest.json")
        manifest = {
            "skill": self.runtime_profile.skill_name,
            "skill_version": self.runtime_profile.skill_version,
            "files": downloaded,
        }
        _write_private_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        return downloaded + [{"kind": "manifest", "path": str(manifest_path.resolve())}]

    def _download_variant(self, variant_id: str, destination: Path) -> dict[str, Any]:
        for attempt in range(MAX_HTTP_ATTEMPTS):
            request = urllib.request.Request(
                self._url(f"/results/{variant_id}/download"),
                headers=self._headers({"Accept": "image/*, application/octet-stream"}),
                method="GET",
            )
            try:
                response = self.opener.open(request, timeout=self.timeout)
                content_type = str(
                    response.headers.get("Content-Type") or "application/octet-stream"
                ).split(";", 1)[0].strip().lower()
                if content_type == "application/json":
                    raise _error_from_payload(
                        _json_from_bytes(response.read()), status=getattr(response, "status", None)
                    )
                if not (content_type.startswith("image/") or content_type == "application/octet-stream"):
                    raise ApiError("INVALID_RESULT_CONTENT", f"结果类型不受支持：{content_type}")
                raw_length = str(response.headers.get("Content-Length") or "").strip()
                content_length = None
                if raw_length:
                    try:
                        content_length = int(raw_length)
                    except ValueError:
                        content_length = None
                    if content_length is not None and content_length > MAX_DOWNLOAD_BYTES:
                        raise ApiError(
                            "RESULT_TOO_LARGE",
                            f"结果超过本地 {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB 下载限制。",
                        )
                filename = _response_filename(response.headers, variant_id, content_type)
                target = _unique_path(destination / filename)
                part = destination / f".{target.name}.{uuid4().hex}.part"
                digest = hashlib.sha256()
                size = 0
                try:
                    file_descriptor = os.open(
                        part,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    with os.fdopen(file_descriptor, "wb") as output:
                        while True:
                            chunk = response.read(1024 * 128)
                            if not chunk:
                                break
                            if size + len(chunk) > MAX_DOWNLOAD_BYTES:
                                raise ApiError(
                                    "RESULT_TOO_LARGE",
                                    f"结果超过本地 {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB 下载限制。",
                                )
                            output.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    if content_length is not None and size != content_length:
                        raise http.client.IncompleteRead(
                            b"", expected=max(content_length - size, 0)
                        )
                    os.replace(part, target)
                    target.chmod(0o600)
                finally:
                    if part.exists():
                        part.unlink()
                return {
                    "kind": "image",
                    "variant_id": variant_id,
                    "path": str(target.resolve()),
                    "mime_type": content_type,
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                }
            except urllib.error.HTTPError as exc:
                try:
                    raw = exc.read()
                except RETRYABLE_TRANSPORT_ERRORS as read_exc:
                    delay = _retry_delay(exc.headers, attempt)
                    if attempt + 1 < MAX_HTTP_ATTEMPTS:
                        if delay > MAX_AUTOMATIC_RETRY_DELAY_SECONDS:
                            error = ApiError(
                                "NETWORK_ERROR",
                                "读取神火下载错误响应时连接中断。",
                                status=exc.code,
                                retryable=True,
                                retry_after=delay,
                            )
                            raise error from read_exc
                        self.diagnostic("读取神火下载错误响应时连接中断；正在重试同一地址。")
                        self.sleeper(delay)
                        continue
                    error = ApiError(
                        "NETWORK_ERROR",
                        "读取神火下载错误响应时连接中断。",
                        status=exc.code,
                        retryable=True,
                    )
                    if exc.headers is not None and exc.headers.get("Retry-After"):
                        error.retry_after = delay
                    raise error from read_exc
                try:
                    api_error = _error_from_payload(_json_from_bytes(raw), status=exc.code)
                except ApiError:
                    api_error = ApiError(
                        f"HTTP_{exc.code}",
                        f"下载结果时神火返回 HTTP {exc.code}。",
                        status=exc.code,
                        retryable=exc.code == 429 or exc.code >= 500,
                    )
                delay = _retry_delay(exc.headers, attempt)
                if exc.headers is not None and exc.headers.get("Retry-After"):
                    api_error.retry_after = delay
                if api_error.retryable and attempt + 1 < MAX_HTTP_ATTEMPTS:
                    if delay > MAX_AUTOMATIC_RETRY_DELAY_SECONDS:
                        self.diagnostic(f"下载错误 {api_error.code}；等待时间过长，停止自动重试。")
                        raise api_error from None
                    self.diagnostic(f"下载错误 {api_error.code}；保持鉴权重试。")
                    self.sleeper(delay)
                    continue
                raise api_error from None
            except RETRYABLE_TRANSPORT_ERRORS as exc:
                if attempt + 1 < MAX_HTTP_ATTEMPTS:
                    self.diagnostic("下载网络暂时不可用；正在重试。")
                    self.sleeper(_retry_delay(None, attempt))
                    continue
                raise ApiError("NETWORK_ERROR", "无法下载神火结果。", retryable=True) from exc
        raise ApiError("NETWORK_ERROR", "无法下载神火结果。", retryable=True)


def _multipart_body(
    *,
    fields: Mapping[str, str],
    field_name: str,
    file_name: str,
    file_bytes: bytes,
    mime_type: str,
    boundary_seed: str,
) -> tuple[bytes, str]:
    boundary = f"----Shenhuo{boundary_seed[:24]}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        safe_key = _header_token(key)
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{safe_key}"\r\n\r\n'.encode("ascii"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    safe_field = _header_token(field_name)
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file_name).name) or "upload.bin"
    encoded_name = urllib.parse.quote(Path(file_name).name.encode("utf-8"))
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("ascii"),
            (
                f'Content-Disposition: form-data; name="{safe_field}"; filename="{ascii_name}"; '
                f"filename*=UTF-8''{encoded_name}\r\n"
            ).encode("ascii"),
            f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"),
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _header_token(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ClientUsageError("multipart 字段名无效。")
    return value


def _path_id(value: str, label: str) -> str:
    clean = value.strip()
    if not clean or not re.fullmatch(r"[A-Za-z0-9_-]+", clean):
        raise ClientUsageError(f"{label} 无效。")
    return clean


def _variant_ids_from_generation(data: Any) -> list[str]:
    if not isinstance(data, dict):
        raise ApiError("INVALID_API_RESPONSE", "生图响应必须是对象。")
    results = data.get("results") or []
    if not isinstance(results, list):
        raise ApiError("INVALID_API_RESPONSE", "生图 results 必须是数组。")
    variant_ids: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        value = item.get("variant_id") or item.get("id")
        if value:
            variant_ids.append(str(value))
    return variant_ids


def _response_filename(headers: Mapping[str, str] | Message, variant_id: str, content_type: str) -> str:
    disposition = str(headers.get("Content-Disposition") or "")
    filename = ""
    if disposition:
        message = Message()
        message["content-disposition"] = disposition
        filename = message.get_filename() or ""
    filename = Path(filename).name
    if not filename:
        extension = mimetypes.guess_extension(content_type) or ".bin"
        filename = f"{variant_id}{'.jpg' if extension == '.jpe' else extension}"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return safe or f"{variant_id}.bin"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ClientUsageError(f"无法为 {path.name} 选择唯一输出路径。")


def _write_private_text(path: Path, content: str) -> None:
    file_descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
            file_descriptor = -1
            output.write(content)
        path.chmod(0o600)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)


def _atomic_replace_private_text(path: Path, content: str) -> None:
    if not path.parent.is_dir():
        raise ClientUsageError("Skill .env 所在目录不存在。")
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    file_descriptor = -1
    try:
        file_descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
            file_descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except OSError as exc:
        raise ClientUsageError("无法安全写入 Skill .env。") from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_replace_catalog_text(path: Path, content: str) -> None:
    """在固定目录内原子刷新公开目录；不跟随父目录或目标符号链接。"""

    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ClientUsageError("设计师目录所在 references 必须是普通目录。")

    directory_descriptor = -1
    file_descriptor = -1
    temporary_name = f".{path.name}.{uuid4().hex}.tmp"
    try:
        directory_descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            existing = os.stat(
                path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ClientUsageError("当前设计师目录缓存必须是普通文件，不能是符号链接。")

        file_descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
            file_descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
    except ClientUsageError:
        raise
    except OSError as exc:
        raise ClientUsageError("无法安全刷新本地设计师目录。") from exc
    finally:
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if directory_descriptor >= 0:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            try:
                os.close(directory_descriptor)
            except OSError:
                pass


def _refresh_designer_catalog(client: ApiClient, path: Path) -> dict[str, Any]:
    catalog = client.designer_catalog()
    _atomic_replace_catalog_text(path, _render_designer_catalog(catalog))
    return {
        "path": str(path),
        "generated_at": catalog["generated_at"],
        "count": len(catalog["items"]),
    }


def _add_designer_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--designer-id")
    parser.add_argument("--designer-slug")
    parser.add_argument("--designer-name")


def _designer_from_args(args: argparse.Namespace) -> dict[str, str]:
    return _clean_designer(
        {
            "id": getattr(args, "designer_id", None),
            "slug": getattr(args, "designer_slug", None),
            "name": getattr(args, "designer_name", None),
        }
    )


def build_parser(
    runtime_profile: RuntimeProfile = GENERIC_RUNTIME_PROFILE,
) -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="shenhuo_client.py", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("configure-key", add_help=False)
    subparsers.add_parser("configure-trial", add_help=False)
    subparsers.add_parser("account", add_help=False)

    upload = subparsers.add_parser("upload", add_help=False)
    upload.add_argument("--file", required=True, type=Path)
    upload.add_argument("--role", default="reference_image")
    upload.add_argument("--label")
    upload.add_argument("--idempotency-key")

    if runtime_profile.routing_enabled:
        subparsers.add_parser("refresh-designers", add_help=False)

        route = subparsers.add_parser("route", add_help=False)
        route.add_argument("--brief", required=True)
        route.add_argument("--asset-id", action="append", default=[])
        route.add_argument("--target-image-count", default="auto", type=_target_image_count)
        route.add_argument("--resolution", default="1K")
        route.add_argument("--aspect-ratio", default="auto")
        route.add_argument("--idempotency-key")

        wait_route = subparsers.add_parser("wait-route", add_help=False)
        wait_route.add_argument("--routing-id", required=True)
        wait_route.add_argument("--timeout", default=900.0, type=_positive_float)
        wait_route.add_argument("--poll-interval", type=_positive_float)

    create = subparsers.add_parser("create-request", add_help=False)
    create.add_argument("--brief", required=True)
    create.add_argument("--asset-id", action="append", default=[])
    if runtime_profile.fixed_designer is None:
        _add_designer_arguments(create)
        create.add_argument("--routing-evaluation-id")
    create.add_argument(
        "--interaction-mode",
        choices=("clarify_if_needed", "complete_brief"),
        default="clarify_if_needed",
    )
    create.add_argument("--target-image-count", default="auto", type=_target_image_count)
    create.add_argument("--resolution", default="1K")
    create.add_argument("--aspect-ratio", default="auto")
    create.add_argument("--idempotency-key")

    wait_request = subparsers.add_parser("wait-request", add_help=False)
    wait_request.add_argument("--request-id", required=True)
    wait_request.add_argument("--timeout", default=900.0, type=_positive_float)
    wait_request.add_argument("--poll-interval", type=_positive_float)

    answer = subparsers.add_parser("answer", add_help=False)
    answer.add_argument("--request-id", required=True)
    answer.add_argument("--action-id", required=True)
    answer.add_argument("--answer", required=True)
    answer.add_argument("--asset-id", action="append", default=[])
    answer.add_argument("--idempotency-key")

    generate = subparsers.add_parser("generate", add_help=False)
    generate.add_argument("--request-id", required=True)
    override_source = generate.add_mutually_exclusive_group()
    override_source.add_argument("--prompt-override", action="append", default=[], type=_parse_prompt_override)
    override_source.add_argument("--prompt-overrides-file", type=Path)
    generate.add_argument("--idempotency-key")

    standalone = subparsers.add_parser("generate-standalone", add_help=False)
    prompt_source = standalone.add_mutually_exclusive_group(required=True)
    prompt_source.add_argument("--prompt", action="append")
    prompt_source.add_argument("--prompts-file", type=Path)
    standalone.add_argument("--asset-id", action="append", default=[])
    standalone.add_argument("--negative-prompt", default="")
    if runtime_profile.fixed_designer is None:
        _add_designer_arguments(standalone)
    standalone.add_argument("--resolution", default="1K")
    standalone.add_argument("--aspect-ratio", default="auto")
    standalone.add_argument("--idempotency-key")

    wait_generation = subparsers.add_parser("wait-generation", add_help=False)
    wait_generation.add_argument("--generation-id", required=True)
    wait_generation.add_argument("--timeout", default=1800.0, type=_positive_float)
    wait_generation.add_argument("--poll-interval", type=_positive_float)

    download = subparsers.add_parser("download", add_help=False)
    source = download.add_mutually_exclusive_group(required=True)
    source.add_argument("--generation-id")
    source.add_argument("--variant-id", action="append")
    download.add_argument("--output-dir", required=True, type=Path)
    return parser


def _execute(
    args: argparse.Namespace,
    client: ApiClient,
    default_poll_interval: float,
    runtime_profile: RuntimeProfile = GENERIC_RUNTIME_PROFILE,
    designer_catalog_path: Path = SKILL_DESIGNER_CATALOG_CURRENT_PATH,
) -> Any:
    if args.command == "account":
        return client.account()
    if args.command == "refresh-designers":
        return _refresh_designer_catalog(client, designer_catalog_path)
    if args.command == "upload":
        return client.upload(
            file_path=args.file,
            role=args.role,
            label=args.label,
            idempotency_key=args.idempotency_key,
        )
    if args.command == "route":
        return client.route(
            brief=args.brief,
            asset_ids=args.asset_id,
            target_image_count=args.target_image_count,
            resolution=args.resolution,
            aspect_ratio=args.aspect_ratio,
            idempotency_key=args.idempotency_key,
        )
    if args.command == "wait-route":
        return client.poll(
            resource_id=args.routing_id,
            getter=client.get_route,
            stop_statuses=STOP_ROUTE_STATUSES,
            timeout=args.timeout,
            poll_interval=args.poll_interval or default_poll_interval,
        )
    if args.command == "create-request":
        return client.create_request(
            brief=args.brief,
            asset_ids=args.asset_id,
            designer=(
                runtime_profile.fixed_designer.as_api_value()
                if runtime_profile.fixed_designer is not None
                else _designer_from_args(args)
            ),
            routing_evaluation_id=getattr(args, "routing_evaluation_id", None),
            interaction_mode=args.interaction_mode,
            target_image_count=args.target_image_count,
            resolution=args.resolution,
            aspect_ratio=args.aspect_ratio,
            idempotency_key=args.idempotency_key,
        )
    if args.command == "wait-request":
        return client.poll(
            resource_id=args.request_id,
            getter=client.get_request,
            stop_statuses=STOP_REQUEST_STATUSES,
            timeout=args.timeout,
            poll_interval=args.poll_interval or default_poll_interval,
        )
    if args.command == "answer":
        return client.answer(
            request_id=args.request_id,
            action_id=args.action_id,
            answer=args.answer,
            asset_ids=args.asset_id,
            idempotency_key=args.idempotency_key,
        )
    if args.command == "generate":
        return client.generate(
            request_id=args.request_id,
            prompt_overrides=(
                _prompt_overrides_from_json(args.prompt_overrides_file)
                if args.prompt_overrides_file
                else args.prompt_override
            ),
            idempotency_key=args.idempotency_key,
        )
    if args.command == "generate-standalone":
        if args.prompts_file and (args.asset_id or args.negative_prompt):
            raise ClientUsageError(
                "使用 --prompts-file 时，每项素材和负向提示词必须写在 JSON 中；不能同时传 --asset-id 或 --negative-prompt。"
            )
        prompt_entries = (
            _standalone_entries_from_json(args.prompts_file) if args.prompts_file else None
        )
        return client.generate_standalone(
            prompts=args.prompt or [],
            asset_ids=args.asset_id,
            negative_prompt=args.negative_prompt,
            designer=(
                runtime_profile.fixed_designer.as_api_value()
                if runtime_profile.fixed_designer is not None
                else _designer_from_args(args)
            ),
            resolution=args.resolution,
            aspect_ratio=args.aspect_ratio,
            idempotency_key=args.idempotency_key,
            prompt_entries=prompt_entries,
        )
    if args.command == "wait-generation":
        return client.poll(
            resource_id=args.generation_id,
            getter=client.get_generation,
            stop_statuses=STOP_GENERATION_STATUSES,
            timeout=args.timeout,
            poll_interval=args.poll_interval or default_poll_interval,
        )
    if args.command == "download":
        variant_ids = args.variant_id or []
        if args.generation_id:
            variant_ids = _variant_ids_from_generation(client.get_generation(args.generation_id))
        return {"files": client.download_variants(variant_ids=variant_ids, output_dir=args.output_dir)}
    raise ClientUsageError(f"不支持的命令：{args.command}")


def _configure_trial_api_key(
    *,
    process_environment: Mapping[str, str],
    env_path: Path,
    channel_path: Path,
    bootstrap_path: Path,
    opener: Any | None,
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
    stderr: TextIO,
    runtime_profile: RuntimeProfile,
) -> dict[str, Any]:
    if str(process_environment.get("SHENHUO_API_KEY") or "").strip():
        raise ProcessEnvironmentOverrideError(
            "进程环境中的 SHENHUO_API_KEY 会覆盖本地试用 Key；请先清除该环境变量。"
        )

    local_environment = _read_skill_env(env_path) if env_path.exists() else {}
    if str(local_environment.get("SHENHUO_API_KEY") or "").strip():
        raise ApiKeyAlreadyConfiguredError(
            "本地已配置 API Key；请先运行 account 验证，不能静默覆盖现有凭证。"
        )

    channel_code = _read_channel_code(channel_path)
    base_url = _safe_trial_base_url(
        str(
            process_environment.get("SHENHUO_API_BASE_URL")
            or local_environment.get("SHENHUO_API_BASE_URL")
            or DEFAULT_BASE_URL
        )
    )
    existing_installation_id = str(
        local_environment.get("SHENHUO_TRIAL_INSTALLATION_ID") or ""
    ).strip()
    installation_id, installation_token = _read_or_create_trial_bootstrap(
        bootstrap_path,
        _clean_installation_id(existing_installation_id)
        if existing_installation_id
        else None,
    )
    timeout = _env_float(
        process_environment,
        "SHENHUO_TIMEOUT_SECONDS",
        DEFAULT_TIMEOUT_SECONDS,
    )
    diagnostic = lambda message: print(message, file=stderr)
    anonymous_client = ApiClient(
        api_key=None,
        allow_anonymous=True,
        base_url=base_url,
        runtime_profile=runtime_profile,
        timeout=timeout,
        opener=opener,
        sleeper=sleeper,
        monotonic=monotonic,
        diagnostic=diagnostic,
    )
    issued = anonymous_client.issue_trial_api_key(
        channel_code=channel_code,
        installation_id=installation_id,
        installation_token=installation_token,
    )
    if not isinstance(issued, dict):
        raise ApiError("INVALID_API_RESPONSE", "试用 Key 签发响应必须是对象。")
    raw_api_key = _clean_api_key(str(issued.get("api_key") or ""))
    configured = _configure_skill_api_key(
        raw_api_key,
        env_path,
        trial_installation_id=installation_id,
    )

    authenticated_client = ApiClient(
        api_key=raw_api_key,
        base_url=base_url,
        runtime_profile=runtime_profile,
        timeout=timeout,
        opener=opener,
        sleeper=sleeper,
        monotonic=monotonic,
        diagnostic=diagnostic,
    )
    try:
        account = authenticated_client.account()
        safe_account = _safe_trial_account_projection(account)
    finally:
        # Key 已原子写入 .env 后，无论 account 验证结果如何都不再保留 bootstrap token。
        # 不删除 .env：验证失败不等于可以安全覆盖或丢弃刚签发的凭证。
        _remove_private_file(bootstrap_path)
    safe_account.update(configured)
    safe_account["process_environment_override_present"] = False
    return safe_account


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    skill_env_path: Path | None = None,
    skill_channel_path: Path | None = None,
    trial_bootstrap_path: Path | None = None,
    skill_root: Path | None = None,
    runtime_profile_path: Path | None = None,
    opener: Any | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    try:
        command_argv = list(argv) if argv is not None else sys.argv[1:]
        if command_argv[:1] == ["configure-key"] and len(command_argv) != 1:
            raise ClientUsageError("configure-key 不接受命令行参数；请通过标准输入提供 API Key。")
        if command_argv[:1] == ["configure-trial"] and len(command_argv) != 1:
            raise ClientUsageError("configure-trial 不接受命令行参数。")
        resolved_skill_root = (skill_root or SKILL_ROOT).resolve()
        verify_package_integrity(resolved_skill_root)
        runtime_profile = load_runtime_profile(
            runtime_profile_path or resolved_skill_root / "runtime-profile.json"
        )
        args = build_parser(runtime_profile).parse_args(command_argv)
        resolved_env_path = skill_env_path or (
            SKILL_ENV_PATH if skill_root is None else resolved_skill_root / ".env"
        )
        if args.command == "configure-key":
            data = _configure_skill_api_key(
                _read_api_key(stdin or sys.stdin, stderr),
                resolved_env_path,
            )
            process_environment = os.environ if environ is None else environ
            data["process_environment_override_present"] = bool(
                str(process_environment.get("SHENHUO_API_KEY") or "").strip()
            )
        elif args.command == "configure-trial":
            data = _configure_trial_api_key(
                process_environment=os.environ if environ is None else environ,
                env_path=resolved_env_path,
                channel_path=skill_channel_path
                or (SKILL_CHANNEL_PATH if skill_root is None else resolved_skill_root / "channel.json"),
                bootstrap_path=(
                    trial_bootstrap_path
                    or (
                        TRIAL_BOOTSTRAP_PATH
                        if skill_root is None
                        else resolved_skill_root / ".trial-bootstrap.json"
                    )
                ),
                opener=opener,
                sleeper=sleeper,
                monotonic=monotonic,
                stderr=stderr,
                runtime_profile=runtime_profile,
            )
        else:
            environment = _runtime_environment(resolved_env_path) if environ is None else environ
            api_key = str(environment.get("SHENHUO_API_KEY") or "").strip()
            if not api_key:
                raise ClientUsageError("SHENHUO_API_KEY 未配置。")
            client = ApiClient(
                api_key=api_key,
                base_url=_safe_base_url(
                    str(environment.get("SHENHUO_API_BASE_URL") or DEFAULT_BASE_URL)
                ),
                runtime_profile=runtime_profile,
                timeout=_env_float(environment, "SHENHUO_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
                opener=opener,
                sleeper=sleeper,
                monotonic=monotonic,
                diagnostic=lambda message: print(message, file=stderr),
            )
            data = _execute(
                args,
                client,
                _env_float(
                    environment,
                    "SHENHUO_POLL_INTERVAL_SECONDS",
                    DEFAULT_POLL_INTERVAL_SECONDS,
                ),
                runtime_profile,
                resolved_skill_root / "references" / "designer-catalog.current.md",
            )
        output = {"ok": True, "command": args.command, "data": data}
        exit_code = 0
    except ClientUsageError as exc:
        code = exc.code
        if code == "CLIENT_USAGE_ERROR" and "SHENHUO_API_KEY 未配置" in str(exc):
            code = "SHENHUO_API_KEY_MISSING"
        output = {"ok": False, "error": {"code": code, "message": str(exc), "retryable": False}}
        exit_code = 2
    except PollTimeoutError as exc:
        output = {
            "ok": False,
            "error": {
                "code": exc.code,
                "message": str(exc),
                "retryable": True,
                "details": {"resource_id": exc.resource_id, "last_data": exc.last_data},
            },
        }
        exit_code = 5
    except ApiError as exc:
        error: dict[str, Any] = {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
        }
        if exc.status is not None:
            error["status"] = exc.status
        if exc.details is not None:
            error["details"] = exc.details
        if exc.retry_after is not None:
            error["retry_after"] = exc.retry_after
        output = {"ok": False, "error": error}
        exit_code = 4
    except Exception:
        output = {
            "ok": False,
            "error": {
                "code": "CLIENT_INTERNAL_ERROR",
                "message": "神火客户端遇到未预期的本地错误。",
                "retryable": False,
            },
        }
        exit_code = 70
    stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()
    return exit_code


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
