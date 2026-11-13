# Dual-T4 Benchmark Matrix Report (VERIFIED HARDWARE BENCHMARK RUN)

- **Generated UTC:** `2026-11-12T09:32:52.045521+00:00`
- **Project:** `nhan-vat-phu-trong-the-gioi-dien-loan` (chapters `2,3`)
- **Commit:** `unknown`
- **Repeats:** `2` | **Warmup:** `True` | **All Valid:** `True`
- **Fastest Profile:** `B4`

## Lifecycle Timings (Excluded from `e2e_compute_wall_sec`)
| Phase | Seconds |
|---|---|
| `dataset_upload_sec` | 80.708 |
| `queue_sec` | 6.324 |
| `kernel_boot_sec` | 176.565 |
| `model_load_sec` (warmup + init) | 389.385 |
| `compute_sec` (all variants x repeats) | 7872.433 |
| `download_sec` | 40.111 |

## Variant Comparison (`B0`..`B5`)
| Profile | Wall (s) | Speedup vs B0 | TTS RTF | Segs/s | Render FPS | Vid Min/Hr | GPU0/1 Util (%) | Peak VRAM (MB) | CPU (%) | Disk (MB/s) | Valid |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `B0` (1_t4_sequential) | 760.084 | 1.00x | 0.066 | 1.28 | 32.7 | 46.7 | 7.4% / 0.0% | 1901 / 0 | 61.2% | 0.56 | `True` |
| `B1` (dedicated_gpu0_tts_gpu1_render) | 644.615 | 1.18x | 0.067 | 1.27 | 41.5 | 55.1 | 7.5% / 2.1% | 1913 / 432 | 73.9% | 0.66 | `True` |
| `B2` (two_gpu_tts_then_two_gpu_render) | 634.553 | 1.20x | 0.055 | 1.09 | 44.8 | 55.9 | 5.3% / 4.7% | 1823 / 1717 | 77.0% | 0.67 | `True` |
| `B3` (elastic_work_stealing) | 636.468 | 1.19x | 0.055 | 1.08 | 45.0 | 56.1 | 5.2% / 4.8% | 1777 / 1631 | 77.0% | 0.67 | `True` |
| `B4` (multi_replica_tts) | 628.293 | 1.21x | 0.040 | 1.15 | 45.2 | 56.9 | 4.7% / 4.2% | 3365 / 3023 | 79.4% | 0.68 | `True` |
| `B5` (elastic_fast_nvenc_coexistence) | 632.204 | 1.20x | 0.056 | 1.08 | 45.2 | 56.3 | 5.6% / 5.0% | 1983 / 1757 | 77.1% | 0.67 | `True` |

## Stage Breakdown Averages (Seconds)
| Profile | model_init | tts_infer | audio_norm | render | mux | validation |
|---|---|---|---|---|---|---|
| `B0` | 16.307 | 39.256 | 49.133 | 542.446 | 3.916 | 132.696 |
| `B1` | 16.323 | 39.631 | 65.723 | 428.101 | 4.960 | 133.375 |
| `B2` | 32.542 | 32.556 | 99.897 | 396.123 | 9.978 | 133.022 |
| `B3` | 32.680 | 32.794 | 106.299 | 396.404 | 9.788 | 133.464 |
| `B4` | 38.225 | 23.734 | 103.960 | 395.139 | 9.969 | 132.917 |
| `B5` | 32.647 | 33.234 | 100.375 | 393.339 | 10.171 | 132.514 |
