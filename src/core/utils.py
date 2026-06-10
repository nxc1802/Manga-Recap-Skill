import re
import unicodedata
from pathlib import Path
from src.core.supabase_client import supabase

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

def get_project_data_dir(project_title: str) -> Path:
    """
    Returns the project-specific data directory: data/{project_slug}
    """
    slug = slugify_project_title(project_title)
    return Path("data") / slug

def get_project_title_by_id(project_id: str) -> str:
    """
    Queries Supabase to fetch the project title by project ID.
    """
    try:
        res = supabase.table("projects").select("title").eq("id", project_id).execute()
        if res.data:
            return res.data[0]["title"]
    except Exception:
        pass
    return "default_project"

def get_project_title_by_chapter_id(chapter_id: str) -> str:
    """
    Queries Supabase to fetch the project title by chapter ID.
    """
    try:
        res = supabase.table("chapters").select("project_id").eq("id", chapter_id).execute()
        if res.data:
            return get_project_title_by_id(res.data[0]["project_id"])
    except Exception:
        pass
    return "default_project"

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
import os
import csv

def get_next_character_image_path(project_dir: Path) -> Path:
    """
    Scans character_images directory and returns the next sequential path (e.g. character_3.jpg)
    """
    char_dir = project_dir / "character_images"
    char_dir.mkdir(parents=True, exist_ok=True)
    try:
        existing = [f for f in os.listdir(char_dir) if f.startswith("character_") and f.endswith(".jpg")]
    except Exception:
        existing = []
    indices = []
    for f in existing:
        match = re.search(r'character_(\d+)\.jpg', f)
        if match:
            indices.append(int(match.group(1)))
    next_idx = max(indices) + 1 if indices else 1
    return char_dir / f"character_{next_idx}.jpg"

def get_next_video_path(project_dir: Path, chapter: str) -> Path:
    """
    Scans video directory and returns the next version path (e.g. chapter_1_2.mp4)
    """
    video_dir = project_dir / "video"
    video_dir.mkdir(parents=True, exist_ok=True)
    try:
        existing = [f for f in os.listdir(video_dir) if f.startswith(f"chapter_{chapter}_") and f.endswith(".mp4")]
    except Exception:
        existing = []
    versions = []
    for f in existing:
        match = re.search(rf'chapter_{chapter}_(\d+)\.mp4', f)
        if match:
            versions.append(int(match.group(1)))
    next_ver = max(versions) + 1 if versions else 1
    return video_dir / f"chapter_{chapter}_{next_ver}.mp4"

def update_csv_script(project_dir: Path, chapter: str, script_data: list[dict]):
    """
    Appends or replaces script data for a chapter in the project's central scripts/script.csv.
    """
    csv_dir = project_dir / "scripts"
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
    Loads and parses script segments for a chapter from scripts/script.csv.
    """
    csv_path = project_dir / "scripts" / "script.csv"
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

