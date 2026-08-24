from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from sandbox.network_policy import ensure_response_allowed, ensure_url_allowed
from sandbox.quota_policy import ensure_quota_allowed

try:
    from langchain.tools import tool
except ImportError:  # pragma: no cover
    tool = None


API_URL = "https://api.minimaxi.com/v1/image_generation"
MODEL_NAME = "image-01"
DEFAULT_OUTPUT_DIR = "outputs/minimax_images"
DEFAULT_FILE_PREFIX = "minimax_image"
DEFAULT_IMAGE_FORMAT = "jpeg"
DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_RESPONSE_FORMAT = "base64"


def build_subject_reference(
    *,
    character_image_url: str | None = None,
    subject_type: str = "character",
) -> list[dict[str, str]] | None:
    if not character_image_url:
        return None

    image_url = character_image_url.strip()
    if not image_url:
        return None

    return [
        {
            "type": subject_type,
            "image_file": image_url,
        }
    ]


def build_payload(
    prompt: str,
    *,
    model: str = MODEL_NAME,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    response_format: str = DEFAULT_RESPONSE_FORMAT,
    subject_reference: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "response_format": response_format,
    }

    if subject_reference:
        payload["subject_reference"] = subject_reference

    return payload


def save_base64_images(
    image_base64_list: list[str],
    *,
    output_dir: str,
    file_prefix: str,
    image_format: str,
) -> list[str]:
    output_paths: list[str] = []
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for i, image_b64 in enumerate(image_base64_list):
        output_path = out_dir / f"{file_prefix}-{i}.{image_format}"
        output_path.write_bytes(base64.b64decode(image_b64))
        output_paths.append(str(output_path.resolve()))

    return output_paths


def generate_image(
    prompt: str,
    *,
    model: str = MODEL_NAME,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    response_format: str = DEFAULT_RESPONSE_FORMAT,
    subject_reference: list[dict[str, str]] | None = None,
    network_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("缺少依赖 requests，请先安装: pip install requests") from exc

    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        raise RuntimeError("未找到 MINIMAX_API_KEY。请先在环境变量或 .env 中设置。")

    clean_prompt = prompt.strip()
    if not clean_prompt:
        raise ValueError("prompt 不能为空。")

    payload = build_payload(
        prompt=clean_prompt,
        model=model,
        aspect_ratio=aspect_ratio,
        response_format=response_format,
        subject_reference=subject_reference,
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
    }

    ensure_url_allowed(API_URL, network_policy)
    timeout = (network_policy or {}).get("timeout_seconds", 300)
    response = requests.post(API_URL, headers=headers, json=payload, timeout=timeout)
    ensure_response_allowed(response, network_policy)
    response.raise_for_status()
    data = response.json()

    base_resp = data.get("base_resp") or {}
    if base_resp and base_resp.get("status_code") not in (0, None):
        raise RuntimeError(f"MiniMax 文生图返回错误: {data}")

    return data


def minimax_generate_image_core(
    prompt: str,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    file_prefix: str = DEFAULT_FILE_PREFIX,
    image_format: str = DEFAULT_IMAGE_FORMAT,
    model: str = MODEL_NAME,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    response_format: str = DEFAULT_RESPONSE_FORMAT,
    character_image_url: str | None = None,
    subject_type: str = "character",
    network_policy: dict[str, Any] | None = None,
    quota_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """根据提示词生成图片，可选传入参考人物图，返回结构化结果。"""
    ensure_quota_allowed(
        "minimax_generate_image",
        quota_policy,
        api_calls=1,
        image_generations=1,
    )
    subject_reference = build_subject_reference(
        character_image_url=character_image_url,
        subject_type=subject_type,
    )

    data = generate_image(
        prompt=prompt,
        model=model,
        aspect_ratio=aspect_ratio,
        response_format=response_format,
        subject_reference=subject_reference,
        network_policy=network_policy,
    )

    image_base64_list = ((data.get("data") or {}).get("image_base64")) or []
    if not image_base64_list:
        raise RuntimeError(f"MiniMax 文生图没有返回图片数据: {data}")

    output_paths = save_base64_images(
        image_base64_list=image_base64_list,
        output_dir=output_dir,
        file_prefix=file_prefix,
        image_format=image_format,
    )

    result = {
        "prompt": prompt,
        "model": model,
        "aspect_ratio": aspect_ratio,
        "response_format": response_format,
        "subject_reference_used": bool(subject_reference),
        "subject_type": subject_type if subject_reference else None,
        "character_image_url": character_image_url if subject_reference else None,
        "image_count": len(output_paths),
        "output_paths": output_paths,
    }
    return result


def minimax_generate_image_tool_payload(
    prompt: str,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    file_prefix: str = DEFAULT_FILE_PREFIX,
    image_format: str = DEFAULT_IMAGE_FORMAT,
    model: str = MODEL_NAME,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    response_format: str = DEFAULT_RESPONSE_FORMAT,
    character_image_url: str | None = None,
    subject_type: str = "character",
) -> str:
    result = minimax_generate_image_core(
        prompt=prompt,
        output_dir=output_dir,
        file_prefix=file_prefix,
        image_format=image_format,
        model=model,
        aspect_ratio=aspect_ratio,
        response_format=response_format,
        character_image_url=character_image_url,
        subject_type=subject_type,
    )
    return json.dumps(result, ensure_ascii=False)


if tool is not None:

    @tool
    def minimax_generate_image_tool(
        prompt: str,
        output_dir: str = DEFAULT_OUTPUT_DIR,
        file_prefix: str = DEFAULT_FILE_PREFIX,
        image_format: str = DEFAULT_IMAGE_FORMAT,
        model: str = MODEL_NAME,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        response_format: str = DEFAULT_RESPONSE_FORMAT,
        character_image_url: str | None = None,
        subject_type: str = "character",
    ) -> str:
        """根据提示词生成图片；可选传入参考人物图片 URL 以保持角色一致性。"""
        return minimax_generate_image_tool_payload(
            prompt=prompt,
            output_dir=output_dir,
            file_prefix=file_prefix,
            image_format=image_format,
            model=model,
            aspect_ratio=aspect_ratio,
            response_format=response_format,
            character_image_url=character_image_url,
            subject_type=subject_type,
        )

else:  # pragma: no cover
    minimax_generate_image_tool = minimax_generate_image_tool_payload
