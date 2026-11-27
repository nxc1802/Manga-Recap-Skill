# V7.1 — Live Kaggle Dual-T4 Policy B Benchmark Report (P4 & P7.2)

**Candidate SHA:** `5d8ca077a4517d76395d22e19abfb8a39eb0c85b`  
**Pinned Kaggle Account:** `existing` (`cuongnguyen1802`, `25.58h` GPU quota remaining at P1 preflight; zero account rotation)  
**Verified Remote Hardware (`cloud_telemetry.json`):** `2× Tesla T4` (`14,911.7 MiB` VRAM each, `CUDA 13.0`, `PyTorch 2.11.0+cu130`, `VieNeu 3.8.5`, `FFmpeg 6.1.1`, `usable_nvenc_devices: [0, 1]`)  

---

## 1. Live Kaggle Runs Executed

| Run | Kernel Ref | Job Mode | Scope | Kernel Poll Time | Total E2E Time | Worker Makespan | Result |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| Standalone TTS (`E06`, `bs=32`) | `cuongnguyen1802/mr-v71-accept-k-c1-76947` | `tts` | Ch. 1 (`24` segs, `187.84s` audio) | `99 s` | `156.48 s` (caught `BUG-09` after sync) | — | **PASS (after `BUG-09` fix `5d8ca07`)** |
| Standalone Render (`E07`, dual NVENC, `tts_devices=[]`) | `cuongnguyen1802/mr-v71-accept-k-c1-77239` | `render` | Ch. 1 `1080p` preview (`60` SRT cues) | `185 s` | `217.27 s` | — | **PASS** |
| Canonical `produce --compile` (`BUG-03` Chunk Planner, Policy B `bs=16`) | `cuongnguyen1802/mr-v71-accept-k-c1-81217` | `produce_series` | Ch. 1–2 (`1080p`, `41` segs, `64` encode spans) | `217 s` | `293.76 s` | `156.93 s` | **PASS** |
| Verified Chunk Resume (`E11`) | *(none dispatched)* | `produce_series` | Re-run Ch. 1–2 (`skipped_verified=["1","2"]`) | `0 s` | `4.99 s` | `0 s` | **PASS** |

---

## 2. Policy B Dual-T4 Stage Breakdown (`produce_series`, `v71-accept-kaggle` Ch. 1–2)

From `data/projects/v71-accept-kaggle/cloud_telemetry.json`:

| Stage | Count | Wall Union (s) | Sum Worker (s) | Intra-Stage Overlap (s) | Parallelism |
| --- | ---: | ---: | ---: | ---: | ---: |
| `bootstrap_verify` | 1 | `0.23` | `0.23` | `0.00` | `1.00x` |
| `tts` (`tts_devices=[0, 1]`) | 2 | `64.98` | `128.96` | `63.98` | **`1.98x`** |
| `shot_prep` | 2 | `5.67` | `5.67` | `0.00` | `1.00x` |
| `encode` (`h264_nvenc`, `encode_devices=[0, 1]`) | 64 | `55.59` | `220.20` | `164.61` | **`3.96x`** |
| `finalize` (CPU) | 1 | `11.17` | `11.17` | `0.00` | `1.00x` |
| `export_and_compile` | 1 | `0.38` | `0.38` | `0.00` | `1.00x` |
| **Total Worker** | — | **`156.93 s` makespan** | `366.61 s` | **`228.60 s` saved** | `0` OOM / `0` failed |

---

## 3. Comparison Caveat vs. Historical V6 Single Run

- Historical V6 reference (`179.04s` worker makespan on `71` segments / `167` shots) used a different script/segment density (`71` segs vs `41` segs here) and was a single run (`n=1`). Therefore this V7.1 2-chapter run (`156.93s` makespan, `n=1`, `0` OOM, `1.98x` TTS + `3.96x` NVENC encode parallelism) establishes a fresh V7.1 measured baseline rather than a strict apples-to-apples comparison on identical 71-segment inputs.
- **Remaining P7.2 Live Gap:** Representative 5-chapter and 10–20-chapter multi-chunk live Kaggle series runs are **`NOT RUN`** in this pass and remain required before certifying full long-series production readiness.
