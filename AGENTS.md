# Manga Recap Agent Guide (V7 Core-Only)

Follow the consolidated skill specification in [Manga Recap skill](.agents/skills/manga-recap/SKILL.md) for all chapter production tasks.

## Ownership & Scope

- **Coding agent owns:** Visually inspecting `contact_sheets/` and `panels/`, OCR and character tracking (`characters.json`, `story_summary.md`), writing faithful Vietnamese single-narrator `script.json`, selecting static/pan framing cues, and creating 16:9 thumbnails.
- **Python CLI owns:** Deterministic crawling, VCS panel segmentation, closed-set `gemini-decision-1` routing, VieNeu-TTS v3 Turbo synthesis, FFmpeg 16:9 `landscape_fit` rendering, Kaggle Dual-T4 Policy B orchestration, Hugging Face archival, and YouTube upload/verification.
- **Out of scope:** Creative cloud LLMs/VLMs, cloud speech APIs, mock TTS in production, Kaggle Policy A/CPU/single-GPU fallbacks, interactive YouTube prompt selectors, and external databases.

## Repository Conventions

- Always invoke `manga-recap ... --json` so state, `next_action`, blockers, and errors stay structured.
- Never fabricate user choices (`Q01–Q05, Q08–Q10`), `voice_selection.json`, or `approve` preview hashes.
- Use atomic local JSON writes (`atomic_write_json`) and keep Chapter 1 preview review human-gated before final promotion or series production.
- Verify changes with `.venv/bin/python -m compileall -q src tests scripts`, `.venv/bin/python scripts/sync_requirements.py --check`, and `.venv/bin/python -m pytest -q`.
