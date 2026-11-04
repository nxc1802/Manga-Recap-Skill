"""
src/tts package
Offline local VieNeu-TTS v3 Turbo engine, 25-voice catalog, and SHA-256 caching.
"""

from src.tts.cache import (
    compute_utterance_cache_key,
    get_cache_root,
    get_cached_audio_path,
    save_to_cache,
)
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
    "compute_utterance_cache_key",
    "get_cache_root",
    "get_cached_audio_path",
    "save_to_cache",
    "VoiceProfile",
    "VoiceRegion",
    "VoiceGender",
    "CATALOG_VOICES",
    "VOICE_ALIASES",
    "get_voice_profile",
    "is_valid_voice_id",
    "resolve_voice_id",
]
