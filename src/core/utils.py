import os
import re
import unicodedata
from pathlib import Path
from src.core.storage import (
    read_json,
    atomic_write_json,
    cleanup_intermediate_files,
    StorageManager,
)

def slugify_project_title(project_title: str) -> str:
    """
    Normalizes a project title by converting Vietnamese accents and non-ASCII characters
    to plain ASCII, lowercasing, and replacing spaces with underscores.
    """
    # Decompose unicode characters to separate base letters from diacritics
    nfkd_form = unicodedata.normalize('NFKD', project_title)
    only_ascii = nfkd_form.encode('ASCII', 'ignore').decode('utf-8')
    
    # Strip non-alphanumeric, convert spaces and hyphens to underscore, lowercase
    slug = re.sub(r'[^a-zA-Z0-9\s-]', '', only_ascii).strip().lower()
    slug = re.sub(r'[\s-]+', '_', slug)
    return slug

def get_project_data_dir(project_slug: str) -> Path:
    """
    Returns the project data directory.
    When MANGA_RECAP_DATA_DIR is set, resolves strictly within that root
    (<MANGA_RECAP_DATA_DIR>/projects/{slug} with legacy fallback <MANGA_RECAP_DATA_DIR>/{slug})
    so isolated test/cloud runs never fall back to repo-local data/.
    Otherwise uses config.storage_dir / project_slug with config.data_dir fallback.
    """
    from src.core.config import config

    env_data_dir = os.getenv("MANGA_RECAP_DATA_DIR")
    if env_data_dir:
        data_dir = Path(env_data_dir)
        storage_dir = data_dir / "projects"
    else:
        storage_dir = config.storage_dir
        data_dir = config.data_dir

    slug = slugify_project_title(str(project_slug))
    canonical_slug = storage_dir / slug
    if canonical_slug.exists():
        return canonical_slug
    if (storage_dir / str(project_slug)).exists():
        return storage_dir / str(project_slug)

    legacy_slug = data_dir / slug
    if legacy_slug.exists():
        return legacy_slug
    if (data_dir / str(project_slug)).exists():
        return data_dir / str(project_slug)

    return canonical_slug

def get_project_title_by_id(project_id: str) -> str:
    """
    Returns the project title from local storage (project.json) or derives it from the slug/ID.
    """
    if not project_id:
        return "default_project"
    project_dir = get_project_data_dir(project_id)
    project_json = project_dir / "project.json"
    if project_json.exists():
        try:
            data = read_json(project_json)
            return data.get("title", project_id)
        except Exception:
            pass
    return project_id

def get_project_title_by_chapter_id(chapter_id: str) -> str:
    """
    Resolves project title by chapter identifier or path.
    """
    if not chapter_id:
        return "default_project"
    # Format: {slug}_{chapter_num}
    if "_" in chapter_id:
        parts = chapter_id.rsplit("_", 1)
        return get_project_title_by_id(parts[0])
    return chapter_id

def format_sequence_id(seq_id: str) -> str:
    """
    Format sequence ID for file naming (e.g. "5" -> "005", "5_1" -> "005_1")
    """
    if not seq_id:
        return "000"
    seq_id = str(seq_id).strip()
    if seq_id.isdigit():
        return f"{int(seq_id):03d}"
    parts = seq_id.split("_")
    if parts[0].isdigit():
        parts[0] = f"{int(parts[0]):03d}"
        return "_".join(parts)
import csv
import shutil
import subprocess
from typing import Union, Optional, List


class DependencyNotFoundError(Exception):
    """Raised when external tool (ffmpeg, ffprobe) is not installed on PATH."""
    pass


class FFmpegExecutionError(Exception):
    """Raised when FFmpeg subprocess returns non-zero exit code."""
    def __init__(self, message: str, returncode: int, stderr: str = ""):
        super().__init__(f"{message} (exit code {returncode}): {stderr.strip()[-500:] if stderr else ''}")
        self.returncode = returncode
        self.stderr = stderr


