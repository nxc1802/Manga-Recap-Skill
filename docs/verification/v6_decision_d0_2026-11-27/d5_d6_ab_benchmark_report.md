# Gemini Decision Optimization — Phase D0–D6 Verification & A/B Benchmark Report

**Date:** 2026-11-27  
**Pinned Base Commit:** `2caaad2bf2c6d9efbdd8cfb1ac443c0f918cdabe` (`feat/kaggle-production-optimization`)  
**Promoted Default:** `DecisionPolicyConfig(policy="4sided", image_resolution="medium", adaptive_tall_resolution="high", split_min_hw=2.2, edge_conf_threshold=0.60, edge_margin_threshold=0.12, group_conf_threshold=0.55, min_crop_width=0.60, min_crop_height=0.60, min_crop_area=0.50, verify_merges=True)`  
**Explicit Rollback Path:** `--decision-policy legacy_9preset` on `manga-recap decide` or `DecisionPolicyConfig(policy="legacy_9preset", verify_merges=False, image_resolution=None, adaptive_tall_resolution=None)`.

---

## 1. Summary Outcome

Replacing the 9-preset `center_crop_2d` heuristic and uncalibrated motion/structure routing with **4-sided independent safe edge trims (`{0, 10, 20, 30, 40}%` per edge)** + **adaptive image resolution (`medium` standard / `high` on tall panels `H/W >= 2.2`)** + **deterministic crop-driven scroll motion** + **confidence/retention guardrails** + **atomic metadata-only repack fast path** passed all Phase D0–D6 offline, live Validation (`bac-si-hoi-quy` Ch1), one-shot Held-Out (`tro-thanh-rong-nho` Ch1), and end-to-end FFmpeg MP4 render parity checks:

- **Held-Out Token & Cost Savings (`tro-thanh-rong-nho`, 118 raw panels):** `F_4sided_adaptive` used **`206,578`** total input tokens vs `A_legacy` **`282,114`** (**`-26.8%`** total tokens) and **`80,976`** image tokens vs **`145,930`** (**`-44.5%`** image tokens; **`-38.6%`** image tokens vs all-high `D_4sided` at `131,821`).
- **Held-Out Speed & I/O Savings:** **`241.29s`** vs `A_legacy` **`313.50s`** (**`-23.0%`** wall time), triggering **`metadata_only`** zero-reencode fast path (vs `structural_staged` in `A_legacy`).
- **Crop & Classification Safety:** Visual audit of overlay comparison sheets confirmed **`0/79`** speech-bubble/face/character clips on Held-Out (where `A_legacy` severely clipped bubbles/heads/characters across numerous panels) and **`0/79`** false `story_art -> skip` drops, while flagging **`5/118`** genuinely ambiguous non-story banners/slivers (`TRUYENQQQ.COM` banners and `40–167px` paw/SFX strips) cleanly as `needs_review` with verified fail-closed render gating.

---

## 2. Stratified `smoke30` Ablation (`bac-si-hoi-quy` Ch1, 30 Panels)

Isolated runs on 30 stratified panels (including 4 `needs_split` candidates, 3 contiguous merge pairs, skips, text cards, and diverse aspect ratios):

### Initial Matrix (`smoke30`)
| Policy | Wall (s) | Total Tokens | Text | Image | Splits Resolved | NR | Mean Area Trimmed | Nonzero Crops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `A_legacy` (`legacy_9preset`, `high`) | 36.43 | 100,480 | 48,268 | 52,212 | 6 | 0 | 0.2677 | 20 / 20 |
| `B_9preset_safe` (`high`, guardrails) | 115.90 | 85,142 | 40,503 | 44,639 | 2 | 0 | 0.0495 | 3 / 16 |
| `C_4sided_motion` (`high`, asks motion) | 59.84 | 77,840 | 35,381 | 42,459 | 2 | 0 | 0.0063 | 1 / 16 |
| `D_4sided` (`high`, deterministic motion) | 115.80 | 75,086 | 32,627 | 42,459 | 2 | 0 | 0.0063 | 1 / 16 |
| `D_4sided_med` (all `medium`) | 61.97 | 50,324 | 30,269 | 20,055 | 2 | 0 | 0.0176 | 1 / 17 |
| `D_4sided_low` (all `low`) | 11.64 | 39,874 | 30,269 | 9,605 | 2 | 0 | 0.0353 | 3 / 17 |
| `E_4sided_verify` (`high` + cropped verify) | 109.05 | 76,376 | 32,825 | 43,551 | 2 | 0 | 0.0063 | 1 / 16 |

