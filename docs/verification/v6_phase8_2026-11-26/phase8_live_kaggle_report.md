# Phase 8 Live Kaggle Dual-T4 Verification & Benchmark Report

**Date:** 2026-11-26  
**Branch:** `feat/kaggle-production-optimization`  
**Execution Corpus:** Isolated `/tmp` sandbox clone of `nhan-vat-phu-trong-the-gioi-dien-loan` (Chapters `2, 3`; `37 + 34 = 71` narration segments; `167` rendered shots; ~`590–593s` combined final MP4 duration; `1920x1080` `30fps` `h264_nvenc` + `aac 48000 Hz`). Production `data/projects/` remained untouched throughout.

---

## 1. Live Dual-T4 Capability Probe

- **Kernel:** `nguyenxuancuongk18dn/mr-v6-p8probe-698572` (`account_2`, dataset `nguyenxuancuongk18dn/mr-v6-p8probe-698366`)
- **Queue / Wall:** `6.33s` queued, `138.96s` remote wall, `117.22s` GPU quota consumed
- **Environment:** Python `3.13.16`, `4` CPU cores, `32098.9 MB` RAM (`30505.0 MB` free), `/kaggle/working` `19969.3 MB` free, `/tmp` `2573162.2 MB` free
- **Stack:** `torch 2.11.0+cu130` (`CUDA 13.0`), `vieneu 3.8.5` (`pip install` in `9.51s`), `pydantic 2.14.1`, `pillow 12.3.0`, `ffmpeg 6.1.1-3ubuntu5`
- **Hardware Probe (`probe_worker_hardware`):**
  - `2x Tesla T4` (`14911.7 MB` VRAM each), `usable_nvenc_devices: [0, 1]` (`h264_nvenc` smoke test `True` on both GPU 0 and GPU 1)
  - Concurrent `mp.get_context("spawn")` test with `VieNeuCUDAWorker(gpu_id=0)` and `VieNeuCUDAWorker(gpu_id=1)` (without overriding `CUDA_VISIBLE_DEVICES`):
    - GPU 0 allocated `[333.8, 0.0] MB` on load -> `[373.45, 0.0] MB` after `native_infer_batch` synthesis (`torch.bfloat16`, `pytorch_cuda`, `VieNeuTTSv3Turbo`)
    - GPU 1 allocated `[0.0, 334.18] MB` on load -> `[0.0, 373.83] MB` after synthesis (`0.0 MB` spilled onto GPU 0)
    - Cold concurrent first load took `49.13s` / `49.31s` (HuggingFace download + CUDA graph init); warm cached `load_sec` dropped to `9.22s`

---

## 2. Live Bugs Discovered & Fixed During Phase 8

1. **`PanelStatus.SKIP` Pruned Cloud Payload Mismatch (`src/render/visual_policy.py`):**
   - Live Policy A initially failed at `panel_004` (`ValueError: Missing or unsafe panel evidence: panel_004`) because `package_payload` (`src/cloud/payload.py`) intentionally omits `PanelStatus.SKIP` images while `validate_narration_evidence` and `iter_segment_shots` still demanded every skipped panel file on disk inside Kaggle.
   - Fixed `validate_narration_evidence` (skip `evidence_path` checksum/dimension open when `panel.status == PanelStatus.SKIP` and missing from pruned payload, while still validating path containment under `panels/` and rejecting any segment referencing a `SKIP` panel) and `iter_segment_shots` (route `SKIP` panels via `detect_omit_visual_band(None, panel)`). Added regression coverage in `tests/providers/test_kaggle_chunking_and_payload.py`.