def run_ffmpeg(cmd: List[str], description: str = "FFmpeg process") -> subprocess.CompletedProcess:
    """
    Executes an FFmpeg command with binary existence check and non-zero exit code enforcement.
    """
    binary = cmd[0] if cmd else "ffmpeg"
    if not shutil.which(binary):
        raise DependencyNotFoundError(f"Binary '{binary}' not found on system PATH. Please install FFmpeg.")
    
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise FFmpegExecutionError(f"{description} failed", result.returncode, result.stderr)
    return result


def get_next_character_image_path(project_dir: Path, character_name: str = "unknown") -> Path:
    """
    Scans image/character/{character_name} directory and returns the next sequential path (e.g. crop_3.jpg)
    """
    char_dir = project_dir / "image" / "character" / character_name
    char_dir.mkdir(parents=True, exist_ok=True)
    try:
        existing = [f for f in os.listdir(char_dir) if f.startswith("crop_") and f.endswith(".jpg")]
    except Exception:
        existing = []
    indices = []
    for f in existing:
        match = re.search(r'crop_(\d+)\.jpg', f)
        if match:
            indices.append(int(match.group(1)))
    next_idx = max(indices) + 1 if indices else 1
    return char_dir / f"crop_{next_idx}.jpg"

def move_character_crop(project_dir: Path, current_path: str, character_name: str) -> str:
    """
    Moves/renames a character crop from its current location to image/character/{character_name}/crop_{next_idx}.jpg
    and returns the new path string.
    """
    if not current_path or not os.path.exists(current_path):
        return current_path
    
    current_p = Path(current_path)
    target_dir = project_dir / "image" / "character" / character_name
    if current_p.parent == target_dir:
        return current_path
        
    new_path = get_next_character_image_path(project_dir, character_name)
    try:
        os.rename(current_path, new_path)
        from src.core.logger import logger
        logger.info(f"Moved crop from {current_path} to {new_path}")
        return str(new_path)
    except Exception as e:
        from src.core.logger import logger
        logger.error(f"Failed to move crop from {current_path} to {new_path}: {e}")
        return current_path

def get_next_video_path(project_dir: Path, chapter: Union[str, int], suffix: Optional[str] = None) -> Path:
    """
    Scans video/chapters directory and returns the next version path.
    If suffix is provided (e.g. 'preview', 'final', 'agent'):
      returns video/chapters/chapter_{chapter}_{suffix}_v{next_ver}.mp4
    If suffix is None or empty:
      returns video/chapters/chapter_{chapter}_{next_ver}.mp4
    """
    video_dir = project_dir / "video" / "chapters"
    video_dir.mkdir(parents=True, exist_ok=True)
    
    chapter_str = str(chapter)
    try:
        existing = os.listdir(video_dir)
    except Exception:
        existing = []

    if suffix:
        pattern = re.compile(rf"^chapter_{re.escape(chapter_str)}_{re.escape(suffix)}_v(\d+)\.mp4$")
        fallback_pattern = re.compile(rf"^chapter_{re.escape(chapter_str)}_{re.escape(suffix)}_(\d+)\.mp4$")
        simple_pattern = re.compile(rf"^chapter_{re.escape(chapter_str)}_{re.escape(suffix)}\.mp4$")
        versions = []
        for f in existing:
            m = pattern.match(f)
            if m:
                versions.append(int(m.group(1)))
                continue
            m2 = fallback_pattern.match(f)
            if m2:
                versions.append(int(m2.group(1)))
                continue
            if simple_pattern.match(f):
                versions.append(1)
        next_ver = max(versions, default=0) + 1
        return video_dir / f"chapter_{chapter_str}_{suffix}_v{next_ver}.mp4"
    else:
        pattern = re.compile(rf"^chapter_{re.escape(chapter_str)}_(\d+)\.mp4$")
        v_pattern = re.compile(rf"^chapter_{re.escape(chapter_str)}_v(\d+)\.mp4$")
        versions = []
        for f in existing:
            m = pattern.match(f)
            if m:
                versions.append(int(m.group(1)))
                continue
            m2 = v_pattern.match(f)
            if m2:
                versions.append(int(m2.group(1)))
        next_ver = max(versions, default=0) + 1
        return video_dir / f"chapter_{chapter_str}_{next_ver}.mp4"

