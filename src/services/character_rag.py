import os
import io
import json
import base64
import math
import httpx
from PIL import Image
from src.core.config import config
from src.core.logger import logger
from src.core.supabase_client import supabase

class CharacterRAGService:
    def __init__(self):
        self.embeddings_cache = {}  # project_id -> list of embedding dicts
        self.api_key = config.openrouter_api_key
        self.embeddings_url = "https://openrouter.ai/api/v1/embeddings"
        self.embedding_model = "nvidia/llama-nemotron-embed-vl-1b-v2:free"

    def crop_character(self, image_path: str, box_2d: list[int], output_path: str) -> str:
        """
        Crops a character from an image and makes it a square.
        box_2d format: [ymin, xmin, ymax, xmax] in normalized 0-1000 range.
        """
        try:
            with Image.open(image_path) as img:
                width, height = img.size
                ymin, xmin, ymax, xmax = box_2d
                
                # Convert to absolute pixel coordinates
                left = int(xmin * width / 1000.0)
                top = int(ymin * height / 1000.0)
                right = int(xmax * width / 1000.0)
                bottom = int(ymax * height / 1000.0)
                
                # Clamp to image boundaries
                left = max(0, min(width - 1, left))
                top = max(0, min(height - 1, top))
                right = max(0, min(width, right))
                bottom = max(0, min(height, bottom))
                
                box_w = right - left
                box_h = bottom - top
                
                if box_w <= 0 or box_h <= 0:
                    logger.warning(f"Invalid crop box size: {box_w}x{box_h} for image {image_path}")
                    return None
                    
                # Make the crop square based on the longer side
                side = max(box_w, box_h)
                center_x = left + box_w // 2
                center_y = top + box_h // 2
                
                new_left = center_x - side // 2
                new_right = new_left + side
                new_top = center_y - side // 2
                new_bottom = new_top + side
                
                # Adjust if square crop goes outside boundaries (slide the window)
                if new_left < 0:
                    new_right -= new_left
                    new_left = 0
                if new_right > width:
                    new_left -= (new_right - width)
                    new_right = width
                if new_top < 0:
                    new_bottom -= new_top
                    new_top = 0
                if new_bottom > height:
                    new_top -= (new_bottom - height)
                    new_bottom = height
                    
                # Re-clamp coordinates to be absolutely safe
                new_left = max(0, min(width - 1, new_left))
                new_top = max(0, min(height - 1, new_top))
                new_right = max(0, min(width, new_right))
                new_bottom = max(0, min(height, new_bottom))
                
                if (new_right - new_left) <= 0 or (new_bottom - new_top) <= 0:
                    return None
                    
                cropped_img = img.crop((new_left, new_top, new_right, new_bottom))
                
                # Ensure output directory exists
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                
                # Save crop
                cropped_img.save(output_path, "JPEG", quality=95)
                logger.info(f"Saved square character crop to {output_path} (size: {new_right-new_left}x{new_bottom-new_top})")
                return output_path
        except Exception as e:
            logger.error(f"Error cropping character from {image_path}: {e}")
            return None

    async def get_image_embedding(self, image_path: str) -> list[float]:
        """
        Generates embedding using nvidia/llama-nemotron-embed-vl-1b-v2:free.
        Truncates output 2048-dim vector to 1024 dimensions and normalizes it.
        """
        try:
            # 1. Base64 encode the image
            with open(image_path, "rb") as f:
                img_data = f.read()
                
            # Compress image if it is too large for fast transfer (e.g. max 224x224 or 384x384 is ideal for CLIP/embeddings)
            with Image.open(io.BytesIO(img_data)) as pil_img:
                pil_img.thumbnail((384, 384))
                buffer = io.BytesIO()
                pil_img.save(buffer, format="JPEG", quality=85)
                img_data = buffer.getvalue()
                
            encoded = base64.b64encode(img_data).decode("utf-8")
            data_uri = f"data:image/jpeg;base64,{encoded}"
            
            # 2. Call OpenRouter Embeddings API
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": self.embedding_model,
                "input": data_uri
            }
            
            timeout = httpx.Timeout(45.0, connect=10.0, read=30.0, write=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.post(self.embeddings_url, headers=headers, json=payload)
                if res.status_code != 200:
                    logger.error(f"Failed to generate embedding: {res.status_code} - {res.text}")
                    return None
                    
                data = res.json()
                raw_emb = data.get("data", [{}])[0].get("embedding", [])
                
                if not raw_emb:
                    logger.error(f"Empty embedding returned from OpenRouter: {data}")
                    return None
                    
                # 3. Truncate to 1024 dimensions
                truncated = raw_emb[:1024]
                
                # 4. L2-Normalize
                norm = math.sqrt(sum(x * x for x in truncated))
                if norm > 0:
                    normalized = [x / norm for x in truncated]
                else:
                    normalized = truncated
                    
                return normalized
        except Exception as e:
            logger.error(f"Error generating embedding for {image_path}: {e}")
            return None

    def load_embeddings_cache(self, project_id: str):
        """
        Loads all project character embeddings into memory for fast RAG search.
        """
        try:
            logger.info(f"Loading character embeddings cache for project {project_id}...")
            res = supabase.table("character_embeddings")\
                .select("id, character_name, embedding")\
                .eq("project_id", project_id)\
                .execute()
            
            self.embeddings_cache[project_id] = res.data or []
            logger.info(f"Loaded {len(self.embeddings_cache[project_id])} character embeddings into cache.")
        except Exception as e:
            logger.error(f"Failed to load character embeddings cache: {e}")
            self.embeddings_cache[project_id] = []

    def match_character(self, project_id: str, embedding: list[float], threshold: float = 0.78) -> str:
        """
        Matches embedding against project cache using Cosine Similarity (Dot Product of normalized vectors).
        Returns matched character_name if similarity >= threshold, else None.
        """
        cache = self.embeddings_cache.get(project_id, [])
        if not cache or not embedding:
            return None
            
        best_match_name = None
        best_similarity = -1.0
        
        for record in cache:
            rec_emb = record.get("embedding")
            if not rec_emb:
                continue
                
            # Parse database vector format if returned as string
            if isinstance(rec_emb, str):
                try:
                    rec_emb = json.loads(rec_emb)
                except Exception:
                    continue
            
            # Since vectors are normalized, Cosine Similarity = Dot Product
            dot_product = sum(a * b for a, b in zip(embedding, rec_emb[:1024]))
            
            if dot_product > best_similarity:
                best_similarity = dot_product
                best_match_name = record.get("character_name")
                
        logger.info(f"RAG Best Match: '{best_match_name}' with similarity {best_similarity:.4f} (Threshold: {threshold})")
        
        if best_similarity >= threshold:
            return best_match_name
        return None

    def store_character_visual(self, project_id: str, chapter_id: str, panel_id: str, character_name: str, crop_path: str, embedding: list[float], description: str = ""):
        """
        Stores the character crop metadata and embedding in Supabase, and updates the local cache.
        """
        try:
            record = {
                "project_id": project_id,
                "chapter_id": chapter_id,
                "panel_id": panel_id,
                "character_name": character_name,
                "description": description,
                "crop_image_url": crop_path,
                "embedding": embedding
            }
            res = supabase.table("character_embeddings").insert(record).execute()
            if res.data:
                logger.info(f"Stored character embedding for '{character_name}' in Supabase.")
                # Append to cache
                if project_id not in self.embeddings_cache:
                    self.embeddings_cache[project_id] = []
                self.embeddings_cache[project_id].append({
                    "id": res.data[0]["id"],
                    "character_name": character_name,
                    "embedding": embedding
                })
        except Exception as e:
            logger.error(f"Failed to store character visual embedding: {e}")

character_rag_service = CharacterRAGService()
