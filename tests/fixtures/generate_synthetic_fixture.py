"""
tests/fixtures/generate_synthetic_fixture.py
Generates the canonical synthetic test fixture for Phase 0 Contract Freeze & Phase 1/2 Verification.
Features:
- Non-sequential segment IDs (e.g. '001', '002_1', '003')
- Multi-panel segments (segment '002_1' maps to panel_002, panel_003, panel_004)
- 2 distinct voices (Ngọc Huyền, Nam An) with varying speeds (1.0, 1.15, 0.95) and emotions (neutral, dramatic, sad)
- One intentionally omitted valid panel (panel_005.jpg is active in manifest and on disk, but omitted from script.json)
- One intentionally missing panel (panel_missing_999.jpg in script_missing_panel.json to test fail-fast behavior)
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import wave
from pathlib import Path

from PIL import Image, ImageDraw


def compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def create_panel_image(path: Path, text: str, color: tuple[int, int, int], size=(800, 600)):
    img = Image.new("RGB", size, color=color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, size[0] - 20, size[1] - 20], outline=(255, 255, 255), width=4)
    draw.text((size[0] // 2, size[1] // 2), text, fill=(255, 255, 255), anchor="mm")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="JPEG", quality=90)


def create_pcm_wav(path: Path, duration: float = 3.0, sample_rate: int = 48000, freq: float = 440.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    num_frames = int(duration * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = []
        for i in range(num_frames):
            val = int(1200 * math.sin(2 * math.pi * freq * (i / sample_rate)))
            frames.append(val)
        wf.writeframes(struct.pack(f"<{num_frames}h", *frames))


def main():
    root = Path(__file__).resolve().parent / "synthetic"
    root.mkdir(parents=True, exist_ok=True)
    chap_dir = root / "chapters" / "1"
    chap_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generate Raw Image
    raw_dir = chap_dir / "raw"
    raw_img = raw_dir / "page_001.jpg"
    create_panel_image(raw_img, "RAW PAGE 1 (Full Strip)", (40, 40, 60), size=(800, 3000))
    raw_sha = compute_sha256(raw_img)

    # 2. Generate Panels
    panels_dir = chap_dir / "panels"
    panels_info = [
        ("panel_001.jpg", "Panel 1: Intro Scene", (180, 50, 50)),
        ("panel_002.jpg", "Panel 2: Sword Drawn", (50, 120, 180)),
        ("panel_003.jpg", "Panel 3: Enemy Approaches", (50, 160, 80)),
        ("panel_004.jpg", "Panel 4: Clash Strike", (180, 140, 40)),
        ("panel_005.jpg", "Panel 5: Omitted Scenery (Valid)", (100, 80, 140)),
    ]

    manifest_panels = []
    for seq_num, (p_name, p_label, p_color) in enumerate(panels_info, 1):
        p_path = panels_dir / p_name
        create_panel_image(p_path, p_label, p_color, size=(800, 600))
        p_sha = compute_sha256(p_path)
        top = (seq_num - 1) * 600
        manifest_panels.append({
            "panel_id": p_name.replace(".jpg", ""),
            "sequence_number": seq_num,
            "filename": p_name,
            "relative_path": f"panels/{p_name}",
            "source_image": "page_001.jpg",
            "crop_box": [0, top, 800, top + 600],
            "width": 800,
            "height": 600,
            "aspect_ratio": round(800.0 / 600.0, 4),
            "sha256": p_sha,
            "status": "active",
            "ocr_text_en": f"Text for {p_name}",
            "notes": "Omitted from script" if p_name == "panel_005.jpg" else "Active recap panel",
        })

    # 3. Generate Audio Clips
    audio_dir = chap_dir / "audio" / "clips"
    wav_specs = [
        ("audio_1_001.wav", 3.5, 440.0),
        ("audio_1_002_1.wav", 5.2, 554.37),
        ("audio_1_003.wav", 4.0, 330.0),
    ]
    wav_meta = {}
    for w_name, w_dur, w_freq in wav_specs:
        w_path = audio_dir / w_name
        create_pcm_wav(w_path, duration=w_dur, sample_rate=48000, freq=w_freq)
        wav_meta[w_name] = {
            "duration": w_dur,
            "sha256": compute_sha256(w_path),
            "relative_path": f"audio/clips/{w_name}",
        }

    # 4. Generate manifest.json
    manifest_data = {
        "project_slug": "synthetic_fixture",
        "chapter_number": "1",
        "source_url": "https://example.com/synthetic/ch1",
        "created_at": "2026-11-08T20:00:00Z",
        "total_raw_images": 1,
        "total_panels": len(manifest_panels),
        "raw_images": [
            {
                "filename": "page_001.jpg",
                "relative_path": "raw/page_001.jpg",
                "width": 800,
                "height": 3000,
                "sha256": raw_sha,
            }
        ],
        "panels": manifest_panels,
        "contact_sheets": [
            {
                "sheet_id": "sheet_01",
                "relative_path": "contact_sheets/sheet_01.jpg",
                "panel_ids": [p["filename"] for p in manifest_panels],
                "columns": 4,
                "rows": 4,
            }
        ],
        "schema_version": "3.0.0",
    }
    with open(chap_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2, ensure_ascii=False)

    # 5. Generate script.json (Canonical Valid Script)
    segments = [
        {
            "seq_id": "001",
            "panel_ids": ["panel_001.jpg"],
            "narrator_text": "Trong bóng đêm mịt mùng, tiếng sấm rền vang khắp sơn cốc Tấn Dương.",
            "character": None,
            "emotion": "neutral",
            "emotion_cue": None,
            "voice_id": "Ngọc Huyền",
            "synthesized_voice": "Ngọc Huyền",
            "voice_source": "manual",
            "camera_motion": "static",
            "focus_box": {
                "x_min": 0.1,
                "y_min": 0.1,
                "x_max": 0.9,
                "y_max": 0.9,
            },
            "duration": wav_meta["audio_1_001.wav"]["duration"],
            "audio_path": wav_meta["audio_1_001.wav"]["relative_path"],
            "checksum": wav_meta["audio_1_001.wav"]["sha256"],
            "fingerprint": "fp_synth_001",
            "status": "synthesized",
        },
        {
            "seq_id": "002_1",  # Non-sequential ID
            "panel_ids": ["panel_002.jpg", "panel_003.jpg", "panel_004.jpg"],  # 3 panels per segment
            "narrator_text": "Ghislain Perdium nắm chặt thanh kiếm, ánh mắt rực lửa sát khí: Ta tuyệt đối không lùi bước!",
            "character": "Ghislain Perdium",
            "emotion": "dramatic",
            "emotion_cue": "[hắng giọng]",
            "voice_id": "Nam An",  # Distinct voice ID
            "synthesized_voice": "Nam An",
            "voice_source": "manual",
            "camera_motion": "zoom_in",
            "focus_box": {
                "x_min": 0.15,
                "y_min": 0.15,
                "x_max": 0.85,
                "y_max": 0.85,
            },
            "duration": wav_meta["audio_1_002_1.wav"]["duration"],
            "audio_path": wav_meta["audio_1_002_1.wav"]["relative_path"],
            "checksum": wav_meta["audio_1_002_1.wav"]["sha256"],
            "fingerprint": "fp_synth_002_1",
            "status": "synthesized",
        },
        {
            "seq_id": "003",
            "panel_ids": ["panel_001.jpg"],  # Reused panel
            "narrator_text": "Hắn thở dài nhìn về phía chân trời xa xăm, nơi bóng tối đang dần nuốt chửng ánh sáng cuối cùng.",
            "character": None,
            "emotion": "sad",
            "emotion_cue": "[thở dài]",
            "voice_id": "Ngọc Huyền",  # Narrator voice with varying emotion
            "synthesized_voice": "Ngọc Huyền",
            "voice_source": "manual",
            "camera_motion": "static_focus",
            "focus_box": {
                "x_min": 0.1,
                "y_min": 0.1,
                "x_max": 0.9,
                "y_max": 0.9,
            },
            "duration": wav_meta["audio_1_003.wav"]["duration"],
            "audio_path": wav_meta["audio_1_003.wav"]["relative_path"],
            "checksum": wav_meta["audio_1_003.wav"]["sha256"],
            "fingerprint": "fp_synth_003",
            "status": "synthesized",
        },
    ]

    total_dur = sum(s["duration"] for s in segments)
    script_data = {
        "project_slug": "synthetic_fixture",
        "chapter_number": "1",
        "title": "Synthetic Fixture Chapter 1",
        "voice_strategy": "single_narrator",
        "default_voice": "Ngọc Huyền",
        "segments": segments,
        "total_duration_seconds": round(total_dur, 2),
        "metadata": {
            "test_notes": "Canonical synthetic fixture: non-sequential seq_id, multi-panel segment, 2 voices, omitted panel_005",
            "omitted_panel_ids": ["panel_005.jpg"],
        },
        "schema_version": "3.0.0",
    }
    with open(chap_dir / "script.json", "w", encoding="utf-8") as f:
        json.dump(script_data, f, indent=2, ensure_ascii=False)

    # 6. Generate script_missing_panel.json (Adversarial test for missing asset fail-fast)
    segments_with_missing = list(segments) + [
        {
            "seq_id": "004_missing_test",
            "panel_ids": ["panel_missing_999.jpg"],  # Intentionally missing panel!
            "narrator_text": "Khung cảnh này tham chiếu một panel hoàn toàn không tồn tại trên đĩa để kiểm thử fail-fast.",
            "character": None,
            "emotion": "neutral",
            "emotion_cue": None,
            "voice_id": "Ngọc Huyền",
            "camera_motion": "static",
            "duration": 3.0,
            "audio_path": "audio/clips/audio_1_004.wav",
            "status": "pending",
        }
    ]
    script_missing_data = dict(script_data)
    script_missing_data["segments"] = segments_with_missing
    script_missing_data["metadata"] = {
        "test_notes": "Adversarial fixture: contains missing panel_missing_999.jpg for fail-fast validation",
    }
    with open(chap_dir / "script_missing_panel.json", "w", encoding="utf-8") as f:
        json.dump(script_missing_data, f, indent=2, ensure_ascii=False)

    # 7. Generate chapter.json
    chapter_meta = {
        "chapter_id": "synthetic_fixture_ch1",
        "chapter_number": "1",
        "project_slug": "synthetic_fixture",
        "title": "Synthetic Fixture Chapter 1",
        "source_url": "https://example.com/synthetic/ch1",
        "created_at": "2026-11-08T20:00:00Z",
        "updated_at": "2026-11-08T20:00:00Z",
        "current_stage": "audio",
        "overall_status": "pending",
        "stages": {
            "ingest": {
                "stage": "ingest",
                "status": "completed",
                "started_at": "2026-11-08T20:00:00Z",
                "completed_at": "2026-11-08T20:01:00Z",
                "artifacts": [
                    "manifest.json",
                    "panels/panel_001.jpg",
                    "panels/panel_002.jpg",
                    "panels/panel_003.jpg",
                    "panels/panel_004.jpg",
                    "panels/panel_005.jpg",
                ],
                "checksums": {},
            },
            "agent": {
                "stage": "agent",
                "status": "completed",
                "started_at": "2026-11-08T20:01:00Z",
                "completed_at": "2026-11-08T20:02:00Z",
                "artifacts": ["script.json"],
                "checksums": {},
            },
            "audio": {
                "stage": "audio",
                "status": "completed",
                "started_at": "2026-11-08T20:02:00Z",
                "completed_at": "2026-11-08T20:03:00Z",
                "artifacts": [
                    "audio/clips/audio_1_001.wav",
                    "audio/clips/audio_1_002_1.wav",
                    "audio/clips/audio_1_003.wav",
                ],
                "checksums": {},
            },
            "render": {
                "stage": "render",
                "status": "pending",
            },
            "export": {
                "stage": "export",
                "status": "pending",
            },
        },
        "stats": {
            "total_raw_images": 1,
            "total_panels": len(manifest_panels),
            "total_segments": len(segments),
            "total_audio_duration_seconds": round(total_dur, 2),
            "final_video_duration_seconds": 0.0,
            "video_path": None,
        },
        "schema_version": "3.0.0",
    }
    with open(chap_dir / "chapter.json", "w", encoding="utf-8") as f:
        json.dump(chapter_meta, f, indent=2, ensure_ascii=False)

    # 8. Generate project.json
    project_cfg = {
        "project_id": "synthetic_fixture_proj",
        "slug": "synthetic_fixture",
        "title": "Synthetic Contract Fixture Series",
        "description": "Enterprise canonical synthetic test fixture for Phase 0 contract freeze",
        "source_url": "https://example.com/synthetic",
        "language": "vi",
        "created_at": "2026-11-08T20:00:00Z",
        "updated_at": "2026-11-08T20:00:00Z",
        "render_settings": {
            "target_width": 1920,
            "target_height": 1080,
            "fps": 30,
            "video_codec": "libx264",
            "audio_codec": "aac",
            "crf": 20,
            "preset": "medium",
            "burn_subtitles": True,
            "video_mode": "auto",
            "render_pan_mode": "static",
            "subtitle": {
                "max_lines": 2,
                "font_size": 26,
                "chunking": "sentence-aware",
                "max_chars_per_line": 42,
            },
        },
        "audio_settings": {
            "default_voice": "Ngọc Huyền",
            "default_speed": 1.0,
            "sample_rate": 48000,
            "bgm_path": None,
            "bgm_volume": 0.15,
            "voice_volume": 1.0,
            "ducking_factor": 0.30,
        },
        "script_pacing": {
            "mode": "balanced",
            "tolerance": 0.20,
            "preserve_important_dialogue": True,
            "adaptive_scene_density": True,
        },
        "chapters": ["1"],
        "schema_version": "3.0.0",
    }
    with open(root / "project.json", "w", encoding="utf-8") as f:
        json.dump(project_cfg, f, indent=2, ensure_ascii=False)

    # 9. Generate characters.json
    characters_data = {
        "project_slug": "synthetic_fixture",
        "characters": {
            "Ghislain Perdium": {
                "canonical_name": "Ghislain Perdium",
                "aliases": ["Ghislain", "Đại Kiếm Sĩ"],
                "role": "protagonist",
                "gender": "male",
                "description": "Nhân vật chính, người sống lại sau thất bại.",
                "core_features": "Tóc đen, mắt sắc bén, kiếm dài sau lưng.",
                "current_outfit": "Chiến bào đen bạc",
                "assigned_voice": "Nam An",
                "voice_settings": {"speed": 1.15},
                "first_appearance": "1",
                "image_crops": [],
                "notes": "Nhân vật chính mang chất giọng hào sảng, kịch tính.",
            },
            "Narrator": {
                "canonical_name": "Narrator",
                "aliases": ["Người kể chuyện"],
                "role": "supporting",
                "gender": "female",
                "description": "Giọng dẫn truyện truyền cảm, linh hoạt cảm xúc.",
                "core_features": "Vô hình",
                "current_outfit": "N/A",
                "assigned_voice": "Ngọc Huyền",
                "voice_settings": {"speed": 1.0},
                "first_appearance": "1",
                "image_crops": [],
                "notes": "Dẫn chuyện mặc định.",
            },
        },
        "updated_at": "2026-11-08T20:00:00Z",
        "schema_version": "3.0.0",
    }
    with open(root / "characters.json", "w", encoding="utf-8") as f:
        json.dump(characters_data, f, indent=2, ensure_ascii=False)

    # 10. Generate voice_selection.json (Review Gate 1 Approval)
    voice_selection_data = {
        "schema_version": "3.0.0",
        "project_slug": "synthetic_fixture",
        "voice_strategy": "single_narrator",
        "primary_voice": "Ngọc Huyền",
        "speed": 1.0,
        "reviewer": "A0_Integration_Architect",
        "approved_at": "2026-11-08T20:00:00Z",
        "status": "approved",
        "notes": "Approved for canonical synthetic test fixture with secondary character voice mapping.",
        "character_voice_mapping": {
            "Ghislain Perdium": "Nam An",
        },
    }
    with open(root / "voice_selection.json", "w", encoding="utf-8") as f:
        json.dump(voice_selection_data, f, indent=2, ensure_ascii=False)

    # 11. Generate story_summary.md
    story_summary = """# Story Summary — Synthetic Fixture

## Lore & Background
Thế giới võ hiệp huyền ảo, nơi các thế lực ma giáo và chính đạo giao tranh tại Tấn Dương.

## Character Trajectory
- **Ghislain Perdium**: Đại kiếm sĩ kiêu hùng, thức tỉnh giữa sơn cốc rực lửa.

## Chapter Chronology
- **Chapter 1**: Tiếng sấm sơn cốc, Ghislain thức tỉnh và thề không lùi bước.
"""
    (root / "story_summary.md").write_text(story_summary, encoding="utf-8")

    print(f"✅ Canonical synthetic fixture successfully generated at: {root}")


if __name__ == "__main__":
    main()
