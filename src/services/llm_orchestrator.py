import re
import os
import json
import asyncio
from src.core.config import config
from src.core.logger import logger
from src.core.supabase_client import supabase
from src.core.openrouter_client import openrouter_client
from src.core.prompts import LLM_SYSTEM_PROMPT, LLM_USER_PROMPT_TEMPLATE
from src.services.character_dictionary import character_dictionary_service

class LLMOrchestrator:
    def __init__(self):
        self.batch_size = config.orchestrator.batch_size
        self.sliding_context_window = config.orchestrator.sliding_context_window
        self.min_length = config.script_limits.min_length
        self.max_length = config.script_limits.max_length

    async def get_previous_scripts_context(self, chapter_id: str, current_start_seq: int, sliding_window: int = None) -> str:
        """
        Retrieves generated script segments for the previous panels
        (based on sliding_context_window) to maintain continuity.
        """
        if current_start_seq <= 1:
            return "Bắt đầu câu chuyện."
            
        window = sliding_window if sliding_window is not None else self.sliding_context_window
        start_search = max(1, current_start_seq - window)
        end_search = current_start_seq - 1
        
        logger.info(f"Retrieving previous scripts for context: panels {start_search} to {end_search}...")
        
        try:
            # Query script_segments that overlap with our search range
            res = supabase.table("script_segments")\
                .select("start_panel_seq, end_panel_seq, script_text")\
                .eq("chapter_id", chapter_id)\
                .order("start_panel_seq")\
                .execute()
                
            segments = res.data
            if not segments:
                return "Không có kịch bản trước đó."
                
            # Parse individual panels from the script text
            panel_scripts = {}
            for seg in segments:
                text = seg["script_text"]
                matches = re.finditer(r"\[Khung\s*hình\s*(\d+)[^\]]*\]:?\s*(.*?)(?=\[Khung\s*hình\s*\d+[^\]]*\]|$)", text, re.DOTALL | re.IGNORECASE)
                for match in matches:
                    seq_num = int(match.group(1))
                    panel_scripts[seq_num] = match.group(2).strip()
            
            # Extract only the ones in our window
            context_parts = []
            for seq in range(start_search, end_search + 1):
                if seq in panel_scripts:
                    context_parts.append(f"[Khung hình {seq}]: {panel_scripts[seq]}")
                    
            if not context_parts:
                return "Không có kịch bản trước đó."
                
            return "\n".join(context_parts)
        except Exception as e:
            logger.error(f"Error getting previous scripts context: {e}")
            return "Không thể lấy ngữ cảnh trước đó."

    async def synthesize_batch(
        self,
        chapter_id: str,
        chapter_num: str,
        project_id: str,
        summary_context: str,
        panels_batch: list,
        llm_model: str = None,
        min_length: int = None,
        max_length: int = None,
        sliding_context_window: int = None
    ) -> tuple[bool, str]:
        """
        Synthesizes a script for a batch of panels.
        1. Query previous scripts context.
        2. Format VLM raw descriptions of panels in this batch.
        3. Send prompt to OpenRouter LLM.
        4. Validate that all expected panels are present in the response.
        5. Save generated script to Supabase.
        """
        start_seq = panels_batch[0]["sequence_number"]
        end_seq = panels_batch[-1]["sequence_number"]
        
        logger.info(f"Synthesizing script for Chapter {chapter_num}, panels {start_seq} to {end_seq}...")
        # 1. Get previous script context
        prev_script_context = await self.get_previous_scripts_context(chapter_id, start_seq, sliding_context_window)
        
        # Helper function to parse JSON description or fallback
        def parse_panel_desc(ocr_text: str, vlm_desc: str, sequence_number: int) -> dict:
            if vlm_desc and vlm_desc.strip().startswith("{") and vlm_desc.strip().endswith("}"):
                try:
                    return json.loads(vlm_desc)
                except Exception:
                    pass
            return {
                "status": "processed",
                "ocr": ocr_text,
                "sub_panels": [
                    {
                        "sequence_id": str(sequence_number),
                        "status": "processed",
                        "ocr": ocr_text,
                        "character_appearance": vlm_desc or "Không có mô tả chi tiết.",
                        "character_action": "",
                        "background": "",
                        "characters": []
                    }
                ]
            }

        # Load and cache database character embeddings for the project
        import os
        from src.services.character_rag import character_rag_service
        character_rag_service.load_embeddings_cache(project_id)
        
        from src.core.utils import get_project_title_by_id, get_project_data_dir, get_next_character_image_path
        project_title = get_project_title_by_id(project_id)
        project_dir = get_project_data_dir(project_title)
        crops_dir = project_dir / "character_images"
        os.makedirs(crops_dir, exist_ok=True)
        
        # 2. Format VLM inputs and execute Character RAG
        vlm_inputs = []
        skipped_panels = set()
        
        for panel in panels_batch:
            seq = panel["sequence_number"]
            raw_desc = panel.get("vlm_description") or ""
            ocr_text = panel.get("ocr_text") or ""
            panel_id = panel["id"]
            
            panel_data = parse_panel_desc(ocr_text, raw_desc, seq)
            if panel_data.get("status") == "skip":
                logger.info(f"Panel {seq} is marked as skipped. Excluding from LLM context.")
                skipped_panels.add(seq)
                continue
                
            for sub in panel_data.get("sub_panels", []):
                sub_seq_id = sub["sequence_id"]
                sub_status = sub.get("status", "processed")
                if sub_status == "skip":
                    continue
                    
                appearance = sub.get("character_appearance") or ""
                action = sub.get("character_action") or ""
                bg = sub.get("background") or ""
                sub_ocr = sub.get("ocr") or ""
                
                # Run visual character RAG
                detected_chars = sub.get("characters") or []
                source_image = panel.get("processed_image_url") or panel.get("raw_image_url")
                
                if detected_chars and source_image and os.path.exists(source_image):
                    for idx, char_info in enumerate(detected_chars):
                        vlm_name = char_info.get("name")
                        box = char_info.get("box_2d")
                        if not vlm_name or not box or len(box) != 4:
                            continue
                            
                        crop_path = get_next_character_image_path(project_dir)
                        
                        # Crop image as square
                        cropped_file = character_rag_service.crop_character(source_image, box, str(crop_path))
                        if cropped_file:
                            # Generate embedding (1024 normalized)
                            emb = await character_rag_service.get_image_embedding(cropped_file)
                            if emb:
                                # Cosine similarity check against cache
                                matched_name = character_rag_service.match_character(project_id, emb, threshold=0.78)
                                if matched_name:
                                    logger.info(f"RAG Match: VLM name '{vlm_name}' matched to known character '{matched_name}' in panel {sub_seq_id}!")
                                    # Perform string replacement in description
                                    appearance = re.sub(r'\b' + re.escape(vlm_name) + r'\b', matched_name, appearance, flags=re.IGNORECASE)
                                    action = re.sub(r'\b' + re.escape(vlm_name) + r'\b', matched_name, action, flags=re.IGNORECASE)
                                    
                                    # Save embedding under the matched name
                                    character_rag_service.store_character_visual(
                                        project_id=project_id,
                                        chapter_id=chapter_id,
                                        panel_id=panel_id,
                                        character_name=matched_name,
                                        crop_path=str(crop_path),
                                        embedding=emb,
                                        description=f"Matched visual of '{vlm_name}': {appearance}"
                                    )
                                else:
                                    logger.info(f"RAG: No match for visual character '{vlm_name}'. Saving as new.")
                                    # Save embedding under the VLM name (or unknown_N)
                                    character_rag_service.store_character_visual(
                                        project_id=project_id,
                                        chapter_id=chapter_id,
                                        panel_id=panel_id,
                                        character_name=vlm_name,
                                        crop_path=str(crop_path),
                                        embedding=emb,
                                        description=appearance
                                    )
                                    
                vlm_inputs.append(
                    f"--- PHÂN CẢNH {sub_seq_id} ---\n"
                    f"1. OCR (Văn bản trích xuất): '{sub_ocr}'\n"
                    f"2. Chi tiết thị giác (VLM):\n"
                    f"   - Ngoại hình: {appearance}\n"
                    f"   - Hành động: {action}\n"
                    f"   - Bối cảnh: {bg}\n"
                )
                
        vlm_data_str = "\n".join(vlm_inputs)
        
        min_len = min_length if min_length is not None else self.min_length
        max_len = max_length if max_length is not None else self.max_length
        
        # Load dynamic character dictionary context
        char_dict_context = character_dictionary_service.get_dictionary_context()
        
        # 3. Create prompt using template from prompts.py
        prompt = LLM_USER_PROMPT_TEMPLATE.format(
            summary_context=summary_context,
            character_dictionary=char_dict_context,
            prev_script_context=prev_script_context,
            vlm_data_str=vlm_data_str,
            start_seq=start_seq,
            end_seq=end_seq,
            min_length=min_len,
            max_length=max_len
        )
        
        messages = [
            {
                "role": "system",
                "content": LLM_SYSTEM_PROMPT
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
        
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Sending batch {start_seq}-{end_seq} to OpenRouter LLM (Attempt {attempt}/{max_attempts})...")
                
                # Save LLM prompt for debugging
                project_title = get_project_title_by_id(project_id)
                project_dir = get_project_data_dir(project_title)
                history_dir = project_dir / "logs" / "llm_history"
                os.makedirs(history_dir, exist_ok=True)
                prompt_file = os.path.join(history_dir, f"chap_{chapter_num}_batch_{start_seq}_{end_seq}_attempt_{attempt}_prompt.txt")
                with open(prompt_file, "w", encoding="utf-8") as f:
                    for msg in messages:
                        f.write(f"=== ROLE: {msg['role']} ===\n{msg['content']}\n\n")
                        
                script_text = await openrouter_client.request_completion(
                    model=llm_model or config.llm_model,
                    messages=messages,
                    temperature=0.7 + (attempt - 1) * 0.1
                )
                
                # Save LLM response for debugging
                response_file = os.path.join(history_dir, f"chap_{chapter_num}_batch_{start_seq}_{end_seq}_attempt_{attempt}_response.txt")
                with open(response_file, "w", encoding="utf-8") as f:
                    f.write(script_text)
                
                # Try parsing response as JSON first
                clean_text = script_text.strip()
                if clean_text.startswith("```"):
                    clean_text = re.sub(r"^```(?:json)?\n", "", clean_text)
                    clean_text = re.sub(r"\n```$", "", clean_text).strip()
                
                parsed_data = {}
                try:
                    parsed_data = json.loads(clean_text)
                except Exception as json_err:
                    logger.warning(f"Initial JSON parse failed: {json_err}. Trying regex fallback...")
                    # Try regex to locate JSON object
                    json_match = re.search(r"(\{.*\})", script_text, re.DOTALL)
                    if json_match:
                        try:
                            parsed_data = json.loads(json_match.group(1))
                        except Exception as regex_err:
                            logger.error(f"Regex fallback JSON parse failed: {regex_err}")
                
                parsed_seqs = set()
                scripts_dict = {}
                character_updates = {}
                
                if parsed_data and isinstance(parsed_data, dict):
                    if "scripts" in parsed_data:
                        scripts_dict = parsed_data["scripts"]
                        character_updates = parsed_data.get("character_updates", {})
                    else:
                        # Fallback: check if keys look like sequence numbers/ids
                        has_seq_keys = any(str(k).replace("_", "").isdigit() for k in parsed_data.keys())
                        if has_seq_keys:
                            scripts_dict = parsed_data
                        else:
                            scripts_dict = {}
                
                if scripts_dict and isinstance(scripts_dict, dict):
                    formatted_parts = []
                    for k, v in scripts_dict.items():
                        seq_str = str(k).strip()
                        # Extract base integer prefix for matching expected seqs
                        match_prefix = re.match(r"^(\d+)", seq_str)
                        if match_prefix:
                            base_seq = int(match_prefix.group(1))
                            parsed_seqs.add(base_seq)
                            formatted_parts.append(f"[Khung hình {seq_str}] {v.strip()}")
                            
                    # Sort and rebuild script_text (convert e.g. "5_1" -> 5.1 for sorting)
                    def get_seq_key(x):
                        m = re.search(r"\[Khung\s*hình\s*([^\]]+)\]", x)
                        if m:
                            val = m.group(1).replace("_", ".")
                            try:
                                return float(val)
                            except ValueError:
                                return 999.0
                        return 999.0
                        
                    formatted_parts.sort(key=get_seq_key)
                    script_text = "\n\n".join(formatted_parts)
                    
                    # Update character dictionary in RAM & Disk
                    if character_updates and isinstance(character_updates, dict):
                        try:
                            character_dictionary_service.update_dictionary(project_id, character_updates)
                        except Exception as dict_err:
                            logger.error(f"Error updating character dictionary: {dict_err}")
                else:
                    # Fallback to regex-based parsing on the raw text
                    matches = re.finditer(r"\[Khung\s*hình\s*([^\]]+)\]:?\s*(.*?)(?=\[Khung\s*hình\s*[^\]]+\]|$)", script_text, re.DOTALL | re.IGNORECASE)
                    for match in matches:
                        seq_str = match.group(1).strip()
                        match_prefix = re.match(r"^(\d+)", seq_str)
                        if match_prefix:
                            parsed_seqs.add(int(match_prefix.group(1)))
                    
                expected_seqs = {panel["sequence_number"] for panel in panels_batch} - skipped_panels
                missing_seqs = expected_seqs - parsed_seqs
                
                if missing_seqs:
                    logger.error(f"Generated script is missing expected panels: {sorted(missing_seqs)} on attempt {attempt}.")
                    logger.warning(f"Raw response: {script_text}")
                    if attempt < max_attempts:
                        # Append message history to guide it to correct the output
                        messages.append({"role": "assistant", "content": script_text})
                        messages.append({"role": "user", "content": f"Kịch bản bạn vừa viết bị thiếu các Khung hình: {sorted(missing_seqs)}. Hãy viết lại đầy đủ và trả về ở định dạng JSON duy nhất."})
                        continue
                    else:
                        raise ValueError(f"Failed to generate all expected panels after {max_attempts} attempts. Missing: {sorted(missing_seqs)}")
                
                logger.info(f"Script batch synthesized successfully (Length: {len(script_text)})")
                
                # 4. Save to Supabase
                supabase.table("script_segments").insert({
                    "chapter_id": chapter_id,
                    "start_panel_seq": start_seq,
                    "end_panel_seq": end_seq,
                    "script_text": script_text,
                    "status": "completed"
                }).execute()
                
                logger.info(f"Script segment {start_seq}-{end_seq} saved in database.")
                return True, script_text
            except Exception as e:
                logger.error(f"Error in synthesizing script batch {start_seq}-{end_seq} on attempt {attempt}: {e}")
                if attempt == max_attempts:
                    return False, ""
                await asyncio.sleep(2.0)
                
        return False, ""

    async def orchestrate_chapter_scripts(
        self,
        chapter_id: str,
        chapter_num: str,
        summary_context: str = None,
        initial_scripts: dict = None,
        llm_model: str = None,
        batch_size: int = None,
        sliding_context_window: int = None,
        min_length: int = None,
        max_length: int = None,
        progress_callback = None
    ) -> bool:
        """
        Orchestrates script synthesis for all panels in a chapter.
        """
        logger.info(f"Starting script synthesis for Chapter {chapter_num}...")
        
        try:
            # 1. Get Chapter Details (Fallback to DB if summary_context is not supplied)
            chap_res = supabase.table("chapters").select("project_id, summary_context").eq("id", chapter_id).execute()
            if not chap_res.data:
                logger.error("Chapter not found in Supabase")
                return False
            project_id = chap_res.data[0]["project_id"]
            if not summary_context:
                summary_context = chap_res.data[0].get("summary_context") or "Không có bối cảnh cốt truyện."
            
            # Load project-specific character dictionary
            character_dictionary_service.load_dictionary(project_id)
            
            # Pre-seed character dictionary from story context if it is empty
            if not character_dictionary_service.dict_cache:
                logger.info("Character dictionary is empty. Pre-seeding from story context...")
                try:
                    seed_prompt = (
                        "Hãy đọc tóm tắt cốt truyện sau và trích xuất thông tin đặc điểm ngoại hình/trang phục cốt lõi "
                        "của nhân vật chính và các nhân vật được nhắc đến (đặc biệt là nhân vật chính như Ghislain Perdium).\n"
                        "Bắt buộc trả về kết quả ở định dạng JSON duy nhất như sau:\n"
                        "{\n"
                        "  \"Tên nhân vật\": {\n"
                        "    \"core_features\": \"Mô tả đặc điểm cốt lõi (ví dụ: tóc vàng nhạt, mắt xanh, thời niên thiếu)\",\n"
                        "    \"current_outfit\": \"Mô tả trang phục (ví dụ: sơ mi trắng, cà vạt đen) hoặc để trống nếu chưa rõ\"\n"
                        "  }\n"
                        "}\n\n"
                        f"CỐT TRUYỆN GỐC:\n{summary_context}"
                    )
                    seed_messages = [
                        {"role": "system", "content": "Bạn là trợ lý phân tích cốt truyện Manga. Luôn trả về kết quả ở định dạng JSON duy nhất."},
                        {"role": "user", "content": seed_prompt}
                    ]
                    
                    # Log prompt
                    from src.core.utils import get_project_title_by_id, get_project_data_dir
                    project_title = get_project_title_by_id(project_id)
                    project_dir = get_project_data_dir(project_title)
                    history_dir = project_dir / "logs" / "llm_history"
                    os.makedirs(history_dir, exist_ok=True)
                    with open(os.path.join(history_dir, f"project_{project_id}_seed_prompt.txt"), "w", encoding="utf-8") as f:
                        f.write(seed_prompt)
                        
                    seed_res = await openrouter_client.request_completion(
                        model=llm_model or config.llm_model,
                        messages=seed_messages,
                        temperature=0.2
                    )
                    
                    # Log response
                    with open(os.path.join(history_dir, f"project_{project_id}_seed_response.txt"), "w", encoding="utf-8") as f:
                        f.write(seed_res)
                        
                    clean_res = seed_res.strip()
                    if clean_res.startswith("```"):
                        clean_res = re.sub(r"^```(?:json)?\n", "", clean_res)
                        clean_res = re.sub(r"\n```$", "", clean_res).strip()
                        
                    parsed_seed = {}
                    try:
                        parsed_seed = json.loads(clean_res)
                    except Exception:
                        json_match = re.search(r"(\{.*\})", seed_res, re.DOTALL)
                        if json_match:
                            try:
                                parsed_seed = json.loads(json_match.group(1))
                            except Exception:
                                pass
                                
                    if parsed_seed and isinstance(parsed_seed, dict):
                        character_dictionary_service.update_dictionary(project_id, parsed_seed)
                        logger.info(f"Successfully pre-seeded character dictionary: {parsed_seed}")
                except Exception as seed_err:
                    logger.error(f"Error pre-seeding character dictionary: {seed_err}")
            
            # Handle manual initial scripts if configured
            max_initial_seq = 0
            
            # 2. Get all PROCESSED panels
            panels_res = supabase.table("panels")\
                .select("*")\
                .eq("chapter_id", chapter_id)\
                .eq("status", "processed")\
                .order("sequence_number")\
                .execute()
                
            panels = panels_res.data
            if not panels:
                logger.warning("No processed panels found. Cannot generate scripts.")
                return False
                
            # Filter panels to only process those beyond max_initial_seq
            if max_initial_seq > 0:
                panels = [p for p in panels if p["sequence_number"] > max_initial_seq]
                logger.info(f"Filtered out first {max_initial_seq} panels from LLM processing. {len(panels)} panels left.")
                
            logger.info(f"Found {len(panels)} processed panels for script generation.")
            
            # Check what script segments already exist to support resume
            exist_res = supabase.table("script_segments")\
                .select("start_panel_seq, end_panel_seq")\
                .eq("chapter_id", chapter_id)\
                .execute()
            existing_ranges = {(row["start_panel_seq"], row["end_panel_seq"]) for row in exist_res.data}
            
            # 3. Batch processing
            i = 0
            batch_sz = batch_size if batch_size is not None else self.batch_size
            while i < len(panels):
                batch = panels[i:i + batch_sz]
                start_seq = batch[0]["sequence_number"]
                end_seq = batch[-1]["sequence_number"]
                
                if (start_seq, end_seq) in existing_ranges:
                    logger.info(f"Script batch for panels {start_seq}-{end_seq} already exists. Skipping.")
                else:
                    success, script_text = await self.synthesize_batch(
                        chapter_id=chapter_id,
                        chapter_num=chapter_num,
                        project_id=project_id,
                        summary_context=summary_context,
                        panels_batch=batch,
                        llm_model=llm_model,
                        min_length=min_length,
                        max_length=max_length,
                        sliding_context_window=sliding_context_window
                    )
                    if not success:
                        logger.error(f"Failed to generate script for batch {start_seq}-{end_seq}. Stopping.")
                        return False
                    
                    if progress_callback:
                        await progress_callback(
                            "llm_script_batch",
                            f"Đã sinh kịch bản thuyết minh cho các khung hình {start_seq} đến {end_seq}.",
                            {
                                "start": start_seq,
                                "end": end_seq,
                                "script_text": script_text
                            }
                        )
                    # Tiny delay
                    await asyncio.sleep(1.0)
                    
                i += batch_sz
                
            # Sync all script segments for this chapter with local script.csv
            seg_res = supabase.table("script_segments").select("script_text").eq("chapter_id", chapter_id).execute()
            all_script_data = []
            if seg_res.data:
                from src.services.tts_service import tts_service
                for row in seg_res.data:
                    parsed = tts_service.parse_script_text(row["script_text"])
                    if parsed:
                        all_script_data.extend(parsed)
            if all_script_data:
                from src.core.utils import update_csv_script
                update_csv_script(project_dir, chapter_num, all_script_data)

            # Update chapter status to scripted
            supabase.table("chapters").update({"status": "scripted"}).eq("id", chapter_id).execute()
            logger.info(f"Chapter {chapter_num} script generation completed successfully.")
            return True
            
        except Exception as e:
            logger.error(f"Error in orchestrate_chapter_scripts: {e}")
            return False

vlm_orchestrator = LLMOrchestrator()
