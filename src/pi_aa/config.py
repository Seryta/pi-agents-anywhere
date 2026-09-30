"""Pi runtime configuration values and JSON schema."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from connector.runtime_protocol import RuntimeInvalidRequestError

logger = logging.getLogger(__name__)

CONFIG_SCHEMA_REVISION = 1
DEFAULT_EXECUTABLE = "pi"
DEFAULT_SESSIONS_DIR = "~/.pi/agent/sessions"
DEFAULT_CWD = "~"
DEFAULT_REQUEST_TIMEOUT_MS = 60_000
PROBE_TIMEOUT_SECONDS = 20.0


async def probe_pi_version(executable: str) -> str | None:
    """Return the ``pi --version`` string, or None when the binary is missing."""

    resolved = shutil.which(executable) if not Path(executable).is_absolute() else executable
    if resolved is None or not Path(resolved).exists():
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            resolved,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.warning("cannot launch pi at %s: %s", executable, exc)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=PROBE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        logger.warning("pi --version timed out for %s", executable)
        return None
    if process.returncode != 0:
        logger.warning(
            "pi --version failed for %s: %s",
            executable,
            stderr.decode("utf-8", errors="replace").strip(),
        )
        return None
    version = stdout.decode("utf-8", errors="replace").strip()
    return version or None


def expand_sessions_dir(raw: str | None) -> Path:
    value = raw or os.environ.get("PI_CODING_AGENT_SESSION_DIR") or DEFAULT_SESSIONS_DIR
    return Path(value).expanduser()


def pi_config_schema() -> dict[str, Any]:
    positive_timeout = {"type": "integer", "minimum": 1_000, "maximum": 600_000}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "executablePath": {
                "type": "string",
                "minLength": 1,
                "title": "pi 可执行文件",
                "description": "pi CLI 的路径或命令名。",
                "default": DEFAULT_EXECUTABLE,
            },
            "sessionsDir": {
                "type": "string",
                "minLength": 1,
                "title": "会话目录",
                "description": "Pi 会话文件目录，默认 ~/.pi/agent/sessions。",
                "default": DEFAULT_SESSIONS_DIR,
            },
            "defaultCwd": {
                "type": "string",
                "minLength": 1,
                "title": "默认工作目录",
                "description": "创建会话且未指定工作目录时使用。",
                "default": DEFAULT_CWD,
            },
            "requestTimeoutMs": {
                **positive_timeout,
                "title": "RPC 超时（毫秒）",
                "description": "单条 RPC 命令等待响应的最长时间。",
                "default": DEFAULT_REQUEST_TIMEOUT_MS,
            },
        },
        "additionalProperties": False,
    }


def default_config_values() -> dict[str, Any]:
    return {
        "executablePath": DEFAULT_EXECUTABLE,
        "sessionsDir": DEFAULT_SESSIONS_DIR,
        "defaultCwd": DEFAULT_CWD,
        "requestTimeoutMs": DEFAULT_REQUEST_TIMEOUT_MS,
    }


def normalized_config_values(raw: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {**default_config_values(), **raw}
    executable = values.get("executablePath")
    if not isinstance(executable, str) or not executable.strip():
        raise RuntimeInvalidRequestError("executablePath must be a non-empty string")
    values["executablePath"] = executable.strip()

    sessions_dir = values.get("sessionsDir")
    if not isinstance(sessions_dir, str) or not sessions_dir.strip():
        raise RuntimeInvalidRequestError("sessionsDir must be a non-empty string")
    values["sessionsDir"] = str(expand_sessions_dir(sessions_dir))

    default_cwd = values.get("defaultCwd")
    if not isinstance(default_cwd, str) or not default_cwd.strip():
        raise RuntimeInvalidRequestError("defaultCwd must be a non-empty string")
    values["defaultCwd"] = str(Path(default_cwd).expanduser())

    timeout = values.get("requestTimeoutMs")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1_000 <= timeout <= 600_000:
        raise RuntimeInvalidRequestError(
            "requestTimeoutMs must be an integer between 1000 and 600000"
        )
    return values