**Key smoke30 findings:**
1. Dropping the redundant `motion_mode` question saved `~2,754` text tokens in Pass 1 + Pass 2 with identical decisions (`C` vs `D`).
2. `D_4sided_med` reduced `needs_split` Pass-1 candidate recall from `6/6` to `3/6` on tall strips; routing standard panels (`H/W < 2.2`) at `medium` and tall strips (`H/W >= 2.2`) at `high` (`F_4sided_adaptive`) restored **`6/6`** split candidate recall (`split_select` requests = `6`) while cutting total tokens `-29.5%` vs `A_legacy`!
3. Adding explicit `"watermark"` permission cleanly to `_EDGE_CRITERIA` (`smoke30_v2`) raised `F_4sided_adaptive` clean border/watermark trimming to `5/17` (`mean_area_trimmed=0.0465`, `70,857` tokens) while keeping characters and speech bubbles intact.

---

## 3. Full Validation Chapter (`bac-si-hoi-quy` Ch1 — 188 Raw Panels)

Isolated runs after Validation criteria calibration, before freezing challenger configuration:

| Policy | Wall (s) | Total Tokens | Text | Image | Final (`act`/`ctx`/`skip`/`nr`) | Splits Resolved (Checked) | Mean Area Trimmed | Nonzero Crops | Fast Path |
|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| `D_4sided` (all `high`) | 484.94 | 431,524 | 210,273 | 221,251 | `98 / 34 / 56 / 1` | `3 (9)` | 0.0563 | 38 / 98 | `structural_staged` |
| `F_4sided_adaptive` (`med` / `high`) | **370.67** | **338,381** | 210,660 | **127,721** | `98 / 34 / 56 / 1` | `3 (9)` | **0.0899** | **52 / 98** | `structural_staged` |

*(Historical Baseline A Pass-1 alone consumed `392,132` input tokens before split selection or Pass 2; `F_4sided_adaptive` total across Pass 1 + split + merge + Pass 2 was `338,381`.)*

- **Cross-run Agreement:** `D_4sided` vs `F_4sided_adaptive` agreed on **`157/159` (`98.7%`)** shared final panel hashes (`panel_001` anime promo banner `needs_review` in `F`, `skip` in `D`; `panel_074` hairline divider `needs_review` in `D`, `skip` in `F`). `F_4sided_adaptive` vs historical manifest agreed on **`145/146` (`99.3%`)** shared hashes.
- **Visual Crop Audit:** Inspected all **52** nonzero/delta active panels in local overlay sheets (`/tmp/decision_eval/val_bac_si/overlays_df/df_delta_01.jpg`–`df_delta_05.jpg`); confirmed clean removal of outer borders and site watermarks (`NewTruyenHOT.Org`, `UTOON.NET`, `RESET-SCAN.CO`) with zero catastrophic face or dialogue clipping.
- **Offline `--reapply-policy` Sensitivity Sweep (`0` API tokens on `F` cache):**
  - `edge_conf=0.55 / margin=0.10`: `60/98` crops (`mean_trim=0.1040`, `edge_changes=17`)
  - **`edge_conf=0.60 / margin=0.12` (frozen)**: **`52/98`** crops (`mean_trim=0.0890`)
  - `edge_conf=0.65 / margin=0.15`: `45/98` crops (`mean_trim=0.0765`, dropped clean bottom watermark trims on `panel_014`, `panel_153`)
  - `edge_conf=0.70 / margin=0.18`: `38/98` crops (`mean_trim=0.0545`)

---

## 4. Frozen One-Shot Held-Out Benchmark (`tro-thanh-rong-nho` Ch1 — 118 Raw Panels)

Executed once on the frozen `F_4sided_adaptive` configuration alongside fresh live `A_legacy` and `D_4sided` baselines (`--workers 2`, zero threshold edits):