def resolve_video_output_path(chapter_dir: Path, chapter_num: Union[str, int], suffix: str = "agent", version: Optional[int] = None) -> Path:
    """
    Resolves video output path in video/ directory: chapter_{chapter_num}_{suffix}_v{version}.mp4.
    If version is None, auto-increments based on existing files in chapter_dir / 'video'.
    """
    video_dir = chapter_dir / "video"
    video_dir.mkdir(parents=True, exist_ok=True)
    chap_str = str(chapter_num)
    if version is not None:
        return video_dir / f"chapter_{chap_str}_{suffix}_v{version}.mp4"
    
    # Auto-increment
    pattern = re.compile(rf"^chapter_{re.escape(chap_str)}_{re.escape(suffix)}_v(\d+)\.mp4$")
    versions = []
    if video_dir.exists():
        for f in os.listdir(video_dir):
            m = pattern.match(f)
            if m:
                versions.append(int(m.group(1)))
    next_ver = max(versions, default=0) + 1
    return video_dir / f"chapter_{chap_str}_{suffix}_v{next_ver}.mp4"

def update_csv_script(project_dir: Path, chapter: str, script_data: list[dict]):
    """
    Appends or replaces script data for a chapter in the project's central memory/script.csv.
    """
    csv_dir = project_dir / "memory"
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_dir / "script.csv"
    
    rows = []
    headers = ["chapter", "sequence_id", "text", "focus_x", "focus_y", "pan_direction", "crop_scale"]
    if csv_path.exists():
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    if str(r.get("chapter")) != str(chapter):
                        rows.append(r)
        except Exception:
            pass
            
    for item in script_data:
        rows.append({
            "chapter": str(chapter),
            "sequence_id": str(item.get("sequence_id", "")),
            "text": str(item.get("text", "")),
            "focus_x": str(item.get("focus_x", 50)),
            "focus_y": str(item.get("focus_y", 50)),
            "pan_direction": str(item.get("pan_direction", "horizontal")),
            "crop_scale": str(item.get("crop_scale", 100))
        })
        
    try:
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
    except Exception as e:
        try:
            from src.core.logger import logger
            logger.error(f"Error writing script.csv: {e}")
        except Exception:
            pass

def load_csv_script_for_chapter(project_dir: Path, chapter: str) -> list[dict]:
    """
    Loads and parses script segments for a chapter from memory/script.csv.
    """
    csv_path = project_dir / "memory" / "script.csv"
    if not csv_path.exists():
        return []
    
    results = []
    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                if str(r.get("chapter")) == str(chapter):
                    results.append({
                        "sequence_id": r.get("sequence_id", ""),
                        "sequence_number": int(re.match(r"^(\d+)", r.get("sequence_id", "0")).group(1)) if re.match(r"^(\d+)", r.get("sequence_id", "0")) else 0,
                        "text": r.get("text", ""),
                        "focus_x": int(r.get("focus_x", 50)) if r.get("focus_x") else 50,
                        "focus_y": int(r.get("focus_y", 50)) if r.get("focus_y") else 50,
                        "pan_direction": r.get("pan_direction", "horizontal") or "horizontal",
                        "crop_scale": int(r.get("crop_scale", 100)) if r.get("crop_scale") else 100
                    })
    except Exception as e:
        try:
            from src.core.logger import logger
            logger.error(f"Error loading script.csv for chapter {chapter}: {e}")
        except Exception:
            pass
            
    def get_sort_key(x):
        val = x["sequence_id"].replace("_", ".")
        try:
            return float(val)
        except ValueError:
            return float(x["sequence_number"])
    results.sort(key=get_sort_key)
    return results

