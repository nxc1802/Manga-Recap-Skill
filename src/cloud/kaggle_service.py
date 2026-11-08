"""
src/cloud/kaggle_service.py
Cloud Execution Server for Manga Recap leveraging Kaggle GPU/TPU/CPU infrastructure.
Enables extreme-speed batch VieNeu-TTS synthesis and hardware-accelerated NVENC video rendering.
"""

from __future__ import annotations

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
from src.core.storage import atomic_write_json, get_chapter_dir, get_project_dir, read_json


class KaggleCloudService:
    """
    Manages remote batch rendering and TTS synthesis execution on Kaggle Cloud.
    """

    DEFAULT_KERNEL_SLUG = "manga-recap-cloud-runner"

    def __init__(self, api_token: Optional[str] = None):
        self.api_token = api_token or os.environ.get("KAGGLE_API_TOKEN")
        self._api = None
        self._username = None
        self._authenticate()

    def _authenticate(self) -> None:
        """Authenticates with the Kaggle API."""
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
            logger.warning(f"KaggleCloudService: Kaggle API authentication failed: {e}")
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
    ) -> Path:
        """
        Packs project.json, chapter manifest, script, and panel images into a tar.gz payload.
        """
        proj_dir = get_project_dir(project_slug)
        chap_dir = get_chapter_dir(project_slug, chapter_number)

        out_tar_path = Path(out_tar_path).resolve()
        out_tar_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Packaging chapter payload for {project_slug} ch.{chapter_number} -> {out_tar_path}")

        with tarfile.open(out_tar_path, "w:gz") as tar:
            # 1. Project config
            p_json = proj_dir / "project.json"
            if p_json.exists():
                tar.add(p_json, arcname="project.json")

            # 2. Chapter metadata
            for fname in ["chapter.json", "manifest.json", "script.json"]:
                fpath = chap_dir / fname
                if fpath.exists():
                    tar.add(fpath, arcname=f"chapter/{fname}")

            # 3. Panels
            panels_dir = chap_dir / "panels"
            if panels_dir.exists():
                tar.add(panels_dir, arcname="chapter/panels")

        logger.info(f"Payload package created: {out_tar_path} ({out_tar_path.stat().st_size / (1024*1024):.2f} MB)")
        return out_tar_path

    def generate_worker_script(
        self,
        project_slug: str,
        chapter_number: str,
        render_workers: int = 4,
        encoder_preference: str = "auto",
    ) -> str:
        """
        Generates the standalone extreme-speed worker script for execution inside the Kaggle Linux VM.
        """
        return f'''#!/usr/bin/env python3
"""
Manga Recap Extreme-Speed Cloud Production Worker for Kaggle GPU.
Project: {project_slug} | Chapter: {chapter_number}
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
from concurrent.futures import ThreadPoolExecutor, as_completed

t_start_wall = time.time()
print("=" * 60)
print("🚀 MANGA RECAP CLOUD PRODUCTION WORKER STARTING ON KAGGLE")
print("=" * 60)

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
    # Search for tar.gz
    for tar_candidate in root_dir.rglob("*.tar.gz"):
        if "payload" in tar_candidate.name.lower() or "manga" in tar_candidate.name.lower():
            found_payload_tar = tar_candidate
            break
    if found_payload_tar:
        break

    # If no tar.gz, search for script.json directly
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
    print(f"📦 Extracting payload archive: {{found_payload_tar}} -> {{workspace}}")
    with tarfile.open(found_payload_tar, "r:gz") as tar:
        tar.extractall(path=workspace)
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

# 3. Bootstrap VieNeu-TTS
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

# 4. Synthesize TTS Utterances
print("\\n" + "=" * 40)
print("🎙️ RUNNING HIGH-SPEED TTS SYNTHESIS")
print("=" * 40)

audio_clips_dir = chapter_dir / "audio" / "clips"
audio_clips_dir.mkdir(parents=True, exist_ok=True)

t_tts_0 = time.time()
all_texts = [seg["narrator_text"] for seg in segments]
target_voice = segments[0].get("voice_id", "Ngọc Huyền") if segments else "Ngọc Huyền"

try:
    print(f"Executing engine.infer_batch for {{len(all_texts)}} utterances on GPU (batch_size=32)...")
    audio_batch = engine.infer_batch(all_texts, voice=target_voice, batch_size=32)
except Exception as e:
    print(f"Warning: infer_batch fallback: {{e}}, running sequential...")
    audio_batch = []
    for t in all_texts:
        try:
            audio_batch.append(engine.infer(text=t, voice=target_voice))
        except Exception:
            audio_batch.append(engine.infer(text=t))

import wave
for idx, (seg, audio_data) in enumerate(zip(segments, audio_batch), 1):
    seq_id = seg["seq_id"]
    out_wav = audio_clips_dir / f"audio_{chapter_number}_{{seq_id}}.wav"
    engine.save(audio_data, str(out_wav))
    with wave.open(str(out_wav), "rb") as wf:
        dur_sec = wf.getnframes() / float(wf.getframerate())
    seg["duration"] = round(dur_sec, 2)
    seg["audio_path"] = f"audio/clips/audio_{chapter_number}_{{seq_id}}.wav"
    seg["status"] = "synthesized"
    with open(out_wav, "rb") as af:
        seg["checksum"] = hashlib.sha256(af.read()).hexdigest()

t_tts_total = time.time() - t_tts_0
print(f"🎉 Batched TTS finished: {{len(segments)}} utterances in {{t_tts_total:.2f}}s (Avg {{t_tts_total/max(1, len(segments)):.3f}}s/seg)")

# Save updated script.json
with open(script_path, "w", encoding="utf-8") as f:
    json.dump(script, f, ensure_ascii=False, indent=2)

# 5. Detect FFmpeg Hardware Encoder
print("\\n" + "=" * 40)
print("🎬 HARDWARE ENCODER PROBING")
print("=" * 40)

def probe_encoder():
    if not has_cuda:
        return "libx264", ["-preset", "ultrafast", "-tune", "stillimage", "-threads", "4"]
    
    # Smoke test h264_nvenc with 1920x1080 (minimum supported dimension is >= 144x144)
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

# 6. Ultra-Fast Parallel Frame Generation & Audio Merging
print("\\n" + "=" * 40)
print("⚡ PARALLEL FRAME GENERATION & AUDIO MERGING")
print("=" * 40)

t_render_0 = time.time()
frames_dir = chapter_dir / "video" / "frames"
frames_dir.mkdir(parents=True, exist_ok=True)

from PIL import Image, ImageDraw, ImageFont, ImageFilter

font = None
for fp in [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]:
    if os.path.exists(fp):
        try:
            font = ImageFont.truetype(fp, 36)
            break
        except Exception:
            pass
if font is None:
    font = ImageFont.load_default()

def compose_frame(seg):
    seq_id = seg["seq_id"]
    p_ids = seg.get("panel_ids", [])
    text = seg["narrator_text"]
    
    panel_file = None
    if p_ids:
        p_name = p_ids[0]
        if not p_name.endswith(".jpg"):
            p_name = f"{{p_name}}.jpg"
        candidate = panels_dir / p_name
        if candidate.exists():
            panel_file = candidate
            
    if not panel_file or not panel_file.exists():
        avail_panels = sorted(list(panels_dir.glob("*.jpg")))
        if avail_panels:
            panel_file = avail_panels[0]
        else:
            raise FileNotFoundError(f"No panels available in {{panels_dir}}")
        
    w, h = 1920, 1080
    canvas = Image.new("RGB", (w, h), (20, 20, 20))
    
    with Image.open(panel_file) as orig_p:
        p_img = orig_p.convert("RGB")
        pw, ph = p_img.size
        
        # Background: fast blur
        bg = p_img.resize((w, h), Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(radius=25))
        canvas.paste(bg, (0, 0))
        
        # Foreground
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
        
    # Subtitles
    draw = ImageDraw.Draw(canvas)
    words = text.split()
    lines = []
    curr = []
    curr_len = 0
    for wrd in words:
        if curr_len + len(wrd) + 1 > 55 and curr:
            lines.append(" ".join(curr))
            curr = [wrd]
            curr_len = len(wrd)
        else:
            curr.append(wrd)
            curr_len += len(wrd) + 1
    if curr:
        lines.append(" ".join(curr))
    if len(lines) > 2:
        lines = lines[:2]
        lines[1] = lines[1] + "..."
    display_sub = "\\n".join(lines)
    
    tx = w // 2
    ty = h - (70 + (len(lines) - 1) * 45)
    draw.text((tx, ty), display_sub, font=font, anchor="ma", align="center", fill="#FFFFFF", stroke_width=4, stroke_fill="#000000")
    
    frame_jpg = frames_dir / f"frame_{{seq_id}}.jpg"
    canvas.save(frame_jpg, quality=92)
    return frame_jpg

print(f"Compositing {{len(segments)}} frames in parallel (ThreadPoolExecutor 8 workers)...")
with ThreadPoolExecutor(max_workers=8) as executor:
    list(executor.map(compose_frame, segments))
t_frames = time.time() - t_render_0
print(f"All {{len(segments)}} frames composited in {{t_frames:.2f}}s!")

# 7. Merge All Audio Clips into Combined Audio
print("Concatenating audio clips...")
combined_audio = chapter_dir / "audio" / "combined_audio.wav"
with wave.open(str(combined_audio), "wb") as outfile:
    first_wf = None
    for idx in range(1, len(segments) + 1):
        seq_id = f"{{idx:03d}}"
        wav_path = audio_clips_dir / f"audio_{chapter_number}_{{seq_id}}.wav"
        with wave.open(str(wav_path), "rb") as infile:
            if first_wf is None:
                first_wf = infile
                outfile.setnchannels(infile.getnchannels())
                outfile.setsampwidth(infile.getsampwidth())
                outfile.setframerate(infile.getframerate())
            outfile.writeframes(infile.readframes(infile.getnframes()))

# 8. Build FFmpeg Concat Script for Single-Pass Render
frames_concat_script = chapter_dir / "video" / "frames_concat.txt"
with open(frames_concat_script, "w", encoding="utf-8") as f:
    f.write("ffconcat version 1.0\\n")
    for idx, seg in enumerate(segments, 1):
        seq_id = f"{{idx:03d}}"
        f_path = (frames_dir / f"frame_{{seq_id}}.jpg").resolve()
        dur = seg.get("duration", 4.0)
        f.write(f"file '{{f_path}}'\\n")
        f.write(f"duration {{dur:.3f}}\\n")
    # Concat demuxer requirement: repeat last frame
    last_seq_id = f"{{len(segments):03d}}"
    last_f_path = (frames_dir / f"frame_{{last_seq_id}}.jpg").resolve()
    f.write(f"file '{{last_f_path}}'\\n")

# 9. Single-Pass High-Speed Hardware Video Encoding
final_video = chapter_dir / "video" / f"chapter_{chapter_number}.mp4"
final_video.parent.mkdir(parents=True, exist_ok=True)

cmd_encode = [
    "ffmpeg", "-y",
    "-f", "concat", "-safe", "0", "-i", str(frames_concat_script),
    "-i", str(combined_audio),
    "-vf", "fps=30,format=yuv420p",
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
    raise RuntimeError("Single-pass video encode failed")
t_enc = time.time() - t_enc_0
t_render_total = time.time() - t_render_0
print(f"🎉 Single-Pass Video Render FINISHED in {{t_enc:.2f}}s (Total render stage: {{t_render_total:.2f}}s)!")

# 8. Generate Subtitles SRT
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
        srt.write(f"{{idx}}\\n{{fmt_srt_time(t_start)}} --> {{fmt_srt_time(t_end)}}\\n{{seg['narrator_text']}}\\n\\n")

# 9. Build Telemetry Report
t_total_wall = time.time() - t_start_wall

telemetry = {{
    "project_slug": "{project_slug}",
    "chapter_number": "{chapter_number}",
    "status": "success",
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
    "video_size_mb": round(final_video.stat().st_size / (1024 * 1024), 2)
}}

print("\\n" + "=" * 60)
print("📊 CLOUD PRODUCTION TELEMETRY SUMMARY")
print("=" * 60)
print(json.dumps(telemetry, indent=2, ensure_ascii=False))

# 10. Package Output Bundle to /kaggle/working/
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
    ) -> Dict[str, Any]:
        """
        Dispatches a chapter production job to Kaggle Cloud, monitors progress, and returns result.
        """
        if not self._api:
            raise RuntimeError("Kaggle API is not authenticated.")

        kernel_slug = kernel_slug or f"manga-recap-{project_slug}-ch{chapter_number}".lower().replace("_", "-")
        staging_dir = Path(f"/tmp/kaggle_job_{project_slug}_{chapter_number}").resolve()
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"KaggleCloudService: Packaging chapter payload in {staging_dir}...")
        payload_tar = staging_dir / "payload.tar.gz"
        self.package_chapter_payload(project_slug, chapter_number, payload_tar)

        # Upload or update payload dataset on Kaggle
        dataset_slug = "manga-recap-payload"
        ds_staging = Path(f"/tmp/kaggle_ds_{dataset_slug}").resolve()
        ds_staging.mkdir(parents=True, exist_ok=True)
        shutil.copy2(payload_tar, ds_staging / "payload.tar.gz")
        ds_meta = {
            "title": "Manga Recap Payload",
            "id": f"{self._username}/{dataset_slug}",
            "licenses": [{"name": "CC0-1.0"}],
        }
        (ds_staging / "dataset-metadata.json").write_text(json.dumps(ds_meta, indent=2))
        try:
            logger.info("Syncing payload dataset to Kaggle...")
            self._api.dataset_create_new(str(ds_staging), public=False, quiet=True)
        except Exception:
            try:
                self._api.dataset_create_version(
                    str(ds_staging),
                    version_notes=f"Job ch.{chapter_number}",
                    quiet=True,
                    delete_old_versions=True,
                )
            except Exception as e:
                logger.warning(f"Dataset upload note: {e}")

        worker_py = staging_dir / "worker.py"
        worker_code = self.generate_worker_script(project_slug, chapter_number)
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

        logger.info(f"KaggleCloudService: Pushing kernel '{self._username}/{kernel_slug}' (GPU: {gpu})...")
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
            }

        logger.info(f"KaggleCloudService: Waiting for cloud execution of '{target_kernel_ref}'...")
        t0 = time.time()
        last_status = "QUEUED"

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

        # Download outputs
        output_dir = Path(f"/tmp/kaggle_out_{project_slug}_{chapter_number}").resolve()
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Downloading outputs from '{target_kernel_ref}' to {output_dir}...")
        self._api.kernels_output(target_kernel_ref, str(output_dir))

        # Unpack back into project directory
        sync_report = self.sync_results(project_slug, chapter_number, output_dir)
        return sync_report

    def sync_results(
        self,
        project_slug: str,
        chapter_number: str,
        downloaded_dir: Path,
    ) -> Dict[str, Any]:
        """
        Unpacks downloaded cloud output into the local chapter directory.
        """
        downloaded_dir = Path(downloaded_dir).resolve()
        chap_dir = get_chapter_dir(project_slug, chapter_number)
        video_dir = chap_dir / "video"
        video_dir.mkdir(parents=True, exist_ok=True)
        subtitles_dir = chap_dir / "subtitles"
        subtitles_dir.mkdir(parents=True, exist_ok=True)
        audio_dir = chap_dir / "audio" / "clips"
        audio_dir.mkdir(parents=True, exist_ok=True)

        # 1. Video
        vid_src = downloaded_dir / f"chapter_{chapter_number}.mp4"
        if not vid_src.exists():
            # Check recursive
            for f in downloaded_dir.rglob("*.mp4"):
                vid_src = f
                break

        if vid_src and vid_src.exists():
            shutil.copy2(vid_src, video_dir / "final.mp4")
            shutil.copy2(vid_src, video_dir / f"chapter_{chapter_number}.mp4")

        # 2. Subtitles
        srt_src = downloaded_dir / f"chapter_{chapter_number}.srt"
        if not srt_src.exists():
            for f in downloaded_dir.rglob("*.srt"):
                srt_src = f
                break
        if srt_src and srt_src.exists():
            shutil.copy2(srt_src, subtitles_dir / f"chapter_{chapter_number}.srt")

        # 3. Audio clips archive
        for f in downloaded_dir.rglob("*.tar.gz"):
            if "audio" in f.name:
                with tarfile.open(f, "r:gz") as atar:
                    atar.extractall(path=chap_dir / "audio")

        # 4. Script
        for f in downloaded_dir.rglob("script.json"):
            shutil.copy2(f, chap_dir / "script.json")

        # 5. Telemetry
        telemetry = {}
        for f in downloaded_dir.rglob("telemetry.json"):
            telemetry = read_json(f)
            shutil.copy2(f, chap_dir / "cloud_telemetry.json")

        # 6. Update chapter.json state
        chap_meta_path = chap_dir / "chapter.json"
        if chap_meta_path.exists():
            chap_meta = read_json(chap_meta_path)
            now_iso = datetime.now(timezone.utc).isoformat()
            chap_meta["updated_at"] = now_iso
            chap_meta["overall_status"] = "completed"
            chap_meta["current_stage"] = "export"
            for stg in ["audio", "render", "export"]:
                if "stages" in chap_meta and stg in chap_meta["stages"]:
                    chap_meta["stages"][stg]["status"] = "completed"
                    chap_meta["stages"][stg]["completed_at"] = now_iso
            atomic_write_json(chap_meta_path, chap_meta)

        logger.info(f"KaggleCloudService: Successfully synchronized Chapter {chapter_number} artifacts.")
        return {
            "status": "success",
            "project_slug": project_slug,
            "chapter_number": chapter_number,
            "telemetry": telemetry,
            "final_video": str(video_dir / "final.mp4"),
            "subtitles": str(subtitles_dir / f"chapter_{chapter_number}.srt"),
        }
