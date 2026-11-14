# V4 Implementation Plan — Strict Setup, Visual Motion, Dual-T4 Production, HF Archive

Status: HISTORICAL PLAN. Runtime implementation is not production verification. See [correctness remediation](V4_Correctness_Improvement_Plan.md) and [current evidence](V4_Correctness_Status.md).
Baseline: `aea2ca9fb56723fa866c4454ad38e2f036ce82ec` (2026-11-10).
Work branch: `feat/v4-strict-form-motion-cloud-archive`.
Owner interfaces: Agent `SKILL.md`; Python `src/cli/`, `src/core/`, `src/render/`, `src/cloud/`, `src/production/`, `src/youtube/`; new `src/archive/`.

## 0. Invariants (every PR MUST preserve)
- No cloud LLM/VLM/TTS API introduced. Agents perform vision/storytelling. Kaggle runs local VieNeu neural inference and FFmpeg, not a cloud TTS provider.
- Never silently mark `human_review_status=approved`. Chapter 1 preview is an actual editorial gate.
- Never remove essential evidence from script. Visual-only omission is separate from story/semantic retention.
- `script.json`, audio/cache hashes, timestamps, and video output remain traceable via stable `seq_id` + `panel_id`.
- No destructive cleanup unless both YouTube and private Hugging Face archive have independent, validated receipts and the user's policy explicitly permits upload/cleanup. `--dry-run` must be default.
- Current `main` behavior is the baseline; no unreviewed release to the `main` branch.

## PR-01: STRICT FORM MODE, 10/10 USER CHOICES

### Existing
`SKILL.md` Stage 0 listed ten setup items, but permitted agent to 'ask or explicitly confirm'; `config.yaml`, CLI defaults, and project inherited settings can mask missing user choices. Current YouTube upload additionally has an interactive terminal prompt (multi-account channel selection); thumbnail has a two-question human gate.

### Change
1. Canonical ten questions Q01–Q10 in `.agents/skills/manga-recap/SKILL.md`; all must be presented using the coding-agent `ask_question` tool; no inferred or preselected answer. If tool size limits prevent one call, split into multiple `ask_question` calls. If missing, fail closed.
2. Q01 source + chapter/range; Q02 Kaggle/local; Q03 voice; Q04 speed; Q05 burn subtitles; Q06 animation; Q07 thumbnail; Q08 YouTube account + privacy or no upload; Q09 preview/final; Q10 BGM path + explicit gains or none.
3. New `src/core/series_setup.py`: `SeriesSetupAnswers` Pydantic model, `question_id` enum (Q01..Q10), `answer_source=user`, `user_confirmed_at`, schema version, immutable snapshot hash, and a `validate_complete_setup` function. No secrets in this JSON.
4. New `manga-recap setup validate --project <slug> --json`, called before the first stage. Enforce through Python at the orchestrating entrypoint(s), and block CLI direct bypass on new series; allow explicitly signed/recorded legacy migration after the same ten-question gate.
5. Persist `series_setup_answers.json` and project configuration together atomically; lock both, record a common `setup_revision`, and reconcile interrupted writes on restart. Make downstream invalidation per affected field.
6. Gate 0 once per series; chapter 1 preview and later thumbnail/channel approval still require human interaction at their stages.

### Acceptance
- For each missing Q01..Q10, no crawl, script, TTS, render, cloud, or upload execution.
- The question collector invokes `ask_question` with the full set. Neither `project.json` defaults nor model memory count as user answers.
- A choice of 'No BGM', 'No thumbnail', or 'No YouTube' counts as an explicit answer; blank and unresolved selection does not.
- Unit tests include missing field, invalid response, re-prompt, conflicting series configuration, duplicate IDs, inherited verified configuration, and forbidden bypass.

## PR-02: VISUAL-FIRST, TOP-DOWN ONLY, NO JITTER

### Existing
`src/render/camera.py` supports upward and reverse motion, zoom, static, and vertical_scroll; `src/render/video_engine.py` has a dedicated local FFmpeg filterchain, while `src/cloud/kaggle_service.py` embeds a separate generated Pillow frame compositor. Local has an `img_h > 2 * img_w` safeguard that turns some motions into STATIC; it does not segment tall panels into readable visual beats. Rendered `panel_ids` are currently also the primary story-panel references.

