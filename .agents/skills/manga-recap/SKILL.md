---
name: manga-recap
version: "5.0.0"
description: Create Vietnamese manga recap scripts, narration, thumbnails, and videos from online chapters or local panel images.
---

# Manga Recap

The agent reads panels and writes the Vietnamese recap. Python handles deterministic processing. VieNeu v3 Turbo generates speech on the local machine or the selected Kaggle instance. Series state lives in `data/projects/<slug>/`.

## Stage 0 · Series setup

Before starting a new series, collect all eight choices with `ask_question` or the host's structured question tool. A preselected option counts only after the user submits it. Stop before starting jobs if the form is incomplete or unavailable.

| ID | Ask the user |
| --- | --- |
| Q01 | Chapter or range and source URL or local image directory |
| Q02 | Local or Kaggle GPU |
| Q03 | Single narrator from the VieNeu catalog |
| Q04 | Reading speed from 0.5x to 2.0x |
| Q05 | Whether to burn subtitles into the video |
| Q08 | Local save or a listed YouTube channel and private, unlisted, or public visibility |
| Q09 | Preview 720p or final 1080p |
| Q10 | No BGM or a BGM file with music gain (0–1) and voice gain (0–2) |

Save an object keyed by question ID. Each item has `user_answer` and `answer_source: "user"`. Q01 uses `chapters` and `source`. Q08 uses `no_upload` or `account` and `privacy`. Q10 uses `none` or `bgm_path`, `bgm_volume`, and `voice_volume`. Run `manga-recap setup record --project <slug> --answers-json <path> --json`. Later chapters inherit the record. Use a project-relative BGM path for Kaggle.

## Ingest and write

Run `crawl`, `segment`, `decide`, and `inspect` with `--project <slug> --chapter <N> --json`. Read contact sheets and source crops. Active panels can appear on screen. Context-only panels inform narration through a visual substitute. Skip panels are excluded.

Read `characters.json` and `story_summary.md`. Write one panel per segment by default, with reuse or multiple panels where the scene calls for it. Keep key actions and dialogue. Mark ambiguous evidence `needs_review` with `[CAN_XAC_MINH: <lý do>]`. Save after each scripting batch and run `validate`. [Narration](rules/narrative_recap.md) · [Vision](rules/vision_ocr.md) · [Characters](rules/character_consistency.md)

## Synthesize and render

Record the approved narrator in `voice_selection.json`. Run `tts`, then `render --quality preview` for Chapter 1. Use `review-pack` for the preview and fingerprints. After human approval, run `review-approve` with the reviewer and those fingerprints. Use `render --quality final` to promote the reviewed video. Run `batch` or `cloud produce-series` for later chapters, and `compile` to join final chapters. Check external jobs after five minutes and report stalls. [Review gates](rules/human_review_gates.md)

## Package and publish

Generate a 16:9 thumbnail and save it as `thumbnail.jpg` or pass its path to `youtube upload --thumbnail`. The `thumbnail` CLI can also compose selected panels. Use `youtube metadata` and reconfirm the destination before uploading. Archive to a private Hugging Face dataset when requested. `clean` lists removable intermediates by default. [YouTube publishing](rules/youtube_publishing.md)
