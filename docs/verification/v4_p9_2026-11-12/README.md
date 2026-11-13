# P9 Dual-T4 Live Performance Benchmark Verification (2026-11-12)

- **Date:** 2026-11-12
- **Hardware:** 2x NVIDIA Tesla T4 (`15360 MB` each, total `30720 MB`) on Kaggle Cloud GPU
- **Execution Target:** `nhan-vat-phu-trong-the-gioi-dien-loan` (chapters `2, 3`)
- **Kernel Run Ref:** `cuongnguyen1802/mr-nhan-vat-p-p9bench-67651`
- **Variants Evaluated:** `B0` (1-GPU sequential baseline), `B1` (dedicated GPU0 TTS, GPU1 Render), `B2` (2x TTS then 2x Render), `B3` (elastic work-stealing), `B4` (multi-replica TTS), `B5` (NVENC coexistence)
- **Repeats:** 2 full iterations per variant (`r1`, `r2`) after initial warmup
- **All Valid:** `True` (12/12 runs passed all Pydantic schema and media validation checks)
- **Winner Profile:** `B4` (`multi_replica_tts`), achieving `628.293s` average compute wall-clock (a **1.21x** speedup over the 1-T4 baseline `760.084s`) with zero dropped frames or audio artifacts.

## Summary Results

| Profile | Profile Name | Wall (s) | Speedup vs B0 | TTS RTF | Render FPS | Peak VRAM (GPU0/GPU1) | All Valid |
|---|---|---|---|---|---|---|---|
| `B0` | 1_t4_sequential | 760.084 | 1.00x | 0.066 | 32.7 | 1901 MB / 0 MB | `True` |
| `B1` | dedicated_gpu0_tts_gpu1_render | 644.615 | 1.18x | 0.067 | 41.5 | 1913 MB / 432 MB | `True` |
| `B2` | two_gpu_tts_then_two_gpu_render | 634.553 | 1.20x | 0.055 | 44.8 | 1823 MB / 1717 MB | `True` |
| `B3` | elastic_work_stealing | 636.468 | 1.19x | 0.055 | 45.0 | 1777 MB / 1631 MB | `True` |
| `B4` | multi_replica_tts | **628.293** | **1.21x** | **0.040** | **45.2** | 3365 MB / 3023 MB | `True` |
| `B5` | elastic_fast_nvenc_coexistence | 632.204 | 1.20x | 0.056 | 45.2 | 1983 MB / 1757 MB | `True` |

## Key Files in This Directory
- `benchmark_matrix_report.md`: Markdown summary report generated directly by the benchmark harness.
- `benchmark_matrix_results.json`: Full machine-readable metrics, per-run timings, hardware telemetry, and stage breakdown.
- `dual_t4_result_manifest.json`: Verified receipt manifest with SHA-256 hashes of all per-run receipt artifacts.
- `mr-nhan-vat-p-p9bench-67651.log`: Raw Kaggle kernel execution log.
- `sha256sums.txt`: Cryptographic checksums of top-level evidence files.