def sync_agent_script_to_storage(project_title: str, chapter_number: str, script_path: str = None) -> bool:
    """
    Imports Agent-authored script (script.txt / script.json) into local storage
    (script.json, chapter.json, and memory/script.csv).
    Zero Supabase or database dependencies.
    """
    from src.core.logger import logger
    from src.services.tts_service import tts_service

    project_dir = get_project_data_dir(project_title)
    chap_dir = project_dir / "chapters" / str(chapter_number)
    legacy_recut_dir = project_dir / "image" / "recut" / f"chap_{chapter_number}"

    if not script_path:
        for candidate in [
            chap_dir / "script.json",
            chap_dir / "script.txt",
            legacy_recut_dir / "script.json",
            legacy_recut_dir / "script.txt"
        ]:
            if candidate.exists():
                script_path = str(candidate)
                break
        if not script_path:
            return False

    if not os.path.exists(script_path):
        return False

    try:
        with open(script_path, "r", encoding="utf-8") as f:
            script_content = f.read()

        parsed_panels = tts_service.parse_script_text(script_content)
        if not parsed_panels:
            logger.error(f"Failed to parse script from {script_path}. No valid panel markers found.")
            return False

        # 1. Save structured script.json
        script_data = {
            "schema_version": "3.0.0",
            "project_title": project_title,
            "chapter_number": str(chapter_number),
            "segments": parsed_panels
        }
        dest_script_json = chap_dir / "script.json"
        dest_script_json.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(dest_script_json, script_data)

        # 2. Update memory/script.csv for render engine compatibility
        update_csv_script(project_dir, chapter_number, parsed_panels)

        # 3. Update chapter.json checkpoint
        chapter_json_path = chap_dir / "chapter.json"
        if chapter_json_path.exists():
            chap_data = read_json(chapter_json_path)
        else:
            chap_data = {
                "schema_version": "3.0.0",
                "chapter_id": f"{slugify_project_title(project_title)}_chap_{chapter_number}",
                "chapter_number": str(chapter_number),
                "project_slug": slugify_project_title(project_title),
                "title": f"Chapter {chapter_number}",
                "status": "pending",
                "stages": {}
            }
        chap_data["status"] = "scripted"
        chap_data.setdefault("stages", {})["script"] = {
            "stage": "script",
            "status": "completed",
            "artifacts": [str(dest_script_json.relative_to(project_dir))]
        }
        atomic_write_json(chapter_json_path, chap_data)

        logger.info(f"Agent script from {script_path} successfully synced to local storage for Chapter {chapter_number}.")
        return True
    except Exception as e:
        logger.error(f"Error syncing agent script to local storage: {e}")
        return False

# Backwards compatibility alias
sync_agent_script_to_db = sync_agent_script_to_storage


def compute_sliding_windows(
    panel_count: int,
    window_size: int = 5,
    stride: int = 3,
) -> list[dict]:
    """
    Computes mathematical sliding window partitions for panels [0, P).
    Batch k commits panels [3k, min(3k + 3, P)) and lookahead to [3k, min(3k + 5, P)).
    Guarantees each panel p in [0, P) is committed to narration exactly once with zero duplicates.
    """
    if panel_count <= 0:
        return []
    windows = []
    num_batches = (panel_count + stride - 1) // stride
    for k in range(num_batches):
        start_idx = k * stride
        commit_end = min(start_idx + stride, panel_count)
        lookahead_end = min(start_idx + window_size, panel_count)
        windows.append({
            "batch_index": k,
            "commit_indices": list(range(start_idx, commit_end)),
            "window_indices": list(range(start_idx, lookahead_end)),
        })
    return windows




