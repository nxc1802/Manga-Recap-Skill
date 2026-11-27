"""
src/services/tts_service.py
Local VieNeu-TTS Service Facade (Zero Cloud TTS, Zero External APIs).
Delegates audio synthesis directly to src.tts.engine.get_tts_engine().
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.core.logger import logger
from src.tts.catalog import CATALOG_VOICES, resolve_voice_id
from src.tts.engine import get_tts_engine, get_wav_duration_seconds

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


class TTSService:
    """Service facade for local VieNeu-TTS speech synthesis and duration calculation."""

    def __init__(self):
        self._engine = None

    @property
    def engine(self):
        if self._engine is None:
            self._engine = get_tts_engine()
        return self._engine

    @engine.setter
    def engine(self, value):
        self._engine = value

    def resolve_voice_name(self, voice_name: Optional[str] = None) -> str:
        """Resolves voice string or alias to a valid VieNeu voice_id."""
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
        from src.media.ffmpeg import run_ffmpeg

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
        try:
            run_ffmpeg(cmd, description="Audio concat")
        finally:
            if list_file_path.exists():
                list_file_path.unlink()


tts_service = TTSService()
