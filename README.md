# Manga Recap V7 (Core-Only)

The coding agent reads manga panels and writes single-narrator Vietnamese recaps. Python CLI tools ingest and segment pages, run closed-set panel routing (`gemini-decision-1`), synthesize speech locally or on Kaggle Dual-T4 GPU with VieNeu-TTS v3 Turbo, render 16:9 `landscape_fit` videos with FFmpeg, archive to private Hugging Face datasets, and publish/verify on YouTube. Project state lives under `data/projects/<slug>/`.

## Quick Start

Install Python 3.10+ (`uv` or `venv`) and FFmpeg with FFprobe. Run from the repository root:

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -e .
manga-recap --help
```

Optional credentials (see `.env.example`):
- **Decision routing:** `GEMINI_API_KEY` (`gemini-decision-1`, closed-set panel grouping, guarded 4-sided edge trims `{0..40%}`).
- **Kaggle Dual-T4 Policy B GPU:** `KAGGLE_API_TOKEN` or managed local registry (`manga-recap cloud accounts`).
- **Hugging Face Archive:** `HF_TOKEN` (private dataset snapshots & SHA-256 verification).
- **YouTube Upload:** Optional extras `uv pip install -e ".[youtube]"` plus Desktop OAuth `client_secrets.json` configured via `manga-recap youtube accounts`.

## Canonical High-Level Workflow (11 Commands)

`setup`, `prepare`, `validate`, `preview`, `approve`, `produce`, `compile`, `publish`, `archive`, `clean`, `status`:

1. **Setup:** Collect all eight confirmed user answers (`Q01–Q05, Q08–Q10`) and persist via `manga-recap setup record --project <slug> --answers-json <path> --json` alongside `voice_selection.json`.
2. **Prepare:** Run `manga-recap prepare --project <slug> --chapter <N> --json` (`prepare` never writes `script.json`).
3. **Author:** Agent visually inspects `contact_sheets/` and `panels/`, updates `characters.json` & `story_summary.md`, writes `script.json`, and checks `manga-recap validate --project <slug> --chapter <N> --pacing --json`.
4. **Preview Chapter 1:** Run `manga-recap preview --project <slug> --chapter 1 --json` to generate `preview.mp4`, `preview.srt`, `review_pack.md`, and `review_pack.json`.
5. **Approve:** After human review of Chapter 1, record approval bound to exact config and artifact hashes:
   `manga-recap approve --project <slug> --reviewer "<user>" --config-fingerprint "<hash>" --artifact-hash "<hash>" --json`
6. **Produce & Compile:** Run `manga-recap produce --project <slug> --chapters <range> --compile --json` (local or Kaggle Dual-T4 Policy B `h264_nvenc` per confirmed `Q02`). Monitor via `manga-recap status --project <slug> --json`.
7. **Publish, Archive & Clean:** Save clean 16:9 thumbnail (`chapters/<N>/thumbnail.jpg`), confirm channel/privacy (`manga-recap youtube accounts --json`), publish with `manga-recap publish --project <slug> --chapter <N> --account <alias> --wait --json`, archive with `manga-recap archive push/verify`, and inspect cleanup with `manga-recap clean --project <slug> --dry-run --json`.

## Granular Repair & Utility Commands

`crawl`, `segment`, `decide`, `repack`, `panel-inspect`, `inspect`, `tts`, `visual-plan`, `render`, `review-pack`, `cloud run|produce-series|pull|reconcile|accounts|info`, `youtube metadata|upload|verify|auth|accounts`.

Standalone maintenance scripts (outside production runtime):
- Legacy V4/V5 project migration: `.venv/bin/python scripts/migrate_legacy_project.py`
- Hugging Face archive restore: `.venv/bin/python scripts/restore_archive.py -p <slug> --target-dir <dir>`
- Voice/emotion audition & benchmark: `.venv/bin/python scripts/devtools/tts_benchmark.py`

## Verification

```bash
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/python scripts/sync_requirements.py --check
.venv/bin/python -m pytest -q
```

See the [V7 Core-Only Implementation Report](docs/verification/v7_core_only_2026-11-27/v7_core_only_report.md) for codebase metrics, decision audit, and verification results.
