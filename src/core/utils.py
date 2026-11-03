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

def get_next_video_path(project_dir: Path, chapter: str) -> Path:
    """
    Scans video/chapters directory and returns the next version path (e.g. chapter_1_2.mp4)
    """
    video_dir = project_dir / "video" / "chapters"
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

def sync_agent_script_to_db(project_title: str, chapter_number: str, script_path: str = None) -> bool:
    """
    Imports Agent-authored script (script.txt / script.json) into Supabase DB and local script.csv.
    Used by both import_agent_script.py and pipeline.py in the Agentic Hybrid Workflow.
    """
    from src.core.logger import logger
    from src.services.tts_service import tts_service

    project_dir = get_project_data_dir(project_title)
    if not script_path:
        json_path = project_dir / "image" / "recut" / f"chap_{chapter_number}" / "script.json"
        txt_path = project_dir / "image" / "recut" / f"chap_{chapter_number}" / "script.txt"
        if json_path.exists():
            script_path = str(json_path)
        elif txt_path.exists():
            script_path = str(txt_path)
        else:
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

        seq_numbers = [p["sequence_number"] for p in parsed_panels]
        min_seq = min(seq_numbers)
        max_seq = max(seq_numbers)

        proj_res = supabase.table("projects").select("id").eq("title", project_title).execute()
        if not proj_res.data:
            logger.error(f"Project '{project_title}' not found in database.")
            return False
        project_id = proj_res.data[0]["id"]

        chap_res = supabase.table("chapters").select("id").eq("project_id", project_id).eq("chapter_number", str(chapter_number)).execute()
        if not chap_res.data:
            logger.error(f"Chapter {chapter_number} not found in database.")
            return False
        chapter_id = chap_res.data[0]["id"]

        # Update panel statuses
        for p in parsed_panels:
            seq_num = p["sequence_number"]
            status = p.get("status", "processed")
            supabase.table("panels").update({"status": status}).eq("chapter_id", chapter_id).eq("sequence_number", seq_num).execute()

        # Clean old script segments
        supabase.table("script_segments").delete().eq("chapter_id", chapter_id).execute()

        # Insert new script segment
        supabase.table("script_segments").insert({
            "chapter_id": chapter_id,
            "start_panel_seq": min_seq,
            "end_panel_seq": max_seq,
            "script_text": script_content,
            "status": "completed"
        }).execute()

        # Update script.csv
        update_csv_script(project_dir, chapter_number, parsed_panels)

        # Update chapter status to 'scripted'
        supabase.table("chapters").update({"status": "scripted"}).eq("id", chapter_id).execute()
        logger.info(f"Agent script from {script_path} successfully synced into database for Chapter {chapter_number}.")
        return True
    except Exception as e:
        logger.error(f"Error syncing agent script to database: {e}")
        return False


