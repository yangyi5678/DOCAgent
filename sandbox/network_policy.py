"""Network sandbox policy defaults."""

from __future__ import annotations

from urllib.parse import urlparse
from typing import Any


DEFAULT_NETWORK_POLICY = {
    "allow_network": False,
    "allowed_domains": [],
    "blocked_domains": [],
    "timeout_seconds": 30,
    "max_response_size": 10 * 1024 * 1024,
}


TOOL_NETWORK_POLICIES: dict[str, dict[str, Any]] = {
    "minimax_tts": {
        "allow_network": True,
        "allowed_domains": ["api.minimaxi.com"],
        "timeout_seconds": 180,
    },
    "minimax_tts_tool": {
        "allow_network": True,
        "allowed_domains": ["api.minimaxi.com"],
        "timeout_seconds": 180,
    },
    "minimax_tts_core": {
        "allow_network": True,
        "allowed_domains": ["api.minimaxi.com"],
        "timeout_seconds": 180,
    },
    "minimax_tts_http": {
        "allow_network": True,
        "allowed_domains": ["api.minimaxi.com"],
        "timeout_seconds": 180,
    },
    "minimax_generate_image": {
        "allow_network": True,
        "allowed_domains": ["api.minimaxi.com"],
        "timeout_seconds": 300,
    },
    "minimax_generate_image_tool": {
        "allow_network": True,
        "allowed_domains": ["api.minimaxi.com"],
        "timeout_seconds": 300,
    },
    "minimax_generate_image_core": {
        "allow_network": True,
        "allowed_domains": ["api.minimaxi.com"],
        "timeout_seconds": 300,
    },
    "minimax_image_http": {
        "allow_network": True,
        "allowed_domains": ["api.minimaxi.com"],
        "timeout_seconds": 300,
    },
}

TOOL_NETWORK_URLS = {
    "minimax_tts": ["https://api.minimaxi.com/v1/t2a_v2"],
    "minimax_tts_tool": ["https://api.minimaxi.com/v1/t2a_v2"],
    "minimax_tts_core": ["https://api.minimaxi.com/v1/t2a_v2"],
    "minimax_tts_http": ["https://api.minimaxi.com/v1/t2a_v2"],
    "minimax_generate_image": ["https://api.minimaxi.com/v1/image_generation"],
    "minimax_generate_image_tool": ["https://api.minimaxi.com/v1/image_generation"],
    "minimax_generate_image_core": ["https://api.minimaxi.com/v1/image_generation"],
    "minimax_image_http": ["https://api.minimaxi.com/v1/image_generation"],
}


def build_network_policy(
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build network policy for one executable node."""
    return {
        **DEFAULT_NETWORK_POLICY,
        **TOOL_NETWORK_POLICIES.get(tool_name or "", {}),
    }


def validate_network_access_for_node(node: dict[str, Any]) -> None:
    """Validate network access for one executable DAG node before dispatch."""
    tool_name = node.get("tool")
    if tool_name not in TOOL_NETWORK_URLS:
        return

    tool_input = node.get("input") or {}
    if not isinstance(tool_input, dict):
        raise TypeError("节点 input 必须是 dict。")

    sandbox = node.get("sandbox") or {}
    policy = sandbox.get("network") or build_network_policy(tool_name, tool_input)
    for url in TOOL_NETWORK_URLS.get(tool_name, []):
        ensure_url_allowed(url, policy)


def ensure_url_allowed(url: str, policy: dict[str, Any] | None = None) -> None:
    """Raise PermissionError if one URL cannot be requested under policy."""
    policy = policy or DEFAULT_NETWORK_POLICY
    if not policy.get("allow_network", False):
        raise PermissionError("当前 network policy 未允许联网。")

    host = _extract_host(url)
    blocked_domains = policy.get("blocked_domains") or []
    allowed_domains = policy.get("allowed_domains") or []

    if any(_domain_matches(host, domain) for domain in blocked_domains):
        raise PermissionError(f"网络域名在禁止列表内: {host}")
    if allowed_domains and not any(_domain_matches(host, domain) for domain in allowed_domains):
        raise PermissionError(f"网络域名不在允许列表内: {host}")


def ensure_response_allowed(response: Any, policy: dict[str, Any] | None = None) -> None:
    """Raise PermissionError if one HTTP response is larger than policy allows."""
    policy = policy or DEFAULT_NETWORK_POLICY
    max_response_size = policy.get("max_response_size")
    if max_response_size is None:
        return

    header_size = response.headers.get("Content-Length") if hasattr(response, "headers") else None
    if header_size and int(header_size) > int(max_response_size):
        raise PermissionError(f"网络响应大小超过限制: {header_size} bytes > {max_response_size} bytes")

    content = getattr(response, "content", b"")
    if content is not None and len(content) > int(max_response_size):
        raise PermissionError(f"网络响应大小超过限制: {len(content)} bytes > {max_response_size} bytes")


def _extract_host(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"URL 不合法: {url}")
    return parsed.hostname or parsed.netloc


def _domain_matches(host: str, domain: str) -> bool:
    clean = domain.lower().strip()
    host = host.lower().strip()
    return host == clean or host.endswith(f".{clean}")
