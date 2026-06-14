import os
import asyncio
from pathlib import Path
from src.core.config import config
from src.core.logger import logger
from src.core.supabase_client import supabase
from src.core.utils import format_sequence_id

from src.services.crawler_service import crawler_service
from src.services.vlm_gateway import vlm_gateway
from src.services.llm_orchestrator import vlm_orchestrator
from src.services.tts_service import tts_service
from src.render.video_engine import video_engine

class MangaRecapPipeline:
    def __init__(self):
        pass

    async def run(
        self,
        chapter_number: str,
        general_story_context: str,
        project_title: str = "Mỗi lính đánh thuê trở về đều có một kế hoạch",
        initial_scripts: dict[int, str] = None,
        crawler_urls: dict[str, str] = None,
        config_overrides: dict = None,
        bgm_path: str = None,
        output_suffix: str = "manual",
        progress_callback = None,
        burn_subtitles: bool = True
    ) -> str:
        """
        Runs the end-to-end Manga Recap Pipeline with custom parameters.
        """
        logger.info(f"=== Starting Manga Recap Pipeline for Chapter {chapter_number} ===")
        
        # Parse overrides
        overrides = config_overrides or {}
        vlm_model = overrides.get("vlm_model") or config.vlm_model
        llm_model = overrides.get("llm_model") or config.llm_model
        batch_size = overrides.get("batch_size") or config.orchestrator.batch_size
        sliding_context_window = overrides.get("sliding_context_window") or config.orchestrator.sliding_context_window
        vlm_image_resize_shortest_edge = overrides.get("vlm_image_resize_shortest_edge") or getattr(config.orchestrator, "vlm_image_resize_shortest_edge", 500)
        vlm_batch_size = overrides.get("vlm_batch_size") or getattr(config.orchestrator, "vlm_batch_size", 4)
        vlm_context_length = overrides.get("vlm_context_length") or getattr(config.orchestrator, "vlm_context_length", 2)
        max_panels = overrides.get("max_panels")
        min_length = overrides.get("min_length") or config.script_limits.min_length
        max_length = overrides.get("max_length") or config.script_limits.max_length
        tts_speed = overrides.get("tts_speed") or config.tts.speed
        tts_voice = overrides.get("tts_voice") or config.tts.voice
        pan_mode = overrides.get("render_pan_mode") or getattr(config.orchestrator, "render_pan_mode", "dynamic")

        # 1. Use manual story title and get or create project
        if progress_callback:
            await progress_callback("project_setup", f"Khởi tạo dự án '{project_title}'...", {"project_title": project_title})
        project_id = await crawler_service.get_or_create_project(project_title)
        
        # 2. Crawl chapter (Images & Summary)
        logger.info("--- Phase 2: Crawling Chapter ---")
        if progress_callback:
            await progress_callback("crawl_start", f"Bắt đầu crawl ảnh chapter {chapter_number}...", {})
        
        chapter_base_url = crawler_urls.get("chapter_base_url") if crawler_urls else None
        chapter_id, summary_context = await crawler_service.crawl_chapter(
            project_id,
            chapter_number,
            general_story_context=general_story_context,
            chapter_base_url=chapter_base_url,
            max_panels=max_panels
        )
        logger.info(f"Chapter ID: {chapter_id}")
        if progress_callback:
            await progress_callback("crawl_complete", f"Crawl hoàn tất chapter {chapter_number}.", {"chapter_id": chapter_id})
        
        # 3. VLM Analysis
        logger.info("--- Phase 3: VLM Panel Analysis ---")
        if progress_callback:
            await progress_callback("vlm_start", "Bắt đầu phân tích hình ảnh VLM...", {})
            
        max_vlm_retries = 3
        total_panels = 0
        processed_panels_db = 0
        
        for attempt in range(1, max_vlm_retries + 1):
            logger.info(f"Running VLM analysis: Attempt {attempt}/{max_vlm_retries}...")
            await vlm_gateway.process_chapter_panels(
                chapter_id=chapter_id,
                chapter_num=chapter_number,
                vlm_model=vlm_model,
                vlm_image_resize_shortest_edge=vlm_image_resize_shortest_edge,
                vlm_batch_size=vlm_batch_size,
                vlm_context_length=vlm_context_length,
                progress_callback=progress_callback
            )
            
            # Check DB status
            panels_res = supabase.table("panels").select("id, status").eq("chapter_id", chapter_id).execute()
            total_panels = len(panels_res.data)
            processed_panels_db = sum(1 for p in panels_res.data if p["status"] == "processed")
            logger.info(f"VLM attempt {attempt} status: {processed_panels_db}/{total_panels} processed.")
            
            if processed_panels_db == total_panels:
                break
                
            if attempt < max_vlm_retries:
                logger.warning(f"Only {processed_panels_db}/{total_panels} panels processed. Retrying in 5 seconds...")
                await asyncio.sleep(5.0)
                
        if progress_callback:
            await progress_callback(
                "vlm_complete",
                f"Đã xử lý xong {processed_panels_db}/{total_panels} khung hình bằng VLM.",
                {"processed": processed_panels_db, "total": total_panels}
            )
            
        if processed_panels_db < total_panels:
            raise RuntimeError(f"VLM analysis failed: only {processed_panels_db}/{total_panels} panels processed successfully after {max_vlm_retries} attempts.")
            
        # 4. LLM Script Orchestration
        logger.info("--- Phase 4: LLM Script Orchestration ---")
        if progress_callback:
            await progress_callback("llm_start", "Bắt đầu sinh kịch bản (LLM)...", {})
            
        script_success = await vlm_orchestrator.orchestrate_chapter_scripts(
            chapter_id=chapter_id,
            chapter_num=chapter_number,
            summary_context=summary_context,
            initial_scripts=initial_scripts,
            llm_model=llm_model,
            batch_size=batch_size,
            sliding_context_window=sliding_context_window,
            min_length=min_length,
            max_length=max_length,
            progress_callback=progress_callback
        )
        if not script_success:
            raise RuntimeError("Script synthesis failed.")
            
        if progress_callback:
            await progress_callback("llm_complete", "Sinh kịch bản hoàn tất.", {})
            
        # 5. TTS Audio Generation
        logger.info("--- Phase 5 (Part 1): TTS Audio Generation ---")
        if progress_callback:
            await progress_callback("tts_start", "Bắt đầu tạo giọng đọc (TTS)...", {})
            
        tts_success = await tts_service.process_chapter_audio(
            chapter_id,
            chapter_number,
            speed=tts_speed,
            voice_name=tts_voice,
            progress_callback=progress_callback
        )
        if not tts_success:
            raise RuntimeError("TTS Audio Generation failed.")
            
        if progress_callback:
            await progress_callback("tts_complete", "Tạo giọng đọc hoàn tất.", {})
            
        # 6. Video Generation
        logger.info("--- Phase 5 (Part 2): Video Generation ---")
        if progress_callback:
            await progress_callback("video_start", "Bắt đầu xử lý và kết xuất video thành phẩm...", {})
            
        video_path = await self.generate_video(
            chapter_id,
            chapter_number,
            bgm_path,
            output_suffix,
            progress_callback,
            burn_subtitles=burn_subtitles,
            pan_mode=pan_mode
        )
        
        # 7. Storage Cleanup
        self.cleanup_temp_files(chapter_number, output_suffix, project_title)
        
        logger.info(f"=== Manga Recap Pipeline Completed successfully! Video saved at: {video_path} ===")
        return video_path

    def cleanup_temp_files(self, chapter_number: str, output_suffix: str, project_title: str):
        """
        Deletes temporary intermediate files (images, audio files, clips, raw video)
        to prevent filling up disk space.
        """
        logger.info("Storage cleanup disabled to keep all data files as requested.")
        return

    async def _process_single_panel_clip(
        self,
        seq_id: str,
        img_path: str,
        audio_path: Path,
        panel_text: str,
        focus_x: int,
        focus_y: int,
        pan_direction: str,
        crop_scale: int,
        project_dir: Path,
        chapter_number: str,
        progress_callback,
        burn_subtitles: bool = True
    ) -> tuple[str, dict]:
        """
        Helper method to process a single panel clip in parallel:
        1. Create background image (cropped + blurred) using Pillow (in thread pool).
        2. Create transparent subtitle overlay PNG if text exists (in thread pool).
        3. Render panel clip with FFmpeg overlaying the static subtitle PNG (in thread pool).
        Enforces a minimum duration of 2.5s (2500ms).
        """
        duration_ms = tts_service.get_audio_duration_ms(str(audio_path))
        clip_duration_ms = max(duration_ms, 2500)  # Enforce min 2.5s duration
        
        formatted_seq = format_sequence_id(seq_id)
        cropped_dir = project_dir / "cropped_images"
        cropped_dir.mkdir(parents=True, exist_ok=True)
        
        if "_" in seq_id:
            parts = seq_id.split("_")
            raw_seq = parts[0]
            crop_idx = parts[1]
        else:
            raw_seq = seq_id
            crop_idx = "1"
            
        processed_image_path = cropped_dir / f"image_{chapter_number}_{raw_seq}_{crop_idx}.webp"
        sub_overlay_path = cropped_dir / f"image_{chapter_number}_{raw_seq}_{crop_idx}_sub_overlay.png"
        output_clip_path = cropped_dir / f"clip_{chapter_number}_{formatted_seq}.mp4"
        
        if output_clip_path.exists():
            logger.info(f"Panel {seq_id} clip already exists at {output_clip_path}. Skipping rendering.")
        else:
            # 1. Create background image without subtitles (run in thread)
            await asyncio.to_thread(
                video_engine.create_processed_panel_image,
                raw_img_path=str(img_path),
                output_path=str(processed_image_path),
                focus_x=focus_x,
                focus_y=focus_y,
                crop_scale=crop_scale
            )
            
            # 2. Create transparent subtitle overlay PNG if text exists and burn_subtitles is enabled
            sub_overlay_str = None
            if burn_subtitles and panel_text:
                await asyncio.to_thread(
                    video_engine.create_subtitle_overlay_png,
                    text=panel_text,
                    output_path=str(sub_overlay_path)
                )
                sub_overlay_str = str(sub_overlay_path)
            
            # Extract an integer sequence index for alternating pan direction
            import re
            try:
                seq_idx = int(re.sub(r'\D', '', seq_id))
            except Exception:
                seq_idx = 1
                
            # 3. Render panel clip with FFmpeg (run in thread to parallelize FFmpeg subprocesses)
            await asyncio.to_thread(
                video_engine.generate_panel_clip,
                img_path=str(processed_image_path),
                audio_path=str(audio_path),
                duration_ms=clip_duration_ms,
                output_path=str(output_clip_path),
                pan_direction=pan_direction,
                seq=seq_idx,
                sub_overlay_path=sub_overlay_str
            )
        
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
        output_suffix: str = "manual",
        progress_callback = None,
        burn_subtitles: bool = True,
        pan_mode: str = None
    ) -> str:
        """
        Assembles individual panel clips and renders the final video for the chapter.
        """
        logger.info(f"Assembling video for chapter {chapter_number}...")
        
        # Resolve pan_mode: argument override -> config default -> "dynamic"
        resolved_pan_mode = pan_mode or getattr(config.orchestrator, "render_pan_mode", "dynamic")
        logger.info(f"Using pan mode: {resolved_pan_mode}")
        
        from src.core.utils import get_project_title_by_chapter_id, get_project_data_dir, load_csv_script_for_chapter, get_next_video_path
        project_title = get_project_title_by_chapter_id(chapter_id)
        project_dir = get_project_data_dir(project_title)
        
        # Query all panels
        panels_res = supabase.table("panels")\
            .select("*")\
            .eq("chapter_id", chapter_id)\
            .order("sequence_number")\
            .execute()
        panels = panels_res.data
        if not panels:
            raise ValueError(f"No panels found in database for chapter {chapter_id}")
            
        # Compile mapping from sequence_id -> script config
        panel_config_map = {}
        
        # Try loading from central script.csv first
        parsed_csv = load_csv_script_for_chapter(project_dir, chapter_number)
        if parsed_csv:
            logger.info(f"Loaded script for chapter {chapter_number} from local script.csv")
            for p in parsed_csv:
                seq_id = p.get("sequence_id") or str(p["sequence_number"])
                panel_config_map[seq_id] = p
        else:
            logger.info("Local script.csv not found. Falling back to Supabase script_segments.")
            # Query all script segments to parse script texts
            segments_res = supabase.table("script_segments")\
                .select("*")\
                .eq("chapter_id", chapter_id)\
                .order("start_panel_seq")\
                .execute()
            segments = segments_res.data
            if not segments:
                raise ValueError(f"No script segments found for chapter {chapter_id}")
                
            for seg in segments:
                parsed = tts_service.parse_script_text(seg["script_text"])
                for p in parsed:
                    seq_id = p.get("sequence_id") or str(p["sequence_number"])
                    panel_config_map[seq_id] = {
                        "sequence_number": p["sequence_number"],
                        "sequence_id": seq_id,
                        "text": p["text"],
                        "focus_x": p.get("focus_x", 50),
                        "focus_y": p.get("focus_y", 50),
                        "pan_direction": p.get("pan_direction", "horizontal"),
                        "crop_scale": p.get("crop_scale", 100)
                    }
                    
        audio_dir = project_dir / "audio"
        
        # Prepare rendering tasks
        tasks = []
        for panel in panels:
            seq = panel["sequence_number"]
            img_path = panel["processed_image_url"] or panel["raw_image_url"]
            
            # Find all sequence_ids associated with this panel seq (e.g. "5", "5_1", "5_2")
            sub_keys = []
            for k in panel_config_map.keys():
                if k == str(seq) or k.startswith(f"{seq}_"):
                    sub_keys.append(k)
                    
            # Sort sub_keys so they render in correct order (e.g. 5_1, 5_2)
            def get_sub_key_sort(x):
                val = x.replace("_", ".")
                try:
                    return float(val)
                except ValueError:
                    return float(seq)
            sub_keys.sort(key=get_sub_key_sort)
            
            if not sub_keys:
                logger.info(f"Panel {seq} has no script entry (skipped). Skipping rendering completely.")
                continue
                
            for k in sub_keys:
                formatted_seq = format_sequence_id(k)
                audio_path = audio_dir / f"audio_{chapter_number}_{formatted_seq}.wav"
                
                if not os.path.exists(str(audio_path)):
                    logger.warning(f"Audio file for panel {k} does not exist at {audio_path}. Skipping this sub-panel.")
                    continue
                    
                panel_cfg = panel_config_map[k]
                
                # Apply unified pan mode override if configured (and not "dynamic")
                pan_dir = panel_cfg["pan_direction"]
                if resolved_pan_mode and resolved_pan_mode != "dynamic":
                    pan_dir = resolved_pan_mode
                    
                tasks.append(
                    self._process_single_panel_clip(
                        seq_id=k,
                        img_path=img_path,
                        audio_path=audio_path,
                        panel_text=panel_cfg["text"],
                        focus_x=panel_cfg["focus_x"],
                        focus_y=panel_cfg["focus_y"],
                        pan_direction=pan_dir,
                        crop_scale=panel_cfg["crop_scale"],
                        project_dir=project_dir,
                        chapter_number=chapter_number,
                        progress_callback=progress_callback,
                        burn_subtitles=burn_subtitles
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
            
        # Concatenate all clips
        video_dir = project_dir / "video"
        video_dir.mkdir(parents=True, exist_ok=True)
        raw_video_path = video_dir / f"chapter_{chapter_number}_raw_temp.mp4"
        
        logger.info(f"Concatenating {len(render_clips)} clips...")
        if progress_callback:
            await progress_callback("video_concat_start", f"Đang thực hiện ghép nối {len(render_clips)} clips...", {})
            
        success_concat = video_engine.concatenate_clips(render_clips, str(raw_video_path))
        if not success_concat:
            raise RuntimeError("Failed to concatenate panel clips.")
            
        # Generate SRT subtitles
        srt_path = video_dir / f"chapter_{chapter_number}_subs_temp.srt"
        video_engine.generate_srt_file(script_items, str(srt_path))
        
        # Mix BGM and output final video
        final_video_path = get_next_video_path(project_dir, chapter_number)
        
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
            
        # Clean up temporary video and subtitle files
        try:
            if os.path.exists(str(raw_video_path)):
                os.remove(str(raw_video_path))
            if os.path.exists(str(srt_path)):
                os.remove(str(srt_path))
        except Exception as e:
            logger.warning(f"Failed to clean up temp files: {e}")
            
        return str(final_video_path)

pipeline = MangaRecapPipeline()
