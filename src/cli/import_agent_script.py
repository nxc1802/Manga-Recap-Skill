import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parents[2]))

from src.core.logger import logger
from src.core.supabase_client import supabase
from src.services.tts_service import tts_service

async def main():
    parser = argparse.ArgumentParser(description="Import Agent's Custom Script into Supabase")
    parser.add_argument("--chapter", type=str, required=True, help="Chapter number (e.g., 1)")
    parser.add_argument("--script", type=str, default=None, help="Path to script.txt (default: data/raw_images/chap_{N}/script.txt)")
    parser.add_argument("--project", type=str, default="Mỗi lính đánh thuê trở về đều có một kế hoạch", help="Project title in database")
    args = parser.parse_args()

    # Determine script path
    script_path = args.script
    if not script_path:
        from src.core.utils import get_project_data_dir
        project_dir = get_project_data_dir(args.project)
        json_path = os.path.join(project_dir, "raw_images", f"chap_{args.chapter}", "script.json")
        txt_path = os.path.join(project_dir, "raw_images", f"chap_{args.chapter}", "script.txt")
        if os.path.exists(json_path):
            script_path = json_path
        else:
            script_path = txt_path

    if not os.path.exists(script_path):
        logger.error(f"Script file not found: {script_path}")
        sys.exit(1)

    try:
        # 1. Read script content
        with open(script_path, "r", encoding="utf-8") as f:
            script_content = f.read()

        # Parse and validate panels in script
        parsed_panels = tts_service.parse_script_text(script_content)
        if not parsed_panels:
            logger.error("Failed to parse script. No panels found in standard format '[Khung hình X]: ...' or JSON.")
            sys.exit(1)

        seq_numbers = [p["sequence_number"] for p in parsed_panels]
        min_seq = min(seq_numbers)
        max_seq = max(seq_numbers)
        logger.info(f"Parsed script ({os.path.basename(script_path)}) contains {len(parsed_panels)} panels, sequence range: {min_seq} to {max_seq}")

        # 2. Get project from Supabase
        proj_res = supabase.table("projects").select("id").eq("title", args.project).execute()
        if not proj_res.data:
            logger.error(f"Project '{args.project}' not found in database. Run crawler first.")
            sys.exit(1)
        project_id = proj_res.data[0]["id"]

        # 3. Get chapter from Supabase
        chap_res = supabase.table("chapters").select("id").eq("project_id", project_id).eq("chapter_number", args.chapter).execute()
        if not chap_res.data:
            logger.error(f"Chapter {args.chapter} not found in database. Run crawler first.")
            sys.exit(1)
        chapter_id = chap_res.data[0]["id"]

        # 4. Fetch existing panels from DB
        panels_res = supabase.table("panels").select("id, sequence_number").eq("chapter_id", chapter_id).execute()
        db_panels = panels_res.data
        if not db_panels:
            logger.error("No panels found in database for this chapter. Run crawler first.")
            sys.exit(1)
        
        db_seq_numbers = {p["sequence_number"] for p in db_panels}
        logger.info(f"Database contains {len(db_panels)} panels for Chapter {args.chapter}")

        # Validate matching sequences
        missing_in_script = db_seq_numbers - set(seq_numbers)
        if missing_in_script:
            logger.warning(f"WARNING: Database panels {sorted(missing_in_script)} are missing in script!")
        
        extra_in_script = set(seq_numbers) - db_seq_numbers
        if extra_in_script:
            logger.warning(f"WARNING: Script contains panels {sorted(extra_in_script)} which are NOT in database!")

        # 5. Update panel status to 'processed' in Supabase
        logger.info("Updating all panel statuses to 'processed' in database...")
        supabase.table("panels").update({"status": "processed"}).eq("chapter_id", chapter_id).execute()

        # 6. Delete old script segments for this chapter
        logger.info("Cleaning up old script segments from database...")
        supabase.table("script_segments").delete().eq("chapter_id", chapter_id).execute()

        # 7. Insert new unified script segment
        logger.info(f"Inserting script segment (panels {min_seq}-{max_seq}) into database...")
        supabase.table("script_segments").insert({
            "chapter_id": chapter_id,
            "start_panel_seq": min_seq,
            "end_panel_seq": max_seq,
            "script_text": script_content,
            "status": "completed"
        }).execute()

        # Sync imported script with local script.csv
        from src.core.utils import get_project_data_dir, update_csv_script
        project_dir = get_project_data_dir(args.project)
        update_csv_script(project_dir, args.chapter, parsed_panels)

        # 8. Update chapter status to 'scripted'
        logger.info("Updating chapter status to 'scripted'...")
        supabase.table("chapters").update({"status": "scripted"}).eq("id", chapter_id).execute()

        logger.info("=========================================================")
        logger.info("SUCCESS: Agent script imported successfully into database!")
        logger.info("The chapter is now ready for audio and video generation.")
        logger.info("=========================================================")

    except Exception as e:
        logger.exception(f"Import script failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
