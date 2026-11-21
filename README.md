# Manga Recap V5 Slim

The coding agent reads manga panels and writes a Vietnamese recap. The CLI ingests and segments pages, synthesizes speech with VieNeu v3 Turbo, and renders with FFmpeg. Project state lives under `data/projects/<slug>/`.

## Quick start

Install Python 3.10+ and FFmpeg with FFprobe. Run from the repository root.

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -e .
manga-recap --help
```

Local TTS uses VieNeu model weights. `decide` uses a Gemini Decision API key. Kaggle requires `KAGGLE_API_TOKEN` or a configured account. YouTube requires `google-api-python-client`, `google-auth-oauthlib`, and a Desktop OAuth client. Hugging Face archive uses `HF_TOKEN`.

## Workflow

1. Record eight user choices with `setup record --answers-json <path>` and record the confirmed voice in `voice_selection.json`.
2. Run `crawl` with `--url` or `--import-dir`, then `segment`, `decide`, and `inspect`. Read the contact sheets before writing `script.json`.
3. Run `validate`, `tts`, and `render --quality preview` for Chapter 1. After the human watches the video, record approval using `review-pack` and `review-approve`. `render --quality final` promotes the reviewed MP4.
4. Use `batch` or `cloud produce-series` for more chapters. On two GPUs, Kaggle encodes shots while the TTS worker produces narration batches. The TTS GPU joins encoding after releasing the model. A single GPU runs TTS before encoding. `compile` joins final chapters.
5. Prepare the thumbnail and YouTube metadata. Confirm the destination before upload. Use `archive push` and `archive verify` for a private Hugging Face copy. Use `clean` to preview local deletions.

| Task | Commands |
| --- | --- |
| Setup and review | `setup record/validate/show`, `status`, `review-pack`, `review-approve` |
| Ingest and inspect | `crawl`, `segment`, `decide`, `inspect`, `validate`, `repack`, `panel-inspect` |
| Synthesize and render | `tts`, `tts_benchmark`, `visual-plan`, `render`, `batch`, `compile` |
| Kaggle | `cloud accounts/info/run/produce-series/pull` |
| Publish and store | `thumbnail`, `youtube accounts/metadata/upload/verify`, `archive push/verify/restore`, `clean` |

Pass `--project <slug>`, `--chapter <N>`, and `--json` where applicable. See `manga-recap <command> --help` for options. If YouTube authorization expires, run `manga-recap youtube accounts --account-action add --account <name>` in a terminal. Browser authorization waits up to five minutes. Headless jobs return exit code 4 when authorization is needed.

Render completion checks MP4 and SRT file sizes. WAV duration comes from the header. Cleanup defaults to a dry run. Deletion requires `--no-dry-run --confirm-project <slug>`. Purging raw images requires a verified archive that covers the current files.

Run `python -m compileall -q src tests` and `python -m pytest -q` to test changes.