### Change
1. Introduce `src/render/visual_policy.py`, pure `VisualPlan`/`Shot` objects, callable by BOTH local and Kaggle renderer (cloud uses packaged shared module, not a second hand-written motion engine).
2. Split `script_evidence_panel_ids` from `render_shots`: preserve full source `panel_ids` for script/traceability. Give `PanelItem` a `visual_role` in `{show,context_only,omit_visual}` and `visual_omit_reason`. Add source-panel provenance for each selected shot.
3. When visual role is `omit_visual`, keep its content referenced in the narration; select a nearby *relevant, approved* shot (can be reused). Never display a text-only panel by default and never invent a substitute image.
4. Auto-split panels whose height/width exceeds a configurable threshold (initial candidate `max_panel_aspect_h_over_w=2.4`); output overlapping vertical crops with bounded target shot aspect ratio `0.65 <= width/height <= 2.0`, preserving reading order. Classify blank/chat-only bands as `omit_visual` without deleting original pixels or OCR evidence.
5. `VisualPlan` stores `source_box` normalized coords and `focus_regions`; suppress OCR/speech-bubble bounding boxes during visual focus selection. Prefer face/action/entity features, then safe geometry crop with review flag.
6. Motion modes allowed in V4: `static`, `slow_pan_down`, optional static focus (not zoom progression). All movements must satisfy `y(t+dt) >= y(t)`; never `pan_up`, reverse zoom, horizontal shake or random offsets. Apply smooth position easing (e.g., smoothstep), no per-frame rounding oscillations; end frame constrained within image. For a single shot the normalized vertical travel per second is capped by `max_scroll_fraction_per_second` (initial 0.12, to be tuned by editor). Expand shot count or use holds to fit audio duration rather than increase velocity.
7. Allocate durations from actual WAV audio; no arbitrary duration change to force a longer pan. Add shot-coverage and text-mask overlays to Human Review Pack.
8. Make local/cloud parity tests compare initial/mid/final frames and focus boxes and test monotonic Y at every frame for tall and square panels.

### Acceptance
- No upward frame-to-frame displacement; zero jitter spikes; no crop focused on OCR-only area.
- Tall panels visually decomposed, readable, in top-to-bottom order; original panels/script still intact.
- Test OCR-only, tall single panel, portrait crop, split/merged gutter, panel reuse and multi-panel segments.
- No audio timeline drift > existing acceptance budget; no missing referenced image.

## PR-03: ONE KAGGLE SESSION, DUAL GPU SPECIALIZATION AND MULTI-CHAPTER JOB

### Existing
`src/cloud/kaggle_service.py` packages one chapter at a time, generates distinct `tts`, `render` or `produce` worker scripts, and has `infer_batch(..., batch_size=32)` plus threaded Pillow/FFmpeg/NVENC rendering. `src/production/runner.py` already has a multi-chapter state machine and persistent TTS engine, but it is primarily a local/runner workflow, not the single multi-chapter Kaggle dataset + one kernel + one download flow. Current cloud render repeated Pillow compositing and has independent motion math.

### Change
1. `manga-recap cloud produce-series --project <slug> --chapters 1-N --json` creates ONE versioned series payload from scripts, selected panels, canonical VisualPlans, assets, config and model options. Upload once using a uniquely named dataset version; capture hash manifest.
2. Preview gate: Stage A processes **chapter 1 preview ONLY**. If rejected, fix and repeat preview. After explicit approval, Stage B includes chapter 1 final **and chapters 2..N** in one upload, one Kaggle kernel session, one downloaded bundle. The engine must reject Stage B without chapter-1 approved hash and config fingerprint.
3. Worker process 0: VieNeu-TTS on `CUDA_VISIBLE_DEVICES=0`; one model instance; group utterances by voice/profile, bucket by input length, dynamic microbatch (begin benchmark 8/16/32/64; no hard-coded optimum), write signed WAV/metrics checkpoints. Adaptive batch-size backoff on OOM, do not drop segments.
4. Worker process 1: Render on `CUDA_VISIBLE_DEVICES=1`; actual GPU binding for CUDA/NVENC (device index must be verified after mapping), hardware encoding, GPU scaling/compositing when faster; FFmpeg command must explicitly select the GPU. CPU decompression, subtitle composition and dataset I/O may remain bottlenecks: profile rather than assume GPU2 makes them free.
5. True pipeline overlap: TTS produces chapter audio assets + readiness manifest; Render consumes only fully verified chapter audio while TTS advances to the next chapter. Use bounded process-safe queue, disk/VRAM backpressure and resumable chapter completion.
6. Pipelined ordering: first render chapter 1 final after its audio; after that, overlap TTS(n+1) with render(n). Track CPU/GPU utilization, CUDA memory reserved/allocated, NVENC throughput, PCIe copies, cache misses, per-stage duration.
7. Distinct work queues and process workers (no sharing one CUDA model between processes). GPU hardware must be probed and validated. If Kaggle provides fewer than 2 GPUs, explicitly fail or use user-approved fallback, never silently pretend dual GPU.
8. Cache across reruns by content fingerprint. Final bundle includes each `chapter_N.mp4`, WAV if archival policy requests, `script.json`, SRT, timing, thumbnails/metadata, manifest + checksums. Download once after all target chapters completed; explicit partial-recovery path for failed worker.
9. Compare three modes on identical scripts: current single-GPU cloud, dual-GPU staged, and dual-GPU overlapped. Include total wall clock from upload initiation to local verified output; measure cold start and Kaggle queue time separately.

### Acceptance
- Chapter 1 preview requires human approval before series final job.
- Only one payload upload, one production kernel, one result bundle download in happy path, even for many chapters.
- Worker assignment proven using GPU telemetry; no TTS and Render accidentally sharing device 0.
- Bitstream/audio integrity, subtitle coverage, no stale approval, no silent skipped chapter.
- Target material total wall-clock speedup >=1.5x on representative >=5-chapter workload **as a benchmark target, not an unsupported promise**; log when hardware bottleneck prevents it.

