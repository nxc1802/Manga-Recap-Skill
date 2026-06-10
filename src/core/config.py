import os
import yaml
from pathlib import Path
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from typing import Optional

# Load .env file
load_dotenv()

class OrchestratorConfig(BaseModel):
    batch_size: int = 10
    sliding_context_window: int = 5
    vlm_image_resize_shortest_edge: int = 500
    max_panels: Optional[int] = None
    vlm_batch_size: int = 4
    vlm_context_length: int = 2

class ScriptLimitsConfig(BaseModel):
    min_length: int = 50
    max_length: int = 250

class CrawlerConfig(BaseModel):
    base_info_url: str = ""
    chapter_base_url: str = ""

class ModelsConfig(BaseModel):
    llm_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    vlm_model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"

class TTSConfig(BaseModel):
    speed: float = 1.0
    voice: str = "xtts-vi"

class AppConfig(BaseModel):
    # From environment
    openrouter_api_key: str = Field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    supabase_url: str = Field(default_factory=lambda: os.getenv("SUPABASE_URL", ""))
    supabase_anon_key: str = Field(default_factory=lambda: os.getenv("SUPABASE_ANON_KEY", ""))
    supabase_service_role_key: str = Field(default_factory=lambda: os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""))
    
    # Models loaded from config.yaml or fallback to defaults
    vlm_model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
    llm_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    
    # From config.yaml
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    script_limits: ScriptLimitsConfig = Field(default_factory=ScriptLimitsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    crawler: CrawlerConfig = Field(default_factory=CrawlerConfig)
    general_story_context: str = ""
    initial_scripts: dict[str, dict[int, str]] = Field(default_factory=dict)

def load_config(config_path: str = "config.yaml") -> AppConfig:
    path = Path(config_path)
    if not path.exists():
        # Fallback default configuration if config.yaml is not found
        cfg = AppConfig()
        # Set models defaults
        cfg.vlm_model = cfg.models.vlm_model
        cfg.llm_model = cfg.models.llm_model
        return cfg
        
    with open(path, "r", encoding="utf-8") as f:
        yaml_data = yaml.safe_load(f) or {}
        
    # Combine Env and YAML
    config = AppConfig(
        orchestrator=yaml_data.get("orchestrator", {}),
        script_limits=yaml_data.get("script_limits", {}),
        models=yaml_data.get("models", {}),
        tts=yaml_data.get("tts", {}),
        crawler=yaml_data.get("crawler", {}),
        general_story_context=yaml_data.get("general_story_context", ""),
        initial_scripts=yaml_data.get("initial_scripts", {})
    )
    
    # Assign model names explicitly to config attributes
    config.vlm_model = config.models.vlm_model
    config.llm_model = config.models.llm_model
    
    # Fallback to env overrides if exists
    env_vlm = os.getenv("VLM_MODEL")
    if env_vlm:
        config.vlm_model = env_vlm
    env_llm = os.getenv("LLM_MODEL")
    if env_llm:
        config.llm_model = env_llm
    
    # Validation checks
    if not config.openrouter_api_key:
        raise ValueError("OPENROUTER_API_KEY must be set in .env")
    if not config.supabase_url or not config.supabase_anon_key:
        raise ValueError("SUPABASE_URL and SUPABASE_ANON_KEY must be set in .env")
        
    return config

# Global config instance
try:
    config = load_config()
except Exception as e:
    # Fallback to loading relative path if running from subdirectories
    try:
        config = load_config(str(Path(__file__).resolve().parents[2] / "config.yaml"))
    except Exception:
        config = None