| Policy | Wall (s) | Total Tokens | Text | Image | Est. Cost (USD) | Final (`act`/`ctx`/`skip`/`nr`) | Splits Resolved (Checked) | Mean Trim | Nonzero Crops | Fast Path |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---|
| `A_legacy` | 313.50 | 282,114 | 136,184 | 145,930 | \$0.028211 | `81 / 13 / 23 / 0` | `4 (6)` | 0.2437 | 76 / 81 | `structural_staged` |
| `D_4sided` | 298.22 | 257,423 | 125,602 | 131,821 | \$0.025742 | `79 / 14 / 21 / 4` | `0 (5)` | 0.0390 | 19 / 79 | `metadata_only` |
| `F_4sided_adaptive` | **241.29** | **206,578** | **125,602** | **80,976** | **\$0.020658** | `79 / 15 / 19 / 5` | `0 (5)` | **0.0529** | **27 / 79** | **`metadata_only`** |

**Held-Out Audit Details:**
- **Group Agreement (on 105 common raw SHA-256 panels):** `D` vs `F` `102/105` (**`97.1%`**); `A` vs `F` `101/105` (**`96.2%`**).
- **`needs_review` Safety:** All 5 flagged panels (`panel_001` `1080x532` promo banner, `panel_029` `1080x77` SFX strip, `panel_068` `1080x40` sliver, `panel_075` `1080x167` paw separator, `panel_116` `1080x537` promo banner; `group_conf` `0.432–0.524`) were genuine non-story/sliver frames; **`0/79`** false story drops.
- **Visual Comparison Overlays (`/tmp/decision_eval/test_rong_nho/overlays_af/af_01.jpg`–`af_08.jpg`):**
  - `A_legacy` produced severe center-crop speech-bubble/face/character cuts (`panel_022`, `panel_047`, `panel_058`, `panel_070`, `panel_080`, `panel_107`, `panel_110`) plus an erratic `cut=0.10` split on continuous illustration `panel_115` (`H/W=7.11`).
  - `F_4sided_adaptive` achieved **`0/79`** catastrophic bubble/face clips across all active panels and cleanly trimmed outer borders and `RINKO COMICS.COM` watermarks (`panel_009`, `022`, `054`, `058`, `080`, `089`, `107`, `110`).

---

## 5. End-to-End Static & Motion Render Parity

Verified on the Held-Out chapter (`tro-thanh-rong-nho`, 39 adapted narration segments, `1920x1080 @ 30fps`) plus real `libx264` + AAC MP4 encodes (`6` cropped/tall segments, `42.48s`):

1. **Fail-Closed `needs_review` Gate:** `generate_visual_plan` raised `ValueError("Panel needs review: panel_001")` cleanly on the unresolved manifest.
2. **Full-Chapter VisualPlan Parity (`F_4sided_adaptive`, 39 segments, `293.60s`):**
   - `static` (`motion_enabled=False`): **`160` shots**, **`128` cropped shots** (4-sided trims + tall-panel slice decomposition honored), `160` `static`.
   - `motion` (`motion_enabled=True`): **`79` shots**, **`27` cropped shots**, deterministic motions `65 slow_pan_down` / `14 static`.
3. **FFmpeg MP4 Encode & `ffprobe` Verification (6-segment subset, `42.48s`):**
   - `F_4sided_adaptive | static`: `wall=4.75s`, `28` shots, `1920x1080@30/1`, `has_audio=True`, `mp4_dur_sec=42.48` (exact match to `plan_dur_sec=42.48`).
   - `F_4sided_adaptive | motion`: `wall=8.46s`, `10` shots, `1920x1080@30/1`, `has_audio=True`, `mp4_dur_sec=42.48`.
   - Visual frame comparison (`/tmp/decision_eval/render_parity/render_parity_frames.jpg` on `panel_009`): `F_4sided_adaptive` (`0.0, 0.1, 0.9, 1.0`) preserves the entire lower sword tip and dark aura in both `static` and `motion`, whereas `A_legacy` (`0.08, 0.1, 0.92, 0.9`) clips both top SFX and bottom blade tip.

---

## 6. Artifacts & Verification Machine Data

- Raw structured metrics: [ab_benchmark_metrics.json](ab_benchmark_metrics.json)
- Phase D0 baseline: [d0_baseline_report.md](d0_baseline_report.md), [d0_baseline_metrics.json](d0_baseline_metrics.json)
