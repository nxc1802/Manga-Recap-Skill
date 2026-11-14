"""Lazy render exports: importing schemas/validators must not initialize engines."""
from importlib import import_module

_EXPORTS = {
    "VideoEngine": "video_engine", "video_engine": "video_engine", "VideoMode": "video_engine",
    "CameraMotionGenerator": "camera",
    "SubtitleGenerator": "subtitle", "SubtitleCue": "subtitle", "SubtitleBlock": "subtitle",
    "format_srt_time": "subtitle", "parse_srt_time": "subtitle", "sanitize_subtitle_text": "subtitle",
    "AudioMixer": "audio_mixer", "SpeechTimelineEntry": "audio_mixer",
    "FFprobeValidator": "validator", "StreamValidator": "validator",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module_name = _EXPORTS[name]
    module = import_module(f"src.render.{module_name}")
    for export, owner in _EXPORTS.items():
        if owner == module_name:
            globals()[export] = getattr(module, export)
    return globals()[name]
