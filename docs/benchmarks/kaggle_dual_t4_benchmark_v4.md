# Kaggle Dual-T4 V4 — Live Empirical Benchmark Report

**Benchmark Date:** 2026-11-12
**Status:** **EMPIRICALLY VERIFIED & AUDITED** (Gate G9 PASS)
**Execution Environment:** Kaggle Cloud GPU (`NvidiaTeslaT4` x2, `15360 MB` per GPU, Driver 535.183.01, CUDA 12.2)
**Corpus:** `nhan-vat-phu-trong-the-gioi-dien-loan` (chapters `2, 3`, total 71 segments, 168 shots, ~594s output audio/video per run)
**Kernel Run:** `cuongnguyen1802/mr-nhan-vat-p-p9bench-67651`
**Evidence Directory:** `docs/verification/v4_p9_2026-11-12/` (manifest SHA verified)

---

## 1. Executive Summary & Measured Speedup

The V4 Dual-T4 production architecture replaces single-threaded sequential execution with an isolated multiprocessing (`spawn`) worker pool and an elastic 4-state scheduler (`TTS_HEAVY`, `BALANCED`, `RENDER_HEAVY`, `DRAIN`).

All 6 benchmark variants (`B0` through `B5`) were evaluated on identical source manga panels, neural VieNeu-TTS settings (`Ngọc Huyền`, 1.0x, bfloat16 on CUDA), resolution (1080p 30fps), and zero-reencode fast-path concat.

| Variant ID | Architecture & Strategy | Mean Compute Wall (s) | Speedup vs Baseline | TTS RTF | Render FPS | Peak VRAM (GPU0/GPU1) | Validity |
|---|---|---|---|---|---|---|---|
| **B0** | 1-T4 Sequential Baseline (GPU0 only) | 760.08s | 1.00x | 0.066 | 32.7 | 1901 MB / 0 MB | **PASS** (100%) |
| **B1** | Dedicated GPU0 TTS + GPU1 Render | 644.62s | 1.18x | 0.067 | 41.5 | 1913 MB / 432 MB | **PASS** (100%) |
| **B2** | 2-GPU TTS Batch $\rightarrow$ 2-GPU Render | 634.55s | 1.20x | 0.055 | 44.8 | 1823 MB / 1717 MB | **PASS** (100%) |
| **B3** | Elastic Work-Stealing Scheduler | 636.47s | 1.19x | 0.055 | 45.0 | 1777 MB / 1631 MB | **PASS** (100%) |
| **B4** | **Multi-Replica TTS + Elastic Render** | **628.29s** | **1.21x** | **0.040** | **45.2** | 3365 MB / 3023 MB | **PASS** (100%) |
| **B5** | Elastic Fast-NVENC Coexistence | 632.20s | 1.20x | 0.056 | 45.2 | 1983 MB / 1757 MB | **PASS** (100%) |

**Winning Configuration:** **B4 (`multi_replica_tts`)** achieved the lowest wall-clock time (`628.29s`), delivering a **21% end-to-end speedup** over the single-T4 baseline while improving TTS real-time factor from `0.066` down to `0.040` (a 39% throughput increase in neural voice generation).

---

## 2. Methodology & Lifecycle Separation

Per the V4 architectural contract, cloud lifecycle overheads are tracked separately and explicitly excluded from `e2e_compute_wall_sec`:
- **Kernel Boot / Python Init:** 176.56s
- **Model Download & Warmup:** 389.38s
- **Active Compute (all 12 runs):** 7872.43s

Each variant was executed for 2 repeated iterations (`r1`, `r2`) following a warm-up phase to eliminate cold-start cache anomalies. Variance between iterations remained strictly below 1.0% (standard deviations: B0 $\pm$4.6s, B2 $\pm$0.19s, B4 $\pm$0.68s).

---

## 3. Stage Breakdown & Bottleneck Analysis

| Profile | Model Init (s) | Neural TTS Infer (s) | Audio Norm (s) | Visual Render (s) | FFmpeg Mux (s) | Artifact Validation (s) |
|---|---|---|---|---|---|---|
| `B0` | 16.31 | 39.26 | 49.13 | 542.45 | 3.92 | 132.70 |
| `B1` | 16.32 | 39.63 | 65.72 | 428.10 | 4.96 | 133.38 |
| `B2` | 32.54 | 32.56 | 99.90 | 396.12 | 9.98 | 133.02 |
| `B3` | 32.68 | 32.79 | 106.30 | 396.40 | 9.79 | 133.46 |
| `B4` | 38.23 | **23.73** | 103.96 | **395.14** | 9.97 | 132.92 |
| `B5` | 32.65 | 33.23 | 100.38 | 393.34 | 10.17 | 132.51 |

### Key Observations:
1. **TTS Scaling:** Multi-replica TTS (`B4`) drops raw inference time from 39.26s to 23.73s by concurrently utilizing CUDA cores across both physical GPUs.
2. **Render Wall-Clock:** Parallel shot rendering with hardware NVENC on both GPUs reduces visual rendering time from 542.45s down to ~395s (a 27% reduction in video creation time).
3. **Validation Dominance:** `validate_final_artifact` takes ~132.8s consistently across all runs because it performs rigorous full-stream decoding, ffprobe sanity checks, and RMS audio validation for every single frame and clip to prevent corrupted media exports.

---

## 4. Hardware Safety & VRAM Headroom

Under the V4 capacity budget rule (`usable_vram_budget = min(90% total, total - 1.5 GB)` $\approx$ 13824 MB):
- Peak allocated VRAM for single-replica profiles (`B0`–`B3`, `B5`) was **1983 MB** on GPU0 and **1757 MB** on GPU1.
- Peak allocated VRAM for multi-replica profile `B4` reached **3365 MB** on GPU0 and **3023 MB** on GPU1.
- Both figures remain well below the 13.8 GB safety ceiling, leaving >10 GB of VRAM headroom and resulting in **0 out-of-memory errors** across all runs.
