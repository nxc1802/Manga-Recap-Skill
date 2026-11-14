"""
src/services/tts_service.py
Local VieNeu-TTS Service Facade (Zero Cloud TTS, Zero External APIs).
Delegates audio synthesis directly to src.tts.engine.get_tts_engine().
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.config import config
from src.core.logger import logger
from src.core.storage import calculate_sha256, calculate_utterance_fingerprint
from src.tts.catalog import CATALOG_VOICES, resolve_voice_id
from src.tts.engine import get_tts_engine, get_wav_duration_seconds

# Backward-compatible voice model mapping (all local VieNeu presets)
VOICE_MODELS = {
    vid: {
        "type": "vieneu",
        "voice": vid,
        "description": v.description,
        "gender": v.gender.value,
        "region": v.region.value,
    }
    for vid, v in CATALOG_VOICES.items()
}

VOICE_ALIASES = {
    "edge-tts-female": "Trúc Ly",
    "edge-tts-male": "Hải Đăng",
    "edge-tts-en-female": "Trúc Ly",
    "edge-tts-en-male": "Hải Đăng",
    "gemini-2.5-flash": "Trúc Ly",
    "gemini-3.1-flash": "Trúc Ly",
    "openai-tts-nova": "Trúc Ly",
    "openai-tts-alloy": "Hải Đăng",
}


class TTSService:
    """Service facade for local speech synthesis, duration calculation, and script parsing."""

    def __init__(self):
        self._engine = None

    @property
    def engine(self):
        if self._engine is None:
            from src.tts.engine import EngineMode
            self._engine = get_tts_engine(force_mode=EngineMode.NEURAL)
        return self._engine

    @engine.setter
    def engine(self, value):
        self._engine = value

    def resolve_voice_name(self, voice_name: Optional[str] = None) -> str:
        """Resolves arbitrary voice string or alias to a valid VieNeu voice_id."""
        if not voice_name and config and config.tts:
            voice_name = config.tts.voice
        return resolve_voice_id(voice_name)

    def get_audio_duration_ms(self, audio_path: str) -> int:
        """
        Retrieves duration of a WAV file in milliseconds directly from binary header.
        Sub-millisecond execution, zero subprocess spawns.
        """
        try:
            sec = get_wav_duration_seconds(audio_path)
            return int(round(sec * 1000))
        except Exception as e:
            logger.warning(f"Failed to read WAV duration via wave header: {e}")
            path = Path(audio_path)
            if path.exists():
                size = path.stat().st_size
                if size > 44:
                    return int(round(((size - 44) / 96000.0) * 1000))
            return 0

    def parse_script_text(self, script_text: str) -> list[dict]:
        """
        Parses script text/JSON to extract panel sequences and script segments.
        Supports both legacy text markers and JSON configuration formats.
        """
        script_text = script_text.strip()
        if script_text.startswith("{") and script_text.endswith("}"):
            try:
                data = json.loads(script_text)
                scripts_dict = {}
                if isinstance(data, dict):
                    if "scripts" in data:
                        scripts_dict = data["scripts"]
                    else:
                        has_seq_keys = any(str(k).replace("_", "").isdigit() for k in data.keys())
                        if has_seq_keys:
                            scripts_dict = data

                results = []
                for k, v in scripts_dict.items():
                    seq_str = str(k).strip()
                    match_prefix = re.match(r"^(\d+)", seq_str)
                    if not match_prefix:
                        continue
                    seq_num = int(match_prefix.group(1))

                    if isinstance(v, dict):
                        tag = v.get("status") or v.get("tag") or ""
                        status = "processed"
                        if tag in ("context_only", "useless", "video_and_context"):
                            status = "context_only" if tag == "context_only" else ("useless" if tag == "useless" else "processed")
                        results.append({
                            "sequence_id": seq_str,
                            "sequence_number": seq_num,
                            "text": v.get("text", ""),
                            "status": status,
                            "focus_x": v.get("focus_x", 50),
                            "focus_y": v.get("focus_y", 50),
                            "pan_direction": v.get("pan_direction", "horizontal"),
                            "crop_scale": v.get("crop_scale", 100)
                        })
                    else:
                        results.append({
                            "sequence_id": seq_str,
                            "sequence_number": seq_num,
                            "text": str(v),
                            "status": "processed",
                            "focus_x": 50,
                            "focus_y": 50,
                            "pan_direction": "horizontal",
                            "crop_scale": 100
                        })

                def get_sort_key(x):
                    val = x["sequence_id"].replace("_", ".")
                    try:
                        return float(val)
                    except ValueError:
                        return float(x["sequence_number"])
                results.sort(key=get_sort_key)
                return results
            except Exception as e:
                logger.error(f"Error parsing script_text as JSON: {e}. Falling back to text regex.")

        # Legacy text regex parsing
        matches = re.finditer(
            r"\[Khung\s*hình\s*([^\]]+)\](?:\s*\[(context_only|video_and_context|useless)\])?:?\s*(.*?)(?=\[Khung\s*hình\s*[^\]]+\]|$)",
            script_text,
            re.DOTALL | re.IGNORECASE
        )
        results = []
        for match in matches:
            seq_str = match.group(1).strip()
            tag = (match.group(2) or "").strip().lower()
            text = match.group(3).strip()
            text = re.sub(r"^:\s*", "", text)

            status = "processed"
            if tag == "context_only":
                status = "context_only"
            elif tag == "useless":
                status = "useless"

            match_prefix = re.match(r"^(\d+)", seq_str)
            if not match_prefix:
                continue
            seq_num = int(match_prefix.group(1))
            results.append({
                "sequence_id": seq_str,
                "sequence_number": seq_num,
                "text": text,
                "status": status,
                "focus_x": 50,
                "focus_y": 50,
                "pan_direction": "horizontal",
                "crop_scale": 100
            })

        def get_sort_key(x):
            val = x["sequence_id"].replace("_", ".")
            try:
                return float(val)
            except ValueError:
                return float(x["sequence_number"])
        results.sort(key=get_sort_key)
        return results

    async def generate_panel_audio(
        self,
        text: str,
        output_path: Path,
        speed: float = 1.0,
        voice_name: Optional[str] = None,
    ) -> int:
        """
        Synthesizes audio for an utterance using local offline VieNeu-TTS engine.
        Returns duration in milliseconds.
        """
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_voice = self.resolve_voice_name(voice_name)

        # Execute local offline synthesis
        self.engine.synthesize_to_file(
            text=text,
            voice_id=resolved_voice,
            output_path=output_path,
            speed=speed,
        )
        return self.get_audio_duration_ms(str(output_path))

    def concatenate_audio_files(self, audio_paths: list[str], output_path: str):
        """Losslessly concatenates multiple WAV files using FFmpeg demuxer."""
        import subprocess
        list_file_path = Path(output_path).parent / "concat_list.txt"
        with open(list_file_path, "w", encoding="utf-8") as f:
            for p in audio_paths:
                f.write(f"file '{Path(p).resolve()}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file_path),
            "-c", "copy",
            str(output_path)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            logger.error(f"Audio concat failed: {res.stderr}")
            raise RuntimeError(f"FFmpeg concat failed: {res.stderr}")
        if list_file_path.exists():
            list_file_path.unlink()

    async def process_chapter_audio(self, chapter_id, chapter_num, speed=None,
                                    voice_name=None, progress_callback=None):
        import argparse
        from src.core.execution_policy import resolve_legacy_chapter
        from src.cli.tts_cmd import handle_tts
        project = resolve_legacy_chapter(chapter_id, chapter_num)
        handle_tts(argparse.Namespace(project=project.name, chapter=str(chapter_num),
            speed=speed, voice=voice_name, engine="neural"))
        return True


tts_service = TTSService()