2. **Kaggle Dataset Propagation Race on `kernels_push` (`src/providers/kaggle.py`, `src/cloud/kaggle_service.py`):**
   - Live probe dataset upload observed transient HTTP `404/400` / `invalidDatasetSources` immediately after `dataset_create_new` + `wait_for_dataset_ready`.
   - Classified explicit HTTP `400/404` and non-empty `invalidDatasetSources` in `KaggleClient.push_kernel` as `category="dataset_propagating"` (preserving transport ambiguity for `SUBMIT_UNKNOWN` on network/5xx timeouts) and retried up to 4 times (`6s` delay) in `KaggleCloudService.dispatch_chapter_job`. Added regression coverage in `tests/providers/test_kaggle_provider_and_lifecycle.py`.
3. **Observed SDK Limitation Recorded (Not Modified):**
   - Live inspection showed `kagglesdk`'s `cancel_kernel_session` expects `kernel_session_id`, which `kernels_status`/`get_kernel` do not expose; queue timeout cleanly persists `QUEUE_TIMEOUT` and rotates accounts via `with_account_fallback`, while remote queued kernels expire/complete independently on Kaggle.

---

## 3. Live Comparative Policy Matrix (A–D)

All 4 policies executed end-to-end on clean sandbox copies (`render_workers=2`, `h264_nvenc` on GPUs `[0, 1]`, chapters `["2", "3"]`):

| Policy ID | Strategy | Kernel Ref | Account | `wall_makespan_sec` | Speedup vs A | `active_union_sec` | `overlap_saved_sec` | Quota Billed (`sec`) | `tts` Wall / Sum (`s`) | `shot_prep` Wall (`s`) | `encode` Wall / Sum (`s`) | `finalize` Wall (`s`) | Peak VRAM `GPU0` / `GPU1` (`MB`) | Media Validation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **A** | `baseline_split` | `cuongnguyenxuan4work/mr-nhan-vat-phu-c2-5685` | `account_3` | `210.18s` | `1.00x` | `201.69s` | `289.05s` | `254.36s` | `69.81` / `69.81` | `91.14` | `125.64` / `279.65` | `49.41` | `1915` / `433` | **PASS** |
| **B** | **`dual_tts_shared_encode`** | `nguynxuncngde180528/mr-nhan-vat-phu-c2-6395` | `account_4` | **`183.89s`** | **`1.14x`** | **`175.11s`** | `370.85s` | **`227.93s`** | **`57.45`** / `112.36` | `31.81` | `90.37` / `349.90` | `51.15` | `2467` / `2139` | **PASS** |
| **C** | `dual_tts_then_dual_encode` | `nguynxuncngde180528/mr-nhan-vat-phu-c2-7022` | `account_4` | `238.46s` | `0.88x` | `210.02s` | **`379.53s`** | `316.38s` | `82.89` / `163.45` | **`31.34`** | **`85.67`** / `339.18` | `54.84` | `433`\* / `433`\* | **PASS** |
| **D** | `one_tts_dual_encode` | `nguynxuncngde180528/mr-nhan-vat-phu-c2-7728` | `account_4` | `212.47s` | `0.99x` | `195.64s` | `313.49s` | `288.28s` | `90.01` / `90.01` | `31.60` | `94.62` / `332.90` | `53.87` | `1753` / `433` | **PASS** |

*\*Note on Policy C Peak VRAM: `ResourceSampler` runs inside each `_run_coordinator_stage` call, so `cloud_telemetry.json["resources"]` reflects Stage 2 (`encode_devices`) after Stage 1 (`tts_devices`) unloaded VieNeu.*

### Validated Media Outputs (`ffprobe`)

Every chapter `2` (`37` segments) and `3` (`34` segments) across all 4 runs passed `sync_series_results` SHA-256 verification and `ffprobe` checks (`h264`, `1920x1080`, `30/1 fps`, `aac 48000 Hz`, non-empty `final.srt`, valid `cloud_receipt.json`):

