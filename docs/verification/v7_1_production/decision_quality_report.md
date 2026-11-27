# V7.1 — Live Gemini Decision Quality Report (P2 & P6)

**Model / Policy:** `gemini-decision-1` · `4sided` (`cache_version: gemini-decision-1:2026-v6.2:4sided:c5d1f6e2120f518e`)  
**Source Series:** `https://truyenqqko.com/truyen-tranh/tro-thanh-rong-nho-24312` (TruyenQQ live crawl + local import verification)  
**Total Reviewed Sample:** `219` raw VCS panels across 2 chapters (`118` Ch. 1 + `101` Ch. 2)  

---

## 1. Per-Chapter Live Telemetry

| Metric | Chapter 1 (`v71-accept-local`, live crawl) | Chapter 2 (`v71-accept-local`, live crawl) | Chapter 1 Warm Cache (`v71-accept-kaggle`, `GEMINI_API_KEY=""`) |
| --- | ---: | ---: | ---: |
| Raw pages ingested | 202 | 141 | 202 (local import) |
| VCS panels extracted | 118 | 101 | 118 |
| Fast path | `metadata_only` | `structural_staged` | `metadata_only` |
| Pass 1 requests / cache hits | `118` / `0` | `101` / `0` | `0` / `118` |
| Split-select requests / resolved | `5` / `0` | `3` / `2` | `0` (`5` cache hits) / `0` |
| Merge-verify requests / merged | `0` / `0` | `4` / `3` (`1` keep_separate) | `0` / `0` |
| Pass 2 child requests | `0` | `7` | `0` |
| Final panel count | 118 | 100 | 118 |
| Classification (`active / context / skip / needs_review`) | `79 / 15 / 19 / 5` | `67 / 8 / 23 / 2` | `79 / 15 / 19 / 5` |
| Total input tokens | `206,578` | `184,225` | `0` |
| Estimated API cost (USD) | `$0.020658` | `$0.018423` | `$0.000000` |
| Rate-limit retries (429 backoff) | 11 | 10 | 0 |
| Latency P50 / Wall duration | `873.35 ms` / `293.38 s` | `781.04 ms` / `206.34 s` | `null` / `3.68 s` |

---

## 2. Manual Visual Quality Review (219 Raw Panels)

- **Chapter 1 V6 Baseline Exact Match:** Total input tokens (`206,578` across `118` raw panels) matched the frozen V6 historical baseline (`206,578` tokens / `118` panels) to the exact token.
- **`needs_review` Audit (7 total across Ch. 1–2):**
  - Ch. 1 (`5`): `panel_001` (TruyenQQ banner), `panel_029` (tiny black SFX fragment), `panel_068` (tiny white SFX sliver), `panel_075` (pawprint divider), `panel_116` (TruyenQQ banner) — **5/5 confirmed non-story** and cleanly resolved to `skip` via explicit `manga-recap repack`.
  - Ch. 2 (`2`): `panel_001` (TruyenQQ banner), `panel_098` (TruyenQQ banner) — **2/2 confirmed non-story** and resolved via `repack`.
- **Structural Split & Merge Accuracy (Ch. 2 live):**
  - `2/2` applied splits (`panel_016` at `0.4254` -> `panel_016, panel_017`; `panel_094` at `0.4401` -> `panel_092, panel_093`) cleanly separated distinct vertical frames without clipping dialogue.
  - `3/3` merges (`032+033`, `035+036`, `053+054`) and `1/1` keep-separate (`078+079`) were visually coherent and contiguous.
  - All `7` newly created child panels received dedicated Pass 2 decisions with verified SHA-256 lineage.
- **Safety Gate Finding (`C04`):** Two thin white sparkle transition strips in Ch. 2 (`panel_073`, `panel_077`) were classified `active` by Decision but caught fail-closed as `blank_band` before compute by `validate_narration_evidence` (`C04` — no automatic substitution), then cleaned via `repack`.
- **Critical Error Counts (219 panels):**
  - Story-to-skip errors: **0**
  - Catastrophic crop/story loss: **0**
  - Serious split/merge losses: **0**
  - Warm-cache requests on unchanged evidence: **0** (verified with `GEMINI_API_KEY=""`).

**Limitation:** Visual review was performed by inspecting all 12 generated contact sheets and high-resolution panels; no separate external human-annotated ground-truth JSON confusion matrix existed for Ch. 2, so metrics above reflect direct visual inspection rather than automated agreement against a pre-labeled external dataset.