## PR-04: HUGGING FACE PRIVATE ARCHIVE + SAFE LOCAL CLEANUP

### Existing
`src/cli/youtube_cmd.py` returns `video_id`, `video_url`, `captions_uploaded`, `thumbnail_uploaded` on successful `videos.insert`; caption and thumbnail errors are logged as warnings. `src/cli/clean_cmd.py` removes intermediate artifacts, but there is no archive lifecycle with HF receipt / restore verification. YouTube success means upload response received, not necessarily public processing status or complete metadata/captions.

### Change
1. Create `src/archive/hf_archive.py` using `huggingface_hub.HfApi` with `HF_TOKEN` (write permissions) and `HF_REPO_ID`; create or use **one PRIVATE dataset repo**, e.g. `<account>/manga-recap-archive`. Use `repo_type="dataset"`. Do not store token, cookies or YouTube OAuth files in the archive.
2. Namespace all series: `series/<slug>/project.json`, `series/<slug>/characters.json`, `series/<slug>/story_summary.md`, `series/<slug>/chapters/<n>/{raw,panels,contact_sheets,script.json,manifest.json,audio,subtitles,video,thumbnail,youtube_metadata.json}`, `series/<slug>/archive_manifest.json`. Include original sources only if content licensing/storage rights allow.
3. Generate a SHA-256 manifest from the exact whitelist of required archive assets, record byte count, SHA and logical role, plus selected YouTube account/channel, video IDs and links. Avoid uploading `.env`, credentials, OAuth tokens, Kaggle access token, logs containing secrets, private cache or stale temp files.
4. `manga-recap archive push --project <slug> --json`: upload via `HfApi.upload_folder`; enable resumable/chunked transport as offered by installed `huggingface_hub`. Store repo SHA and commit in immutable local archive receipt.
5. `manga-recap archive verify --project <slug> --json`: enumerate expected paths from the remote at the committed revision, download or stream verification of every required artifact and independently SHA-256 compare. Remote existence/size alone is NOT sufficient for destructive cleanup. Record verified set, revision SHA, timestamp.
6. `manga-recap youtube verify --project <slug> --chapter <n> --json`: re-query YouTube `videos.list` for correct video ID and selected channel after upload, validate any user-required captions and thumbnail operations separately. Private/unlisted videos are valid if explicitly selected. Failed caption/thumbnail must block cleanup when those features were requested.
7. `manga-recap archive finalize --project <slug> --dry-run --json`: fail closed until all expected chapters/videos, HF verified receipt, YouTube verified receipt, and human approval of published assets. Destructive mode requires explicit user-approved archiving/cleanup policy and a `--confirm-project <slug>` argument. Clean ONLY scoped project assets, not credentials, unrelated projects, account registries, or manifests needed for recovery. Keep a small local tombstone with HF repo/revision and YouTube IDs (no content payload).
8. `manga-recap archive restore --project <slug> --revision <sha> --json`: round-trip verified restore; test before permitting destructive cleanup in production. Failed restore => cleanup blocked.
9. Account for Hugging Face quota, copyright, access controls and storage costs. Avoid claiming a private repo establishes copyright entitlement.

### Acceptance
- Permission errors, upload interruption, checksum mismatch, incomplete set, or YouTube partial failures prevent deletion.
- Full archive can be restored from HF to an empty local workspace and passes `validate`/media integrity.
- Recovery receipt survives local project cleanup; idempotent repeated push and finalize.
- Secret fixture files are never present in archive; directory traversal and symlink escape are rejected.

## Dependency and PR sequencing

`PR-01` strict user answers -> `PR-02` canonical VisualPlan/local-cloud parity -> `PR-03` dual-T4 single-session cloud batch -> `PR-04` archive+verify+restore+safe cleanup -> `PR-05` E2E production acceptance & documentation sync.

PR-02 and PR-04 core utilities can be developed in parallel once schemas are frozen; PR-03 depends on PR-02 canonical frame semantics. PR-04 delete mode remains disabled until full E2E success.

## Benchmark report specification

Capture: Git SHA, corpus/chapter counts, number of panels and utterances, output video duration, codec/resolution/FPS, Kaggle hardware count, GPU0/GPU1 assigned tasks, peak VRAM per GPU, TTS segments/s, Render video FPS, job queue/cold boot time, actual upload and download time, cache-hit ratio, failed or retried chapters, quality review outcome, and normalized total time per finished video-minute.

No benchmark claim without reproducible command, captured telemetry and fixed sample.

## Release Definition of Done

- `pytest` all existing suites plus new contract/adversarial/E2E suites, no regressions.
- Two independent actual-human demonstrations: 10/10 form and chapter-1 preview/thumbnail/channel approvals.
- Identical video/caption/audio correctness across local and Kaggle.
- Dual GPU evidence from the same worker session, actual measured speedup versus existing worker.
- HF private archive round-trip and YouTube verified receipt; cleanup default dry run; destructive integration manually reviewed.
