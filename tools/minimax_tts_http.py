from __future__ import annotations

import argparse
import binascii
import json
import os
from pathlib import Path
from typing import Any

from sandbox.network_policy import ensure_response_allowed, ensure_url_allowed
from sandbox.quota_policy import ensure_quota_allowed

try:
    from langchain.tools import tool
except ImportError:  # pragma: no cover - lets the module stay importable without langchain
    tool = None


API_URL = "https://api.minimaxi.com/v1/t2a_v2"
MODEL_NAME = "speech-2.8-turbo"
DEFAULT_VOICE_ID = "Chinese (Mandarin)_Sincere_Adult"
DEFAULT_OUTPUT_DIR = "outputs/minimax_output"
DEFAULT_OUTPUT_FILE = "commercial_space_minimax.mp3"


def save_audio_from_hex(hex_audio: str, output_path: Path) -> None:
    audio_bytes = binascii.unhexlify(hex_audio)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(audio_bytes)


def build_payload(
    text: str,
    *,
    voice_id: str = DEFAULT_VOICE_ID,
    speed: float = 0.9,
    volume: float = 1,
    pitch: int = -1,
) -> dict[str, Any]:
    return {
        "model": MODEL_NAME,
        "text": text,
        "stream": False,
        "language_boost": "Chinese",
        "output_format": "hex",
        "voice_setting": {
            "voice_id": voice_id,
            "speed": float(speed),
            "vol": float(volume),
            "pitch": int(pitch),
        },
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
    }


def synthesize_text(
    text: str,
    output_path: Path,
    *,
    voice_id: str = DEFAULT_VOICE_ID,
    speed: float = 0.9,
    volume: float = 1,
    pitch: float = -1,
    network_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("缺少依赖 requests，请先安装: pip install requests") from exc

    api_key = os.environ.get("MINIMAX_TTS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未找到 MINIMAX_TTS_API_KEY。语音合成必须使用 TTS 专用 key，请先在环境变量或 .env 中设置。"
        )

    payload = build_payload(
        text,
        voice_id=voice_id,
        speed=speed,
        volume=volume,
        pitch=pitch,
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    ensure_url_allowed(API_URL, network_policy)
    timeout = (network_policy or {}).get("timeout_seconds", 180)
    response = requests.post(API_URL, headers=headers, json=payload, timeout=timeout)
    ensure_response_allowed(response, network_policy)
    response.raise_for_status()
    data = response.json()

    base_resp = data.get("base_resp") or {}
    if base_resp.get("status_code") not in (0, None):
        raise RuntimeError(f"MiniMax TTS 返回错误: {data}")

    audio_hex = ((data.get("data") or {}).get("audio")) or ""
    if not audio_hex:
        raise RuntimeError(f"MiniMax TTS 没有返回音频数据: {data}")

    save_audio_from_hex(audio_hex, output_path)
    return data


def minimax_tts_core(
    text: str,
    output_file: str = DEFAULT_OUTPUT_FILE,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    voice_id: str = DEFAULT_VOICE_ID,
    speed: float = 0.9,
    volume: float = 1,
    pitch: float = -1,
    network_policy: dict[str, Any] | None = None,
    quota_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将文本合成为 MP3 音频并返回结构化结果。

    这个函数是普通 Python 调用入口，适合在 LangGraph 或其他业务代码中直接使用。
    """
    ensure_quota_allowed(
        "minimax_tts",
        quota_policy,
        api_calls=1,
        tts_generations=1,
    )
    clean_text = text.strip()
    if not clean_text:
        raise ValueError("text 不能为空。")

    output_path = Path(output_dir) / output_file
    data = synthesize_text(
        clean_text,
        output_path,
        voice_id=voice_id,
        speed=speed,
        volume=volume,
        pitch=pitch,
        network_policy=network_policy,
    )
    extra_info = data.get("extra_info") or {}

    result = {
        "output_path": str(output_path.resolve()),
        "audio_format": extra_info.get("audio_format"),
        "audio_sample_rate": extra_info.get("audio_sample_rate"),
        "audio_size": extra_info.get("audio_size"),
        "audio_length": extra_info.get("audio_length"),
        "voice_id": voice_id,
    }
    return result


def minimax_tts_tool_payload(
    text: str,
    output_file: str = DEFAULT_OUTPUT_FILE,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    voice_id: str = DEFAULT_VOICE_ID,
    speed: float = 0.9,
    volume: float = 1,
    pitch: float = -1,
) -> str:
    """将普通函数结果转为 tool 友好的 JSON 字符串。"""
    result = minimax_tts_core(
        text=text,
        output_file=output_file,
        output_dir=output_dir,
        voice_id=voice_id,
        speed=speed,
        volume=volume,
        pitch=pitch,
    )
    return json.dumps(result, ensure_ascii=False)


if tool is not None:
    
    '''
把 工具函数minimax_tts_tool 封装成公句对象tool ，可以在 langchain agent 中直接调用
实际上就是：
Tool(
    name="minimax_tts_tool",   # ← 对象名字，缺失时候默认取函数名
    func=minimax_tts_tool,（函数名）
    description="...",
)
    
    '''
    

    @tool
    def minimax_tts_tool(
        text: str,
        output_file: str = DEFAULT_OUTPUT_FILE,
        output_dir: str = DEFAULT_OUTPUT_DIR,
        voice_id: str = DEFAULT_VOICE_ID,
        speed: float = 0.9,
        volume: float = 1,
        pitch: float = -1,
    ) -> str:
        """将文本合成为 MP3 音频并返回结果 JSON。"""
        return minimax_tts_tool_payload(
            text=text,
            output_file=output_file,
            output_dir=output_dir,
            voice_id=voice_id,
            speed=speed,
            volume=volume,
            pitch=pitch,
        )
else:  # pragma: no cover - fallback for environments without langchain
    minimax_tts_tool = minimax_tts_tool_payload
