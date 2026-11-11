# Kaggle Dual-T4 V4 — Benchmark audit / NOT VERIFIED

**Review date:** 2026-11-11
**Status:** BLOCKED — no trustworthy live benchmark or production media result was established in the reviewed repository.

## Why the previous “EMPIRICAL BENCHMARK VERIFIED” claim was withdrawn

The previous report asserted GPU 0/1 memory readings, 108.4 encode FPS, a 1.63x speedup and per-chapter measurements, but did not provide a corroborating live Kaggle run identifier, original `nvidia-smi` logs, output checksums, playback-quality samples, or reproducible hardware telemetry. Code review found that `TTSWorker._write_wav_clip` previously emitted *silent* PCM instead of using neural audio, and `RenderWorker._encode_video` previously generated a *black* FFmpeg source, regardless of the manga panels. Consequently the prior performance figures **cannot be accepted as real neural speech + real panel rendering benchmarks**. Those values are not repeated here as verified results.

The orchestration design (TTS GPU 0 overlapping render GPU 1) is a proposed architecture, not proof of two independent, correctly functioning GPU jobs. Moreover, the current `dispatch_series_job` has no working dataset-upload implementation and its generated Kaggle worker expects a `src/` package that is not included with the series payload. Series dispatch is now intentionally blocked to avoid producing misleading output.

## Exit criteria

1. Implement a real and fully verified neural waveform write path, including speed control and WAV integrity validation. **Fail on missing model or failed inference; never silently synthesize placeholder speech.**
2. Implement a real panel/shot-to-video renderer that consumes the canonical `VisualPlan`; match local previews and chapter 1 human-approved output.
3. Upload exactly one versioned series dataset containing code and all approved assets, execute exactly one Kaggle kernel session, retrieve one verified output bundle.
4. Verify Kaggle actually assigned two T4 GPUs. The CUDA child with `CUDA_VISIBLE_DEVICES=1` must address its **visible GPU index 0** for NVENC.
5. Record per-GPU utilization and VRAM (accurately distinguishing PyTorch and NVENC), actual chapter runtime, total upload/queue/boot/download time, dropped/failed utterances, audiovisual QA, screenshots and media hashes.
6. Benchmark against the existing per-chapter cloud worker on the **same corpus, quality, subtitles, FPS, resolution and codec**. Include independent repeated runs and variance.

The theoretical ideal balanced two-stage pipeline has an asymptotic 2x ceiling; it is **not an observed speedup guarantee**. No production performance claim should be made until all criteria pass.
