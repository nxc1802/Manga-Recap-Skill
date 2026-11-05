import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.logger import logger
from src.core.storage import StorageManager
from src.core.utils import get_project_data_dir, sync_agent_script_to_storage, slugify_project_title
from src.services.tts_service import tts_service
from src.pipeline import pipeline
from src.core.config import config

async def main():
    parser = argparse.ArgumentParser(description="Render Audio and Video for Manga Recap")
    parser.add_argument("--chapter", type=str, required=True, help="Chapter number (e.g., 1)")
    parser.add_argument("--project", type=str, default="Mỗi lính đánh thuê trở về đều có một kế hoạch", help="Project title")
    parser.add_argument("--bgm", type=str, default=None, help="Path to BGM audio file (default: None)")
    parser.add_argument("--voice", type=str, default=None, help="Voice name (default: from config)")
    parser.add_argument("--speed", type=float, default=None, help="TTS speed adjustment (default: from config)")
    parser.add_argument("--suffix", type=str, default="agent", help="Suffix for output video (default: agent)")
    parser.add_argument("--keep_temp", action="store_true", help="Keep temporary images and audio files (default: False)")
    parser.add_argument("--no-subs", action="store_true", help="Disable burning subtitles onto the video")
    parser.add_argument("--video-mode", type=str, default="auto", choices=["auto", "portrait_padded", "landscape_fit", "portrait_vertical"], help="Video layout mode (default: auto)")
    parser.add_argument("--lang", type=str, default="vi", choices=["vi", "en"], help="Narration and subtitles language (default: vi)")
    args = parser.parse_args()

    # Apply language to config
    if config and config.orchestrator:
        config.orchestrator.language = args.lang

    # Fallback configs
    voice = args.voice or (config.tts.voice if config else "Trúc Ly")
    speed = args.speed or (config.tts.speed if config else 1.0)
    burn_subtitles = not args.no_subs

    try:
        project_dir = get_project_data_dir(args.project)
        chap_dir = project_dir / "chapters" / str(args.chapter)
        legacy_recut_dir = project_dir / "image" / "recut" / f"chap_{args.chapter}"

        if not project_dir.exists():
            logger.error(f"Project '{args.project}' directory not found at {project_dir}. Please crawl and import script first.")
            sys.exit(1)

        chapter_id = f"{slugify_project_title(args.project)}_chap_{args.chapter}"

        # Progress callback to show live events in console
        async def progress_cb(event_type: str, message: str, data: dict = None):
            logger.info(f"[Progress - {event_type}]: {message} | Data: {data}")

        # Ensure script is available locally
        script_json = chap_dir / "script.json"
        script_txt = legacy_recut_dir / "script.txt"
        script_csv = project_dir / "memory" / "script.csv"

        if not script_json.exists() and not script_csv.exists():
            if script_txt.exists():
                logger.info("Found script.txt; syncing to local storage...")
                sync_agent_script_to_storage(args.project, args.chapter, str(script_txt))
            else:
                logger.error(
                    f"LỖI: Kịch bản chưa tồn tại cho Chapter {args.chapter}. "
                    "Quy trình yêu cầu Agent viết kịch bản vào script.json hoặc script.txt trước."
                )
                sys.exit(1)

        # 3. TTS Audio Generation
        logger.info(f"--- Phase 5: Generating Speech Audio (Voice: {voice}, Speed: {speed}) ---")
        tts_success = await tts_service.process_chapter_audio(
            chapter_id=chapter_id,
            chapter_num=args.chapter,
            speed=speed,
            voice_name=voice,
            progress_callback=progress_cb
        )
        if not tts_success:
            logger.error("TTS Audio Generation failed.")
            sys.exit(1)
            
        logger.info("TTS Audio Generation completed successfully.")

        # 4. Video Generation
        logger.info("--- Phase 6: Rendering Video ---")
        video_path = await pipeline.generate_video(
            chapter_id=chapter_id,
            chapter_number=args.chapter,
            bgm_path=args.bgm,
            output_suffix=args.suffix,
            progress_callback=progress_cb,
            burn_subtitles=burn_subtitles,
            video_mode=args.video_mode
        )
        
        logger.info("Video generation completed successfully.")

        # 5. Optional Storage Cleanup
        if not args.keep_temp:
            cleaned = StorageManager.cleanup_intermediate_files(chap_dir, keep_temp=False)
            pipeline.cleanup_temp_files(args.chapter, args.suffix, args.project)
            logger.info(f"Temporary files cleaned up ({len(cleaned)} files removed).")
        else:
            logger.info("Keeping temporary files as requested.")


        logger.info("=========================================================")
        logger.info("SUCCESS: Video rendered successfully!")
        logger.info(f"Final Video File: {Path(video_path).resolve()}")
        logger.info("=========================================================")

    except Exception as e:
        logger.exception(f"Render CLI failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
