import os
import json
from pathlib import Path
from src.core.logger import logger
from src.core.storage import read_json, atomic_write_json
from src.core.utils import get_project_title_by_id, get_project_data_dir

class CharacterDictionaryService:
    def __init__(self):
        self.dict_cache = {}
        self.current_project_id = None

    def _get_file_path(self, project_id: str) -> Path:
        project_title = get_project_title_by_id(project_id)
        project_dir = get_project_data_dir(project_title)
        v3_path = project_dir / "characters.json"
        legacy_path = project_dir / "memory" / "character_dictionary.json"
        if not v3_path.exists() and legacy_path.exists():
            return legacy_path
        return v3_path

    def load_dictionary(self, project_id: str) -> dict:
        """
        Loads the character dictionary from disk into RAM.
        """
        self.current_project_id = project_id
        file_path = self._get_file_path(project_id)
        if file_path.exists():
            try:
                data = read_json(file_path)
                # Support both raw dict or CharacterDictionary schema format
                if isinstance(data, dict) and "characters" in data and isinstance(data["characters"], dict):
                    self.dict_cache = data["characters"]
                else:
                    self.dict_cache = data
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
        Merges updates into the RAM cache and flushes to disk immediately using atomic_write_json.
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

        # Flush to disk immediately with atomic write
        file_path = self._get_file_path(project_id)
        try:
            atomic_write_json(file_path, self.dict_cache)
            logger.info(f"Flushed character dictionary atomically to {file_path}.")
        except Exception as e:
            logger.error(f"Failed to save character dictionary to {file_path}: {e}")

character_dictionary_service = CharacterDictionaryService()
