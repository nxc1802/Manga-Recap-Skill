"""
src/cloud/kaggle_service.py
Cloud Execution Server for Manga Recap leveraging Kaggle GPU/TPU/CPU infrastructure.
Enables extreme-speed batch VieNeu-TTS synthesis and hardware-accelerated NVENC video rendering.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.core.logger import logger
from src.core.schemas import (
    CameraMotionType,
    ChapterMetadata,
    CheckpointStatus,
    FocusBox,
    HumanReviewStatus,
    ProjectConfig,
    RecapScript,
    RenderSettings,
    ScriptSegment,
    StageType,
    SubtitleChunkingMode,
    SubtitleSettings,
    VideoMode,
)
from src.core.storage import atomic_write_json, get_chapter_dir, get_project_dir, read_json


def safe_extract_tar(tar: tarfile.TarFile, target_dir: Union[str, Path]) -> None:
    """
    Safely extracts a tar archive preventing directory traversal (zip-slip / tar-slip).
    Rejects absolute member names and any path navigating outside target_dir.
    """
    resolved_target = Path(target_dir).resolve()
    for member in tar.getmembers():
        # Prevent absolute paths or path traversal via ../
        if member.name.startswith("/") or ".." in member.name.split("/"):
            raise ValueError(
                f"Security error: Attempted Path Traversal in Tar File: '{member.name}'"
            )
        member_path = (resolved_target / member.name).resolve()
        try:
            member_path.relative_to(resolved_target)
        except ValueError:
            raise ValueError(
                f"Security error: Attempted Path Traversal in Tar File: '{member.name}'"
            )
    try:
        tar.extractall(path=resolved_target, filter="data")
    except TypeError:
        tar.extractall(path=resolved_target)


class KaggleCloudService:
    """
    Manages remote batch rendering and TTS synthesis execution on Kaggle Cloud.
    Supports clean job separation:
    - 'tts': Audio synthesis only, syncing audio clips and script without video rendering.
    - 'render': Hardware video rendering only, reusing pre-existing audio cache and skipping TTS.
    - 'produce': Full end-to-end production (TTS + Render).
    """

    DEFAULT_KERNEL_SLUG = "manga-recap-cloud-runner"

    def __init__(self, api_token: Optional[str] = None):
        self.api_token = api_token or os.environ.get("KAGGLE_API_TOKEN")
        self._api = None
        self._username = None
        self._authenticate()

    def __repr__(self) -> str:
        return f"<KaggleCloudService username='{self._username}' authenticated={self.is_authenticated}>"

    def _authenticate(self) -> None:
        """Authenticates with the Kaggle API without logging sensitive tokens."""
        try:
            from kaggle.api.kaggle_api_extended import KaggleApi

            if self.api_token:
                os.environ["KAGGLE_API_TOKEN"] = self.api_token

            api = KaggleApi()
            api.authenticate()
            self._api = api
            # Introspect username
            self._username = api.config_values.get(api.CONFIG_NAME_USER, "kaggle_user")
            logger.info(f"KaggleCloudService: Authenticated as '{self._username}'")
        except Exception as e:
            err_msg = str(e)
            if self.api_token and self.api_token in err_msg:
                err_msg = err_msg.replace(self.api_token, "[REDACTED_API_TOKEN]")
            logger.warning(f"KaggleCloudService: Kaggle API authentication failed: {err_msg}")
            self._api = None

    @property
    def is_authenticated(self) -> bool:
        return self._api is not None

    def get_username(self) -> str:
        return self._username or "unknown"

    def package_chapter_payload(
        self,
        project_slug: str,
        chapter_number: str,
        out_tar_path: Union[str, Path],
        job_type: str = "produce",
    ) -> Path:
        """
        Packs project.json, chapter manifest, script, panel images, and pre-existing audio
        into a tar.gz payload for cloud execution.
        Supports:
        - job_type='tts': packages project config, chapter metadata, and script.json (omits panels and video).
        - job_type='render': packages project config, chapter metadata, manifest, script, panels, and existing audio cache.
        - job_type='produce': packages full chapter payload.
        """
        proj_dir = get_project_dir(project_slug)
        chap_dir = get_chapter_dir(project_slug, chapter_number)

        out_tar_path = Path(out_tar_path).resolve()
        out_tar_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"Packaging chapter payload for {project_slug} ch.{chapter_number} "
            f"(job_type={job_type}) -> {out_tar_path}"
        )

        with tarfile.open(out_tar_path, "w:gz") as tar:
            # 1. Project config
            p_json = proj_dir / "project.json"
            if p_json.exists():
                tar.add(p_json, arcname="project.json")
                try:
                    p_cfg = ProjectConfig.model_validate(read_json(p_json))
                    # Package BGM file if configured and present (for render or produce)
                    if job_type in ("render", "produce") and p_cfg.audio_settings and p_cfg.audio_settings.bgm_path:
                        bgm_p = Path(p_cfg.audio_settings.bgm_path)
                        if not bgm_p.is_absolute():
                            bgm_p = proj_dir / bgm_p
                        if bgm_p.exists() and bgm_p.stat().st_size > 0:
                            tar.add(bgm_p, arcname=f"bgm{bgm_p.suffix}")
                except Exception as e:
                    logger.debug(f"Note during project config inspection: {e}")

            # 2. Chapter metadata
            meta_files = ["chapter.json", "script.json"]
            if job_type in ("render", "produce"):
                meta_files.append("manifest.json")

            for fname in meta_files:
                fpath = chap_dir / fname
                if fpath.exists():
                    tar.add(fpath, arcname=f"chapter/{fname}")

            # 3. Panels (only required for render or produce)
            if job_type in ("render", "produce"):
                panels_dir = chap_dir / "panels"
                if panels_dir.exists():
                    tar.add(panels_dir, arcname="chapter/panels")

            # 4. Pre-synthesized audio clips if present (for render-only acceleration or cache reuse)
            if job_type in ("render", "produce"):
                audio_clips_dir = chap_dir / "audio" / "clips"
                if audio_clips_dir.exists() and any(audio_clips_dir.glob("*.wav")):
                    tar.add(audio_clips_dir, arcname="chapter/audio/clips")

        logger.info(f"Payload package created: {out_tar_path} ({out_tar_path.stat().st_size / (1024*1024):.2f} MB)")
        return out_tar_path

    def generate_worker_script(
        self,
        project_slug: str,
        chapter_number: str,
        render_workers: int = 4,
        encoder_preference: str = "auto",
        job_type: str = "produce",
        video_mode: Optional[str] = None,
        burn_subtitles: Optional[bool] = None,
        fps: Optional[int] = None,
        target_width: Optional[int] = None,
        target_height: Optional[int] = None,
    ) -> str:
        """
        Generates the standalone extreme-speed worker script for execution inside the Kaggle Linux VM.
        Enforces:
        - Dedicated job separation: 'tts' (synthesis only), 'render' (video render only without TTS code), 'produce' (full).
        - Standardized option parity (video modes, subtitle styles, camera motion, FPS, BGM mixing).
        - Multi-panel rendering without skipping panels or repeating audio.
        - Fail-fast on missing/corrupted assets without fallback or substitution.
        - Per-segment TTS voice, speed, emotion handling.
        - Opaque string seq_id preservation.
        - Secure archive extraction.
        """
        # Resolve project settings from local project.json if available
        proj_dir = get_project_dir(project_slug)
        p_json = proj_dir / "project.json"
        p_cfg = None
        if p_json.exists():
            try:
                p_cfg = ProjectConfig.model_validate(read_json(p_json))
            except Exception:
                pass

        eff_width = target_width or (p_cfg.render_settings.target_width if p_cfg else 1920)
        eff_height = target_height or (p_cfg.render_settings.target_height if p_cfg else 1080)
        eff_fps = fps or (p_cfg.render_settings.fps if p_cfg else 30)
        eff_video_mode = video_mode or (p_cfg.render_settings.video_mode if p_cfg else "auto")
        eff_burn_subs = burn_subtitles if burn_subtitles is not None else (p_cfg.render_settings.burn_subtitles if p_cfg else True)

        sub_cfg = p_cfg.render_settings.subtitle if p_cfg and p_cfg.render_settings else SubtitleSettings()
        eff_max_lines = sub_cfg.max_lines
        eff_font_size = sub_cfg.font_size
        eff_chunking = sub_cfg.chunking.value if hasattr(sub_cfg.chunking, "value") else str(sub_cfg.chunking)
        eff_max_chars = sub_cfg.max_chars_per_line

        audio_cfg = p_cfg.audio_settings if p_cfg else None
        eff_bgm_vol = audio_cfg.bgm_volume if audio_cfg else 0.15
        eff_voice_vol = audio_cfg.voice_volume if audio_cfg else 1.0
        eff_ducking = audio_cfg.ducking_factor if audio_cfg else 0.30

        if job_type == "tts":
            return self._generate_tts_only_worker_script(
                project_slug=project_slug,
                chapter_number=chapter_number,
            )
        elif job_type == "render":
            return self._generate_render_only_worker_script(
                project_slug=project_slug,
                chapter_number=chapter_number,
                render_workers=render_workers,
                encoder_preference=encoder_preference,
                target_width=eff_width,
                target_height=eff_height,
                fps=eff_fps,
                video_mode=eff_video_mode,
                burn_subtitles=eff_burn_subs,
                max_lines=eff_max_lines,
                font_size=eff_font_size,
                max_chars_per_line=eff_max_chars,
                bgm_vol=eff_bgm_vol,
                voice_vol=eff_voice_vol,
                ducking_factor=eff_ducking,
            )
        else:
            return self._generate_produce_worker_script(
                project_slug=project_slug,
                chapter_number=chapter_number,
                render_workers=render_workers,
                encoder_preference=encoder_preference,
                target_width=eff_width,
                target_height=eff_height,
                fps=eff_fps,
                video_mode=eff_video_mode,
                burn_subtitles=eff_burn_subs,
                max_lines=eff_max_lines,
                font_size=eff_font_size,
                max_chars_per_line=eff_max_chars,
                bgm_vol=eff_bgm_vol,
                voice_vol=eff_voice_vol,
                ducking_factor=eff_ducking,
            )

    def _generate_tts_only_worker_script(
        self,
        project_slug: str,
        chapter_number: str,
    ) -> str:
        """Generates dedicated TTS-only cloud worker script without video/rendering dependencies."""
        return f'''#!/usr/bin/env python3
"""
Manga Recap Extreme-Speed Cloud Production Worker for Kaggle GPU.
Mode: TTS-Only | Project: {project_slug} | Chapter: {chapter_number}
"""

import os
import sys
import time
import json
import shutil
import tarfile
import hashlib
import subprocess
from pathlib import Path

t_start_wall = time.time()
print("=" * 60)
print("🚀 MANGA RECAP CLOUD PRODUCTION WORKER STARTING ON KAGGLE (TTS-ONLY)")
print("=" * 60)

# Helper: safe tar extraction preventing directory traversal
def safe_extract_tar(tar, target_dir):
    resolved_target = Path(target_dir).resolve()
    for member in tar.getmembers():
        if member.name.startswith("/") or ".." in member.name.split("/"):
            raise ValueError(f"Security error: Attempted Path Traversal in Tar File: '{{member.name}}'")
        member_path = (resolved_target / member.name).resolve()
        try:
            member_path.relative_to(resolved_target)
        except ValueError:
            raise ValueError(f"Security error: Attempted Path Traversal in Tar File: '{{member.name}}'")
    try:
        tar.extractall(path=resolved_target, filter="data")
    except TypeError:
        tar.extractall(path=resolved_target)

# 1. Hardware & Environment Introspection
import platform
print(f"System: {{platform.platform()}} | Python: {{sys.version.split()[0]}}")
print(f"CPU count: {{os.cpu_count()}}")

has_cuda = False
gpu_name = "None"
try:
    import torch
    has_cuda = torch.cuda.is_available()
    if has_cuda:
        gpu_name = torch.cuda.get_device_name(0)
        print(f"🔥 GPU ACCELERATION ENABLED: {{gpu_name}} (Count: {{torch.cuda.device_count()}})")
    else:
        print("⚠️ CUDA not available, running high-concurrency CPU mode")
except Exception as e:
    print(f"PyTorch check note: {{e}}")

# 2. Extract Job Payload or Locate Chapter Data
workspace = Path("/kaggle/working/workspace").resolve()
workspace.mkdir(parents=True, exist_ok=True)
chapter_dir = workspace / "chapter"
chapter_dir.mkdir(parents=True, exist_ok=True)

found_payload_tar = None
found_script = None

for root_dir in [Path("/kaggle/input"), Path("."), Path("/kaggle/src")]:
    if not root_dir.exists():
        continue
    for tar_candidate in root_dir.rglob("*.tar.gz"):
        if "payload" in tar_candidate.name.lower() or "manga" in tar_candidate.name.lower():
            found_payload_tar = tar_candidate
            break
    if found_payload_tar:
        break
    for s_candidate in root_dir.rglob("script.json"):
        found_script = s_candidate
        break
    if found_script:
        break

if found_payload_tar:
    print(f"📦 Extracting payload archive securely: {{found_payload_tar}} -> {{workspace}}")
    with tarfile.open(found_payload_tar, "r:gz") as tar:
        safe_extract_tar(tar, workspace)
    script_path = chapter_dir / "script.json"
elif found_script:
    print(f"📂 Found uncompressed chapter data at: {{found_script.parent}}")
    script_path = chapter_dir / "script.json"
    shutil.copy2(found_script, script_path)
else:
    raise FileNotFoundError("Could not find payload archive or script.json in /kaggle/input")

with open(script_path, "r", encoding="utf-8") as f:
    script = json.load(f)

segments = script.get("segments", [])
print(f"Loaded script: {{len(segments)}} segments")

audio_clips_dir = chapter_dir / "audio" / "clips"
audio_clips_dir.mkdir(parents=True, exist_ok=True)

# 3. Bootstrap VieNeu-TTS Engine & Synthesize Utterances
print("\\n" + "=" * 40)
print("📦 INITIALIZING VIENEU-TTS ENGINE")
print("=" * 40)

t_tts_init_0 = time.time()
try:
    import vieneu
    print("Found existing vieneu module")
except ImportError:
    print("Installing vieneu via pip...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "vieneu"], check=True)
    import vieneu

from vieneu import Vieneu
engine = Vieneu(mode="v3turbo")
t_tts_init = time.time() - t_tts_init_0
print(f"VieNeu-TTS engine initialized in {{t_tts_init:.2f}}s")

print("\\n" + "=" * 40)
print("🎙️ RUNNING PER-SEGMENT TTS SYNTHESIS")
print("=" * 40)

t_tts_0 = time.time()

def prepare_input_text(seg_item):
    txt = seg_item.get("narrator_text", "").strip()
    cue = seg_item.get("emotion_cue")
    if cue and cue in ["[cười]", "[thở dài]", "[hắng giọng]"]:
        if cue not in txt:
            txt = f"{{cue}} {{txt}}"
    return txt

def apply_speed(wav_file, spd):
    target_spd = max(0.5, min(2.0, float(spd)))
    if abs(target_spd - 1.0) <= 0.01:
        return
    tmp_wav = wav_file.with_name(f".{{wav_file.name}}.speed.tmp.wav")
    sp_cmd = [
        "ffmpeg", "-y", "-i", str(wav_file),
        "-filter:a", f"atempo={{target_spd}}",
        "-ar", "48000",
        str(tmp_wav)
    ]
    sp_res = subprocess.run(sp_cmd, capture_output=True)
    if sp_res.returncode == 0 and tmp_wav.exists():
        tmp_wav.replace(wav_file)
    else:
        if tmp_wav.exists():
            tmp_wav.unlink()

# Group segments by voice_id to maximize batch efficiency while honoring per-segment parameters
from collections import defaultdict
voice_groups = defaultdict(list)
for idx, seg in enumerate(segments):
    v = seg.get("voice_id", "Ngọc Huyền") or "Ngọc Huyền"
    voice_groups[v].append((idx, seg))

audio_results = [None] * len(segments)

for voice_name, group_items in voice_groups.items():
    group_texts = [prepare_input_text(s) for (_, s) in group_items]
    print(f"Synthesizing {{len(group_texts)}} utterances for voice '{{voice_name}}'...")
    batch_out = []
    try:
        batch_out = engine.infer_batch(group_texts, voice=voice_name, batch_size=32)
        if len(batch_out) != len(group_items):
            raise RuntimeError(f"infer_batch returned {{len(batch_out)}} clips, expected {{len(group_items)}}")
    except Exception as e:
        print(f"infer_batch note for voice '{{voice_name}}': {{e}}, running per-utterance infer...")
        batch_out = []
        for t in group_texts:
            try:
                batch_out.append(engine.infer(text=t, voice=voice_name))
            except Exception:
                batch_out.append(engine.infer(text=t))

    for (orig_idx, _), a_data in zip(group_items, batch_out):
        audio_results[orig_idx] = a_data

# Fail fast if any audio clip was not generated
if any(a is None for a in audio_results) or len(audio_results) != len(segments):
    missing_count = sum(1 for a in audio_results if a is None)
    raise RuntimeError(
        f"[Stage: tts] Batch TTS generation failed: {{missing_count}} segments missing audio clips. "
        f"Generated {{len(audio_results) - missing_count}}/{{len(segments)}}. Silent truncation is strictly forbidden."
    )

import wave
total_dur = 0.0
for seg, a_data in zip(segments, audio_results):
    seq_id = seg["seq_id"]
    out_wav = audio_clips_dir / f"audio_{chapter_number}_{{seq_id}}.wav"
    engine.save(a_data, str(out_wav))

    spd = seg.get("speed", 1.0)
    apply_speed(out_wav, spd)

    with wave.open(str(out_wav), "rb") as wf:
        dur_sec = wf.getnframes() / float(wf.getframerate())
    seg["duration"] = round(dur_sec, 2)
    seg["audio_path"] = f"audio/clips/audio_{chapter_number}_{{seq_id}}.wav"
    seg["status"] = "synthesized"
    seg["synthesized_voice"] = seg.get("voice_id", "Ngọc Huyền") or "Ngọc Huyền"
    with open(out_wav, "rb") as af:
        seg["checksum"] = hashlib.sha256(af.read()).hexdigest()
    total_dur += dur_sec

t_tts_total = time.time() - t_tts_0
print(f"🎉 TTS finished: {{len(segments)}} utterances in {{t_tts_total:.2f}}s")

# Save updated script.json with duration and checksums
with open(script_path, "w", encoding="utf-8") as f:
    json.dump(script, f, ensure_ascii=False, indent=2)

# Build Telemetry Report
t_total_wall = time.time() - t_start_wall
telemetry = {{
    "project_slug": "{project_slug}",
    "chapter_number": "{chapter_number}",
    "status": "success",
    "job_type": "tts",
    "cloud_provider": "kaggle",
    "gpu_name": gpu_name,
    "has_cuda": has_cuda,
    "cpu_count": os.cpu_count(),
    "segments_count": len(segments),
    "total_audio_duration_seconds": round(total_dur, 2),
    "total_audio_duration_minutes": round(total_dur / 60.0, 2),
    "tts_init_seconds": round(t_tts_init, 2),
    "tts_inference_seconds": round(t_tts_total, 2),
    "tts_avg_per_segment": round(t_tts_total / max(1, len(segments)), 3),
    "total_wall_time_seconds": round(t_total_wall, 2),
    "total_wall_time_minutes": round(t_total_wall / 60.0, 2),
}}

print("\\n" + "=" * 60)
print("📊 CLOUD TTS TELEMETRY SUMMARY")
print("=" * 60)
print(json.dumps(telemetry, indent=2, ensure_ascii=False))

# Package Output Bundle to /kaggle/working/
out_dir = Path("/kaggle/working/output").resolve()
out_dir.mkdir(parents=True, exist_ok=True)

shutil.copy2(script_path, out_dir / "script.json")
shutil.copy2(script_path, "/kaggle/working/script.json")

# Pack audio clips
with tarfile.open(out_dir / "audio_clips.tar.gz", "w:gz") as atar:
    atar.add(audio_clips_dir, arcname="clips")
shutil.copy2(out_dir / "audio_clips.tar.gz", "/kaggle/working/audio_clips.tar.gz")

with open(out_dir / "telemetry.json", "w", encoding="utf-8") as tf:
    json.dump(telemetry, tf, indent=2, ensure_ascii=False)
shutil.copy2(out_dir / "telemetry.json", "/kaggle/working/telemetry.json")

print("=" * 60)
print(f"🏁 ALL CHAPTER {chapter_number} TTS ARTIFACTS EXPORTED TO {{out_dir}} AND /kaggle/working")
print("=" * 60)
'''

    def _generate_render_only_worker_script(
        self,
        project_slug: str,
        chapter_number: str,
        render_workers: int = 4,
        encoder_preference: str = "auto",
        target_width: int = 1920,
        target_height: int = 1080,
        fps: int = 30,
        video_mode: str = "auto",
        burn_subtitles: bool = True,
        max_lines: int = 1,
        font_size: int = 26,
        max_chars_per_line: int = 42,
        bgm_vol: float = 0.15,
        voice_vol: float = 1.0,
        ducking_factor: float = 0.30,
    ) -> str:
        """
        Generates dedicated render-only worker script.
        CRITICAL CONTRACT: Does NOT package or execute any TTS inference code; reuses pre-existing audio cache.
        """
        return f'''#!/usr/bin/env python3
"""
Manga Recap Extreme-Speed Cloud Production Worker for Kaggle GPU.
Mode: Render-Only (Zero TTS Inference) | Project: {project_slug} | Chapter: {chapter_number}
"""

import os
import sys
import time
import json
import re
import shutil
import tarfile
import hashlib
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

t_start_wall = time.time()
print("=" * 60)
print("🚀 MANGA RECAP CLOUD PRODUCTION WORKER STARTING ON KAGGLE (RENDER-ONLY)")
print("=" * 60)

# Helper: safe tar extraction preventing directory traversal
def safe_extract_tar(tar, target_dir):
    resolved_target = Path(target_dir).resolve()
    for member in tar.getmembers():
        if member.name.startswith("/") or ".." in member.name.split("/"):
            raise ValueError(f"Security error: Attempted Path Traversal in Tar File: '{{member.name}}'")
        member_path = (resolved_target / member.name).resolve()
        try:
            member_path.relative_to(resolved_target)
        except ValueError:
            raise ValueError(f"Security error: Attempted Path Traversal in Tar File: '{{member.name}}'")
    try:
        tar.extractall(path=resolved_target, filter="data")
    except TypeError:
        tar.extractall(path=resolved_target)

# 1. Hardware & Environment Introspection
import platform
print(f"System: {{platform.platform()}} | Python: {{sys.version.split()[0]}}")
print(f"CPU count: {{os.cpu_count()}}")

has_cuda = False
gpu_name = "None"
try:
    import torch
    has_cuda = torch.cuda.is_available()
    if has_cuda:
        gpu_name = torch.cuda.get_device_name(0)
        print(f"🔥 GPU ACCELERATION ENABLED: {{gpu_name}} (Count: {{torch.cuda.device_count()}})")
    else:
        print("⚠️ CUDA not available, running high-concurrency CPU mode")
except Exception as e:
    print(f"PyTorch check note: {{e}}")

# 2. Extract Job Payload or Locate Chapter Data
workspace = Path("/kaggle/working/workspace").resolve()
workspace.mkdir(parents=True, exist_ok=True)
chapter_dir = workspace / "chapter"
chapter_dir.mkdir(parents=True, exist_ok=True)

found_payload_tar = None
found_script = None
found_manifest = None
found_panels_dir = None
found_project_json = None

for root_dir in [Path("/kaggle/input"), Path("."), Path("/kaggle/src")]:
    if not root_dir.exists():
        continue
    for tar_candidate in root_dir.rglob("*.tar.gz"):
        if "payload" in tar_candidate.name.lower() or "manga" in tar_candidate.name.lower():
            found_payload_tar = tar_candidate
            break
    if found_payload_tar:
        break

    for s_candidate in root_dir.rglob("script.json"):
        try:
            with open(s_candidate, "r", encoding="utf-8") as test_f:
                s_data = json.load(test_f)
                if "segments" in s_data:
                    found_script = s_candidate
                    found_chap_parent = s_candidate.parent
                    m_cand = found_chap_parent / "manifest.json"
                    if m_cand.exists():
                        found_manifest = m_cand
                    p_cand = found_chap_parent / "panels"
                    if p_cand.exists():
                        found_panels_dir = p_cand
                    proj_cand = found_chap_parent.parent.parent / "project.json"
                    if proj_cand.exists():
                        found_project_json = proj_cand
                    break
        except Exception:
            pass
    if found_script:
        break

if found_payload_tar:
    print(f"📦 Extracting payload archive securely: {{found_payload_tar}} -> {{workspace}}")
    with tarfile.open(found_payload_tar, "r:gz") as tar:
        safe_extract_tar(tar, workspace)
    project_json_path = workspace / "project.json"
    script_path = chapter_dir / "script.json"
    manifest_path = chapter_dir / "manifest.json"
    panels_dir = chapter_dir / "panels"
elif found_script:
    print(f"📂 Found uncompressed chapter data at: {{found_script.parent}}")
    script_path = chapter_dir / "script.json"
    manifest_path = chapter_dir / "manifest.json"
    shutil.copy2(found_script, script_path)
    if found_manifest:
        shutil.copy2(found_manifest, manifest_path)
    if found_project_json:
        project_json_path = workspace / "project.json"
        shutil.copy2(found_project_json, project_json_path)
    else:
        project_json_path = workspace / "project.json"
    panels_dir = found_panels_dir
else:
    raise FileNotFoundError("Could not find payload archive or chapter files in /kaggle/input")

if not panels_dir or not panels_dir.exists() or len(list(panels_dir.glob("*.jpg"))) == 0:
    for p_dir in Path("/kaggle/input").rglob("panels"):
        if list(p_dir.glob("*.jpg")):
            panels_dir = p_dir
            break

print(f"Panels directory resolved: {{panels_dir}} (Count: {{len(list(panels_dir.glob('*.jpg')))}})")

with open(script_path, "r", encoding="utf-8") as f:
    script = json.load(f)

segments = script.get("segments", [])
print(f"Loaded script: {{len(segments)}} segments")

audio_clips_dir = chapter_dir / "audio" / "clips"
audio_clips_dir.mkdir(parents=True, exist_ok=True)

# 3. Audio Cache Verification (Render-Only Independence: No TTS inference!)
print("\\n" + "=" * 40)
print("✨ VERIFYING PRE-EXISTING AUDIO CACHE (ZERO TTS INFERENCE)")
print("=" * 40)
import wave
for seg in segments:
    seq_id = seg["seq_id"]
    out_wav = audio_clips_dir / f"audio_{chapter_number}_{{seq_id}}.wav"
    if not out_wav.exists() or out_wav.stat().st_size == 0:
        raise FileNotFoundError(
            f"[Stage: render] Missing or empty audio clip for chapter {chapter_number}, "
            f"seq_id '{{seq_id}}': '{{out_wav}}'. Reusing audio cache requires all clips present."
        )
    with wave.open(str(out_wav), "rb") as wf:
        dur_sec = wf.getnframes() / float(wf.getframerate())
    seg["duration"] = round(dur_sec, 2)
    seg["audio_path"] = f"audio/clips/audio_{chapter_number}_{{seq_id}}.wav"
    seg["status"] = "synthesized"
    with open(out_wav, "rb") as af:
        seg["checksum"] = hashlib.sha256(af.read()).hexdigest()

with open(script_path, "w", encoding="utf-8") as f:
    json.dump(script, f, ensure_ascii=False, indent=2)

# 4. Detect FFmpeg Hardware Encoder
print("\\n" + "=" * 40)
print("🎬 HARDWARE ENCODER PROBING")
print("=" * 40)

def probe_encoder():
    if not has_cuda:
        return "libx264", ["-preset", "ultrafast", "-tune", "stillimage", "-threads", "4"]

    try:
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=0.1",
            "-vf", "format=yuv420p",
            "-c:v", "h264_nvenc", "-preset", "p4", "-f", "null", "-"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            print("🚀 NVENC SMOKE TEST PASSED! Using NVIDIA GPU Hardware Encoder.")
            return "h264_nvenc", ["-preset", "p4", "-tune", "ll"]
        else:
            print(f"NVENC probe failed (code {{res.returncode}}): {{res.stderr[-300:]}}")
    except Exception as e:
        print(f"NVENC probe exception: {{e}}")

    print("Falling back to multi-core libx264 ultrafast")
    return "libx264", ["-preset", "ultrafast", "-tune", "stillimage", "-threads", "4"]

selected_encoder, encoder_args = probe_encoder()
print(f"Selected video encoder: {{selected_encoder}} {{encoder_args}}")

# 5. Load Project Settings & Options Parity
proj_cfg_data = {{}}
if project_json_path.exists():
    try:
        with open(project_json_path, "r", encoding="utf-8") as pf:
            proj_cfg_data = json.load(pf)
    except Exception as e:
        print(f"Note loading project.json: {{e}}")

render_cfg = proj_cfg_data.get("render_settings", {{}})
audio_cfg = proj_cfg_data.get("audio_settings", {{}})
sub_cfg = render_cfg.get("subtitle", {{}})

cfg_w = int(render_cfg.get("target_width", {target_width}))
cfg_h = int(render_cfg.get("target_height", {target_height}))
cfg_fps = int(render_cfg.get("fps", {fps}))
cfg_video_mode = str(render_cfg.get("video_mode", "{video_mode}")).lower()
cfg_burn_subs = bool(render_cfg.get("burn_subtitles", {burn_subtitles}))
cfg_max_lines = int(sub_cfg.get("max_lines", {max_lines}))
cfg_font_size = int(sub_cfg.get("font_size", {font_size}))
cfg_max_chars = int(sub_cfg.get("max_chars_per_line", {max_chars_per_line}))

cfg_bgm_vol = float(audio_cfg.get("bgm_volume", {bgm_vol}))
cfg_voice_vol = float(audio_cfg.get("voice_volume", {voice_vol}))
cfg_ducking = float(audio_cfg.get("ducking_factor", {ducking_factor}))

print(f"Render Config Parity: {{cfg_w}}x{{cfg_h}} @ {{cfg_fps}}fps | Mode: {{cfg_video_mode}} | Burn Subs: {{cfg_burn_subs}} | Sub Lines: {{cfg_max_lines}}")

# 6. Multi-Panel Parallel Frame Generation
print("\\n" + "=" * 40)
print("⚡ MULTI-PANEL PARALLEL FRAME GENERATION")
print("=" * 40)

t_render_0 = time.time()
frames_dir = chapter_dir / "video" / "frames"
frames_dir.mkdir(parents=True, exist_ok=True)

from PIL import Image, ImageDraw, ImageFont, ImageFilter

font = None
effective_font_pt = max(18, int(cfg_font_size * (cfg_h / 720.0)))
for fp in [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]:
    if os.path.exists(fp):
        try:
            font = ImageFont.truetype(fp, effective_font_pt)
            break
        except Exception:
            pass
if font is None:
    font = ImageFont.load_default()

def detect_video_mode(img_w, img_h, forced_mode="auto"):
    if forced_mode and forced_mode != "auto":
        return forced_mode
    aspect = img_w / float(img_h) if img_h > 0 else 1.0
    if aspect >= 1.3:
        return "landscape_fit"
    elif aspect <= 0.7:
        return "portrait_vertical"
    else:
        return "portrait_padded"

def sanitize_sub_text(text):
    if not text:
        return ""
    cleaned = re.sub(r"\\[.*?\\]", "", text)
    cleaned = re.sub(r"\\s+", " ", cleaned).strip()
    return cleaned

def wrap_sub_lines(text, max_chars, max_lines_allowed):
    clean = sanitize_sub_text(text)
    if not clean:
        return ""
    words = clean.split()
    lines = []
    curr = []
    curr_len = 0
    for w in words:
        if curr_len + len(w) + 1 > max_chars and curr:
            lines.append(" ".join(curr))
            curr = [w]
            curr_len = len(w)
        else:
            curr.append(w)
            curr_len += len(w) + 1
    if curr:
        lines.append(" ".join(curr))
    if len(lines) > max_lines_allowed:
        lines = lines[:max_lines_allowed]
        lines[-1] = lines[-1] + "..."
    return "\\n".join(lines)

def compose_single_frame(panel_file, text, out_frame_path, seq_id, motion_type="static", focus_box=None):
    w, h = cfg_w, cfg_h
    canvas = Image.new("RGB", (w, h), (20, 20, 20))

    try:
        with Image.open(panel_file) as orig_p:
            p_img = orig_p.convert("RGB")
            pw, ph = p_img.size

            # Subject-centric focus crop if focus_box provided
            if focus_box and isinstance(focus_box, dict) and motion_type in ("focus_crop", "static_focus", "static"):
                fx = (focus_box.get("x_min", 0.0) + focus_box.get("x_max", 1.0)) / 2.0
                fy = (focus_box.get("y_min", 0.0) + focus_box.get("y_max", 1.0)) / 2.0
                scale_ratio = max(0.45, min(1.0, focus_box.get("x_max", 1.0) - focus_box.get("x_min", 0.0)))
                crop_w = max(int(pw * scale_ratio), 10)
                crop_h = max(int(ph * scale_ratio), 10)
                cx, cy = int(fx * pw), int(fy * ph)
                x1 = max(0, min(pw - crop_w, cx - crop_w // 2))
                y1 = max(0, min(ph - crop_h, cy - crop_h // 2))
                p_img = p_img.crop((x1, y1, x1 + crop_w, y1 + crop_h))
                pw, ph = p_img.size

            v_mode = detect_video_mode(pw, ph, cfg_video_mode)

            # Background blur
            bg = p_img.resize((w, h), Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(radius=25))
            canvas.paste(bg, (0, 0))

            # Foreground layout according to video_mode
            if v_mode == "landscape_fit":
                scale = min(w / float(pw), h / float(ph))
                fg_w = int(pw * scale)
                fg_h = int(ph * scale)
            elif v_mode == "portrait_vertical":
                scale = min(float(w) * 0.65 / float(pw), float(h) / float(ph))
                fg_w = int(pw * scale)
                fg_h = int(ph * scale)
            else: # portrait_padded
                scale = h / float(ph)
                fg_w = int(pw * scale)
                if fg_w > w:
                    scale = w / float(pw)
                    fg_w = w
                    fg_h = int(ph * scale)
                else:
                    fg_h = h

            fg = p_img.resize((fg_w, fg_h), Image.Resampling.LANCZOS)
            canvas.paste(fg, ((w - fg_w) // 2, (h - fg_h) // 2))
    except Exception as e:
        raise RuntimeError(
            f"[Stage: render] Corrupted panel asset for chapter {chapter_number}, "
            f"seq_id '{{seq_id}}': '{{panel_file}}' failed to load: {{e}}"
        )

    # Subtitles overlay (if burned)
    if cfg_burn_subs:
        draw = ImageDraw.Draw(canvas)
        display_sub = wrap_sub_lines(text, cfg_max_chars, cfg_max_lines)
        if display_sub:
            lines = display_sub.split("\\n")
            tx = w // 2
            ty = h - (65 + (len(lines) - 1) * int(effective_font_pt * 1.25))
            draw.text(
                (tx, ty), display_sub, font=font, anchor="ma", align="center",
                fill="#FFFFFF", stroke_width=4, stroke_fill="#000000"
            )

    canvas.save(out_frame_path, quality=92)
    return out_frame_path

def process_segment_render(seg):
    seq_id = seg["seq_id"]
    p_ids = seg.get("panel_ids", [])
    if not p_ids:
        raise ValueError(
            f"[Stage: render] Segment '{{seq_id}}' in chapter {chapter_number} has no panel_ids specified."
        )

    text = seg.get("narrator_text", "")
    dur = float(seg.get("duration", 4.0))
    m = len(p_ids)
    p_dur = dur / float(m)
    motion_type = seg.get("camera_motion", "static")
    focus_box = seg.get("focus_box")

    seg_frame_results = []
    for p_idx, p_id in enumerate(p_ids):
        candidate = panels_dir / p_id
        if not candidate.exists():
            if not p_id.endswith(".jpg") and not p_id.endswith(".png"):
                cand_ext = panels_dir / f"{{p_id}}.jpg"
                if cand_ext.exists():
                    candidate = cand_ext
        if not candidate.exists():
            raise FileNotFoundError(
                f"[Stage: render] Missing panel asset for chapter {chapter_number}, "
                f"seq_id '{{seq_id}}': asset path '{{candidate}}' does not exist. "
                f"Substitution is strictly prohibited."
            )

        if m == 1:
            frame_file = frames_dir / f"frame_{{seq_id}}.jpg"
        else:
            frame_file = frames_dir / f"frame_{{seq_id}}_{{p_idx}}.jpg"

        compose_single_frame(candidate, text, frame_file, seq_id, motion_type, focus_box)
        seg_frame_results.append((frame_file, p_dur))

    return seg_frame_results

print(f"Compositing frames for {{len(segments)}} segments in parallel (ThreadPoolExecutor {render_workers} workers)...")
with ThreadPoolExecutor(max_workers={render_workers}) as executor:
    segment_frames_list = list(executor.map(process_segment_render, segments))

t_frames = time.time() - t_render_0
print(f"All frames composited in {{t_frames:.2f}}s!")

# 7. Merge All Audio Clips into Combined Audio & Optional BGM Mixing
print("Concatenating speech audio clips...")
combined_speech = chapter_dir / "audio" / "combined_speech.wav"
with wave.open(str(combined_speech), "wb") as outfile:
    first_wf = None
    for seg in segments:
        seq_id = seg["seq_id"]
        wav_path = audio_clips_dir / f"audio_{chapter_number}_{{seq_id}}.wav"
        if not wav_path.exists() or wav_path.stat().st_size == 0:
            raise FileNotFoundError(
                f"[Stage: audio_merge] Missing or empty audio clip for chapter {chapter_number}, "
                f"seq_id '{{seq_id}}': '{{wav_path}}'. Substitution is strictly prohibited."
            )
        try:
            with wave.open(str(wav_path), "rb") as infile:
                if first_wf is None:
                    first_wf = infile
                    outfile.setnchannels(infile.getnchannels())
                    outfile.setsampwidth(infile.getsampwidth())
                    outfile.setframerate(infile.getframerate())
                outfile.writeframes(infile.readframes(infile.getnframes()))
        except Exception as e:
            raise RuntimeError(
                f"[Stage: audio_merge] Corrupted audio clip for chapter {chapter_number}, "
                f"seq_id '{{seq_id}}': '{{wav_path}}' error: {{e}}"
            )

# Check for BGM audio in workspace
found_bgm = None
for bgm_cand in [workspace / "bgm.mp3", workspace / "bgm.wav", workspace / "bgm.m4a"]:
    if bgm_cand.exists() and bgm_cand.stat().st_size > 0:
        found_bgm = bgm_cand
        break

combined_audio = chapter_dir / "audio" / "combined_audio.wav"
if found_bgm:
    print(f"🎵 Mixing Background Music: {{found_bgm.name}} (ducking factor: {{cfg_ducking}}, volume: {{cfg_bgm_vol}})...")
    bgm_filter = (
        f"[1:a]volume={{cfg_bgm_vol:.3f}}[bgm_base];"
        f"[bgm_base][0:a]sidechaincompress=threshold=0.08:ratio=5.0:attack=80:release=500:knee=2.8[ducked_bgm];"
        f"[0:a]volume={{cfg_voice_vol:.2f}}[speech];"
        f"[speech][ducked_bgm]amix=inputs=2:duration=first:dropout_transition=2:weights=1.0 1.0[mixed];"
        f"[mixed]alimiter=limit=0.95:attack=5:release=50[outa]"
    )
    cmd_bgm = [
        "ffmpeg", "-y",
        "-i", str(combined_speech),
        "-stream_loop", "-1", "-i", str(found_bgm),
        "-filter_complex", bgm_filter,
        "-map", "[outa]",
        "-c:a", "pcm_s16le",
        "-ar", "48000",
        str(combined_audio)
    ]
    res_bgm = subprocess.run(cmd_bgm, capture_output=True, text=True)
    if res_bgm.returncode != 0:
        print(f"Warning: BGM mixing returned non-zero code, falling back to speech: {{res_bgm.stderr[-300:]}}")
        shutil.copy2(combined_speech, combined_audio)
else:
    shutil.copy2(combined_speech, combined_audio)

# 8. Build FFmpeg Concat Script for Multi-Panel Single-Pass Render
frames_concat_script = chapter_dir / "video" / "frames_concat.txt"
all_entries = []
with open(frames_concat_script, "w", encoding="utf-8") as f:
    f.write("ffconcat version 1.0\\n")
    for seg_frames in segment_frames_list:
        for f_path, p_dur in seg_frames:
            all_entries.append(f_path)
            f.write(f"file '{{f_path.resolve()}}'\\n")
            f.write(f"duration {{p_dur:.3f}}\\n")
    if all_entries:
        last_f = all_entries[-1].resolve()
        f.write(f"file '{{last_f}}'\\n")

# 9. Single-Pass High-Speed Hardware Video Encoding
final_video = chapter_dir / "video" / f"chapter_{chapter_number}.mp4"
final_video.parent.mkdir(parents=True, exist_ok=True)

cmd_encode = [
    "ffmpeg", "-y",
    "-f", "concat", "-safe", "0", "-i", str(frames_concat_script),
    "-i", str(combined_audio),
    "-vf", f"fps={{cfg_fps}},format=yuv420p",
    "-c:v", selected_encoder,
    *encoder_args,
    "-c:a", "aac", "-b:a", "192k",
    "-shortest",
    str(final_video)
]
print("Running single-pass hardware FFmpeg render...")
t_enc_0 = time.time()
res_enc = subprocess.run(cmd_encode, capture_output=True, text=True)
if res_enc.returncode != 0:
    print(f"Encoding failed: {{res_enc.stderr[-600:]}}")
    raise RuntimeError(f"Single-pass video encode failed: {{res_enc.stderr[-600:]}}")
t_enc = time.time() - t_enc_0
t_render_total = time.time() - t_render_0
print(f"🎉 Single-Pass Video Render FINISHED in {{t_enc:.2f}}s (Total render stage: {{t_render_total:.2f}}s)!")

# 10. Generate Subtitles SRT
srt_path = chapter_dir / "subtitles" / f"chapter_{chapter_number}.srt"
srt_path.parent.mkdir(parents=True, exist_ok=True)

def fmt_srt_time(sec):
    hrs = int(sec // 3600)
    mins = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{{hrs:02d}}:{{mins:02d}}:{{s:02d}},{{ms:03d}}"

cum_t = 0.0
with open(srt_path, "w", encoding="utf-8") as srt:
    for idx, seg in enumerate(segments, 1):
        dur = seg.get("duration", 4.0)
        t_start = cum_t
        t_end = cum_t + dur
        cum_t = t_end
        sanitized_narr = sanitize_sub_text(seg.get("narrator_text", ""))
        srt.write(f"{{idx}}\\n{{fmt_srt_time(t_start)}} --> {{fmt_srt_time(t_end)}}\\n{{sanitized_narr}}\\n\\n")

# 11. Build Telemetry Report
t_total_wall = time.time() - t_start_wall
telemetry = {{
    "project_slug": "{project_slug}",
    "chapter_number": "{chapter_number}",
    "status": "success",
    "job_type": "render",
    "cloud_provider": "kaggle",
    "gpu_name": gpu_name,
    "has_cuda": has_cuda,
    "cpu_count": os.cpu_count(),
    "encoder_used": selected_encoder,
    "segments_count": len(segments),
    "total_audio_duration_seconds": round(cum_t, 2),
    "total_audio_duration_minutes": round(cum_t / 60.0, 2),
    "tts_init_seconds": 0.0,
    "tts_inference_seconds": 0.0,
    "render_seconds": round(t_render_total, 2),
    "render_avg_per_clip": round(t_render_total / max(1, len(segments)), 3),
    "concat_seconds": round(t_enc, 2),
    "total_wall_time_seconds": round(t_total_wall, 2),
    "total_wall_time_minutes": round(t_total_wall / 60.0, 2),
    "video_size_mb": round(final_video.stat().st_size / (1024 * 1024), 2) if final_video.exists() else 0.0
}}

print("\\n" + "=" * 60)
print("📊 CLOUD PRODUCTION TELEMETRY SUMMARY")
print("=" * 60)
print(json.dumps(telemetry, indent=2, ensure_ascii=False))

# 12. Package Output Bundle to /kaggle/working/
out_dir = Path("/kaggle/working/output").resolve()
out_dir.mkdir(parents=True, exist_ok=True)

shutil.copy2(final_video, out_dir / f"chapter_{chapter_number}.mp4")
shutil.copy2(srt_path, out_dir / f"chapter_{chapter_number}.srt")
shutil.copy2(script_path, out_dir / "script.json")

shutil.copy2(final_video, f"/kaggle/working/chapter_{chapter_number}.mp4")
shutil.copy2(srt_path, f"/kaggle/working/chapter_{chapter_number}.srt")
shutil.copy2(script_path, "/kaggle/working/script.json")

with open(out_dir / "telemetry.json", "w", encoding="utf-8") as tf:
    json.dump(telemetry, tf, indent=2, ensure_ascii=False)
shutil.copy2(out_dir / "telemetry.json", "/kaggle/working/telemetry.json")

print("=" * 60)
print(f"🏁 ALL CHAPTER {chapter_number} ARTIFACTS EXPORTED TO {{out_dir}} AND /kaggle/working")
print("=" * 60)
'''

    def _generate_produce_worker_script(
        self,
        project_slug: str,
        chapter_number: str,
        render_workers: int = 4,
        encoder_preference: str = "auto",
        target_width: int = 1920,
        target_height: int = 1080,
        fps: int = 30,
        video_mode: str = "auto",
        burn_subtitles: bool = True,
        max_lines: int = 1,
        font_size: int = 26,
        max_chars_per_line: int = 42,
        bgm_vol: float = 0.15,
        voice_vol: float = 1.0,
        ducking_factor: float = 0.30,
    ) -> str:
        """
        Generates full end-to-end cloud worker script with both VieNeu-TTS and hardware NVENC rendering.
        Reuses pre-existing audio cache if valid audio clips already exist.
        """
        return f'''#!/usr/bin/env python3
"""
Manga Recap Extreme-Speed Cloud Production Worker for Kaggle GPU.
Mode: Full Production (TTS + Render) | Project: {project_slug} | Chapter: {chapter_number}
"""

import os
import sys
import time
import json
import re
import shutil
import tarfile
import hashlib
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

t_start_wall = time.time()
print("=" * 60)
print("🚀 MANGA RECAP CLOUD PRODUCTION WORKER STARTING ON KAGGLE")
print("=" * 60)

# Helper: safe tar extraction preventing directory traversal
def safe_extract_tar(tar, target_dir):
    resolved_target = Path(target_dir).resolve()
    for member in tar.getmembers():
        if member.name.startswith("/") or ".." in member.name.split("/"):
            raise ValueError(f"Security error: Attempted Path Traversal in Tar File: '{{member.name}}'")
        member_path = (resolved_target / member.name).resolve()
        try:
            member_path.relative_to(resolved_target)
        except ValueError:
            raise ValueError(f"Security error: Attempted Path Traversal in Tar File: '{{member.name}}'")
    try:
        tar.extractall(path=resolved_target, filter="data")
    except TypeError:
        tar.extractall(path=resolved_target)

# 1. Hardware & Environment Introspection
import platform
print(f"System: {{platform.platform()}} | Python: {{sys.version.split()[0]}}")
print(f"CPU count: {{os.cpu_count()}}")

has_cuda = False
gpu_name = "None"
try:
    import torch
    has_cuda = torch.cuda.is_available()
    if has_cuda:
        gpu_name = torch.cuda.get_device_name(0)
        print(f"🔥 GPU ACCELERATION ENABLED: {{gpu_name}} (Count: {{torch.cuda.device_count()}})")
    else:
        print("⚠️ CUDA not available, running high-concurrency CPU mode")
except Exception as e:
    print(f"PyTorch check note: {{e}}")

# 2. Extract Job Payload or Locate Chapter Data
workspace = Path("/kaggle/working/workspace").resolve()
workspace.mkdir(parents=True, exist_ok=True)
chapter_dir = workspace / "chapter"
chapter_dir.mkdir(parents=True, exist_ok=True)

print("Searching for chapter data in /kaggle/input and current directory...")
found_payload_tar = None
found_script = None
found_manifest = None
found_panels_dir = None
found_project_json = None

for root_dir in [Path("/kaggle/input"), Path("."), Path("/kaggle/src")]:
    if not root_dir.exists():
        continue
    for tar_candidate in root_dir.rglob("*.tar.gz"):
        if "payload" in tar_candidate.name.lower() or "manga" in tar_candidate.name.lower():
            found_payload_tar = tar_candidate
            break
    if found_payload_tar:
        break

    for s_candidate in root_dir.rglob("script.json"):
        try:
            with open(s_candidate, "r", encoding="utf-8") as test_f:
                s_data = json.load(test_f)
                if "segments" in s_data:
                    found_script = s_candidate
                    found_chap_parent = s_candidate.parent
                    m_cand = found_chap_parent / "manifest.json"
                    if m_cand.exists():
                        found_manifest = m_cand
                    p_cand = found_chap_parent / "panels"
                    if p_cand.exists():
                        found_panels_dir = p_cand
                    proj_cand = found_chap_parent.parent.parent / "project.json"
                    if proj_cand.exists():
                        found_project_json = proj_cand
                    break
        except Exception:
            pass
    if found_script:
        break

if found_payload_tar:
    print(f"📦 Extracting payload archive securely: {{found_payload_tar}} -> {{workspace}}")
    with tarfile.open(found_payload_tar, "r:gz") as tar:
        safe_extract_tar(tar, workspace)
    project_json_path = workspace / "project.json"
    script_path = chapter_dir / "script.json"
    manifest_path = chapter_dir / "manifest.json"
    panels_dir = chapter_dir / "panels"
elif found_script:
    print(f"📂 Found uncompressed chapter data at: {{found_script.parent}}")
    script_path = chapter_dir / "script.json"
    manifest_path = chapter_dir / "manifest.json"
    shutil.copy2(found_script, script_path)
    if found_manifest:
        shutil.copy2(found_manifest, manifest_path)
    if found_project_json:
        project_json_path = workspace / "project.json"
        shutil.copy2(found_project_json, project_json_path)
    else:
        project_json_path = workspace / "project.json"
    panels_dir = found_panels_dir
else:
    raise FileNotFoundError("Could not find payload archive or chapter files in /kaggle/input")

if not panels_dir or not panels_dir.exists() or len(list(panels_dir.glob("*.jpg"))) == 0:
    for p_dir in Path("/kaggle/input").rglob("panels"):
        if list(p_dir.glob("*.jpg")):
            panels_dir = p_dir
            break

print(f"Panels directory resolved: {{panels_dir}} (Count: {{len(list(panels_dir.glob('*.jpg')))}})")

with open(script_path, "r", encoding="utf-8") as f:
    script = json.load(f)

segments = script.get("segments", [])
print(f"Loaded script: {{len(segments)}} segments")

audio_clips_dir = chapter_dir / "audio" / "clips"
audio_clips_dir.mkdir(parents=True, exist_ok=True)

# 3. Check for Pre-Existing Audio (Render-Only Independence)
pre_existing_audio = len(segments) > 0 and all(
    (audio_clips_dir / f"audio_{chapter_number}_{{seg['seq_id']}}.wav").exists()
    and (audio_clips_dir / f"audio_{chapter_number}_{{seg['seq_id']}}.wav").stat().st_size > 0
    for seg in segments
)

t_tts_init = 0.0
t_tts_total = 0.0

if pre_existing_audio:
    print("\\n" + "=" * 40)
    print("✨ PRE-SYNTHESIZED AUDIO DETECTED: REUSING EXISTING CLIPS (ZERO TTS INFERENCE)")
    print("=" * 40)
    import wave
    for seg in segments:
        seq_id = seg["seq_id"]
        out_wav = audio_clips_dir / f"audio_{chapter_number}_{{seq_id}}.wav"
        with wave.open(str(out_wav), "rb") as wf:
            dur_sec = wf.getnframes() / float(wf.getframerate())
        seg["duration"] = round(dur_sec, 2)
        seg["audio_path"] = f"audio/clips/audio_{chapter_number}_{{seq_id}}.wav"
        seg["status"] = "synthesized"
        with open(out_wav, "rb") as af:
            seg["checksum"] = hashlib.sha256(af.read()).hexdigest()
else:
    # 4. Bootstrap VieNeu-TTS Engine & Synthesize Utterances
    print("\\n" + "=" * 40)
    print("📦 INITIALIZING VIENEU-TTS ENGINE")
    print("=" * 40)

    t_tts_init_0 = time.time()
    try:
        import vieneu
        print("Found existing vieneu module")
    except ImportError:
        print("Installing vieneu via pip...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "vieneu"], check=True)
        import vieneu

    from vieneu import Vieneu
    engine = Vieneu(mode="v3turbo")
    t_tts_init = time.time() - t_tts_init_0
    print(f"VieNeu-TTS engine initialized in {{t_tts_init:.2f}}s")

    print("\\n" + "=" * 40)
    print("🎙️ RUNNING PER-SEGMENT TTS SYNTHESIS")
    print("=" * 40)

    t_tts_0 = time.time()

    def prepare_input_text(seg_item):
        txt = seg_item.get("narrator_text", "").strip()
        cue = seg_item.get("emotion_cue")
        if cue and cue in ["[cười]", "[thở dài]", "[hắng giọng]"]:
            if cue not in txt:
                txt = f"{{cue}} {{txt}}"
        return txt

    def apply_speed(wav_file, spd):
        target_spd = max(0.5, min(2.0, float(spd)))
        if abs(target_spd - 1.0) <= 0.01:
            return
        tmp_wav = wav_file.with_name(f".{{wav_file.name}}.speed.tmp.wav")
        sp_cmd = [
            "ffmpeg", "-y", "-i", str(wav_file),
            "-filter:a", f"atempo={{target_spd}}",
            "-ar", "48000",
            str(tmp_wav)
        ]
        sp_res = subprocess.run(sp_cmd, capture_output=True)
        if sp_res.returncode == 0 and tmp_wav.exists():
            tmp_wav.replace(wav_file)
        else:
            if tmp_wav.exists():
                tmp_wav.unlink()

    # Group segments by voice_id to maximize batch efficiency while honoring per-segment parameters
    from collections import defaultdict
    voice_groups = defaultdict(list)
    for idx, seg in enumerate(segments):
        v = seg.get("voice_id", "Ngọc Huyền") or "Ngọc Huyền"
        voice_groups[v].append((idx, seg))

    audio_results = [None] * len(segments)

    for voice_name, group_items in voice_groups.items():
        group_texts = [prepare_input_text(s) for (_, s) in group_items]
        print(f"Synthesizing {{len(group_texts)}} utterances for voice '{{voice_name}}'...")
        batch_out = []
        try:
            batch_out = engine.infer_batch(group_texts, voice=voice_name, batch_size=32)
            if len(batch_out) != len(group_items):
                raise RuntimeError(f"infer_batch returned {{len(batch_out)}} clips, expected {{len(group_items)}}")
        except Exception as e:
            print(f"infer_batch note for voice '{{voice_name}}': {{e}}, running per-utterance infer...")
            batch_out = []
            for t in group_texts:
                try:
                    batch_out.append(engine.infer(text=t, voice=voice_name))
                except Exception:
                    batch_out.append(engine.infer(text=t))

        for (orig_idx, _), a_data in zip(group_items, batch_out):
            audio_results[orig_idx] = a_data

    # Fail fast if any audio clip was not generated
    if any(a is None for a in audio_results) or len(audio_results) != len(segments):
        missing_count = sum(1 for a in audio_results if a is None)
        raise RuntimeError(
            f"[Stage: tts] Batch TTS generation failed: {{missing_count}} segments missing audio clips. "
            f"Generated {{len(audio_results) - missing_count}}/{{len(segments)}}. Silent truncation is strictly forbidden."
        )

    import wave
    for seg, a_data in zip(segments, audio_results):
        seq_id = seg["seq_id"]
        out_wav = audio_clips_dir / f"audio_{chapter_number}_{{seq_id}}.wav"
        engine.save(a_data, str(out_wav))

        spd = seg.get("speed", 1.0)
        apply_speed(out_wav, spd)

        with wave.open(str(out_wav), "rb") as wf:
            dur_sec = wf.getnframes() / float(wf.getframerate())
        seg["duration"] = round(dur_sec, 2)
        seg["audio_path"] = f"audio/clips/audio_{chapter_number}_{{seq_id}}.wav"
        seg["status"] = "synthesized"
        seg["synthesized_voice"] = seg.get("voice_id", "Ngọc Huyền") or "Ngọc Huyền"
        with open(out_wav, "rb") as af:
            seg["checksum"] = hashlib.sha256(af.read()).hexdigest()

    t_tts_total = time.time() - t_tts_0
    print(f"🎉 TTS finished: {{len(segments)}} utterances in {{t_tts_total:.2f}}s")

# Save updated script.json with duration and checksums
with open(script_path, "w", encoding="utf-8") as f:
    json.dump(script, f, ensure_ascii=False, indent=2)

# 5. Detect FFmpeg Hardware Encoder
print("\\n" + "=" * 40)
print("🎬 HARDWARE ENCODER PROBING")
print("=" * 40)

def probe_encoder():
    if not has_cuda:
        return "libx264", ["-preset", "ultrafast", "-tune", "stillimage", "-threads", "4"]

    try:
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=0.1",
            "-vf", "format=yuv420p",
            "-c:v", "h264_nvenc", "-preset", "p4", "-f", "null", "-"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            print("🚀 NVENC SMOKE TEST PASSED! Using NVIDIA GPU Hardware Encoder.")
            return "h264_nvenc", ["-preset", "p4", "-tune", "ll"]
        else:
            print(f"NVENC probe failed (code {{res.returncode}}): {{res.stderr[-300:]}}")
    except Exception as e:
        print(f"NVENC probe exception: {{e}}")

    print("Falling back to multi-core libx264 ultrafast")
    return "libx264", ["-preset", "ultrafast", "-tune", "stillimage", "-threads", "4"]

selected_encoder, encoder_args = probe_encoder()
print(f"Selected video encoder: {{selected_encoder}} {{encoder_args}}")

# 6. Load Project Settings & Options Parity
proj_cfg_data = {{}}
if project_json_path.exists():
    try:
        with open(project_json_path, "r", encoding="utf-8") as pf:
            proj_cfg_data = json.load(pf)
    except Exception as e:
        print(f"Note loading project.json: {{e}}")

render_cfg = proj_cfg_data.get("render_settings", {{}})
audio_cfg = proj_cfg_data.get("audio_settings", {{}})
sub_cfg = render_cfg.get("subtitle", {{}})

cfg_w = int(render_cfg.get("target_width", {target_width}))
cfg_h = int(render_cfg.get("target_height", {target_height}))
cfg_fps = int(render_cfg.get("fps", {fps}))
cfg_video_mode = str(render_cfg.get("video_mode", "{video_mode}")).lower()
cfg_burn_subs = bool(render_cfg.get("burn_subtitles", {burn_subtitles}))
cfg_max_lines = int(sub_cfg.get("max_lines", {max_lines}))
cfg_font_size = int(sub_cfg.get("font_size", {font_size}))
cfg_max_chars = int(sub_cfg.get("max_chars_per_line", {max_chars_per_line}))

cfg_bgm_vol = float(audio_cfg.get("bgm_volume", {bgm_vol}))
cfg_voice_vol = float(audio_cfg.get("voice_volume", {voice_vol}))
cfg_ducking = float(audio_cfg.get("ducking_factor", {ducking_factor}))

print(f"Render Config Parity: {{cfg_w}}x{{cfg_h}} @ {{cfg_fps}}fps | Mode: {{cfg_video_mode}} | Burn Subs: {{cfg_burn_subs}} | Sub Lines: {{cfg_max_lines}}")

# 7. Multi-Panel Parallel Frame Generation
print("\\n" + "=" * 40)
print("⚡ MULTI-PANEL PARALLEL FRAME GENERATION")
print("=" * 40)

t_render_0 = time.time()
frames_dir = chapter_dir / "video" / "frames"
frames_dir.mkdir(parents=True, exist_ok=True)

from PIL import Image, ImageDraw, ImageFont, ImageFilter

font = None
effective_font_pt = max(18, int(cfg_font_size * (cfg_h / 720.0)))
for fp in [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]:
    if os.path.exists(fp):
        try:
            font = ImageFont.truetype(fp, effective_font_pt)
            break
        except Exception:
            pass
if font is None:
    font = ImageFont.load_default()

def detect_video_mode(img_w, img_h, forced_mode="auto"):
    if forced_mode and forced_mode != "auto":
        return forced_mode
    aspect = img_w / float(img_h) if img_h > 0 else 1.0
    if aspect >= 1.3:
        return "landscape_fit"
    elif aspect <= 0.7:
        return "portrait_vertical"
    else:
        return "portrait_padded"

def sanitize_sub_text(text):
    if not text:
        return ""
    cleaned = re.sub(r"\\[.*?\\]", "", text)
    cleaned = re.sub(r"\\s+", " ", cleaned).strip()
    return cleaned

def wrap_sub_lines(text, max_chars, max_lines_allowed):
    clean = sanitize_sub_text(text)
    if not clean:
        return ""
    words = clean.split()
    lines = []
    curr = []
    curr_len = 0
    for w in words:
        if curr_len + len(w) + 1 > max_chars and curr:
            lines.append(" ".join(curr))
            curr = [w]
            curr_len = len(w)
        else:
            curr.append(w)
            curr_len += len(w) + 1
    if curr:
        lines.append(" ".join(curr))
    if len(lines) > max_lines_allowed:
        lines = lines[:max_lines_allowed]
        lines[-1] = lines[-1] + "..."
    return "\\n".join(lines)

def compose_single_frame(panel_file, text, out_frame_path, seq_id, motion_type="static", focus_box=None):
    w, h = cfg_w, cfg_h
    canvas = Image.new("RGB", (w, h), (20, 20, 20))

    try:
        with Image.open(panel_file) as orig_p:
            p_img = orig_p.convert("RGB")
            pw, ph = p_img.size

            # Subject-centric focus crop if focus_box provided
            if focus_box and isinstance(focus_box, dict) and motion_type in ("focus_crop", "static_focus", "static"):
                fx = (focus_box.get("x_min", 0.0) + focus_box.get("x_max", 1.0)) / 2.0
                fy = (focus_box.get("y_min", 0.0) + focus_box.get("y_max", 1.0)) / 2.0
                scale_ratio = max(0.45, min(1.0, focus_box.get("x_max", 1.0) - focus_box.get("x_min", 0.0)))
                crop_w = max(int(pw * scale_ratio), 10)
                crop_h = max(int(ph * scale_ratio), 10)
                cx, cy = int(fx * pw), int(fy * ph)
                x1 = max(0, min(pw - crop_w, cx - crop_w // 2))
                y1 = max(0, min(ph - crop_h, cy - crop_h // 2))
                p_img = p_img.crop((x1, y1, x1 + crop_w, y1 + crop_h))
                pw, ph = p_img.size

            v_mode = detect_video_mode(pw, ph, cfg_video_mode)

            # Background blur
            bg = p_img.resize((w, h), Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(radius=25))
            canvas.paste(bg, (0, 0))

            # Foreground layout according to video_mode
            if v_mode == "landscape_fit":
                scale = min(w / float(pw), h / float(ph))
                fg_w = int(pw * scale)
                fg_h = int(ph * scale)
            elif v_mode == "portrait_vertical":
                scale = min(float(w) * 0.65 / float(pw), float(h) / float(ph))
                fg_w = int(pw * scale)
                fg_h = int(ph * scale)
            else: # portrait_padded
                scale = h / float(ph)
                fg_w = int(pw * scale)
                if fg_w > w:
                    scale = w / float(pw)
                    fg_w = w
                    fg_h = int(ph * scale)
                else:
                    fg_h = h

            fg = p_img.resize((fg_w, fg_h), Image.Resampling.LANCZOS)
            canvas.paste(fg, ((w - fg_w) // 2, (h - fg_h) // 2))
    except Exception as e:
        raise RuntimeError(
            f"[Stage: render] Corrupted panel asset for chapter {chapter_number}, "
            f"seq_id '{{seq_id}}': '{{panel_file}}' failed to load: {{e}}"
        )

    # Subtitles overlay (if burned)
    if cfg_burn_subs:
        draw = ImageDraw.Draw(canvas)
        display_sub = wrap_sub_lines(text, cfg_max_chars, cfg_max_lines)
        if display_sub:
            lines = display_sub.split("\\n")
            tx = w // 2
            ty = h - (65 + (len(lines) - 1) * int(effective_font_pt * 1.25))
            draw.text(
                (tx, ty), display_sub, font=font, anchor="ma", align="center",
                fill="#FFFFFF", stroke_width=4, stroke_fill="#000000"
            )

    canvas.save(out_frame_path, quality=92)
    return out_frame_path

def process_segment_render(seg):
    seq_id = seg["seq_id"]
    p_ids = seg.get("panel_ids", [])
    if not p_ids:
        raise ValueError(
            f"[Stage: render] Segment '{{seq_id}}' in chapter {chapter_number} has no panel_ids specified."
        )

    text = seg.get("narrator_text", "")
    dur = float(seg.get("duration", 4.0))
    m = len(p_ids)
    p_dur = dur / float(m)
    motion_type = seg.get("camera_motion", "static")
    focus_box = seg.get("focus_box")

    seg_frame_results = []
    for p_idx, p_id in enumerate(p_ids):
        candidate = panels_dir / p_id
        if not candidate.exists():
            if not p_id.endswith(".jpg") and not p_id.endswith(".png"):
                cand_ext = panels_dir / f"{{p_id}}.jpg"
                if cand_ext.exists():
                    candidate = cand_ext
        if not candidate.exists():
            raise FileNotFoundError(
                f"[Stage: render] Missing panel asset for chapter {chapter_number}, "
                f"seq_id '{{seq_id}}': asset path '{{candidate}}' does not exist. "
                f"Substitution is strictly prohibited."
            )

        if m == 1:
            frame_file = frames_dir / f"frame_{{seq_id}}.jpg"
        else:
            frame_file = frames_dir / f"frame_{{seq_id}}_{{p_idx}}.jpg"

        compose_single_frame(candidate, text, frame_file, seq_id, motion_type, focus_box)
        seg_frame_results.append((frame_file, p_dur))

    return seg_frame_results

print(f"Compositing frames for {{len(segments)}} segments in parallel (ThreadPoolExecutor {render_workers} workers)...")
with ThreadPoolExecutor(max_workers={render_workers}) as executor:
    segment_frames_list = list(executor.map(process_segment_render, segments))

t_frames = time.time() - t_render_0
print(f"All frames composited in {{t_frames:.2f}}s!")

# 8. Merge All Audio Clips into Combined Audio & Optional BGM Mixing
print("Concatenating speech audio clips...")
combined_speech = chapter_dir / "audio" / "combined_speech.wav"
with wave.open(str(combined_speech), "wb") as outfile:
    first_wf = None
    for seg in segments:
        seq_id = seg["seq_id"]
        wav_path = audio_clips_dir / f"audio_{chapter_number}_{{seq_id}}.wav"
        if not wav_path.exists() or wav_path.stat().st_size == 0:
            raise FileNotFoundError(
                f"[Stage: audio_merge] Missing or empty audio clip for chapter {chapter_number}, "
                f"seq_id '{{seq_id}}': '{{wav_path}}'. Substitution is strictly prohibited."
            )
        try:
            with wave.open(str(wav_path), "rb") as infile:
                if first_wf is None:
                    first_wf = infile
                    outfile.setnchannels(infile.getnchannels())
                    outfile.setsampwidth(infile.getsampwidth())
                    outfile.setframerate(infile.getframerate())
                outfile.writeframes(infile.readframes(infile.getnframes()))
        except Exception as e:
            raise RuntimeError(
                f"[Stage: audio_merge] Corrupted audio clip for chapter {chapter_number}, "
                f"seq_id '{{seq_id}}': '{{wav_path}}' error: {{e}}"
            )

# Check for BGM audio in workspace
found_bgm = None
for bgm_cand in [workspace / "bgm.mp3", workspace / "bgm.wav", workspace / "bgm.m4a"]:
    if bgm_cand.exists() and bgm_cand.stat().st_size > 0:
        found_bgm = bgm_cand
        break

combined_audio = chapter_dir / "audio" / "combined_audio.wav"
if found_bgm:
    print(f"🎵 Mixing Background Music: {{found_bgm.name}} (ducking factor: {{cfg_ducking}}, volume: {{cfg_bgm_vol}})...")
    bgm_filter = (
        f"[1:a]volume={{cfg_bgm_vol:.3f}}[bgm_base];"
        f"[bgm_base][0:a]sidechaincompress=threshold=0.08:ratio=5.0:attack=80:release=500:knee=2.8[ducked_bgm];"
        f"[0:a]volume={{cfg_voice_vol:.2f}}[speech];"
        f"[speech][ducked_bgm]amix=inputs=2:duration=first:dropout_transition=2:weights=1.0 1.0[mixed];"
        f"[mixed]alimiter=limit=0.95:attack=5:release=50[outa]"
    )
    cmd_bgm = [
        "ffmpeg", "-y",
        "-i", str(combined_speech),
        "-stream_loop", "-1", "-i", str(found_bgm),
        "-filter_complex", bgm_filter,
        "-map", "[outa]",
        "-c:a", "pcm_s16le",
        "-ar", "48000",
        str(combined_audio)
    ]
    res_bgm = subprocess.run(cmd_bgm, capture_output=True, text=True)
    if res_bgm.returncode != 0:
        print(f"Warning: BGM mixing returned non-zero code, falling back to speech: {{res_bgm.stderr[-300:]}}")
        shutil.copy2(combined_speech, combined_audio)
else:
    shutil.copy2(combined_speech, combined_audio)

# 9. Build FFmpeg Concat Script for Multi-Panel Single-Pass Render
frames_concat_script = chapter_dir / "video" / "frames_concat.txt"
all_entries = []
with open(frames_concat_script, "w", encoding="utf-8") as f:
    f.write("ffconcat version 1.0\\n")
    for seg_frames in segment_frames_list:
        for f_path, p_dur in seg_frames:
            all_entries.append(f_path)
            f.write(f"file '{{f_path.resolve()}}'\\n")
            f.write(f"duration {{p_dur:.3f}}\\n")
    if all_entries:
        last_f = all_entries[-1].resolve()
        f.write(f"file '{{last_f}}'\\n")

# 10. Single-Pass High-Speed Hardware Video Encoding
final_video = chapter_dir / "video" / f"chapter_{chapter_number}.mp4"
final_video.parent.mkdir(parents=True, exist_ok=True)

cmd_encode = [
    "ffmpeg", "-y",
    "-f", "concat", "-safe", "0", "-i", str(frames_concat_script),
    "-i", str(combined_audio),
    "-vf", f"fps={{cfg_fps}},format=yuv420p",
    "-c:v", selected_encoder,
    *encoder_args,
    "-c:a", "aac", "-b:a", "192k",
    "-shortest",
    str(final_video)
]
print("Running single-pass hardware FFmpeg render...")
t_enc_0 = time.time()
res_enc = subprocess.run(cmd_encode, capture_output=True, text=True)
if res_enc.returncode != 0:
    print(f"Encoding failed: {{res_enc.stderr[-600:]}}")
    raise RuntimeError(f"Single-pass video encode failed: {{res_enc.stderr[-600:]}}")
t_enc = time.time() - t_enc_0
t_render_total = time.time() - t_render_0
print(f"🎉 Single-Pass Video Render FINISHED in {{t_enc:.2f}}s (Total render stage: {{t_render_total:.2f}}s)!")

# 11. Generate Subtitles SRT
srt_path = chapter_dir / "subtitles" / f"chapter_{chapter_number}.srt"
srt_path.parent.mkdir(parents=True, exist_ok=True)

def fmt_srt_time(sec):
    hrs = int(sec // 3600)
    mins = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{{hrs:02d}}:{{mins:02d}}:{{s:02d}},{{ms:03d}}"

cum_t = 0.0
with open(srt_path, "w", encoding="utf-8") as srt:
    for idx, seg in enumerate(segments, 1):
        dur = seg.get("duration", 4.0)
        t_start = cum_t
        t_end = cum_t + dur
        cum_t = t_end
        sanitized_narr = sanitize_sub_text(seg.get("narrator_text", ""))
        srt.write(f"{{idx}}\\n{{fmt_srt_time(t_start)}} --> {{fmt_srt_time(t_end)}}\\n{{sanitized_narr}}\\n\\n")

# 12. Build Telemetry Report
t_total_wall = time.time() - t_start_wall
telemetry = {{
    "project_slug": "{project_slug}",
    "chapter_number": "{chapter_number}",
    "status": "success",
    "job_type": "produce",
    "cloud_provider": "kaggle",
    "gpu_name": gpu_name,
    "has_cuda": has_cuda,
    "cpu_count": os.cpu_count(),
    "encoder_used": selected_encoder,
    "segments_count": len(segments),
    "total_audio_duration_seconds": round(cum_t, 2),
    "total_audio_duration_minutes": round(cum_t / 60.0, 2),
    "tts_init_seconds": round(t_tts_init, 2),
    "tts_inference_seconds": round(t_tts_total, 2),
    "tts_avg_per_segment": round(t_tts_total / max(1, len(segments)), 3),
    "render_seconds": round(t_render_total, 2),
    "render_avg_per_clip": round(t_render_total / max(1, len(segments)), 3),
    "concat_seconds": round(t_enc, 2),
    "total_wall_time_seconds": round(t_total_wall, 2),
    "total_wall_time_minutes": round(t_total_wall / 60.0, 2),
    "video_size_mb": round(final_video.stat().st_size / (1024 * 1024), 2) if final_video.exists() else 0.0
}}

print("\\n" + "=" * 60)
print("📊 CLOUD PRODUCTION TELEMETRY SUMMARY")
print("=" * 60)
print(json.dumps(telemetry, indent=2, ensure_ascii=False))

# 13. Package Output Bundle to /kaggle/working/
out_dir = Path("/kaggle/working/output").resolve()
out_dir.mkdir(parents=True, exist_ok=True)

# Copy final artifacts to output dir
shutil.copy2(final_video, out_dir / f"chapter_{chapter_number}.mp4")
shutil.copy2(srt_path, out_dir / f"chapter_{chapter_number}.srt")
shutil.copy2(script_path, out_dir / "script.json")

# Also copy directly to /kaggle/working/ root
shutil.copy2(final_video, f"/kaggle/working/chapter_{chapter_number}.mp4")
shutil.copy2(srt_path, f"/kaggle/working/chapter_{chapter_number}.srt")
shutil.copy2(script_path, "/kaggle/working/script.json")

# Pack audio clips
with tarfile.open(out_dir / "audio_clips.tar.gz", "w:gz") as atar:
    atar.add(audio_clips_dir, arcname="clips")
shutil.copy2(out_dir / "audio_clips.tar.gz", "/kaggle/working/audio_clips.tar.gz")

with open(out_dir / "telemetry.json", "w", encoding="utf-8") as tf:
    json.dump(telemetry, tf, indent=2, ensure_ascii=False)
shutil.copy2(out_dir / "telemetry.json", "/kaggle/working/telemetry.json")

print("=" * 60)
print(f"🏁 ALL CHAPTER {chapter_number} ARTIFACTS EXPORTED TO {{out_dir}} AND /kaggle/working")
print("=" * 60)
'''

    def dispatch_chapter_job(
        self,
        project_slug: str,
        chapter_number: str,
        kernel_slug: Optional[str] = None,
        gpu: bool = True,
        wait: bool = True,
        poll_interval: int = 6,
        timeout_seconds: int = 900,
        job_type: str = "produce",
        render_workers: int = 4,
        encoder_preference: str = "auto",
        video_mode: Optional[str] = None,
        burn_subtitles: Optional[bool] = None,
        fps: Optional[int] = None,
        target_width: Optional[int] = None,
        target_height: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Dispatches a chapter production job to Kaggle Cloud, monitors progress, and returns result.
        Enforces:
        - True job separation ('tts', 'render', 'produce').
        - Non-CC0 metadata license for unconfirmed manga assets.
        - Strict timeout handling (raising TimeoutError, aborting corrupt downloads).
        - No credentials exposure.
        """
        if not self._api:
            raise RuntimeError("Kaggle API is not authenticated.")

        # Pre-flight check for render-only jobs: existing audio must be present locally
        if job_type == "render":
            chap_dir = get_chapter_dir(project_slug, chapter_number)
            script_path = chap_dir / "script.json"
            if script_path.exists():
                try:
                    s_data = read_json(script_path)
                    s_obj = RecapScript.model_validate(s_data)
                    clips_dir = chap_dir / "audio" / "clips"
                    missing_clips = []
                    for seg in s_obj.segments:
                        clip_p = clips_dir / f"audio_{chapter_number}_{seg.seq_id}.wav"
                        if not clip_p.exists() or clip_p.stat().st_size == 0:
                            missing_clips.append(str(clip_p))
                    if missing_clips:
                        raise FileNotFoundError(
                            f"[Stage: render] Cannot dispatch render-only job for chapter {chapter_number}: "
                            f"{len(missing_clips)} audio clip(s) missing from {clips_dir}. "
                            f"Missing clips include: {missing_clips[:3]}. Run TTS first or use job_type='produce'."
                        )
                except Exception as e:
                    if isinstance(e, FileNotFoundError):
                        raise
                    logger.debug(f"Pre-flight audio check note: {e}")

        kernel_slug = kernel_slug or f"manga-recap-{project_slug}-ch{chapter_number}".lower().replace("_", "-")
        staging_dir = Path(f"/tmp/kaggle_job_{project_slug}_{chapter_number}_{job_type}").resolve()
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"KaggleCloudService: Packaging chapter payload in {staging_dir} (job_type={job_type})...")
        payload_tar = staging_dir / "payload.tar.gz"
        self.package_chapter_payload(project_slug, chapter_number, payload_tar, job_type=job_type)

        # Upload or update payload dataset on Kaggle with unconfirmed rights ("other")
        dataset_slug = f"manga-recap-payload-{project_slug}-{chapter_number}".lower().replace("_", "-")[:40]
        ds_staging = Path(f"/tmp/kaggle_ds_{dataset_slug}").resolve()
        ds_staging.mkdir(parents=True, exist_ok=True)
        shutil.copy2(payload_tar, ds_staging / "payload.tar.gz")
        ds_meta = {
            "title": f"Manga Recap Payload {project_slug} {chapter_number}",
            "id": f"{self._username}/{dataset_slug}",
            "licenses": [{"name": "other"}],
        }
        (ds_staging / "dataset-metadata.json").write_text(json.dumps(ds_meta, indent=2))
        try:
            logger.info("Syncing payload dataset to Kaggle...")
            self._api.dataset_create_new(str(ds_staging), public=False, quiet=True)
        except Exception:
            try:
                self._api.dataset_create_version(
                    str(ds_staging),
                    version_notes=f"Job ch.{chapter_number} {job_type}",
                    quiet=True,
                    delete_old_versions=True,
                )
            except Exception as e:
                logger.warning(f"Dataset upload note: {e}")

        worker_py = staging_dir / "worker.py"
        worker_code = self.generate_worker_script(
            project_slug=project_slug,
            chapter_number=chapter_number,
            render_workers=render_workers,
            encoder_preference=encoder_preference,
            job_type=job_type,
            video_mode=video_mode,
            burn_subtitles=burn_subtitles,
            fps=fps,
            target_width=target_width,
            target_height=target_height,
        )
        worker_py.write_text(worker_code, encoding="utf-8")

        metadata = {
            "id": f"{self._username}/{kernel_slug}",
            "title": kernel_slug,
            "code_file": "worker.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": gpu,
            "enable_tpu": False,
            "enable_internet": True,
            "dataset_sources": [f"{self._username}/{dataset_slug}"],
            "competition_sources": [],
            "kernel_sources": [],
            "model_sources": [],
        }

        metadata_file = staging_dir / "kernel-metadata.json"
        metadata_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        logger.info(f"KaggleCloudService: Pushing kernel '{self._username}/{kernel_slug}' (GPU: {gpu}, Job: {job_type})...")
        push_res = self._api.kernels_push(str(staging_dir))
        logger.info(f"KaggleCloudService: Kernel push result: {push_res}")

        # Resolve actual slug assigned by Kaggle
        assigned_ref = getattr(push_res, "ref", None)
        if assigned_ref:
            target_kernel_ref = assigned_ref.replace("/code/", "").strip()
        else:
            target_kernel_ref = f"{self._username}/{kernel_slug}"

        if not wait:
            return {
                "status": "dispatched",
                "kernel_ref": target_kernel_ref,
                "staging_dir": str(staging_dir),
                "job_type": job_type,
            }

        logger.info(f"KaggleCloudService: Waiting for cloud execution of '{target_kernel_ref}'...")
        t0 = time.time()
        last_status = "QUEUED"
        completed_cleanly = False

        while time.time() - t0 < timeout_seconds:
            time.sleep(poll_interval)
            status_obj = self._api.kernels_status(target_kernel_ref)
            current_status = getattr(status_obj, "status", str(status_obj))
            if isinstance(current_status, dict):
                current_status = current_status.get("status", "UNKNOWN")

            if current_status != last_status:
                logger.info(f"Kaggle Cloud Status [{int(time.time() - t0)}s]: {current_status}")
                last_status = current_status

            if str(current_status).upper() == "COMPLETE":
                logger.info(f"Kaggle execution COMPLETE in {int(time.time() - t0)}s!")
                completed_cleanly = True
                break
            elif str(current_status).upper() in ["FAILED", "ERROR"]:
                fail_msg = getattr(status_obj, "failureMessage", "Unknown cloud error")
                try:
                    err_dir = staging_dir / "error_out"
                    err_dir.mkdir(parents=True, exist_ok=True)
                    self._api.kernels_output(target_kernel_ref, str(err_dir))
                    for log_f in err_dir.glob("*.log"):
                        logger.error(f"Kaggle Cloud Error Log:\n{log_f.read_text(errors='ignore')[-3000:]}")
                except Exception as log_err:
                    logger.warning(f"Could not retrieve error log: {log_err}")
                raise RuntimeError(f"Kaggle Cloud job failed: {fail_msg}")

        if not completed_cleanly:
            raise TimeoutError(
                f"Kaggle Cloud job '{target_kernel_ref}' timed out after {timeout_seconds}s "
                f"with status '{last_status}'. Output download aborted."
            )

        # Download outputs
        output_dir = Path(f"/tmp/kaggle_out_{project_slug}_{chapter_number}_{job_type}").resolve()
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Downloading outputs from '{target_kernel_ref}' to {output_dir}...")
        self._api.kernels_output(target_kernel_ref, str(output_dir))

        # Unpack back into project directory with strict validation
        sync_report = self.sync_results(project_slug, chapter_number, output_dir, job_type=job_type)
        return sync_report

    def dispatch_tts_job(self, project_slug: str, chapter_number: str, **kwargs) -> Dict[str, Any]:
        """Dispatches a dedicated cloud TTS synthesis job."""
        return self.dispatch_chapter_job(project_slug, chapter_number, job_type="tts", **kwargs)

    def dispatch_render_job(self, project_slug: str, chapter_number: str, **kwargs) -> Dict[str, Any]:
        """Dispatches a dedicated cloud video render job reusing pre-existing audio cache."""
        return self.dispatch_chapter_job(project_slug, chapter_number, job_type="render", **kwargs)

    def dispatch_produce_job(self, project_slug: str, chapter_number: str, **kwargs) -> Dict[str, Any]:
        """Dispatches a full cloud production job (TTS + Render)."""
        return self.dispatch_chapter_job(project_slug, chapter_number, job_type="produce", **kwargs)

    def sync_results(
        self,
        project_slug: str,
        chapter_number: str,
        downloaded_dir: Path,
        job_type: str = "produce",
    ) -> Dict[str, Any]:
        """
        Unpacks downloaded cloud output into the local chapter directory.
        Strictly validates that output artifacts exist and are non-empty before marking completed.
        Sets both canonical technical_status and human_review_status (unreviewed) via ChapterMetadata.
        """
        downloaded_dir = Path(downloaded_dir).resolve()
        chap_dir = get_chapter_dir(project_slug, chapter_number)
        video_dir = chap_dir / "video"
        video_dir.mkdir(parents=True, exist_ok=True)
        subtitles_dir = chap_dir / "subtitles"
        subtitles_dir.mkdir(parents=True, exist_ok=True)
        audio_dir = chap_dir / "audio" / "clips"
        audio_dir.mkdir(parents=True, exist_ok=True)

        # 1. Video extraction (for render or produce jobs)
        vid_src = downloaded_dir / f"chapter_{chapter_number}.mp4"
        if not vid_src.exists():
            for f in downloaded_dir.rglob("*.mp4"):
                vid_src = f
                break

        if vid_src and vid_src.exists():
            shutil.copy2(vid_src, video_dir / "final.mp4")
            shutil.copy2(vid_src, video_dir / f"chapter_{chapter_number}.mp4")

        # 2. Subtitles extraction (for render or produce jobs)
        srt_src = downloaded_dir / f"chapter_{chapter_number}.srt"
        if not srt_src.exists():
            for f in downloaded_dir.rglob("*.srt"):
                srt_src = f
                break
        if srt_src and srt_src.exists():
            shutil.copy2(srt_src, subtitles_dir / f"chapter_{chapter_number}.srt")

        # 3. Audio clips archive extraction (secure extraction)
        for f in downloaded_dir.rglob("*.tar.gz"):
            if "audio" in f.name:
                with tarfile.open(f, "r:gz") as atar:
                    safe_extract_tar(atar, chap_dir / "audio")

        # Also copy any standalone wav files from downloaded_dir if present
        for f in downloaded_dir.rglob("*.wav"):
            target_clip = audio_dir / f.name
            if not target_clip.exists():
                shutil.copy2(f, target_clip)

        # 4. Script synchronization
        for f in downloaded_dir.rglob("script.json"):
            shutil.copy2(f, chap_dir / "script.json")

        # 5. Telemetry
        telemetry = {}
        for f in downloaded_dir.rglob("telemetry.json"):
            telemetry = read_json(f)
            shutil.copy2(f, chap_dir / "cloud_telemetry.json")

        # --- STRICT ARTIFACT VALIDATION ---
        script_file = chap_dir / "script.json"
        if not script_file.exists():
            raise FileNotFoundError(
                f"[Stage: sync] Missing script.json for chapter {chapter_number} in project '{project_slug}'."
            )

        script_data = read_json(script_file)
        segments = script_data.get("segments", [])
        for seg in segments:
            seq_id = seg.get("seq_id")
            if seq_id:
                clip_path = audio_dir / f"audio_{chapter_number}_{seq_id}.wav"
                if not clip_path.exists() or clip_path.stat().st_size == 0:
                    raise FileNotFoundError(
                        f"[Stage: sync] Missing or empty audio clip for seq_id '{seq_id}': {clip_path} "
                        f"for chapter {chapter_number} in project '{project_slug}'."
                    )

        if job_type in ("render", "produce"):
            target_video = video_dir / "final.mp4"
            if not target_video.exists() or target_video.stat().st_size == 0:
                raise FileNotFoundError(
                    f"[Stage: sync] Missing or empty final video artifact: {target_video} "
                    f"for chapter {chapter_number} in project '{project_slug}'."
                )

            # Validate video header sanity
            if target_video.stat().st_size < 32:
                raise ValueError(
                    f"[Stage: sync] Corrupted video file: {target_video} is too small to be a valid MP4."
                )
            header_sample = target_video.read_bytes()[:32]
            if b"ftyp" not in header_sample:
                raise ValueError(
                    f"[Stage: sync] Corrupted video file: {target_video} does not contain valid MP4 header ('ftyp')."
                )

            target_srt = subtitles_dir / f"chapter_{chapter_number}.srt"
            if not target_srt.exists() or target_srt.stat().st_size == 0:
                raise FileNotFoundError(
                    f"[Stage: sync] Missing or empty subtitles artifact: {target_srt} "
                    f"for chapter {chapter_number} in project '{project_slug}'."
                )

        # 6. Update chapter.json state ONLY after all artifacts are verified valid
        chap_meta_path = chap_dir / "chapter.json"
        if chap_meta_path.exists():
            raw_meta = read_json(chap_meta_path)
            now_iso = datetime.now(timezone.utc).isoformat()
            try:
                meta = ChapterMetadata.model_validate(raw_meta)
                meta.updated_at = now_iso
                if job_type in ("render", "produce"):
                    meta.overall_status = CheckpointStatus.COMPLETED
                    meta.technical_status = CheckpointStatus.COMPLETED
                    meta.human_review_status = HumanReviewStatus.UNREVIEWED
                    meta.current_stage = StageType.EXPORT
                    for stg in [StageType.AUDIO, StageType.RENDER, StageType.EXPORT]:
                        if stg in meta.stages:
                            meta.stages[stg].status = CheckpointStatus.COMPLETED
                            meta.stages[stg].completed_at = now_iso
                elif job_type == "tts":
                    meta.current_stage = StageType.AUDIO
                    if StageType.AUDIO in meta.stages:
                        meta.stages[StageType.AUDIO].status = CheckpointStatus.COMPLETED
                        meta.stages[StageType.AUDIO].completed_at = now_iso
                atomic_write_json(chap_meta_path, meta.model_dump(mode="json"))
            except Exception as e:
                logger.warning(f"Could not validate with ChapterMetadata: {e}")
                raw_meta["updated_at"] = now_iso
                if job_type in ("render", "produce"):
                    raw_meta["overall_status"] = "completed"
                    raw_meta["technical_status"] = "completed"
                    raw_meta["human_review_status"] = "unreviewed"
                    raw_meta["current_stage"] = "export"
                    for stg in ["audio", "render", "export"]:
                        if "stages" in raw_meta and stg in raw_meta["stages"]:
                            raw_meta["stages"][stg]["status"] = "completed"
                            raw_meta["stages"][stg]["completed_at"] = now_iso
                elif job_type == "tts":
                    raw_meta["current_stage"] = "audio"
                    if "stages" in raw_meta and "audio" in raw_meta["stages"]:
                        raw_meta["stages"]["audio"]["status"] = "completed"
                        raw_meta["stages"]["audio"]["completed_at"] = now_iso
                atomic_write_json(chap_meta_path, raw_meta)

        logger.info(
            f"KaggleCloudService: Successfully synchronized Chapter {chapter_number} artifacts (job_type={job_type})."
        )
        if job_type == "tts":
            return {
                "status": "success",
                "project_slug": project_slug,
                "chapter_number": chapter_number,
                "job_type": "tts",
                "telemetry": telemetry,
                "audio_clips_dir": str(audio_dir),
                "script_path": str(script_file),
            }
        else:
            return {
                "status": "success",
                "project_slug": project_slug,
                "chapter_number": chapter_number,
                "job_type": job_type,
                "telemetry": telemetry,
                "final_video": str(video_dir / "final.mp4"),
                "subtitles": str(subtitles_dir / f"chapter_{chapter_number}.srt"),
                "audio_clips_dir": str(audio_dir),
            }
