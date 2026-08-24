"""Sensitive data redaction helpers.

脱敏模块应该放在所有持久化/日志出口之前调用。它不负责权限判断，
只负责把已经进入内存的数据中的 key/token/password 等敏感值替换掉。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


REDACTED = "***REDACTED***"
SENSITIVE_KEYWORDS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "client_secret",
    "cookie",
    "database_url",
    "db_password",
    "dsn",
    "key",
    "openai_api_key",
    "password",
    "postgres_uri",
    "private_key",
    "secret",
    "token",
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(token\s*[:=]\s*)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(secret\s*[:=]\s*)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
)


def redact(value: Any) -> Any:
    """Return a copy of value with sensitive fields and substrings redacted."""
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_sensitive_key(str(key)) else redact(item)
            for key, item in value.items()
        }

    if isinstance(value, str):
        return redact_text(value)

    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact(item) for item in value]

    return value


def redact_text(text: str) -> str:
    """Redact secret-looking substrings in free-form text."""
    redacted = text
    for pattern in SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(_replace_match, redacted)
    return redacted


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(keyword in normalized for keyword in SENSITIVE_KEYWORDS)


def _replace_match(match: re.Match[str]) -> str:
    if match.lastindex:
        return f"{match.group(1)}{REDACTED}"
    return REDACTED
