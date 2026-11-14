# Dual-T4 Benchmark Matrix Report (VERIFIED HARDWARE BENCHMARK RUN)

- **Generated UTC:** `2026-11-14T15:19:18.058339+00:00`
- **Project:** `nhan-vat-phu-trong-the-gioi-dien-loan` (chapters `2,3`)
- **Commit:** `unknown`
- **Repeats:** `1` | **Warmup:** `True` | **All Valid:** `True`
- **Fastest Profile:** `B4`

## Lifecycle Timings (Excluded from `e2e_compute_wall_sec`)
| Phase | Seconds |
|---|---|
| `dataset_upload_sec` | 73.039 |
| `queue_sec` | 6.378 |
| `kernel_boot_sec` | 183.217 |
| `model_load_sec` (warmup + init) | 127.381 |
| `compute_sec` (all variants x repeats) | 823.485 |
| `download_sec` | 10.984 |

## Variant Comparison (`B0`..`B5`)
| Profile | Wall (s) | Speedup vs B0 | TTS RTF | Segs/s | Render FPS | Vid Min/Hr | GPU0/1 Util (%) | Peak VRAM (MB) | CPU (%) | Disk (MB/s) | Valid |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `B0` (1_t4_sequential) | 356.916 | 1.00x | 0.064 | 1.35 | 72.1 | 99.6 | 12.9% / 0.0% | 1859 / 0 | 29.5% | 1.19 | `True` |
| `B4` (multi_replica_tts) | 228.009 | 1.56x | 0.036 | 1.29 | 169.7 | 155.9 | 11.4% / 9.6% | 3351 / 2965 | 60.2% | 1.86 | `True` |
| `B5` (elastic_fast_nvenc_coexistence) | 238.561 | 1.50x | 0.054 | 1.13 | 162.4 | 149.0 | 12.8% / 10.4% | 1627 / 1715 | 53.8% | 1.78 | `True` |

## Stage Breakdown Averages (Seconds)
| Profile | model_init | tts_infer | audio_norm | render | mux | validation |
|---|---|---|---|---|---|---|
| `B0` | 14.924 | 37.675 | 38.546 | 246.733 | 3.748 | 29.454 |
| `B4` | 33.913 | 21.189 | 47.675 | 104.737 | 5.107 | 29.986 |
| `B5` | 30.311 | 32.273 | 47.486 | 109.419 | 4.694 | 29.398 |
