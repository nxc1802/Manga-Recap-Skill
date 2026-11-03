import pytest
from src.render.video_engine import VideoEngine, VideoMode

def test_mode_detection_portrait():
    engine = VideoEngine()
    # Aspect ratio = 600 / 1200 = 0.5 (<= 0.7) -> portrait_vertical
    mode = engine.detect_video_mode(600, 1200)
    assert mode == VideoMode.PORTRAIT_VERTICAL

def test_mode_detection_landscape():
    engine = VideoEngine()
    # Aspect ratio = 1600 / 900 = 1.77 (>= 1.5) -> landscape_fit
    mode = engine.detect_video_mode(1600, 900)
    assert mode == VideoMode.LANDSCAPE_FIT

def test_mode_detection_square():
    engine = VideoEngine()
    # Aspect ratio = 800 / 800 = 1.0 (between 0.7 and 1.5) -> portrait_padded
    mode = engine.detect_video_mode(800, 800)
    assert mode == VideoMode.PORTRAIT_PADDED
    
    # Aspect ratio = 900 / 800 = 1.125 -> portrait_padded
    mode = engine.detect_video_mode(900, 800)
    assert mode == VideoMode.PORTRAIT_PADDED

def test_mode_detection_forced():
    engine = VideoEngine()
    # Forced to landscape_fit even if portrait size
    mode = engine.detect_video_mode(600, 1200, forced_mode="landscape_fit")
    assert mode == VideoMode.LANDSCAPE_FIT
    
    # Invalid forced mode should fall back to auto-detect
    mode = engine.detect_video_mode(600, 1200, forced_mode="invalid_mode")
    assert mode == VideoMode.PORTRAIT_VERTICAL
