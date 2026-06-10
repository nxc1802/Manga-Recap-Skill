import os
import json
import base64
import asyncio
from pathlib import Path
from PIL import Image
import io
import re
from src.core.config import config
from src.core.logger import logger
from src.core.supabase_client import supabase
from src.core.openrouter_client import openrouter_client

from src.core.prompts import VLM_PANEL_PROMPT, VLM_MULTI_IMAGE_PROMPT

class VLMGateway:
    def __init__(self):
        self.target_size = 768 # max dimension for compression
        
    def compress_image(self, input_path: str, output_path: str, target_shortest: int = 500) -> str:
        """
        Compresses an image to WebP format, resizing so that its shortest edge
        is target_shortest. Returns the output path.
        """
        try:
            with Image.open(input_path) as img:
                # Calculate new size maintaining aspect ratio
                width, height = img.size
                # Calculate new size targeting shortest edge
                target_shortest = target_shortest
                if width < height:
                    new_width = target_shortest
                    new_height = int(height * (target_shortest / width))
                else:
                    new_height = target_shortest
                    new_width = int(width * (target_shortest / height))
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                logger.info(f"Resized image from {width}x{height} to {new_width}x{new_height} (shortest edge = {target_shortest}px)")
                
                # Ensure output folder exists
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                
                # Save as WebP with quality 80
                img.save(output_path, "WEBP", quality=80)
                logger.info(f"Compressed and saved image to {output_path}")
                return output_path
        except Exception as e:
            logger.error(f"Error compressing image {input_path}: {e}")
            raise

    def get_image_base64_data_uri(self, image_path: str) -> str:
        """
        Reads a local image and encodes it to a base64 WebP data URI.
        """
        try:
            with open(image_path, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
                return f"data:image/webp;base64,{encoded_string}"
        except Exception as e:
            logger.error(f"Error base64 encoding image {image_path}: {e}")
            raise

    def parse_vlm_response(self, response_text: str) -> tuple[str, str]:
        """
        Parses VLM response text to extract:
        - ocr_text (from 'ocr' field or '1. OCR:' section)
        - vlm_description (combining character_appearance, character_action, and background or '2. Ngoại hình' section)
        """
        import json
        ocr_text = ""
        vlm_description = response_text
        
        # Try cleaning the string for JSON parsing
        clean_text = response_text.strip()
        if clean_text.startswith("```"):
            clean_text = re.sub(r"^```(?:json)?\n", "", clean_text)
            clean_text = re.sub(r"\n```$", "", clean_text).strip()
            
        try:
            data = json.loads(clean_text)
            ocr_text = data.get("ocr", "").strip()
            if ocr_text.lower() in ["không có", "khong co", "none", "n/a", ""]:
                ocr_text = ""
            desc_parts = []
            if data.get("character_appearance"):
                desc_parts.append(f"Ngoại hình: {data['character_appearance']}")
            if data.get("character_action"):
                desc_parts.append(f"Hành động: {data['character_action']}")
            if data.get("background"):
                desc_parts.append(f"Bối cảnh: {data['background']}")
            
            vlm_description = "\n".join(desc_parts)
            return ocr_text, vlm_description
        except Exception:
            # Try regex to locate JSON object within text
            json_match = re.search(r"(\{.*\})", response_text, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                    ocr_text = data.get("ocr", "").strip()
                    if ocr_text.lower() in ["không có", "khong co", "none", "n/a", ""]:
                        ocr_text = ""
                    desc_parts = []
                    if data.get("character_appearance"):
                        desc_parts.append(f"Ngoại hình: {data['character_appearance']}")
                    if data.get("character_action"):
                        desc_parts.append(f"Hành động: {data['character_action']}")
                    if data.get("background"):
                        desc_parts.append(f"Bối cảnh: {data['background']}")
                    
                    vlm_description = "\n".join(desc_parts)
                    return ocr_text, vlm_description
                except Exception:
                    pass
                    
        # Fallback to regex-based text parsing
        ocr_match = re.search(r"1\.\s*OCR:?\s*(.*?)(?=2\.\s*Ngoại hình|$)", response_text, re.DOTALL | re.IGNORECASE)
        if ocr_match:
            ocr_text = ocr_match.group(1).strip()
            if ocr_text.lower() in ["không có", "khong co", "none"]:
                ocr_text = ""
                
        desc_match = re.search(r"2\.\s*(Ngoại hình.*)$", response_text, re.DOTALL | re.IGNORECASE)
        if desc_match:
            vlm_description = desc_match.group(1).strip()
            
        return ocr_text, vlm_description


    async def process_batch_with_retry(
        self,
        batch: list,
        chapter_num: str,
        chapter_id: str,
        previous_context_data: list,
        vlm_model: str,
        vlm_image_resize_shortest_edge: int,
        vlm_context_length: int,
        progress_callback
    ) -> bool:
        """
        Processes a batch of panels together using OpenRouter multi-image VLM.
        """
        start_seq = batch[0]["sequence_number"]
        end_seq = batch[-1]["sequence_number"]
        max_attempts = 3
        
        # 1. Compress all images in this batch and prepare base64 URIs
        image_data_uris = []
        try:
            for p in batch:
                seq = p["sequence_number"]
                raw_image_path = p["raw_image_url"]
                from src.core.utils import get_project_title_by_chapter_id, get_project_data_dir
                project_title = get_project_title_by_chapter_id(chapter_id)
                project_dir = get_project_data_dir(project_title)
                processed_dir = project_dir / "raw_images" / f"chap_{chapter_num}"
                processed_path = processed_dir / f"panel_{seq:03d}_processed.webp"
                
                # Check if processed image already exists, otherwise compress
                if not processed_path.exists():
                    self.compress_image(raw_image_path, str(processed_path), target_shortest=vlm_image_resize_shortest_edge)
                    
                image_data_uri = self.get_image_base64_data_uri(str(processed_path))
                image_data_uris.append((p["id"], seq, image_data_uri, str(processed_path)))
        except Exception as e:
            logger.error(f"Error preparing images for VLM batch {start_seq}-{end_seq}: {e}")
            return False
            
        # 2. Format previous context string
        prev_context_str = ""
        if previous_context_data and vlm_context_length > 0:
            recent_context = previous_context_data[-vlm_context_length:]
            context_parts = []
            for ctx in recent_context:
                context_parts.append(
                    f"- Khung hình {ctx['seq']}: OCR: '{ctx['ocr']}', Mô tả: '{ctx['desc']}'"
                )
            prev_context_str = (
                "BỐI CẢNH TỪ CÁC KHUNG HÌNH TRƯỚC ĐÓ:\n"
                "Để giúp bạn nhận diện nhân vật đồng nhất ngắn hạn, dưới đây là phân tích của các khung hình ngay trước đó:\n"
                + "\n".join(context_parts)
            )
            
        # 3. Format VLM prompt using VLM_MULTI_IMAGE_PROMPT
        prompt = VLM_MULTI_IMAGE_PROMPT.format(
            num_images=len(batch),
            start_seq=start_seq,
            previous_context_str=prev_context_str
        )
        
        # Prepare VLM payload content: text prompt followed by image URIs
        content = [{"type": "text", "text": prompt}]
        for _, seq, uri, _ in image_data_uris:
            content.append({
                "type": "image_url",
                "image_url": {"url": uri}
            })
            
        messages = [{"role": "user", "content": content}]
        
        # Save VLM prompt text for debugging
        from src.core.utils import get_project_title_by_chapter_id, get_project_data_dir
        project_title = get_project_title_by_chapter_id(chapter_id)
        project_dir = get_project_data_dir(project_title)
        history_dir = project_dir / "logs" / "vlm_history"
        os.makedirs(history_dir, exist_ok=True)
        with open(os.path.join(history_dir, f"chap_{chapter_num}_batch_{start_seq}_{end_seq}_prompt.txt"), "w", encoding="utf-8") as f:
            f.write(prompt)
            
        # Retry loop for the API call
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Sending VLM batch {start_seq}-{end_seq} to OpenRouter (Attempt {attempt}/{max_attempts})...")
                response_text = await openrouter_client.request_completion(
                    model=vlm_model or config.vlm_model,
                    messages=messages,
                    temperature=0.2
                )
                
                # Save VLM response for debugging
                with open(os.path.join(history_dir, f"chap_{chapter_num}_batch_{start_seq}_{end_seq}_response.txt"), "w", encoding="utf-8") as f:
                    f.write(response_text)
                    
                # Parse the response
                parsed_panels = self.parse_multi_vlm_response(response_text, [seq for _, seq, _, _ in image_data_uris])
                if not parsed_panels:
                    logger.warning(f"VLM response parsing failed or incomplete for batch {start_seq}-{end_seq} on attempt {attempt}.")
                    continue
                    
                # Update Supabase for each panel in the batch
                for p_id, seq, _, proc_path in image_data_uris:
                    panel_data = parsed_panels.get(seq, {"ocr": "", "description": "Không có mô tả."})
                    supabase.table("panels").update({
                        "processed_image_url": proc_path,
                        "ocr_text": panel_data["ocr"],
                        "vlm_description": panel_data["description"],
                        "status": "processed"
                    }).eq("id", p_id).execute()
                    
                    if progress_callback:
                        await progress_callback(
                            "vlm_panel",
                            f"Hoàn thành phân tích VLM cho khung hình {seq}.",
                            {
                                "sequence_number": seq,
                                "ocr_text": panel_data["ocr"],
                                "vlm_description": panel_data["description"]
                            }
                        )
                logger.info(f"Successfully processed and saved VLM batch {start_seq}-{end_seq}")
                return True
                
            except Exception as e:
                logger.error(f"Error processing VLM batch {start_seq}-{end_seq} on attempt {attempt}: {e}")
                if attempt < max_attempts:
                    await asyncio.sleep(2.0)
                    
        return False

    def parse_multi_vlm_response(self, response_text: str, expected_seqs: list[int]) -> dict[int, dict]:
        """
        Parses VLM response for multi-image processing.
        Returns a dict mapping sequence_number -> {'ocr': ..., 'description': ...}
        where 'description' is a serialized JSON string containing sub-panels and skipped status.
        """
        clean_text = response_text.strip()
        if clean_text.startswith("```"):
            clean_text = re.sub(r"^```(?:json)?\n", "", clean_text)
            clean_text = re.sub(r"\n```$", "", clean_text).strip()
            
        parsed_data = {}
        try:
            parsed_data = json.loads(clean_text)
        except Exception:
            json_match = re.search(r"(\{.*\})", response_text, re.DOTALL)
            if json_match:
                try:
                    parsed_data = json.loads(json_match.group(1))
                except Exception:
                    pass
                    
        result = {}
        # Group sub-panels by their base integer sequence number
        if parsed_data and isinstance(parsed_data, dict) and "panels" in parsed_data:
            panels_list = parsed_data["panels"]
            if isinstance(panels_list, list):
                # Temporary mapping: base_seq -> list of sub-panel dicts
                grouped = {}
                for p in panels_list:
                    seq_id = str(p.get("sequence_id") or p.get("sequence_number") or "")
                    status = p.get("status") or "processed"
                    
                    # Extract integer prefix
                    match_prefix = re.match(r"^(\d+)", seq_id)
                    if not match_prefix:
                        continue
                    base_seq = int(match_prefix.group(1))
                    
                    if base_seq not in grouped:
                        grouped[base_seq] = []
                    grouped[base_seq].append(p)
                
                # Format each group into a JSON string for vlm_description
                for base_seq, sub_list in grouped.items():
                    # Check if all sub-panels in this group are skipped
                    is_skipped = all(p.get("status") == "skip" for p in sub_list)
                    
                    combined_ocr_list = [p.get("ocr", "").strip() for p in sub_list if p.get("ocr", "").strip()]
                    combined_ocr = "\n".join(combined_ocr_list)
                    
                    sub_panels_data = []
                    for idx, p in enumerate(sub_list):
                        sub_seq_id = p.get("sequence_id") or p.get("sequence_number") or f"{base_seq}_{idx+1}"
                        
                        sub_panels_data.append({
                            "sequence_id": str(sub_seq_id),
                            "status": p.get("status", "processed"),
                            "ocr": p.get("ocr", "").strip(),
                            "character_appearance": p.get("character_appearance", "").strip(),
                            "character_action": p.get("character_action", "").strip(),
                            "background": p.get("background", "").strip(),
                            "characters": p.get("characters") or []
                        })
                        
                    result[base_seq] = {
                        "ocr": combined_ocr,
                        "description": json.dumps({
                            "status": "skip" if is_skipped else "processed",
                            "ocr": combined_ocr,
                            "sub_panels": sub_panels_data
                        }, ensure_ascii=False)
                    }
                            
        # Validate that we got at least some of the expected sequence numbers.
        missing = set(expected_seqs) - set(result.keys())
        if missing:
            logger.warning(f"VLM JSON response is missing sequence numbers: {missing}")
            if len(result) == 0:
                return None
                
        return result

    async def process_chapter_panels(
        self,
        chapter_id: str,
        chapter_num: str,
        vlm_model: str = None,
        vlm_image_resize_shortest_edge: int = 500,
        vlm_batch_size: int = 4,
        vlm_context_length: int = 2,
        progress_callback = None
    ) -> int:
        """
        Orchestrates processing panels using VLM multi-image batching.
        """
        logger.info(f"Fetching panels for Chapter ID: {chapter_id}...")
        try:
            res = supabase.table("panels").select("*").eq("chapter_id", chapter_id).order("sequence_number").execute()
            panels = res.data
            
            if not panels:
                logger.warning("No panels found in database for this chapter.")
                return 0
                
            logger.info(f"Found {len(panels)} panels in database. Commencing VLM Batch processing...")
            
            vlm_batch_size = vlm_batch_size or getattr(config.orchestrator, "vlm_batch_size", 4)
            vlm_context_length = vlm_context_length or getattr(config.orchestrator, "vlm_context_length", 2)
            
            # Form batches
            batches = []
            i = 0
            while i < len(panels):
                batches.append(panels[i:i + vlm_batch_size])
                i += vlm_batch_size
                
            success_count = 0
            previous_context_data = []
            
            for b_idx, batch in enumerate(batches):
                # Check if all panels in this batch are already processed
                all_processed = True
                for p in batch:
                    processed_img = p.get("processed_image_url")
                    if p.get("status") != "processed" or not processed_img or not os.path.exists(processed_img):
                        all_processed = False
                        break
                        
                if all_processed:
                    logger.info(f"Batch {b_idx + 1}/{len(batches)} (panels {batch[0]['sequence_number']}-{batch[-1]['sequence_number']}) already processed. Skipping.")
                    success_count += len(batch)
                    for p in batch:
                        previous_context_data.append({
                            "seq": p["sequence_number"],
                            "ocr": p.get("ocr_text") or "",
                            "desc": p.get("vlm_description") or ""
                        })
                    continue
                    
                success = await self.process_batch_with_retry(
                    batch, chapter_num, chapter_id, previous_context_data, vlm_model, vlm_image_resize_shortest_edge, vlm_context_length, progress_callback
                )
                if success:
                    success_count += len(batch)
                    for p in batch:
                        pan_res = supabase.table("panels").select("ocr_text, vlm_description").eq("id", p["id"]).execute()
                        if pan_res.data:
                            previous_context_data.append({
                                "seq": p["sequence_number"],
                                "ocr": pan_res.data[0].get("ocr_text") or "",
                                "desc": pan_res.data[0].get("vlm_description") or ""
                            })
                else:
                    logger.error(f"Failed to process VLM batch {b_idx + 1} (panels {batch[0]['sequence_number']}-{batch[-1]['sequence_number']})")
                    for p in batch:
                        if p.get("status") != "processed":
                            supabase.table("panels").update({"status": "failed"}).eq("id", p["id"]).execute()
                            
            return success_count
            
        except Exception as e:
            logger.error(f"Error in process_chapter_panels: {e}")
            raise

vlm_gateway = VLMGateway()