- **Policy A:** Ch2 `300.967s` (`9029` frames, `22,536,326` bytes); Ch3 `290.800s` (`8724` frames, `19,004,449` bytes)
- **Policy B:** Ch2 `298.567s` (`8957` frames, `22,506,342` bytes); Ch3 `291.680s` (`8750` frames, `19,259,995` bytes)
- **Policy C:** Ch2 `300.080s` (`9002` frames, `22,516,587` bytes); Ch3 `292.000s` (`8760` frames, `19,270,428` bytes)
- **Policy D:** Ch2 `300.167s` (`9005` frames, `22,521,682` bytes); Ch3 `292.480s` (`8774` frames, `19,281,434` bytes)

### Key Empirical Findings & Default Policy Decision

1. **Why Policy B (`dual_tts_shared_encode`) won cleanly:**
   - **12.5% faster makespan (`183.89s` vs `210.18s`)** and **10.4% lower GPU quota billed (`227.93s` vs `254.36s`)** than Baseline A, despite Policy B paying a slightly slower cold batch-1 HF download (`44.3s` vs `41.8s`).
   - `_run_scheduled_pipeline` decouples `shot_prep` onto the CPU coordinator instead of blocking GPU 0 between batches (`31.81s` in B vs `91.14s` in A), feeds Dual-GPU TTS across both T4s (`57.45s` TTS wall), overlaps Dual-GPU NVENC encoding (`90.37s` encode wall vs `125.64s` in A), and overlaps Chapter 2 CPU finalization completely inside Chapter 3 encoding (`123.2s -> 155.1s` vs `183.89s` makespan).
   - Peak VRAM remained at `2467 MB` (GPU 0) and `2139 MB` (GPU 1) — under `17%` of `14,911.7 MB` per T4 — with zero OOM backoffs.
2. **Why Policy C (`dual_tts_then_dual_encode`) was slowest (`238.46s`):**
   - Although Stage 2 raw encode achieved `85.67s`, enforcing a hard barrier + process respawn after Stage 1 TTS eliminated TTS-to-encode overlap, left CPU `finalize` unoverlapped after TTS, and incurred `~70.8s` cold batch-1 HF download/init latency.
3. **Cold-start variance caveat:**
   - Each policy executed `n=1` cold Kaggle container (`model_cache_warm: false`), so initial HuggingFace download + CUDA graph initialization in batch 1 varied (`41.78s` in A, `44.27s/44.35s` in B, `63.98s` in D, `70.74s/70.86s` in C). Even after discounting batch-1 cold download differences, Policy B leads both `A` and `D` in steady-state throughput and quota efficiency.
4. **Updated Default:** `resolve_execution_policy(..., policy="auto", job="produce")` in `src/cloud/scheduler.py` now resolves to `dual_tts_shared_encode` when Dual-GPU CUDA + Dual-NVENC (`>=2` devices) are verified, keeping `baseline_split` as single-GPU/degraded fallback and explicit rollback (`--cloud-policy baseline_split`).

---

## 4. Warm Tesla T4 `batch_size` Sweep & End-to-End `Policy B` Tuning

### A. Single-Session Warm Raw VieNeu-TTS Sweep (`4` -> `32`)

- **Kernel:** `nguynxuncngde180528/mr-v6-bssweep-710014` (`account_4`, dataset `nguynxuncngde180528/mr-v6-bssweep-709893`), `queue_sec=7.15s`, `remote_wall_sec=666.66s`, `quota_delta=656.76s`
- **Setup:** Single Tesla T4 warm container (`pip_sec=8.70s`, `load_sec=33.40s`, `warmup_4_sec=8.29s`), ABBA design (`capture_asc`, `warm_asc`, `warm_desc`, plus `cpu_workers=4` at `bs=12`) over all `71` sandbox Ch2+Ch3 utterances (`~590–593s` audio per pass, `0` OOMs across all rounds). Raw data saved in [`batchsize_sweep_summary.json`](batchsize_sweep_summary.json).

