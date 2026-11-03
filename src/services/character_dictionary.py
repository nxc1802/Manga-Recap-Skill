import os
import json
from src.core.logger import logger

class CharacterDictionaryService:
    def __init__(self):
        self.dict_cache = {}
        self.current_project_id = None

    def _get_file_path(self, project_id: str) -> str:
        from src.core.utils import get_project_title_by_id, get_project_data_dir
        project_title = get_project_title_by_id(project_id)
        project_dir = get_project_data_dir(project_title)
        return str(project_dir / "memory" / "character_dictionary.json")

    def load_dictionary(self, project_id: str) -> dict:
        """
        Loads the character dictionary from disk into RAM.
        """
        self.current_project_id = project_id
        file_path = self._get_file_path(project_id)
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    self.dict_cache = json.load(f)
                logger.info(f"Loaded character dictionary from {file_path} (contains {len(self.dict_cache)} characters).")
            except Exception as e:
                logger.error(f"Failed to load character dictionary from {file_path}: {e}")
                self.dict_cache = {}
        else:
            logger.info(f"No character dictionary file found at {file_path}. Starting fresh.")
            self.dict_cache = {}
        return self.dict_cache

    def get_dictionary_context(self) -> str:
        """
        Formats the dictionary as a JSON string for prompt embedding.
        """
        if not self.dict_cache:
            return "Chưa có thông tin nhân vật."
        return json.dumps(self.dict_cache, ensure_ascii=False, indent=2)

    def update_dictionary(self, project_id: str, updates: dict):
        """
        Merges updates into the RAM cache and flushes to disk immediately (Hybrid Approach).
        """
        if not updates:
            return
            
        self.current_project_id = project_id
        
        # Merge updates
        for char_name, info in updates.items():
            if not isinstance(info, dict):
                continue
            
            target_name = info.get("identified_as")
            if target_name and target_name != char_name:
                # Merge and rename unknown_N into target_name
                logger.info(f"Merging/Renaming character '{char_name}' to '{target_name}'...")
                source_info = self.dict_cache.get(char_name, {})
                
                if target_name not in self.dict_cache:
                    self.dict_cache[target_name] = {}
                
                # Merge features safely
                source_core = source_info.get("core_features", "")
                source_outfit = source_info.get("current_outfit", "")
                
                target_core = self.dict_cache[target_name].get("core_features", "")
                target_outfit = self.dict_cache[target_name].get("current_outfit", "")
                
                # Merge new updates if specified, otherwise keep old ones
                new_core = info.get("core_features") or source_core or target_core
                new_outfit = info.get("current_outfit") or source_outfit or target_outfit
                
                self.dict_cache[target_name]["core_features"] = new_core
                self.dict_cache[target_name]["current_outfit"] = new_outfit
                
                # Rename in Supabase character_embeddings table
                try:
                    from src.core.supabase_client import supabase
                    supabase.table("character_embeddings")\
                        .update({"character_name": target_name})\
                        .eq("project_id", project_id)\
                        .eq("character_name", char_name)\
                        .execute()
                    logger.info(f"Renamed character '{char_name}' to '{target_name}' in Supabase character_embeddings.")
                except Exception as db_err:
                    logger.error(f"Failed to rename character in Supabase character_embeddings: {db_err}")
                    
                # Delete the old temporary key if it exists
                if char_name in self.dict_cache:
                    del self.dict_cache[char_name]
                    
                logger.info(f"Merged and deleted old character key '{char_name}'. Current state for '{target_name}': {self.dict_cache[target_name]}")
            else:
                # Normal update
                if char_name not in self.dict_cache:
                    self.dict_cache[char_name] = {}
                    
                if "core_features" in info and info["core_features"]:
                    self.dict_cache[char_name]["core_features"] = info["core_features"]
                if "current_outfit" in info and info["current_outfit"]:
                    self.dict_cache[char_name]["current_outfit"] = info["current_outfit"]
                    
                logger.info(f"Updated character '{char_name}' in cache: {self.dict_cache[char_name]}")

        # Flush to disk immediately
        file_path = self._get_file_path(project_id)
        os.makedirs(os.path.dirname(file_path) if os.path.dirname(file_path) else ".", exist_ok=True)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(self.dict_cache, f, ensure_ascii=False, indent=2)
            logger.info(f"Flushed character dictionary to {file_path}.")
        except Exception as e:
            logger.error(f"Failed to save character dictionary to {file_path}: {e}")

character_dictionary_service = CharacterDictionaryService()
