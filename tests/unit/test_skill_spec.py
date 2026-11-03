"""
tests/unit/test_skill_spec.py
Unit tests verifying Agentic Skill specifications, YAML frontmatter,
Root AGENTS.md, template schemas, and prompt documents for PR-03.
"""

import json
from pathlib import Path
import pytest
import yaml

from src.core.schemas import ProjectConfig, RecapScript

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / ".agents" / "skills" / "manga-recap"
SKILL_FILE = SKILL_DIR / "SKILL.md"
AGENTS_FILE = REPO_ROOT / "AGENTS.md"


class TestAgentSkillSpec:
    """Verifies that .agents/skills/manga-recap/SKILL.md adheres to Agent Skill standards."""

    def test_skill_file_exists(self):
        assert SKILL_FILE.exists(), f"Missing skill file at {SKILL_FILE}"
        assert SKILL_FILE.is_file()

    def test_skill_yaml_frontmatter(self):
        content = SKILL_FILE.read_text(encoding="utf-8")
        assert content.startswith("---"), "SKILL.md must start with YAML frontmatter delimiter '---'"
        parts = content.split("---", 2)
        assert len(parts) >= 3, "SKILL.md must have opening and closing '---' delimiters for frontmatter"

        frontmatter_raw = parts[1].strip()
        data = yaml.safe_load(frontmatter_raw)
        assert isinstance(data, dict), "Frontmatter must parse into a dictionary"

        assert data.get("name") == "manga-recap", f"Expected name 'manga-recap', got {data.get('name')}"
        assert "description" in data and len(data["description"]) > 20
        assert data.get("version") == "3.0.0"
        assert "tags" in data and isinstance(data["tags"], list)
        assert "manga" in data["tags"]
        assert "recap" in data["tags"]

    def test_skill_workflow_steps_covered(self):
        content = SKILL_FILE.read_text(encoding="utf-8")
        required_steps = [
            "Crawl",
            "Inspect",
            "Visual Panel Review",
            "Character Identity",
            "Recap Scripting",
            "Validate",
            "Review Gate 1",
            "TTS",
            "Video Render",
            "Review Gate 2",
        ]
        for step in required_steps:
            assert step.lower() in content.lower(), f"Workflow step '{step}' missing from SKILL.md"

    def test_skill_narrative_guidelines_covered(self):
        content = SKILL_FILE.read_text(encoding="utf-8")
        assert "5-second hook" in content.lower() or "5s" in content.lower()
        assert "camera motion" in content.lower()
        assert "zoom_in" in content
        assert "focus_box" in content
        assert "[cười]" in content
        assert "[thở dài]" in content


class TestRootAgentsSpec:
    """Verifies root AGENTS.md contents and principles."""

    def test_agents_file_exists(self):
        assert AGENTS_FILE.exists(), f"Missing AGENTS.md at {AGENTS_FILE}"
        assert AGENTS_FILE.is_file()

    def test_agents_architecture_principles(self):
        content = AGENTS_FILE.read_text(encoding="utf-8")
        # Principle 1: Zero cloud LLM/VLM in python
        assert "Zero" in content or "zero" in content
        assert "LLM" in content
        assert "deterministic" in content.lower()
        # Principle 2: Local JSON storage
        assert "data/projects" in content
        assert "Supabase" in content
        # Principle 3: Skill reference
        assert ".agents/skills/manga-recap/SKILL.md" in content

    def test_agents_cli_reference(self):
        content = AGENTS_FILE.read_text(encoding="utf-8")
        subcommands = ["crawl", "inspect", "validate", "tts", "render", "compile", "status"]
        for cmd in subcommands:
            assert cmd in content, f"Subcommand '{cmd}' missing from AGENTS.md CLI reference"


class TestTemplateSchemas:
    """Verifies that templates parse as valid JSON and validate cleanly against Pydantic schemas."""

    def test_project_template_valid(self):
        tmpl_file = SKILL_DIR / "templates" / "project.json.template"
        assert tmpl_file.exists(), f"Missing template at {tmpl_file}"

        raw = tmpl_file.read_text(encoding="utf-8")
        data = json.loads(raw)
        cfg = ProjectConfig.model_validate(data)
        assert cfg.schema_version == "3.0.0"
        assert cfg.slug is not None
        assert cfg.render_settings.target_width == 1280
        assert cfg.audio_settings.sample_rate == 48000

    def test_script_template_valid(self):
        tmpl_file = SKILL_DIR / "templates" / "script.json.template"
        assert tmpl_file.exists(), f"Missing template at {tmpl_file}"

        raw = tmpl_file.read_text(encoding="utf-8")
        data = json.loads(raw)
        script = RecapScript.model_validate(data)
        assert script.schema_version == "3.0.0"
        assert len(script.segments) >= 3
        # Check first segment has hook characteristics
        seg0 = script.segments[0]
        assert len(seg0.narrator_text) > 10
        assert seg0.camera_motion is not None


class TestPromptTemplates:
    """Verifies that prompt guideline documents exist and contain required instructions."""

    def test_vision_prompt_exists(self):
        p_file = SKILL_DIR / "prompts" / "vision_analysis.md"
        assert p_file.exists(), f"Missing prompt file at {p_file}"
        content = p_file.read_text(encoding="utf-8")
        assert "OCR" in content
        assert "overlap" in content.lower()
        assert "panel_id" in content

    def test_scripting_prompt_exists(self):
        p_file = SKILL_DIR / "prompts" / "recap_scripting.md"
        assert p_file.exists(), f"Missing prompt file at {p_file}"
        content = p_file.read_text(encoding="utf-8")
        assert "hook" in content.lower()
        assert "camera" in content.lower()
        assert "Vietnamese" in content or "tiếng Việt" in content