| `batch_size` | Warm Median Wall (`s`) | Speedup vs `12` | Median `wall_rtf` | Median `segs/sec` | Max Peak Alloc / Reserved VRAM (`MB`) | OOM Events |
|---|---|---|---|---|---|---|
| `4` | `47.778s` | `0.63x` | `0.08084` | `1.486` | `1389.7` / `2660.0` | `0` |
| `8` | `32.552s` | `0.92x` | `0.05489` | `2.181` | `1403.8` / `2600.0` | `0` |
| `12` (`cpu=2`) | `30.076s` | `1.00x` | `0.05092` | `2.361` | `1474.6` / `2468.0` | `0` |
| `12` (`cpu=4`) | `29.779s` | `1.01x` | `0.05035` | `2.384` | `1474.6` / `2580.0` | `0` |
| `16` | `25.035s` | `1.20x` | `0.04222` | `2.836` | `1446.9` / `2514.0` | `0` |
| `24` | `24.485s` | `1.23x` | `0.04133` | `2.900` | `1788.7` / `2744.0` | `0` |
| **`32`** | **`21.596s`** | **`1.39x`** | **`0.03645`** | **`3.287`** | `1731.3` / `2624.0` | `0` |

### B. End-to-End Dual-T4 `Policy B` (`dual_tts_shared_encode`) Validation (`bs=12` vs `16` vs `32`)

All runs executed on clean Ch2+Ch3 sandbox copies (`render_workers=2`, `h264_nvenc` `[0, 1]`, all `ffprobe` checks **PASS**):

| `batch_size` | Kernel Ref | `wall_makespan_sec` | `active_union_sec` | First Encode Start (`s`) | `tts` Wall / Sum (`s`) | `encode` Wall / Sum (`s`) | Quota Billed (`s`) | Peak VRAM `GPU0` / `GPU1` (`MB`) | Media Validation |
|---|---|---|---|---|---|---|---|---|---|
| `12` | `nguynxuncngde180528/mr-nhan-vat-phu-c2-6395` | `183.89s` | `175.11s` | `66.21s` | `57.45` / `112.36` | `90.37` / `349.90` | `227.93s` | `2467` / `2139` | **PASS** (`298.57s` / `291.68s`) |
| **`16`** | **`nguynxuncngde180528/mr-nhan-vat-phu-c2-12228`** | **`179.04s`** | **`170.83s`** | **`63.21s`** | **`53.70`** / `102.34` | **`88.21`** / `344.23` | `245.89s` | `2183` / `2051` | **PASS** (`300.33s` / `290.97s`) |
| `32` | `nguynxuncngde180528/mr-nhan-vat-phu-c2-12653` | `190.57s` | `182.21s` | `75.68s` | `57.93` / `114.97` | `88.72` / `348.73` | `216.72s` | `2403` / `2197` | **PASS** (`302.24s` / `290.73s`) |

### C. Final Tuned Batch-Size Defaults (`src/cloud/scheduler.py`)

1. **`tts` jobs (`default_cloud_batch_size("tts") == 32`):** When TTS runs standalone without shot-prep/NVENC overlap (`job="tts"`), `batch_size=32` (`MAX_CLOUD_TTS_BATCH_SIZE`) is strictly optimal (`21.60s` warm median, `1.39x` faster than `bs=12`, `<19%` T4 reserved VRAM, zero OOMs).
2. **`produce` jobs (`default_cloud_batch_size("produce") == 16`):** In `dual_tts_shared_encode`, `batch_size=32` packs nearly all first-chapter segments into only two huge prefix chunks (`tts count=4`), delaying first `shot_prep` and NVENC start until `75.68s` (`+12.47s` pipeline stall vs `63.21s`) and regressing end-to-end makespan to `190.57s`. Tuning `produce` default to **`batch_size=16`** (`6` smaller chunks) starts NVENC earliest (`63.21s`), delivers the fastest end-to-end makespan (**`179.04s`**), lowest `tts` wall (`53.70s`), and lower VRAM peaks (`2183 / 2051 MB`).
