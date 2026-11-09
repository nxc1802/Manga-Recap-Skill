"""
scripts/benchmark/workload_generator.py
Synthetic workload generator for reproducible performance profiling.
Creates standardized Small (30-60 segs), Large (100-200 segs), and Batch (5 chapters)
benchmarking datasets with genuine geometric comic art panels, canonical schemas,
and diverse narrative recap scripts without copyrighted or private assets.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image, ImageDraw, ImageFont

from src.core.schemas import (
    CameraMotionType,
    CheckpointStatus,
    FocusBox,
    HumanReviewStatus,
    ProjectConfig,
    RecapScript,
    RenderSettings,
    ScriptSegment,
    StageType,
    VideoMode,
    utc_iso_now,
)
from src.core.storage import (
    atomic_write_json,
    calculate_sha256,
    get_chapter_dir,
    get_project_dir,
)

# Realistic Vietnamese narrative prompts for diverse benchmarking
BENCHMARK_NARRATIVE_TEMPLATES = [
    (
        "Trận đại chiến giữa các thế lực hắc ám và liên minh võ lâm chính thức bùng nổ tại đỉnh Thần Kiếm Sơn.",
        "neutral",
        None,
        CameraMotionType.ZOOM_IN,
        1.0,
    ),
    (
        "Khói lửa ngút trời, tiếng binh khí va chạm đinh tai nhức óc làm rung chuyển cả một vùng sơn cước hiểm trở.",
        "dramatic",
        "[hắng giọng]",
        CameraMotionType.PAN_LEFT,
        1.05,
    ),
    (
        "Hắn vung kiếm tạo nên một luồng kiếm khí xé toạc màn sương mù dày đặc, trực tiếp đánh lui hàng chục cao thủ đối địch.",
        "excited",
        None,
        CameraMotionType.STATIC,
        1.1,
    ),
    (
        "Dù đã dốc toàn lực chiến đấu nhưng thương thế quá nặng khiến bước chân của chàng thiếu niên dần trở nên lảo đảo.",
        "sad",
        "[thở dài]",
        CameraMotionType.PAN_DOWN,
        0.95,
    ),
    (
        "Ngay trong khoảnh khắc ngàn cân treo sợi tóc, một bóng đen bí ẩn bỗng nhiên xuất hiện từ trên không trung.",
        "suspenseful",
        None,
        CameraMotionType.STATIC_FOCUS,
        1.0,
    ),
    (
        "Kẻ vừa xuất hiện không ai khác chính là vị đại tông sư đã ẩn cư suốt ba mươi năm qua tại vùng đất cấm.",
        "whispered",
        None,
        CameraMotionType.ZOOM_OUT,
        0.95,
    ),
    (
        "Không thể tha thứ cho những kẻ đã tàn sát đồng môn, ngọn lửa phục thù bùng cháy dữ dội trong đôi mắt rực đỏ.",
        "angry",
        None,
        CameraMotionType.ZOOM_IN,
        1.15,
    ),
    (
        "Chỉ với một cái phẩy tay nhẹ nhàng, uy áp khủng khiếp của cảnh giới chí tôn đã đè bẹp toàn bộ kẻ thù xung quanh.",
        "cinematic_epic",
        None,
        CameraMotionType.PAN_RIGHT,
        1.0,
    ),
]


def create_synthetic_panel_image(
    file_path: Path,
    panel_idx: int,
    width: int = 800,
    height: int = 1200,
    title: str = "Benchmark Scene",
) -> None:
    """Generates an illustrative comic panel with gradient background and geometric features."""
    file_path.parent.mkdir(parents=True, exist_ok=True)

    # Color palette variation based on panel index
    hue_phase = (panel_idx * 45) % 360
    r = int(127 + 100 * math.sin(math.radians(hue_phase)))
    g = int(127 + 100 * math.sin(math.radians(hue_phase + 120)))
    b = int(127 + 100 * math.sin(math.radians(hue_phase + 240)))
    base_color = (max(20, min(240, r)), max(20, min(240, g)), max(20, min(240, b)))

    img = Image.new("RGB", (width, height), color=(15, 18, 24))
    draw = ImageDraw.Draw(img)

    # Inner decorative border
    margin = 30
    draw.rectangle(
        [margin, margin, width - margin, height - margin],
        outline=(220, 225, 230),
        width=4,
    )

    # Background geometric gradient/block
    draw.rectangle(
        [margin + 10, margin + 10, width - margin - 10, height // 2],
        fill=base_color,
    )

    # Illustrative character / action representation
    center_x = width // 2
    center_y = height // 2
    draw.ellipse(
        [center_x - 120, center_y - 120, center_x + 120, center_y + 120],
        fill=(255, 255, 255),
        outline=(0, 0, 0),
        width=6,
    )
    draw.rectangle(
        [center_x - 180, center_y + 80, center_x + 180, height - margin - 60],
        fill=(base_color[0] // 2, base_color[1] // 2, base_color[2] // 2),
        outline=(240, 240, 240),
        width=3,
    )

    # Label panel
    font = ImageFont.load_default()
    draw.text((margin + 20, margin + 20), f"PANEL #{panel_idx:03d}: {title}", fill=(255, 255, 255), font=font)
    draw.text((margin + 20, height - margin - 40), f"DIM: {width}x{height} | NO COPYRIGHT DATA", fill=(180, 180, 180), font=font)

    img.save(file_path, "JPEG", quality=90)


def create_synthetic_bgm(bgm_path: Path, duration_seconds: float = 30.0) -> None:
    """Generates a pleasant 48kHz stereo sine ambient track for BGM ducking tests."""
    import struct
    import wave

    bgm_path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 48000
    total_frames = int(duration_seconds * sample_rate)
    frames = bytearray()

    freq1 = 220.0  # A3
    freq2 = 330.0  # E4
    for i in range(total_frames):
        t = i / sample_rate
        # Gentle chord pulse
        v1 = int(2500 * math.sin(2 * math.pi * freq1 * t))
        v2 = int(2000 * math.sin(2 * math.pi * freq2 * t))
        sample_l = max(-32768, min(32767, v1 + v2))
        sample_r = max(-32768, min(32767, int(v1 * 0.9 + v2 * 1.1)))
        frames.extend(struct.pack("<hh", sample_l, sample_r))

    with wave.open(str(bgm_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))


def generate_benchmark_project(
    project_slug: str,
    chapters_spec: List[Dict[str, Any]],
    target_width: int = 1920,
    target_height: int = 1080,
    fps: int = 30,
) -> Path:
    """
    Constructs a fully populated, canonical benchmark project directory.
    chapters_spec: list of dicts with {"chapter": "1", "num_segments": 35, "num_panels": 25}
    """
    proj_dir = get_project_dir(project_slug)
    if proj_dir.exists():
        shutil.rmtree(proj_dir)
    proj_dir.mkdir(parents=True, exist_ok=True)

    # 1. Project Config
    bgm_rel_path = "bgm.wav"
    bgm_full_path = proj_dir / bgm_rel_path
    create_synthetic_bgm(bgm_full_path, duration_seconds=45.0)

    proj_cfg = {
        "schema_version": "3.0.0",
        "project_id": project_slug,
        "slug": project_slug,
        "project_slug": project_slug,
        "title": f"Benchmark Suite - {project_slug.replace('_', ' ').title()}",
        "render_settings": {
            "target_width": target_width,
            "target_height": target_height,
            "fps": fps,
            "video_mode": "auto",
            "burn_subtitles": True,
            "subtitle_lines": 2,
            "subtitle_font_size": 26,
            "subtitle_chunking": "sentence-aware",
            "animation_enabled": True,
        },
        "audio_settings": {
            "bgm_path": bgm_rel_path,
            "bgm_volume": 0.15,
            "voice_volume": 1.0,
            "ducking_factor": 0.2,
            "sample_rate": 48000,
        },
        "chapters": [str(c.get("chapter", "1")) for c in chapters_spec],
    }
    atomic_write_json(proj_dir / "project.json", proj_cfg)

    # 2. Characters & Voice Selection
    chars_cfg = {
        "schema_version": "3.0.0",
        "project_slug": project_slug,
        "characters": {
            "narrator": {
                "canonical_name": "Người dẫn chuyện",
                "aliases": ["narrator"],
                "role": "supporting",
                "gender": "female",
                "assigned_voice": "Ngọc Huyền",
            },
            "hero": {
                "canonical_name": "Lâm Phong",
                "aliases": ["hero"],
                "role": "protagonist",
                "gender": "male",
                "assigned_voice": "Nam An",
            },
        },
    }
    atomic_write_json(proj_dir / "characters.json", chars_cfg)

    voice_sel_cfg = {
        "schema_version": "3.0.0",
        "project_slug": project_slug,
        "primary_voice": "Ngọc Huyền",
        "status": "approved",
        "reviewer": "benchmark_harness",
    }
    atomic_write_json(proj_dir / "voice_selection.json", voice_sel_cfg)

    # 3. Chapters construction
    for c_spec in chapters_spec:
        chap_num = str(c_spec.get("chapter", "1"))
        num_segs = int(c_spec.get("num_segments", 35))
        num_panels = int(c_spec.get("num_panels", max(15, num_segs // 2)))

        chap_dir = proj_dir / "chapters" / chap_num
        panels_dir = chap_dir / "panels"
        audio_dir = chap_dir / "audio" / "clips"
        video_dir = chap_dir / "video"
        sub_dir = chap_dir / "subtitles"
        for d in (panels_dir, audio_dir, video_dir, sub_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Generate synthetic panels
        panel_records = []
        for p_idx in range(1, num_panels + 1):
            p_name = f"panel_{p_idx:03d}.jpg"
            p_path = panels_dir / p_name
            # Alternate portrait (800x1200) and landscape (1200x800)
            if p_idx % 3 == 0:
                pw, ph = 1200, 800
            else:
                pw, ph = 800, 1200
            create_synthetic_panel_image(p_path, p_idx, pw, ph, title=f"Ch.{chap_num} P.{p_idx}")
            panel_records.append({
                "panel_id": p_name,
                "sequence_number": p_idx,
                "filename": p_name,
                "relative_path": f"panels/{p_name}",
                "source_image": f"raw/page_{p_idx:03d}.jpg",
                "crop_box": [0, 0, pw, ph],
                "width": pw,
                "height": ph,
                "aspect_ratio": round(pw / float(ph), 3),
                "sha256": calculate_sha256(p_path),
                "status": "active",
            })

        # Manifest
        manifest_cfg = {
            "schema_version": "3.0.0",
            "project_slug": project_slug,
            "chapter_number": chap_num,
            "raw_images": [],
            "panels": panel_records,
            "contact_sheets": [],
        }
        atomic_write_json(chap_dir / "manifest.json", manifest_cfg)

        # Script segments (supporting N:M mapping, multi-voice, emotions)
        segments = []
        for s_idx in range(1, num_segs + 1):
            seq_id = f"{s_idx:03d}"
            # Template rotation
            tmpl = BENCHMARK_NARRATIVE_TEMPLATES[(s_idx - 1) % len(BENCHMARK_NARRATIVE_TEMPLATES)]
            text, emotion, cue, motion, speed = tmpl

            # N:M mapping logic: some segments use 1 panel, some use 2 panels, some reuse
            if s_idx % 7 == 0 and num_panels >= 2:
                # Multi-panel segment
                p1 = f"panel_{(s_idx % num_panels) + 1:03d}.jpg"
                p2 = f"panel_{((s_idx + 1) % num_panels) + 1:03d}.jpg"
                seg_panels = [p1, p2]
            else:
                p_selected = f"panel_{(s_idx % num_panels) + 1:03d}.jpg"
                seg_panels = [p_selected]

            voice = "Nam An" if s_idx % 4 == 0 else "Ngọc Huyền"
            focus_box = (
                {"x_min": 0.2, "y_min": 0.2, "x_max": 0.8, "y_max": 0.8}
                if motion == CameraMotionType.STATIC_FOCUS
                else None
            )

            segments.append({
                "seq_id": seq_id,
                "panel_ids": seg_panels,
                "narrator_text": f"{text} (Đoạn #{s_idx})",
                "character": "hero" if voice == "Nam An" else "narrator",
                "voice_id": voice,
                "emotion": emotion,
                "emotion_cue": cue,
                "speed": speed,
                "camera_motion": motion.value if hasattr(motion, "value") else str(motion),
                "focus_box": focus_box,
                "duration": 4.0,  # initial estimate, updated during TTS
                "status": "draft",
            })

        script_cfg = {
            "schema_version": "3.0.0",
            "project_slug": project_slug,
            "chapter_number": chap_num,
            "title": f"Hồi {chap_num}: Phong Vân Biến Ảo",
            "segments": segments,
        }
        atomic_write_json(chap_dir / "script.json", script_cfg)

        # Chapter metadata
        chap_meta = {
            "schema_version": "3.0.0",
            "chapter_id": f"{project_slug}_{chap_num}",
            "chapter_number": chap_num,
            "project_slug": project_slug,
            "title": f"Hồi {chap_num}",
            "overall_status": CheckpointStatus.COMPLETED.value,
            "technical_status": CheckpointStatus.COMPLETED.value,
            "human_review_status": HumanReviewStatus.UNREVIEWED.value,
            "current_stage": StageType.INGEST.value,
            "stages": {
                StageType.INGEST.value: {"stage": StageType.INGEST.value, "status": CheckpointStatus.COMPLETED.value, "updated_at": utc_iso_now()},
                StageType.AGENT.value: {"stage": StageType.AGENT.value, "status": CheckpointStatus.COMPLETED.value, "updated_at": utc_iso_now()},
                StageType.AUDIO.value: {"stage": StageType.AUDIO.value, "status": CheckpointStatus.PENDING.value, "updated_at": utc_iso_now()},
                StageType.RENDER.value: {"stage": StageType.RENDER.value, "status": CheckpointStatus.PENDING.value, "updated_at": utc_iso_now()},
                StageType.EXPORT.value: {"stage": StageType.EXPORT.value, "status": CheckpointStatus.PENDING.value, "updated_at": utc_iso_now()},
            },
        }
        atomic_write_json(chap_dir / "chapter.json", chap_meta)

    return proj_dir
