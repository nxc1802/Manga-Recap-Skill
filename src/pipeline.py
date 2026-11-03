import os
import asyncio
from pathlib import Path
from src.core.config import config
from src.core.logger import logger
from src.core.utils import (
    format_sequence_id,
    sync_agent_script_to_db,
    get_project_data_dir,
    get_next_video_path,
    run_ffmpeg,
    cleanup_intermediate_files,
)

from src.services.crawler_service import crawler_service
from src.services.tts_service import tts_service
from src.render.video_engine import video_engine, VideoMode

class MangaRecapPipeline:
    def __init__(self):
        pass

    def cleanup_temp_files(self, chapter: int | str, suffix: str = "", project: str = None, keep_temp: bool = False) -> list:
        """
        Cleans up temporary video and subtitle files generated during chapter processing.
        """
        proj_name = project or "Mỗi lính đánh thuê trở về đều có một kế hoạch"
        project_dir = get_project_data_dir(proj_name)
        chap_dir = project_dir / "chapters" / str(chapter)
        cleaned = []
        if chap_dir.exists():
            cleaned.extend(cleanup_intermediate_files(chap_dir, keep_temp=keep_temp))
        video_chap_dir = project_dir / "video" / "chapters"
        if video_chap_dir.exists():
            cleaned.extend(cleanup_intermediate_files(video_chap_dir, keep_temp=keep_temp))
        logger.info(f"Cleaned up {len(cleaned)} temporary files for chapter {chapter}: {cleaned}")
        return cleaned

    async def run(
        self,
        chapter_number: str,
        general_story_context: str = "Không có bối cảnh cốt truyện.",
        project_title: str = "Mỗi lính đánh thuê trở về đều có một kế hoạch",
        crawler_urls: dict[str, str] = None,
        config_overrides: dict = None,
        bgm_path: str = None,
        output_suffix: str = "agent",
        progress_callback = None,
        burn_subtitles: bool = True
    ) -> str:
        """
        Runs the Manga Recap Pipeline following the Agentic Hybrid Workflow:
        1. Setup project & crawl/recut images.
        2. Validate / auto-sync Agent's custom script (script.txt / script.json).
        3. Cloud TTS audio generation.
        4. Video rendering with subtitles & cinematic pan/zoom.
        """
        logger.info(f"=== Starting Manga Recap Pipeline (Agentic Hybrid Workflow) for Chapter {chapter_number} ===")
        
        overrides = config_overrides or {}
        max_panels = overrides.get("max_panels")
        tts_speed = overrides.get("tts_speed") or config.tts.speed
        tts_voice = overrides.get("tts_voice") or config.tts.voice
        video_mode = overrides.get("video_mode") or getattr(config.orchestrator, "video_mode", "auto")
        
        # 1. Project setup
        if progress_callback:
            await progress_callback("project_setup", f"Khởi tạo dự án '{project_title}'...", {"project_title": project_title})
        project_id = await crawler_service.get_or_create_project(project_title)
        
        # 2. Crawl & Recut Chapter
        logger.info("--- Phase 1: Crawl & Recut Chapter Panels ---")
        if progress_callback:
            await progress_callback("crawl_start", f"Kiểm tra và crawl ảnh chapter {chapter_number}...", {})
        
        chapter_base_url = crawler_urls.get("chapter_base_url") if crawler_urls else None
        chapter_id, summary_context = await crawler_service.crawl_chapter(
            project_id=project_id,
            chapter_number=chapter_number,
            general_story_context=general_story_context,
            chapter_base_url=chapter_base_url,
            max_panels=max_panels
        )
        logger.info(f"Chapter ID in DB: {chapter_id}")
        if progress_callback:
            await progress_callback("crawl_complete", f"Crawl và phân cảnh hoàn tất chapter {chapter_number}.", {"chapter_id": chapter_id})
        
        # 3. Agentic Script Validation & Sync
        logger.info("--- Phase 2: Agent Script Validation & Sync ---")
        if progress_callback:
            await progress_callback("script_check", "Kiểm tra kịch bản Agent...", {})
            
        # Check if already scripted locally
        project_dir = get_project_data_dir(project_title)
        chap_dir = project_dir / "chapters" / str(chapter_number)
        script_json = chap_dir / "script.json"
        script_csv = project_dir / "memory" / "script.csv"
        is_scripted = script_json.exists() or script_csv.exists()

        if not is_scripted:
            # Check if script.txt or script.json exists on disk to auto-sync
            logger.info("Script not found in local storage. Checking local disk for Agent script...")
            synced = sync_agent_script_to_db(project_title, chapter_number)
            if synced:
                logger.info("Agent script on disk automatically synced into local storage.")
                is_scripted = True
            else:
                project_dir = get_project_data_dir(project_title)
                recut_dir = project_dir / "image" / "recut" / f"chap_{chapter_number}"
                err_msg = (
                    f"Agentic Hybrid Workflow yêu cầu Agent (AI) phải trực tiếp xem các ảnh phân cảnh trong "
                    f"'{recut_dir}' và viết kịch bản tiếng Việt vào file 'script.txt' trước khi kết xuất.\n"
                    f"Sau khi viết xong, hãy chạy 'python src/cli/import_agent_script.py --chapter {chapter_number} --project \"{project_title}\"'."
                )
                logger.error(err_msg)
                raise RuntimeError(err_msg)

        if progress_callback:
            await progress_callback("script_complete", "Kịch bản Agent đã sẵn sàng.", {})

        # 4. Cloud TTS Audio Generation
        logger.info("--- Phase 3: Cloud TTS Audio Generation ---")
        if progress_callback:
            await progress_callback("tts_start", f"Bắt đầu tạo giọng đọc TTS ({tts_voice})...", {})
            
        tts_success = await tts_service.process_chapter_audio(
            chapter_id=chapter_id,
            chapter_num=chapter_number,
            speed=tts_speed,
            voice_name=tts_voice,
            progress_callback=progress_callback
        )
        if not tts_success:
            raise RuntimeError("TTS Audio Generation failed.")
            
        if progress_callback:
            await progress_callback("tts_complete", "Tạo giọng đọc hoàn tất.", {})
            
        # 5. Video Generation
        logger.info("--- Phase 4: Video Generation ---")
        if progress_callback:
            await progress_callback("video_start", "Bắt đầu xử lý và kết xuất video thành phẩm...", {})
            
        video_path = await self.generate_video(
            chapter_id=chapter_id,
            chapter_number=chapter_number,
            bgm_path=bgm_path,
            output_suffix=output_suffix,
            progress_callback=progress_callback,
            burn_subtitles=burn_subtitles,
            video_mode=video_mode
        )
        
        logger.info(f"=== Manga Recap Pipeline Completed successfully! Video saved at: {video_path} ===")
        return video_path

    async def _process_single_panel_clip(
        self,
        seq_id: str,
        img_path: str,
        audio_path: Path,
        panel_text: str,
        video_mode,
        project_dir: Path,
        chapter_number: str,
        progress_callback,
        burn_subtitles: bool = True
    ) -> tuple[str, dict]:
        """
        Helper method to process a single panel clip in parallel:
        1. Create background image (cropped + blurred) using Pillow (in thread pool).
        2. Render panel clip with FFmpeg overlaying the static subtitle PNG (in thread pool).
        Uses actual audio duration.
        """
        duration_ms = tts_service.get_audio_duration_ms(str(audio_path))
        clip_duration_ms = duration_ms
        
        formatted_seq = format_sequence_id(seq_id)
        cropped_dir = project_dir / "image" / "cropped"
        cropped_dir.mkdir(parents=True, exist_ok=True)
        clips_dir = project_dir / "video" / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        
        if "_" in seq_id:
            parts = seq_id.split("_")
            raw_seq = parts[0]
            crop_idx = parts[1]
        else:
            raw_seq = seq_id
            crop_idx = "1"
            
        processed_image_path = cropped_dir / f"image_{chapter_number}_{raw_seq}_{crop_idx}.webp"
        output_clip_path = clips_dir / f"clip_{chapter_number}_{formatted_seq}.mp4"
        
        is_uptodate = False
        if output_clip_path.exists() and audio_path.exists():
            clip_mtime = os.path.getmtime(str(output_clip_path))
            audio_mtime = os.path.getmtime(str(audio_path))
            img_mtime = os.path.getmtime(str(img_path)) if os.path.exists(str(img_path)) else 0
            if clip_mtime >= audio_mtime and clip_mtime >= img_mtime:
                is_uptodate = True

        if is_uptodate:
            logger.info(f"Panel {seq_id} clip already exists at {output_clip_path} and is up-to-date. Skipping rendering.")
        else:
            # 1. Create processed panel image (thread pool)
            await asyncio.to_thread(
                video_engine.create_processed_panel_image,
                raw_img_path=str(img_path),
                output_path=str(processed_image_path),
                video_mode=video_mode
            )
            
            import re
            try:
                seq_idx = int(re.sub(r'\D', '', seq_id))
            except Exception:
                seq_idx = 1
                
            # 2. Render panel clip with FFmpeg (thread pool)
            clip_success = await asyncio.to_thread(
                video_engine.generate_panel_clip,
                img_path=str(processed_image_path),
                audio_path=str(audio_path),
                duration_ms=clip_duration_ms,
                output_path=str(output_clip_path),
                video_mode=video_mode,
                seq=seq_idx,
                panel_text=panel_text,
                burn_subtitles=burn_subtitles
            )
            if not clip_success or not output_clip_path.exists() or output_clip_path.stat().st_size == 0:
                raise RuntimeError(f"Failed to generate panel clip for sequence {seq_id}: {output_clip_path}")


        if progress_callback:
            await progress_callback(
                "video_clip",
                f"Đã kết xuất xong video clip cho khung hình {seq_id}.",
                {
                    "sequence_id": seq_id,
                    "duration_ms": clip_duration_ms
                }
            )
            
        return str(output_clip_path), {
            "duration_ms": clip_duration_ms,
            "text": panel_text
        }

    async def generate_video(
        self,
        chapter_id: str,
        chapter_number: str,
        bgm_path: str = None,
        output_suffix: str = "agent",
        progress_callback = None,
        burn_subtitles: bool = True,
        video_mode: str = None
    ) -> str:
        """
        Assembles individual panel clips and renders the final video for the chapter.
        """
        logger.info(f"Assembling video for chapter {chapter_number}...")
        
        from src.core.utils import get_project_title_by_chapter_id, get_project_data_dir, load_csv_script_for_chapter, get_next_video_path
        from PIL import Image
        project_title = get_project_title_by_chapter_id(chapter_id)
        project_dir = get_project_data_dir(project_title)
        
        chap_dir = project_dir / "chapters" / str(chapter_number)
        manifest_path = chap_dir / "manifest.json"
        recut_manifest = project_dir / "image" / "recut" / f"chap_{chapter_number}" / "manifest.json"

        panels = []
        if manifest_path.exists():
            from src.core.storage import read_json
            panels_data = read_json(manifest_path).get("panels", [])
            for p in panels_data:
                rel = p.get("relative_path", "")
                full_p = str(project_dir / rel) if rel else str(p.get("filename", ""))
                panels.append({
                    "sequence_number": p.get("sequence_number", 1),
                    "raw_image_url": full_p,
                    "processed_image_url": None,
                    "status": p.get("status", "processed")
                })
        elif recut_manifest.exists():
            from src.core.storage import read_json
            panels_data = read_json(recut_manifest).get("panels", [])
            for p in panels_data:
                rel = p.get("relative_path", "")
                full_p = str(project_dir / rel) if rel else str(p.get("filename", ""))
                panels.append({
                    "sequence_number": p.get("sequence_number", 1),
                    "raw_image_url": full_p,
                    "processed_image_url": None,
                    "status": p.get("status", "processed")
                })
        else:
            # Fallback: scan recut directory
            recut_dir = project_dir / "image" / "recut" / f"chap_{chapter_number}"
            if recut_dir.exists():
                img_files = sorted([f for f in recut_dir.iterdir() if f.suffix.lower() in (".jpg", ".png", ".webp")])
                for idx, img_p in enumerate(img_files):
                    panels.append({
                        "sequence_number": idx + 1,
                        "raw_image_url": str(img_p),
                        "processed_image_url": None,
                        "status": "processed"
                    })

        if not panels:
            raise ValueError(f"No panels found for chapter {chapter_number} in {chap_dir} or {recut_manifest.parent}")
            
        resolved_video_mode_str = video_mode or getattr(config.orchestrator, "video_mode", "auto")
        logger.info(f"Configured video mode: {resolved_video_mode_str}")
        
        if resolved_video_mode_str == "auto" and panels:
            first_panel = panels[0]
            first_img_path = first_panel["processed_image_url"] or first_panel["raw_image_url"]
            if first_img_path and os.path.exists(first_img_path):
                try:
                    with Image.open(first_img_path) as tmp_img:
                        w, h = tmp_img.size
                    resolved_video_mode = video_engine.detect_video_mode(w, h)
                    logger.info(f"Auto-detected video mode from first panel ({w}x{h}): {resolved_video_mode.value}")
                except Exception as e:
                    logger.warning(f"Failed to auto-detect video mode: {e}. Falling back to portrait_padded.")
                    resolved_video_mode = VideoMode.PORTRAIT_PADDED
            else:
                resolved_video_mode = VideoMode.PORTRAIT_PADDED
        else:
            try:
                resolved_video_mode = VideoMode(resolved_video_mode_str)
            except Exception:
                resolved_video_mode = VideoMode.PORTRAIT_PADDED
                
        panel_config_map = {}
        parsed_csv = load_csv_script_for_chapter(project_dir, chapter_number)
        if parsed_csv:
            logger.info(f"Loaded script for chapter {chapter_number} from local script.csv")
            for p in parsed_csv:
                seq_id = p.get("sequence_id") or str(p["sequence_number"])
                panel_config_map[seq_id] = p
        else:
            script_json_path = chap_dir / "script.json"
            if script_json_path.exists():
                from src.core.storage import read_json
                s_data = read_json(script_json_path)
                logger.info(f"Loaded script for chapter {chapter_number} from local script.json")
                for p in s_data.get("segments", []):
                    seq_id = p.get("sequence_id") or str(p.get("sequence_number", 0))
                    panel_config_map[seq_id] = {
                        "sequence_number": p.get("sequence_number", 0),
                        "sequence_id": seq_id,
                        "text": p.get("text", "") or p.get("narrator_text", "")
                    }
            else:
                raise ValueError(f"No script found for chapter {chapter_number} (neither script.csv nor script.json exist)")
                    
        audio_dir = project_dir / "audio" / "clips"
        ffmpeg_max_concurrent = getattr(config.orchestrator, "ffmpeg_max_concurrent", 4)
        semaphore = asyncio.Semaphore(ffmpeg_max_concurrent)
        
        async def bounded_process(task_coro):
            async with semaphore:
                return await task_coro
                
        active_groups = []
        current_group = None
        pending_context_seqs = []
        
        for panel in panels:
            seq = panel["sequence_number"]
            img_path = panel["processed_image_url"] or panel["raw_image_url"]
            status = panel.get("status", "processed")
            is_skipped = (img_path and "skip" in str(img_path).lower()) or status in ("skip", "useless")
            
            if is_skipped:
                continue
            elif status == "context_only":
                if current_group is not None:
                    current_group["panel_seqs"].append(seq)
                else:
                    pending_context_seqs.append(seq)
            else:
                current_group = {
                    "active_panel": panel,
                    "panel_seqs": [seq]
                }
                if pending_context_seqs:
                    current_group["panel_seqs"] = pending_context_seqs + current_group["panel_seqs"]
                    pending_context_seqs = []
                active_groups.append(current_group)
                
        if pending_context_seqs and active_groups:
            active_groups[-1]["panel_seqs"].extend(pending_context_seqs)
            pending_context_seqs = []

        import re
        tasks = []
        for group in active_groups:
            active_panel = group["active_panel"]
            active_seq = active_panel["sequence_number"]
            img_path = active_panel["processed_image_url"] or active_panel["raw_image_url"]
            group_seqs = group["panel_seqs"]
            
            sub_keys = []
            for k in panel_config_map.keys():
                match = re.match(r"^(\d+)", k)
                if match:
                    k_seq = int(match.group(1))
                    if k_seq in group_seqs:
                        sub_keys.append(k)
                        
            def get_sub_key_sort(x):
                val = x.replace("_", ".")
                try:
                    return float(val)
                except ValueError:
                    match = re.match(r"^(\d+)", x)
                    if match:
                        return float(match.group(1))
                    return 0.0
            sub_keys.sort(key=get_sub_key_sort)
            
            if not sub_keys:
                continue
                
            for k in sub_keys:
                formatted_seq = format_sequence_id(k)
                audio_path = audio_dir / f"audio_{chapter_number}_{formatted_seq}.wav"
                
                if not os.path.exists(str(audio_path)):
                    logger.warning(f"Audio file for panel {k} does not exist at {audio_path}. Skipping this sub-panel.")
                    continue
                    
                panel_cfg = panel_config_map[k]
                
                tasks.append(
                    bounded_process(
                        self._process_single_panel_clip(
                            seq_id=k,
                            img_path=img_path,
                            audio_path=audio_path,
                            panel_text=panel_cfg["text"],
                            video_mode=resolved_video_mode,
                            project_dir=project_dir,
                            chapter_number=chapter_number,
                            progress_callback=progress_callback,
                            burn_subtitles=burn_subtitles
                        )
                    )
                )
            
        if not tasks:
            raise ValueError("No video clips were rendered.")
            
        logger.info(f"Rendering {len(tasks)} panel clips concurrently...")
        results = await asyncio.gather(*tasks)
        
        render_clips = []
        script_items = []
        for clip_path, script_item in results:
            render_clips.append(clip_path)
            script_items.append(script_item)
            
        video_chapters_dir = project_dir / "video" / "chapters"
        video_chapters_dir.mkdir(parents=True, exist_ok=True)
        raw_video_path = video_chapters_dir / f"chapter_{chapter_number}_raw_temp.mp4"
        
        logger.info(f"Concatenating {len(render_clips)} clips...")
        if progress_callback:
            await progress_callback("video_concat_start", f"Đang thực hiện ghép nối {len(render_clips)} clips...", {})
            
        success_concat = video_engine.concatenate_clips(render_clips, str(raw_video_path))
        if not success_concat:
            raise RuntimeError("Failed to concatenate panel clips.")
            
        srt_path = video_chapters_dir / f"chapter_{chapter_number}_subs_temp.srt"
        video_engine.generate_srt_file(script_items, str(srt_path))
        
        final_video_path = get_next_video_path(project_dir, chapter_number, suffix=output_suffix)
        
        logger.info("Mixing BGM and burning subtitles...")
        if progress_callback:
            await progress_callback("video_mixing_start", "Đang lồng ghép nhạc nền...", {})
            
        success_final = video_engine.mix_bgm_and_burn_subs(
            video_path=str(raw_video_path),
            srt_path=str(srt_path),
            bgm_path=bgm_path,
            output_path=str(final_video_path)
        )
        if not success_final:
            raise RuntimeError("Failed to generate final video.")
            
        try:
            chapter_audio_dir = project_dir / "audio" / "chapters"
            chapter_audio_dir.mkdir(parents=True, exist_ok=True)
            chapter_audio_path = chapter_audio_dir / f"chapter_{chapter_number}.wav"
            cmd = [
                "ffmpeg", "-y",
                "-i", str(final_video_path),
                "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
                str(chapter_audio_path)
            ]
            logger.info(f"Extracting chapter audio to {chapter_audio_path}...")
            run_ffmpeg(cmd, description="Extract chapter audio")
            if not chapter_audio_path.exists() or chapter_audio_path.stat().st_size == 0:
                raise RuntimeError(f"Extracted chapter audio file is empty or missing: {chapter_audio_path}")
        except Exception as ae:
            logger.error(f"Failed to extract chapter audio: {ae}")
            raise
            
        try:
            if os.path.exists(str(raw_video_path)):
                os.remove(str(raw_video_path))
            if os.path.exists(str(srt_path)):
                os.remove(str(srt_path))
        except Exception as e:
            logger.warning(f"Failed to clean up temp files: {e}")
            
        try:
            chapter_json_path = chap_dir / "chapter.json"
            if chapter_json_path.exists():
                from src.core.storage import update_checkpoint
                update_checkpoint(chap_dir, stage="render", status="completed", artifacts=[str(Path(final_video_path).relative_to(project_dir))])
                update_checkpoint(chap_dir, stage="finalize", status="completed")
        except Exception as ce:
            logger.warning(f"Could not update chapter render checkpoint: {ce}")

        return str(final_video_path)

pipeline = MangaRecapPipeline()
