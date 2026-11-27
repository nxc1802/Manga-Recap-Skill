# V7.1 — End-to-End Production Readiness Plan

**Repository:** nxc1802/Manga-Recap-Skill  
**Baseline:** `4bd550eef9f3405c3f6cd250337edb736a8bfc4f` (V7 main)  
**Scope:** Production acceptance of the existing Skill; correctness fixes only.  
**Status:** Plan approved for handoff; execution and production certification pending.

References: [Approved V7 scope](V7_CORE_ONLY_IMPLEMENTATION_PLAN.md), [V7 verification report](verification/v7_core_only_2026-11-27/v7_core_only_report.md), [Skill](../.agents/skills/manga-recap/SKILL.md).

## 1. Mission and non-negotiable boundaries

Run the actual Manga Recap Skill from confirmed setup through real final media and authorized release operations. Verify each stage using observable outputs, not merely successful exit codes. Fix reproducible correctness defects and re-run the affected stage. Establish whether the current release candidate is ready for official operation.

V7 reported 555 passing tests, 93 runtime Python files, 998,175 bytes and 21,507 non-blank LOC. Live Gemini Decision, Kaggle Dual-T4, HF and YouTube verification was NOT RUN. These are baseline report claims, not evidence of a new V7.1 pass.

**This is not a feature or refactoring project.** All 66 V7 decisions remain authoritative. Do not introduce new fallback, alternate execution paths, models, policies, services, general-purpose frameworks, speculative optimizations or unnecessary abstraction. Do not revive retired features. No broad code movement or LOC targets. Correct an existing function or consolidate an existing duplicate only when necessary to fix a demonstrated defect. Tests, fixtures and evidence documentation may grow; every runtime addition must be justified by a concrete defect. Use the smallest coherent patch.

For each bug: reproduce → record expected/actual and root cause → add focused regression test → fix the existing path → rerun that test and affected Skill stage → verify downstream artifacts/approval validity. Do not paper over errors, skip gates, weaken tests or introduce catch-all recovery. Stop with an actionable error for unsupported conditions.

### Execution authorization and test isolation

- Use fresh, isolated acceptance projects and authorized manga pages/assets, never overwrite existing user series or credentials. Record exact baseline SHA, final tested SHA, dependencies, OS, hardware and fixture hashes. Use real native VieNeu-TTS and FFmpeg for media acceptance; mocks only for tests/fault injection.
- Ask for explicit setup answers using the host's structured question mechanism. Never impersonate the reviewer or fabricate approval. Stop at each required human gate until actually approved. Automated negative fixtures can use test-only synthetic consent records.
- Obtain explicit authorization for actual Gemini requests, Kaggle quota-consuming jobs, HF archive writes, YouTube uploads and destructive operations. Agree on available budget/quota, target account, permitted assets and privacy before execution. Do not rotate accounts or evade limits.
- Prefer private, disposable test resources and separately authorized cleanup; retain evidence needed for verification. Never publish copyrighted or private materials without permission. Redact secrets and private source content from tracked reports; keep media evidence locally or in explicitly authorized private storage.
- Missing access, hardware, quota, reviewer approval or evidence is BLOCKED / NOT RUN, not PASS. Continue independent permitted tests, but do not claim complete readiness.

## 2. Acceptance workload and stage order

Use two independently confirmed projects for Local and Kaggle, ideally with equivalent source/script fixtures where comparisons require them. Local: 2–3 chapters. Kaggle: 2-chapter historical comparison workload, representative 5-chapter series, and 10–20-chapter long series with multiple chunks and resume. Reuse authorized completed artifacts when valid and combine runs where possible to avoid redundant spending. The historical benchmark is comparable only with identical inputs/settings/hardware; if unavailable, report non-comparability and establish a new measured baseline.

Cover both supported ingestion modes (site profile and local import), representative 720p/1080p, subtitle burn on/off and BGM/no BGM across the test set. Cover retained local encoders on available corresponding hardware; report any missing hardware explicitly. Do not build a new runtime testing framework.

