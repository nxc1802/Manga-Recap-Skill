# Gemini Decision Optimization — Phase D0 Baseline Report

**Date:** 2026-11-27  
**Pinned Base Commit:** `2caaad2bf2c6d9efbdd8cfb1ac443c0f918cdabe` (`feat/kaggle-production-optimization`)  
**Note:** Working tree contains isolated Phase 8 Kaggle optimization + `batch_size` changes (`src/cloud/`, `src/providers/kaggle.py`, `src/render/visual_policy.py` `SKIP` guard, docs/tests) untouched by Decision work.

## 1. Baseline Test Verification

- Full suite: `.venv/bin/python -m pytest -q` -> **`576 passed in 61.79s`**
- Decision + Ingest + Media subset: `.venv/bin/python -m pytest -q tests/ingest/ tests/media/` -> **`186 passed in 10.88s`**

## 2. Live `gemini-decision-1` API Schema & Resolution Capture

Captured on 2 panels from `nhan-vat-phu-trong-the-gioi-dien-loan/chapters/1` (read-only):

- `panel_005.jpg` (`1080x2646`), baseline 4 questions:
  - `panel_group`: `group_1_story_art` (`confidence=0.9809`)
  - `structure_action`: `ok_single` (`confidence=0.9036`)
  - `center_crop_2d`: `bottom_focus` (`confidence=0.3260` vs `top_focus=0.1922`, `center_2d_tight=0.1003`) — demonstrates preset rigidity & suppressed low confidence
  - `motion_mode`: `slow_pan_down` (`confidence=0.9481`)
  - `usage`: `total_input_tokens=2110` (`text=1018`, `image=1092`, `total_output_tokens=0`)
- `panel_004.jpg` (`1080x3335`), baseline 4 questions:
  - `usage`: `total_input_tokens=2120` (`text=1018`, `image=1102`, `total_output_tokens=0`)
- Image `resolution` token scaling on `panel_005.jpg` (`text=105`):
  - `None` (default) / `high`: `1092` image tokens (`1197` total)
  - `medium`: `555` image tokens (`660` total, **-49.2% image tokens**)
  - `low`: `260` image tokens (`365` total, **-76.2% image tokens**)

## 3. Historical Real Chapter Baselines (`data/projects/`)

- `bac-si-hoi-quy-chi-muon-song-yen-binh/ch1`: `188` cached raw decisions (`95` active, `36` text_only, `57` skip; `9` splits, `11` merge_with_prev), `392,132` Pass-1 input tokens (`2,093` median/panel), `cache_version=None` (pre-v6).
- `tro-thanh-rong-nho/ch1`: `118` cached raw decisions (`79` active, `16` text_only, `23` skip; `6` splits, `16` merge_with_prev), `247,570` Pass-1 input tokens (`2,107` median/panel), `cache_version=None`.

## 4. Consumer Audit Summary

- `src/render/visual_policy.py`: static/default `iter_segment_shots` branch (`motion_enabled` `False`/`None`) ignores `evidence_metadata["display_crop"]`; `center_crop_preset` has zero runtime render consumers.
- `src/ingest/decision.py`: forces `text_detection_uncertain=False` unconditionally; discards API `confidence` and `probabilities`; does not cache split selection; omits `no_split` when `H/W >= 4.0`.
- `src/core/panel_repack.py`: `apply_panel_plan` deletes existing `panels/` before non-atomic replacement, re-encodes unchanged `KEEP` panels from `raw/`, and builds contact sheets right before `run_decision_pipeline` deletes and rebuilds them again.
