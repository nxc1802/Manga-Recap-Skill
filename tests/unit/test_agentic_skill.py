"""
tests/unit/test_agentic_skill.py
Validation test suite for PR-03: Agentic Skill Integration:
- .agents/skills/manga-recap/SKILL.md frontmatter and 10-stage workflow
- Root AGENTS.md decoupled architecture, zero-LLM/TTS, zero-Supabase, and exit codes
- Specialized rulebooks in rules/
- Enterprise Pydantic schema validation for template JSON files
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
RULES_DIR = SKILL_DIR / "rules"
TEMPLATES_DIR = SKILL_DIR / "templates"


class TestSkillFrontmatterAndWorkflow:
    """Verifies that SKILL.md adheres to the standard Agentic Skill specification."""

    def test_skill_file_exists(self):
        assert SKILL_FILE.exists(), f"SKILL.md not found at {SKILL_FILE}"
        assert SKILL_FILE.is_file()

    def test_skill_yaml_frontmatter_validity(self):
        content = SKILL_FILE.read_text(encoding="utf-8")
        assert content.startswith("---"), "SKILL.md must start with '---'"
        parts = content.split("---", 2)
        assert len(parts) >= 3, "SKILL.md must have opening and closing '---' delimiters"

        frontmatter = yaml.safe_load(parts[1].strip())
        assert isinstance(frontmatter, dict), "Frontmatter must parse into a YAML dictionary"
        assert frontmatter.get("name") == "manga-recap"
        assert frontmatter.get("version") == "3.0.0"
        assert "description" in frontmatter and len(frontmatter["description"]) > 20
        assert "tags" in frontmatter and "manga" in frontmatter["tags"] and "recap" in frontmatter["tags"]
        assert "metadata" in frontmatter

    def test_skill_10_stages_declared(self):
        content = SKILL_FILE.read_text(encoding="utf-8")
        required_stages = [
            "Stage 1: Ingestion & Crawl",
            "Stage 2",
            "Stage 3",
            "Stage 4",
            "Stage 5",
            "Stage 6",
            "Stage 7: Human Review Gate 1",
            "Stage 8",
            "Stage 9: Human Review Gate 2",
            "Stage 10: Multi-Chapter Compilation",
        ]
        for stage in required_stages:
            assert stage.lower() in content.lower(), f"Stage '{stage}' missing from SKILL.md"

    def test_skill_cli_exit_codes_documented(self):
        content = SKILL_FILE.read_text(encoding="utf-8")
        assert "| `0` | Success |" in content or "0" in content
        assert "Validation Error" in content
        assert "Dependency Error" in content


class TestRootAgentsMd:
    """Verifies that AGENTS.md declares repository rules and architectural boundaries."""

    def test_agents_file_exists(self):
        assert AGENTS_FILE.exists(), f"AGENTS.md not found at {AGENTS_FILE}"
        assert AGENTS_FILE.is_file()

    def test_agents_decoupled_architecture(self):
        content = AGENTS_FILE.read_text(encoding="utf-8")
        # Decoupled agent vs CLI
        assert "Cognition & Vision" in content
        assert "Deterministic Tooling" in content
        # Zero cloud LLM/TTS in python
        assert "zero" in content.lower()
        assert "LLM" in content
        # Zero Supabase
        assert "Supabase" in content
        # CLI exit codes
        assert "Exit Codes" in content or "exit_code" in content

    def test_agents_skill_index_reference(self):
        content = AGENTS_FILE.read_text(encoding="utf-8")
        assert ".agents/skills/manga-recap/SKILL.md" in content


class TestRulebooksSpecification:
    """Verifies that modular rule files exist and contain actionable directives."""

    def test_rule_files_exist(self):
        expected_rules = [
            "narrative_recap.md",
            "vision_ocr.md",
            "character_consistency.md",
            "human_review_gates.md",
        ]
        for rule_name in expected_rules:
            rule_path = RULES_DIR / rule_name
            assert rule_path.exists(), f"Rule file missing: {rule_path}"
            assert rule_path.stat().st_size > 50, f"Rule file empty: {rule_path}"

    def test_narrative_rules_content(self):
        content = (RULES_DIR / "narrative_recap.md").read_text(encoding="utf-8")
        assert "5-second" in content.lower()
        assert "50 characters" in content or "50" in content
        assert "250 characters" in content or "250" in content
        assert "[cười]" in content
        assert "[thở dài]" in content

    def test_vision_ocr_rules_content(self):
        content = (RULES_DIR / "vision_ocr.md").read_text(encoding="utf-8")
        assert "contact sheet" in content.lower()
        assert "focus_box" in content
        assert "pan_left" in content
        assert "zoom_in" in content

    def test_character_consistency_rules_content(self):
        content = (RULES_DIR / "character_consistency.md").read_text(encoding="utf-8")
        assert "characters.json" in content
        assert "Trúc Ly" in content
        assert "canonical" in content.lower()

    def test_human_review_gates_content(self):
        content = (RULES_DIR / "human_review_gates.md").read_text(encoding="utf-8")
        assert "Review Gate 1" in content
        assert "Review Gate 2" in content
        assert "voice_selection.json" in content
        assert "CAN_XAC_MINH" in content


class TestTemplatesSchemaConformity:
    """Verifies that JSON templates validate cleanly against Pydantic schemas."""

    def test_script_template_pydantic_validation(self):
        tmpl_file = TEMPLATES_DIR / "script.json.template"
        assert tmpl_file.exists(), f"Missing template: {tmpl_file}"
        raw = tmpl_file.read_text(encoding="utf-8")
        data = json.loads(raw)

        # Must parse cleanly with Pydantic v2
        script = RecapScript.model_validate(data)
        assert script.schema_version == "3.0.0"
        assert len(script.segments) >= 3
        assert script.segments[0].seq_id == "001"
        assert script.segments[0].camera_motion.value == "pan_down"
        assert script.segments[0].focus_box is not None

    def test_project_template_pydantic_validation(self):
        tmpl_file = TEMPLATES_DIR / "project.json.template"
        assert tmpl_file.exists(), f"Missing template: {tmpl_file}"
        raw = tmpl_file.read_text(encoding="utf-8")
        data = json.loads(raw)

        # Must parse cleanly with Pydantic v2
        project = ProjectConfig.model_validate(data)
        assert project.schema_version == "3.0.0"
        assert project.slug == "warrior_grandpa"
        assert project.render_settings.target_width == 1280
        assert project.render_settings.fps == 30
        assert project.audio_settings.sample_rate == 48000

    def test_story_summary_template_exists(self):
        tmpl_file = TEMPLATES_DIR / "story_summary.md.template"
        assert tmpl_file.exists(), f"Missing template: {tmpl_file}"
        content = tmpl_file.read_text(encoding="utf-8")
        assert "# Story Summary" in content
        assert "World Building" in content
