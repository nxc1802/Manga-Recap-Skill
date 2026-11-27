# V7.1 Kaggle Dual-T4 Benchmark Report

- **Policy:** Kaggle Dual-T4 Policy B (`h264_nvenc` + `VieNeu-TTS v3 Turbo`)
- **Run A (Chapters 1–5, Ch. 1–2 skipped as verified, Ch. 3–5 dispatched):**
  - Kernel: `cuongnguyen1802/mr-v71-accept-k-c3-9116`
  - Poll time: `274s` | End-to-end: `395.04s`
  - `wall_makespan_sec`: `221.78s` (`sum_all_spans_sec=747.70s`, `overlap_saved_sec=535.37s`, `200` NVENC encode spans, `3` TTS spans)
- **Run B (Chapters 6–10):**
  - Kernel: `cuongnguyen1802/mr-v71-accept-k-c6-10304`
  - Poll time: `381s` | End-to-end: `506.16s`
  - All 5 chapters (`6`–`10`) rendered, exported, and verified with `ffmpeg` decode (`rc=0`).
