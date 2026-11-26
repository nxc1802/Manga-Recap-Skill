# Manga Recap V6

The coding agent reads manga panels and writes single-narrator Vietnamese recaps. Python CLI tools ingest and segment pages, run closed-set panel routing (`gemini-decision-1`), synthesize speech locally or on Kaggle with VieNeu-TTS v3 Turbo, render videos with FFmpeg, archive to private Hugging Face datasets, and publish/verify on YouTube. Project state lives under `data/projects/<slug>/`.

## Quick Start

Install Python 3.10+ (using `uv` or `venv`) and FFmpeg with FFprobe. Run from the repository root:

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -e .
manga-recap --help
```

Optional credentials (see `.env.example`):
- **Decision routing:** `GEMINI_API_KEY` (`gemini-decision-1`, closed-set panel classification only).
- **Kaggle Dual-T4 GPU:** `KAGGLE_API_TOKEN` or managed local registry (`manga-recap cloud accounts`).
- **Hugging Face Archive:** `HF_TOKEN` (private dataset snapshots & SHA-256 verification).
- **YouTube Upload:** Optional extras `uv pip install -e ".[youtube]"` plus Desktop OAuth `client_secrets.json` configured via `manga-recap youtube accounts`.

## Canonical Workflow

1. **Setup:** Collect the eight confirmed user answers (`Q01–Q05, Q08–Q10`) and persist via `manga-recap setup record --project <slug> --answers-json <path> --json` alongside `voice_selection.json`.
2. **Prepare:** Run `manga-recap prepare --project <slug> --chapter <N> --json` (ingests URL/local folder, segments pages into `panels/` & `contact_sheets/`, and runs closed-set Decision routing). `prepare` never writes `script.json`.
3. **Author:** Agent visually inspects contact sheets and panels, updates `characters.json` and `story_summary.md`, writes `script.json`, and checks `manga-recap validate --project <slug> --chapter <N> --pacing --json`.
4. **Preview Chapter 1:** Run `manga-recap preview --project <slug> --chapter 1 --json` to synthesize TTS, render `preview.mp4`, and build `review_pack.html` / `review_pack.json`.
5. **Approve:** After human review of Chapter 1's video/pack, record approval bound to exact config and artifact hashes:
   `manga-recap approve --project <slug> --reviewer "<user>" --config-fingerprint "<hash>" --artifact-hash "<hash>" --json`
6. **Produce:** Run `manga-recap produce --project <slug> --chapters <range> --compile --json` (routes to local machine or Kaggle Dual-T4 per confirmed `Q02`; optional `--cloud-policy auto|baseline_split|dual_tts_shared_encode|dual_tts_then_dual_encode|one_tts_dual_encode`). Monitor progress via `manga-recap status --project <slug> --json`, which also surfaces active Kaggle job state, resolved execution policy/hardware telemetry, and chunk resume progress.
7. **Publish, Archive & Clean:** Generate a clean 16:9 thumbnail (`chapters/<N>/thumbnail.jpg`), confirm channel/visibility (`manga-recap youtube accounts --json`), publish with `manga-recap publish --project <slug> --chapter <N> --account <alias> --wait --json`, archive with `manga-recap archive push/verify`, and preview cleanup with `manga-recap clean --project <slug> --dry-run --json`.

*(Note: Kaggle Dual-T4 comparative execution policies A–D, `SUBMIT_UNKNOWN` reconciliation, chunked series resume, and minimal pruned payloads are verified offline with mocked GPUs and local FFmpeg parity tests; live Kaggle Dual-T4 multi-session throughput benchmarks remain unverified until executed live.)*

## Compatibility Commands

All low-level and legacy commands remain available for debugging and granular execution:
`crawl`, `segment`, `decide`, `repack`, `panel-inspect`, `inspect`, `validate`, `tts`, `tts_benchmark`, `visual-plan`, `render`, `compile`, `batch`, `cloud run|produce-series|pull|accounts|info`, `review-pack`, `review-approve`, `youtube metadata|upload|verify|auth|accounts`, `archive push|verify|restore`, `clean`.

## Testing & Verification

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest -q
.venv/bin/python scripts/sync_requirements.py --check
```