| Phase | Skill stage / purpose | Main evidence |
|---|---|---|
| P0 | Baseline and known correctness fixes | Reproduction tests and clean preflight |
| P1 | Stage 1: Series Setup | Confirmed settings and voice |
| P2 | Stage 2: Prepare Chapter 1 | Raw images, manifest, Decision quality |
| P3 | Stage 3: Author Chapter 1 | Valid script and independent content review |
| P4 | Stage 4: Preview | Real Local and Kaggle MP4/SRT, MD/JSON |
| P5 | Stage 5: Human Approval | Exact approval and negative gate tests |
| P6 | Stage 6: Prepare/author remaining chapters | All evidence/scripts and continuity |
| P7 | Stage 7: Full Production | Verified chapter/feature media and live benchmark |
| P8 | Stage 8: Package/release; separate explicit cleanup | Upload/archive receipts and safe cleanup |
| P9 | Final candidate regression and release gate | Auditable go/no-go decision |

Follow the real Skill CLI, not hand-crafted artifacts used to bypass it. Reuse stages 2–3 for remaining chapters. Commands below are examples; inspect current CLI help on the tested commit. Resolve discrepancies through minimal correctness/documentation fixes, not additional aliases.

## P0 — Baseline & Known Correctness Fixes

Capture actual starting tests, git status, runtime size and version. Preserve prior evidence. The following issues from V7 source review must have focused reproduction/verification before expensive live runs. If a suspected issue is not reproducible, document evidence; do not add speculative logic.

