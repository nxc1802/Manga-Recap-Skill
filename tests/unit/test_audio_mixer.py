"""
tests/unit/test_audio_mixer.py
Unit test suite for AudioMixer, PCM concatenation, and dynamic BGM ducking (PR-07).
"""

import math
import struct
import wave
from pathlib import Path

import pytest
from src.render.audio_mixer import AudioMixer
from src.tts.engine import get_wav_duration_seconds


def create_mock_wav(file_path: Path, duration_sec: float, sample_rate: int = 48000, channels: int = 1) -> Path:
    """Creates a synthetic sine wave WAV file for deterministic audio testing."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    num_samples = int(duration_sec * sample_rate)
    with wave.open(str(file_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        # 440 Hz tone
        raw_bytes = bytearray()
        for i in range(num_samples):
            value = int(10000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            raw_bytes.extend(struct.pack("<h", value) * channels)
        wf.writeframes(raw_bytes)
    return file_path


class TestAudioMixer:
    """Tests for AudioMixer speech concatenation and BGM ducking."""

    def setup_method(self):
        self.mixer = AudioMixer(target_sample_rate=48000, target_channels=2)

    def test_concatenate_speech_utterances_fast_pcm(self, tmp_path):
        wav1 = create_mock_wav(tmp_path / "u1.wav", duration_sec=1.5)
        wav2 = create_mock_wav(tmp_path / "u2.wav", duration_sec=2.5)

        out_wav = tmp_path / "master.wav"
        out_path, total_dur = self.mixer.concatenate_speech_utterances([wav1, wav2], out_wav)

        assert out_path.exists()
        assert pytest.approx(total_dur, 0.01) == 4.0
        # Check actual frames in output WAV
        with wave.open(str(out_path), "rb") as wf:
            assert wf.getframerate() == 48000
            assert wf.getnframes() == int(4.0 * 48000)

    def test_concatenate_with_inter_segment_pause(self, tmp_path):
        wav1 = create_mock_wav(tmp_path / "u1.wav", duration_sec=1.0)
        wav2 = create_mock_wav(tmp_path / "u2.wav", duration_sec=1.0)

        out_wav = tmp_path / "master_paused.wav"
        _, total_dur = self.mixer.concatenate_speech_utterances(
            [wav1, wav2], out_wav, inter_segment_pause=0.5
        )

        assert pytest.approx(total_dur, 0.01) == 2.5
        with wave.open(str(out_wav), "rb") as wf:
            assert wf.getnframes() == int(2.5 * 48000)

    def test_concatenate_raises_on_empty_list(self, tmp_path):
        out_wav = tmp_path / "empty.wav"
        with pytest.raises(FileNotFoundError):
            self.mixer.concatenate_speech_utterances([], out_wav)

    def test_mix_audio_track_without_bgm(self, tmp_path):
        speech = create_mock_wav(tmp_path / "speech.wav", duration_sec=2.0)
        out_m4a = tmp_path / "speech_only.m4a"

        result = self.mixer.mix_audio_track(speech, out_m4a, bgm_path=None)
        assert result.exists()
        assert result.stat().st_size > 0

    def test_mix_audio_track_with_bgm_ducking(self, tmp_path):
        speech = create_mock_wav(tmp_path / "speech.wav", duration_sec=3.0)
        bgm = create_mock_wav(tmp_path / "bgm.wav", duration_sec=2.0)  # BGM shorter than speech -> loop
        out_m4a = tmp_path / "ducked_mix.m4a"

        result = self.mixer.mix_audio_track(
            speech_wav_path=speech,
            output_path=out_m4a,
            bgm_path=bgm,
            bgm_volume=0.15,
            ducking_ratio=5.0,
            fadeout_seconds=1.0,
        )
        assert result.exists()
        assert result.stat().st_size > 0
