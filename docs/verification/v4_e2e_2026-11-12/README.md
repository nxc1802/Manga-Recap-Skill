# Manga Recap V4 Exact-HEAD End-to-End Verification Package (2026-11-12)

This directory contains the exact-HEAD verification package for V4 P0–P7 Chapter 1 End-to-End execution at committed `HEAD` `90f1786e31f45ddcac38376a3161ac9a7fff615b` (`tree = d1b032eaf5f2ed178fc28e71740664bb778dacd5`), matching passing GitHub Actions CI runs [`43083930743`](https://github.com/nxc1802/Manga-Recap-Skill/actions/runs/43083930743) (`pull_request`) and [`43083921539`](https://github.com/nxc1802/Manga-Recap-Skill/actions/runs/43083921539) (`push`).

## Verification Summary

| Gate | Scope | Status | Evidence Document | Key Hash / Metric |
|---|---|---|---|---|
| **G4** | Real VieNeu-TTS Synthesis | **PASS** | `local_run.json` | 37 clips, 303.68s duration, 48 kHz, RMS `0.10505`–`0.12156`, neural `v3turbo` ("Ngọc Huyền") |
| **G5** | Canonical Render Preview | **PASS** | `local_artifact_report.json` | 97 shots (`static`), 9110 frames, 303.68s, 187 cues, Video SHA: `d6d8f75bb868736765dfb58afc4c1f25e69183c73c5a327633cae1a842b64b4b` |
| **G6** | Human Approval & Final Promotion | **PASS** | `human_approval.json`, `human_review.md` | Approved by `cuongnguyen1802` (`config_fingerprint = 7984f869b60c41cecdbc441489ff8fc1a69c2727ff7224dbc70bfc19d84eb5dc`); `final.mp4` & `chapter_001.mp4` bit-for-bit matched preview |
| **G7** | Live Kaggle Cloud Run | **PASS** | `kaggle_run.json`, `semantic_parity.json` | Kernel `cuongnguyen1802/mr-nhan-vat-phu-c1-40228` (`1756s`), Dataset `cuongnguyen1802/mr-payload-18a295c18950-728376000`, `semantic_parity = true`, `visual_plan_matched = true` |
| **K3** | Failure-Path & Payload Hardening | **PASS** | `failure_path_checks.json` | 8/8 tamper/mismatch scenarios rejected fail-closed (`sync_result` + `verify_payload_manifest`) |

## Evidence Files Description

- `environment.json`: Exact `git_sha`, `tree_sha`, CI run IDs/URLs, host OS, Python, FFmpeg/FFprobe, VieNeu `3.8.3`, ONNX Runtime, and execution device metadata.
- `local_run.json`: Local Chapter 1 execution record (`setup` Q01–Q10 snapshot, neural TTS, visual plan, preview/final promotion hashes, thumbnail SHA-256 `09afbc3d2606804a8e3d6550fae55ea10c56d165ee77b8a14025562cbc9dcef2`, approval binding).
- `local_artifact_report.json`: Pre-Kaggle local `artifact_validation.json` snapshot (frame variance samples, per-clip RMS/duration/SHA-256, SRT cue count).
- `human_approval.json`: Explicit human approval binding (`cuongnguyen1802`, `2026-11-11T23:34:39.947928Z`) recorded via `review-approve`.
- `human_review.md`: Reviewer checklist documenting story fidelity, panel/crop verification, audio integrity, subtitles, and thumbnail composition.
- `kaggle_run.json`: Live Kaggle cloud dispatch, dataset/kernel references, `payload_manifest_sha256`, `sync_result` report, and post-sync fail-closed local state (`chapters/1/chapter.json` reset to `unreviewed`, `preview_approval.json` preserved).
- `kaggle_result_manifest.json`: Downloaded `result_manifest.json` from `/tmp/kaggle_out_nhan-vat-phu-trong-the-gioi-dien-loan_1_produce/results/result_manifest.json` (389 output files).
- `semantic_parity.json`: Explicit local-vs-cloud parity verification across `plan_fingerprint`, `script_fingerprint`, `evidence_fingerprint`, subtitle SHA-256, duration, frame count, and audio clip count.
- `failure_path_checks.json`: Isolated `tempfile.TemporaryDirectory()` empirical validation of 8 fail-closed rejection scenarios across `sync_result` and `verify_payload_manifest`.
- `sha256sums.txt`: SHA-256 digests of all `.json` and `.md` verification files in this directory.