| ID | Finding at baseline | Required correction / regression |
|---|---|---|
| BUG-01 (P0) | [cloud/jobs.py:64–73](../src/cloud/jobs.py#L64-L73): state-write failures swallowed | Required reservation/intent write failures propagate; no remote submit after failed persistence. Inject disk/permission/write failure. |
| BUG-02 (P0) | [cloud/lifecycle.py:41–60](../src/cloud/lifecycle.py#L41-L60), [152–192](../src/cloud/lifecycle.py#L152-L192): unrecognized remote status may become non-active | Unknown/ambiguous/timeout statuses remain unresolved; only confirmed terminal failure permits retry, remote complete must sync. Test unknown, unfamiliar enum, timeout, complete-unsynced and concurrent overlapping submissions. |
| BUG-03 (P1) | [core/workflow.py:1749–1768](../src/core/workflow.py#L1749-L1768): canonical produce bypasses retained chunk planner | Route canonical Kaggle produce through existing automatic chunk planner; one small chunk or multiple as required, verified receipts and resume. No second production workflow. |
| BUG-04 (P1) | [cli/main.py:290–301](../src/cli/main.py#L290-L301), [core/workflow.py:1434–1468](../src/core/workflow.py#L1434-L1468): backend override/default conflicts with Q02 | Preview/produce use confirmed backend; remove conflicting overrides, preserve explicit granular repair under same gates. Test both directions. |
| BUG-05 (P1) | [ingest/decision.py:1115–1116](../src/ingest/decision.py#L1115-L1116), [1436–1449](../src/ingest/decision.py#L1436-L1449): cache-resume key resolution only on Pass 1 miss | Resolve API key when an actual split/merge/Pass 2 request is needed; complete warm cache must require no request. Test Pass 1 fully cached with later cache miss. |
| BUG-06 (investigate) | [core/panel_repack.py:189–210](../src/core/panel_repack.py#L189-L210): metadata-only repack may overwrite lineage | Reproduce repeated prepare/repack and shifted child panels; preserve verified lineage and cache identity. Fix only if incorrect. |
| BUG-07 | Skill has six stages, omits explicit remaining-chapter preparation, mixes optional archive/cleanup; publish example has unsupported --wait | Align documentation with eight agreed stages and working CLI. Remove unsupported flag from docs rather than inventing it. Explicit optional archive/cleanup. |
| BUG-08 | CLI reports 5.0.0 while Skill reports 7.0.0 | Align existing version declarations for release candidate; do not change persisted schema version gratuitously. |

Retain previously repaired panel lineage/cache integrity, pinned account, strict Dual-T4, verified receipts, approvals and atomic writes. Check ambiguous HTTP/empty submit results and cancellation reconciliation in existing tests. Never interpret a failed status probe as proof of remote termination.

Commands:
```bash
python -m compileall -q src tests scripts
python scripts/sync_requirements.py --check
python -m pytest -q
manga-recap --version
manga-recap --help
```

**PASS:** All confirmed P0/P1 correctness defects fixed, focused regression tests and baseline suite pass, no new fallback/policy/framework. Suspected defects resolved with evidence or explicitly documented as unresolved blockers.

## P1 — Series Setup Verification (Stage 1)

Execute the actual eight-question user setup (Q01–Q05, Q08–Q10) and record approved narrator. Confirm source, chapters, backend, voice, speed, subtitle burn, destination/privacy, quality and BGM/gains. For Kaggle use a configured pinned account and project-relative BGM path. Keep one canonical configuration, no per-stage overrides.

```bash
manga-recap setup record --project <slug> --answers-json <path> --json
manga-recap status --project <slug> --json
```

| Test ID | Expected |
|---|---|
| SET-01 | All eight explicitly confirmed answers → valid setup |
| SET-02 | Missing/unconfirmed answer → stop before processing |
| SET-03 | Q02 Local → Local preview and produce |
| SET-04 | Q02 Kaggle → Kaggle preview and produce |
| SET-05 | Approved single voice and speed applied exactly |
| SET-06 | BGM/gains and subtitle burn match Q05/Q10 |
| SET-07 | 720p/1080p matches Q09 |
| SET-08 | Relevant config change invalidates previous approval |

Inspect `series_setup_answers.json`, `project.json`, `voice_selection.json`; compare recorded settings and actual backend/media metadata during later stages. Do not mutate the approved configuration to switch backend mid-series.

**PASS:** Explicit user provenance and correct validated setup; all downstream stages inherit settings; no bypass or redundant overrides.

## P2 — Data Preparation & Gemini Decision (Stage 2)

Run the actual prepare command on authorized real pages for Chapter 1.

```bash
manga-recap prepare --project <slug> --chapter 1 --json
manga-recap status --project <slug> --chapter 1 --json
```

Ingest via supported profile/local import → verify raw order → VCS → closed-set Gemini Decision (six questions, fixed 4-sided adaptive crop) → verified split/merge → child-only Pass 2 → contact sheets/manifest. Preserve original raw evidence and current-image integrity. Keep only canonical algorithm. Manual repack/inspect is an explicit repair action, never a missing-API fallback.

| Test ID | Verification |
|---|---|
| PRE-01 | Supported website profile crawl produces ordered complete raw images |
| PRE-02 | Local import preserves source/order |
| PRE-03 | Page/panel sequence and file hashes correct |
| PRE-04 | VCS segmentation preserves important content |
| PRE-05 | Active / Context Only / Skip classification quality |
| PRE-06 | Split boundaries correct; no unwanted split of tall single scene |
| PRE-07 | Semantic merge correct and contiguous |
| PRE-08 | 4-sided crop retains required story art/dialogue |
| PRE-09 | Pass 2 uses actual child image, exact lineage/hash; renumbered KEEP/DROP correct |
| PRE-10 | Complete unchanged warm cache → zero actual API requests |
| PRE-11 | Interrupted later phase resumes correctly from validated cache |
| PRE-12 | Explicit manual repack/inspect works, no bypass |
| PRE-13 | Unsupported site/missing API/invalid evidence → clear failure |

### Real quality evidence

Use an independently reviewed sample of approximately 150–300 panels across chapters, including difficult crop, tall single-panel, multi-frame, merge, text-only and non-story cases. Select sample and label original source before inspecting final Decision outputs; report denominators, uncertainty handling and any missing categories. Use existing historical corpus if authorized/available, not a new dataset construction project. Store minimal allowed evidence.

Report classification confusion matrix and macro F1, needs_review counts, split/merge precision/recall or correct/incorrect counts with denominators, original vs final crop comparisons, story-to-skip errors, severe clips, exact lineage/hash checks, request counts by phase, tokens and wall time. Inspect all severe/uncertain cases manually. Compare with V6 on the identical labeled set where possible; if no comparable independently labeled baseline exists, state that limitation and report absolute quality rather than calling agreement accuracy.

Proposed acceptance thresholds: zero story-to-skip errors and catastrophic crop/story loss on reviewed sample; zero serious split/merge losses; 100% verified lineage/cache integrity on tested cases; zero warm-cache requests for complete unchanged evidence; no meaningful classification/token regression on comparable workload. Report sample limits rather than universal guarantees. Resolve required needs_review panels before authoring/rendering. Include original/final crop/rendered-frame examples via permitted private evidence.

**PASS:** Real Gemini run and manual quality review pass, correct final manifest/contact sheets, no script generated by prepare, next action author_script only when ready.

## P3 — Script Generation (Stage 3)

Coding Agent must follow actual Skill visual/narration/character references. Inspect contact sheets and high-resolution panels (batches of five with two-panel overlap); use canonical single narrator and flexible N:M evidence mapping. No narration from filenames or metadata alone. Read/update `characters.json` and `story_summary.md`, save canonical `script.json` atomically. Unclear evidence gets needs_review and [CAN_XAC_MINH: reason], not fabricated text.

```bash
manga-recap validate --project <slug> --chapter 1 --pacing --json
```

| Test ID | Verification |
|---|---|
| SCR-01 | Contact sheets and actual high-resolution evidence inspected |
| SCR-02 | Essential plot and dialogue preserved |
| SCR-03 | Characters consistent |
| SCR-04 | Natural Vietnamese narrative |
| SCR-05 | Single approved narrator |
| SCR-06 | Valid panel/evidence mapping and ordering |
| SCR-07 | No invented event/dialogue |
| SCR-08 | Character and story continuity records updated |
| SCR-09 | Ambiguity marked and resolved before production |

Independent human review against original source: factual accuracy /30, essential plot coverage /25, natural Vietnamese /20, character consistency /15, pacing/transitions /10. Proposed pass >=85/100 per chapter, no serious factual/coverage errors. Document reviewer, sample and corrections; Agent self-review alone is not acceptance.

**PASS:** Schema, evidence, continuity and human content review all pass. No automatic visual substitution.

## P4 — Chapter 1 Preview (Stage 4)

Create real previews for both independently configured Local and Kaggle projects. Use Q02 automatically after BUG-04 fix; use actual VieNeu-TTS and FFmpeg. Do not create dummy media or tune preview speed unnecessarily.

```bash
manga-recap preview --project <slug> --chapter 1 --json
```

Artifacts: `chapters/1/video/preview.mp4`, `chapters/1/subtitles/preview.srt`, review_pack.md and review_pack.json (verify actual canonical locations), visual plan, audio and approval-relevant metadata.

| Check | Acceptance |
|---|---|
| Resolution/aspect | Exact Q09 target, 16:9 |
| Codecs/FPS | H.264/AAC, configured FPS |
| Decode | Full FFmpeg decode succeeds with no errors |
| Audio/video | Durations and synchronization agree; no truncation or unintelligible gaps |
| Voice | Approved VieNeu narrator, correct speed, intelligible Vietnamese pronunciation |
| BGM/gains | Match confirmed configuration, no masking/clipping |
| Subtitles | Accurate text, monotonic timestamps, no out-of-range cues, burn matches Q05 |
| Framing/motion | Visual plan, static/slow_pan_down/static_focus as supported; no important content cut |
| Review artifacts | MD + JSON, correct hashes, no stale report |

Use ffprobe JSON and full decode (e.g. `ffmpeg -v error -xerror -i <video> -f null -`); check exit code. Listen and watch the entire Chapter 1 preview with source evidence. Record actual observations and defects, not just probe results. Technical tolerance for A/V duration differences should reflect frame/audio packet rounding; record actual measured values and agreed tolerance, no audible/visible drift.

**PASS:** Both real backend previews have verified technical and human-perceived quality, ready for review. Preview performance is secondary.

## P5 — Human Approval (Stage 5)

Present the actual preview MP4, SRT and review evidence. Explicitly stop and obtain human approval; never self-approve. Record exact fingerprints supplied by review pack and actual reviewer.

```bash
manga-recap approve --project <slug> --reviewer <reviewer> --config-fingerprint <sha256> --artifact-hash <sha256> --json
```

| Test ID | Expected |
|---|---|
| APR-01 | No approval → produce/compile/publish and other protected production routes blocked |
| APR-02 | Exact valid approval → production permitted |
| APR-03 | Script/manifest/visual plan change → stale approval |
| APR-04 | Preview MP4 bytes change → stale approval |
| APR-05 | Voice/settings change → stale approval |
| APR-06 | Chapter 1 final promotion → exact approved MP4 SHA-256 and associated verified SRT |

Perform negative mutations on copied disposable fixtures, not approved production artifacts. Test retained granular commands cannot bypass gates. If target quality or relevant artifact changes, require re-review, never silently upscale/re-encode approved bytes.

**PASS:** User truly approves both required real previews; negative cases fail closed; Chapter 1 reuse later verified.

## P6 — Prepare & Author Remaining Chapters (Stage 6)

After approval, repeat P2 and P3 for remaining requested chapters with identical confirmed settings. No new algorithm, orchestration layer or alternate authoring route. Verify chapter ordering, character/story continuity, no duplicated or missing important plot, valid scripts, resolved required evidence, no unnecessary processing of valid unchanged assets. Explicitly inspect per-chapter status, validation and manifest/hash evidence before compute.

**PASS:** Every requested chapter ready; chapter count/order equals confirmed selection; scripts and review records complete.

## P7 — Full Production (Stage 7)

### P7.1 — Local

Run canonical `produce --compile` on the approved 2–3-chapter Local test project. Use actual native speech, FFmpeg and supported local encoder. Test retained encoder paths where actual hardware exists; use focused offline tests for other conditions and report live gaps. Verify chapter media, approved Chapter 1 exact-byte reuse, compiled feature MP4/SRT, master WAV and output exports. Listen/watch all short-series output.

```bash
manga-recap produce --project <slug> --chapters <range> --compile --json
manga-recap status --project <slug> --json
```

### P7.2 — Kaggle live

Execute same canonical produce command on authorized Kaggle project. Automatic chunk planning is part of main workflow (BUG-03). Require two actual Tesla T4 CUDA devices and dual usable NVENC for render/produce. Policy B only: both GPUs TTS and shared encode, batch size 16 for produce, 32 for standalone TTS, CPU finalizer. Retain existing native batching, bounded queues, OOM backoff in same model/device, verified receipts and resume. No Policy A, CPU/single GPU, account rotation, new fallback or scheduler redesign.

| Workload | Purpose |
|---|---|
| 2-chapter frozen historical workload if available | Compare V6 worker time/output |
| Representative 5-chapter series | Typical production |
| 10–20 chapters | Long-series throughput and multiple chunks/resume |
| Standalone remote TTS | Real dual CUDA and bs32 |
| Standalone remote render | Real dual NVENC, no TTS initialization |

Use authorized quota efficiently, combine overlapping workload evidence when justified. Ensure actual multi-chunk behavior observed; if natural workload fits one chunk, exercise retained chunk planner with existing supported chunk limits in isolated acceptance test, not new production options. Verify completed chunks are skipped and new chunks alone submitted. Do not intentionally damage live jobs. Use existing `cloud reconcile` / `cloud pull` for legitimate resume; record remote job ID and original account safely.

### P7.3 — Performance evidence

| Metric | Historical V6 comparison | Candidate actual |
|---|---|---|
| Hardware | 2× T4 | Measure |
| Segments / shots | 71 / 167 | Same workload for comparison |
| Produce batch size | 16 | Verify |
| Worker makespan | 179.04 s | Measure |
| End-to-end time | No complete comparable baseline | Measure |
| GPU quota consumed | 245.89 s | Measure |
| GPU/NVENC utilization, memory | Historical logs if available | Measure using existing telemetry/devtools |
| OOM / failed tasks | 0 reported | Measure |

Record packaging, upload, queue, bootstrap/model init, TTS, shot preparation, encode, CPU finalization, download/sync, compile and total durations where available from existing telemetry or external timestamps. Record output duration, codecs/resolution, GPU roles and batch sizes. Distinguish worker time from total elapsed and quota. Prefer three comparable trials when quota allows; report median/range/sample count, cold/warm condition and any non-comparability. Historical V6 result was a single run. Aim for no meaningful regression (provisional 5% comparable median tolerance per V7 plan, investigate deviations); do not add profiling infrastructure or speculative performance optimizations. Simulations are not live evidence.

### P7.4 — Failure and integrity regression tests

| Case | Required outcome |
|---|---|
| Missing GPU / NVENC | Fail clearly; no fallback |
| Quota exhausted | Report, no account change |
| Queue timeout / client disconnect | Durable identity and explicit reconcile/pull |
| Unknown submission / remote state | No duplicate submit |
| Reservation write failure | No submit |
| Overlapping series/chapter dispatch | No duplicate |
| Interrupted chunk | Preserve verified prior results, resume pending only |
| Output checksum mismatch | No completion receipt |
| Completed-unsynced remote job | Sync/verify, not resubmit |

Use fault injection for rare or destructive failures. Retain fail-closed semantics and original account. Verify no accidental live job on negative mocked tests.

**PASS:** Real Local and Dual-T4 preview-to-production workflows produce complete verified outputs, both standalone cloud modes work, actual multi-chunk/long-series behavior and safe resume evidenced, performance assessed honestly. No serious media defect; no missing/duplicate chapters. If quota/hardware prevents live verification, this gate is not PASS.

## P8 — Packaging & Release (Stage 8), Explicit Cleanup

### P8.1 — Final product

Verify every chapter and compiled MP4 decodes fully, output chapter sequence complete and correct, duration consistent with narration/compile, SRT timestamps monotonic and within bounds, master WAV valid/synchronized, 16:9 thumbnail truthful and clean, Vietnamese title/description/timestamps accurate. Review all short-series output and chapter boundaries plus representative beginning/middle/end and reported anomalies of long series. Verify each long-series chapter individually for technical integrity. Human acceptance must cover delivered content; record reviewed ranges and limitations.

### P8.2 — YouTube live

Only after separate explicit authorization, reconfirm account/channel and visibility. Use authorized test content and private visibility; if confirmed setup conflicts, use separate confirmed test project rather than override. Test both single-chapter and compiled feature uploads.

```bash
manga-recap publish --project <slug> --chapter <N-or-feature> --action metadata --json
manga-recap youtube accounts --json
manga-recap publish --project <slug> --chapter <N-or-feature> --account <alias> --privacy private --json
manga-recap publish --project <slug> --chapter <N-or-feature> --action verify --json
```

Use current CLI help for required verification parameters; do not add deprecated/unsupported --wait.

| Test ID | Expected |
|---|---|
| YT-01 | Authenticated intended channel/account |
| YT-02 | Successful private video upload |
| YT-03 | Correct thumbnail |
| YT-04 | Correct Vietnamese captions |
| YT-05 | Title, description, timestamps, visibility verified |
| YT-06 | Correct video ID and durable receipt |
| YT-07 | Re-run/resume does not duplicate publication |

Check provider-reported processing/metadata, not merely local upload exit code. Test duplicate protection with existing receipt and fault injection where needed. Do not make extra public uploads. Preserve evidence and only delete authorized test uploads if separately requested.

### P8.3 — Hugging Face live

With explicit permission, push to approved private dataset, verify pinned revision and checksums, restore with retained standalone utility into a new empty test directory.

```bash
manga-recap archive push --project <slug> --json
manga-recap archive verify --project <slug> --json
python scripts/restore_archive.py --help
```

| Test ID | Expected |
|---|---|
| HF-01 | Actual archive push succeeds |
| HF-02 | Repository verified private |
| HF-03 | Archive manifest includes all intended allowed files |
| HF-04 | Exact remote revision recorded |
| HF-05 | Every archived file checksum verified |
| HF-06 | Standalone restore to empty directory succeeds |
| HF-07 | Restored files match manifest hashes |

Use actual restore utility arguments from help. Do not archive secrets or unrelated data; verify allowlist. No runtime restore path reintroduced.

### P8.4 — Cleanup (separate explicit action)

```bash
manga-recap clean --project <slug> --dry-run --json
```

Verify dry-run changes no files, wrong/missing confirmation blocks deletion, raw purge without verified receipt for current files fails, unrelated paths are protected. Test real deletion only on explicitly authorized disposable copy with current verified archive and correct project confirmation; compare planned and actual file list/hashes, ensure retained media and restore evidence remain valid. Never delete user original data as part of this task. No automatic raw cleanup.

**PASS:** Actual authorized uploads, archive and restore verified, no duplicates, final product accepted, cleanup safe. Missing authorization is BLOCKED, not simulated PASS.

## P9 — Final regression and release gate

Freeze candidate commit. Rerun full test suite and static checks on clean environment using documented dependencies. Re-run affected stages after any correctness patch, including fresh human review if approved evidence changed. Confirm live evidence is tied to candidate or explicitly demonstrate relevant code/input unchanged and rerun affected live path when authorized. Do not carry forward incompatible results.

Mandatory readiness checklist (all must have evidence):
- [ ] No unresolved P0/P1 correctness issues; regression tests pass.
- [ ] All retained V7 scope and CLI gates preserved; retired paths remain absent.
- [ ] Actual Setup and Human Approval verified.
- [ ] Gemini Decision live and manual quality review pass.
- [ ] All required script reviews pass.
- [ ] Real Local preview/full production pass.
- [ ] Real Kaggle preview/full production and standalone modes pass.
- [ ] Long series, multi-chunk behavior and verified resume pass.
- [ ] Live YouTube chapter and feature verification pass.
- [ ] Live private HF push/verify and standalone restore pass.
- [ ] Explicit cleanup safety tests pass.
- [ ] No fallback/new policy/feature/unnecessary abstraction; every runtime diff justified.
- [ ] Skill, README, CLI and version aligned with actual workflow.

A passing unit suite alone is insufficient. Mark **READY** only if every mandatory gate passes on the release candidate, user quality approval obtained, and no unresolved material correctness or verification blocker remains. Otherwise mark **NOT READY**, list exact blocker/affected scope and evidence required. Do not promise absolute absence of future bugs or bypass user authorization. No automatic deployment/release tag, external publication or destructive action beyond authorization.

## 3. Evidence and final report

Suggested report directory (documentation only, no new runtime modules):

```text
docs/verification/v7_1_production/
├── PRODUCTION_READINESS_REPORT.md
├── test_results.json
├── decision_quality_report.md
├── media_quality_report.md
└── kaggle_benchmark_report.md
```

For each test record test ID, phase, commit, input fixture/hash, command, environment, expected, actual, evidence reference, reviewer where applicable, duration, and PASS / FAIL / BLOCKED / NOT RUN. Explicitly identify live vs mock/simulation and sample coverage. Keep private images/videos out of Git; link authorized private evidence or record local artifact paths and hashes without exposing credentials.

Final report must include:
- Phase-by-phase results, exact commands and evidence, acceptance matrix and READY / NOT READY decision.
- Known defect reproductions, root causes, minimal fixes, regression tests and commits; unchanged suspected issues and reasons.
- Actual media outputs and review observations; Decision labels/metrics and limitations; live Kaggle metrics, quota and comparison caveats.
- External receipt verification, approval record references, retained/unsupported hardware and operations.
- Runtime bytes, LOC, file count before/after, added/deleted files and why every runtime addition was necessary; no target LOC or broad refactor.
- Remaining blockers and non-destructive rollback instructions. Preserve existing user data and history.

## 4. Handoff instruction

Read this plan and the approved V7 scope. Execute every real Skill stage in order, stopping at genuine human/authorization gates. Verify actual results with evidence. Fix only reproducible correctness defects through minimal changes to existing code. Do not introduce fallback, additional branches, features, new policies, unnecessary abstraction or speculative optimization. Preserve the V7 scope and tested fast paths. Commit coherent bugfixes and tests, record truthful verification results and declare Production Ready only after all mandatory gates pass on the final candidate.
