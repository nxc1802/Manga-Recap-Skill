import os
import yaml
from pathlib import Path
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from typing import Optional

# Load .env file
load_dotenv()

class OrchestratorConfig(BaseModel):
    video_mode: str = "auto"
    render_pan_mode: str = "dynamic"
    ffmpeg_max_concurrent: int = 4
    max_panels: Optional[int] = None
    language: str = "vi"
    panel_max_height_ratio: float = 3.0
    panel_soft_gutter_variance: float = 25.0
    panel_min_height_ratio: float = 2.0

class ScriptLimitsConfig(BaseModel):
    min_length: int = 50
    max_length: int = 250

class CrawlerConfig(BaseModel):
    base_info_url: str = ""
    chapter_base_url: str = ""

class TTSConfig(BaseModel):
    speed: float = 1.0
    voice: str = "Trúc Ly"

class AppConfig(BaseModel):
    # Local Storage Root
    storage_dir: Path = Field(default=Path("data/projects"))
    data_dir: Path = Field(default=Path("data"))

    # Deprecated Cloud API keys (optional, not required)
    openrouter_api_key: str = Field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    supabase_url: str = Field(default_factory=lambda: os.getenv("SUPABASE_URL", ""))
    supabase_anon_key: str = Field(default_factory=lambda: os.getenv("SUPABASE_ANON_KEY", ""))
    supabase_service_role_key: str = Field(default_factory=lambda: os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""))
    google_api_key: str = Field(default_factory=lambda: os.getenv("GOOGLE_API_KEY", ""))
    openai_api_key: str = Field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    
    # From config.yaml
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    script_limits: ScriptLimitsConfig = Field(default_factory=ScriptLimitsConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    crawler: CrawlerConfig = Field(default_factory=CrawlerConfig)
    general_story_context: str = ""
    initial_scripts: dict[str, dict[int, str]] = Field(default_factory=dict)

def load_config(config_path: str = "config.yaml") -> AppConfig:
    path = Path(config_path)
    if not path.exists():
        return AppConfig()
        
    with open(path, "r", encoding="utf-8") as f:
        yaml_data = yaml.safe_load(f) or {}
        
    config = AppConfig(
        orchestrator=yaml_data.get("orchestrator", {}),
        script_limits=yaml_data.get("script_limits", {}),
        tts=yaml_data.get("tts", {}),
        crawler=yaml_data.get("crawler", {}),
        general_story_context=yaml_data.get("general_story_context", ""),
        initial_scripts=yaml_data.get("initial_scripts", {})
    )
    
    # Validation checks: Local storage directory initialization
    config.storage_dir.mkdir(parents=True, exist_ok=True)
    return config

# Global config instance
try:
    config = load_config()
except Exception as e:
    try:
        config = load_config(str(Path(__file__).resolve().parents[2] / "config.yaml"))
    except Exception:
        config = AppConfig()
