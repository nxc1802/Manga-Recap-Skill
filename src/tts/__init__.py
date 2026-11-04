"""
src/tts package
Offline local VieNeu-TTS v3 Turbo engine and 25-voice catalog.
"""

from src.tts.catalog import (
    CATALOG_VOICES,
    VOICE_ALIASES,
    VoiceGender,
    VoiceProfile,
    VoiceRegion,
    get_voice_profile,
    is_valid_voice_id,
    resolve_voice_id,
)
from src.tts.engine import (
    EngineMode,
    MockTTSBackend,
    NeuralTTSBackend,
    VieNeuEngine,
    get_tts_engine,
    get_wav_duration_seconds,
)

__all__ = [
    "VieNeuEngine",
    "get_tts_engine",
    "EngineMode",
    "MockTTSBackend",
    "NeuralTTSBackend",
    "get_wav_duration_seconds",
    "VoiceProfile",
    "VoiceRegion",
    "VoiceGender",
    "CATALOG_VOICES",
    "VOICE_ALIASES",
    "get_voice_profile",
    "is_valid_voice_id",
    "resolve_voice_id",
]
