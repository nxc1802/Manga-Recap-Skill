# Manga Recap Skill — V6 Refactor Plan

**Repository:** `nxc1802/Manga-Recap-Skill`  
**Audit baseline:** `main` tại commit `f242338af23cfe47c7bfa3b4924ebdbc5be2c455` (21/11/2026)  
**Phạm vi:** Refactor kiến trúc, workflow Skill, integrations, cấu hình, CLI và tests; không thay đổi mục tiêu sản phẩm.  
**Nguyên tắc:** Behavior-preserving refactor, ưu tiên đơn giản hóa thực sự thay vì chỉ di chuyển code.

> Baseline trên là snapshot đã được audit. Trước khi triển khai, kiểm tra HEAD hiện tại, working tree và các thay đổi mới. Không ghi đè công việc của người dùng. Kế hoạch này chưa được triển khai hay kiểm thử trên repo.

## 1. Objective

Đưa dự án về một kiến trúc nhỏ gọn, rõ trách nhiệm, dễ bảo trì và dễ điều khiển bằng coding agent. Giữ đầy đủ các tính năng đang sử dụng, loại bỏ duplication, legacy leakage và những abstraction không còn giá trị.

- Một canonical workflow, một nguồn quy định cho mỗi invariant.
- Skill ngắn, rõ, đủ chi tiết để agent thực hiện đúng và resume được.
- CLI mỏng, không chứa business logic hoặc provider transport.
- Local/Kaggle dùng chung production pipeline và artifact contracts.
- Decision API, Kaggle, Hugging Face, YouTube có boundary rõ, credentials riêng, lỗi có cấu trúc.
- Một nguồn khai báo dependency; một nguồn authoritative cho từng loại configuration.
- Giữ hoặc cải thiện correctness, chất lượng media, hiệu suất và khả năng recovery.
- Không thêm framework, database, queue service, generic plugin system hoặc tầng abstraction nếu không giải quyết vấn đề hiện hữu.

## 2. Baseline & technical debt

Audit snapshot có 74 file Python trong `src/`, tổng kích thước khoảng 864 KB (kích thước source, không phải LOC). Các điểm nóng đã quan sát:

| Area | Existing files | Refactor concern |
|---|---|---|
| Storage | `core/storage.py` (~1,074 lines), `core/utils.py` | Path/JSON/locking/checkpoint/cleanup cùng một module; `StorageManager` bọc lại function; legacy CSV/path |
| Schemas | `core/schemas.py` (~894 lines) | Nhiều domain cùng file, canonical stage + aliases, legacy conversions |
| CLI | `cli/main.py`, `cli/batch_cmd.py`, `cli/validate_cmd.py`, `cli/review_pack.py` | Parser/dispatcher dài; orchestration, path resolution, validation và presentation phân tán |
| Decision | `core/decision_engine.py` (~504 lines) | HTTP client, API key, retry, cache, domain decisions cùng module |
| Kaggle | `cloud/kaggle_service.py` (~749 lines), `cloud/payload.py`, account files | Auth/quota/fallback/submit/poll/sync/worker generation trộn lẫn; worker source được ghép chuỗi |
| HF | `archive/hf_archive.py` | Khá coherent; config/token/manifest boundary cần rõ, không rewrite không cần thiết |
| YouTube | `cli/youtube_cmd.py` (~1,028 lines), `youtube/account_manager.py`, `youtube/metadata.py` | CLI gánh OAuth, readiness, upload, attachment, verification, receipts |
| TTS | `tts/engine.py`, `tts/cuda_worker.py`, `services/tts_service.py` | Nhiều public entry point, legacy voice aliases; giữ native batch và OOM recovery |
| Render | `render/fast_renderer.py`, `render/chapter_renderer.py`, `render/visual_policy.py` | FFmpeg execution, capability, media validation và rendering logic cần boundary rõ |
| Skill | `SKILL.md`, `AGENTS.md`, `prompts/`, `rules/` | SKILL chỉ 41 dòng nhưng rules/prompts trùng và có mâu thuẫn single narrator/multi-voice, camera semantics, pacing |
| Packaging | `pyproject.toml`, `requirements.txt`, `uv.lock`, `setup.sh`, `setup.bat`, `.env.example` | Dependency duplication, env example còn V3; cloud payload cài từ requirements |
| Tests | `tests/unit/test_phase_*.py`, `test_p*_*.py`, `tests/adversarial/` | Tên và một số mô tả theo milestone cũ; cần regroup theo behavior, không xóa coverage |

Kiểm tra dependency graph, call sites, test baseline và schema thực tế trước khi quyết định xóa một function/file. Kích thước lớn không tự chứng minh code dư thừa.

## 3. Feature preservation contract

Các tính năng sau phải được giữ nguyên về capability, trừ khi người dùng phê duyệt thay đổi riêng.

| Domain | Required behavior |
|---|---|
| Setup | Thu thập đủ 8 lựa chọn do người dùng xác nhận; không coi fallback hay preselection là approval; chapter sau kế thừa setup đã xác nhận |
| Ingest | Online source và local import; website adapters, image validation, chapter/source ordering, segmentation, contact sheets, merge/split/repack |
| Decision API | Gemini Decision closed-set routing; active/context-only/skip/review, split/merge/crop; cache, bounded retries, evidence preservation |
| Agent authoring | Agent trực tiếp đọc hình/OCR, character continuity, story summary, Vietnamese narration, N:M mapping, essential-panel retention, uncertainty protocol |
| TTS | VieNeu v3 Turbo local/offline, single confirmed narrator, speed, emotion cues, native batch, utterance cache; mock chỉ dùng test, không dùng production |
| Render | Visual plan, adaptive motion, safe framing, static/pan fast paths, 720p preview/1080p final theo config, subtitles/SRT, BGM/voice gain, watermark, FFmpeg/NVENC |
| Review | Voice approval và Chapter 1 human preview approval gắn đúng fingerprints; invalidate khi input được bảo vệ thay đổi; không bypass bằng flag/status cũ |
| Batch | Multi-chapter, chapter-level resume/retry/failure isolation, compile final, timestamps và artifact validation |
| Kaggle | Local hoặc remote production; single GPU và Dual-T4 scheduling; multi-account hợp lệ, quota/queue handling, payload/result integrity, pull/resume |
| YouTube | Agent-generated 16:9 thumbnail, metadata, multi-channel OAuth, destination/privacy reconfirmation, upload + captions + thumbnail + processing verification, receipt |
| HF | Private dataset archive, whitelist, manifest/checksum, pinned revision verification, atomic restore, receipt |
| Cleanup | Dry run mặc định, xác nhận project khi xóa, bảo vệ outputs và raw assets theo điều kiện hiện hành, giữ các receipt/gate đã được kiểm chứng |
| Storage | Atomic JSON writes, locking, typed validation, checkpoints, stable file contracts, crash recovery, no false-success |

**Không suy diễn rằng mọi gate từng xuất hiện ở V4 đều còn active ở V5.** Đọc implementation và regression tests tại HEAD để xác định chính xác điều kiện cleanup/receipt đang được enforce. Không vô tình bỏ gate đang hoạt động hoặc tái áp đặt một gate đã được chủ dự án chủ động loại bỏ.

## 4. Target architecture

Target layout mang tính định hướng; không tạo file rỗng chỉ để khớp tree. Giữ package lớn nếu thực sự có nhiều concern độc lập.

```text
src/manga_recap/
├── cli/
│   ├── app.py                 # parsing, dispatch, JSON envelope, exit codes
│   ├── project.py             # setup/prepare/status/validate
│   ├── production.py          # preview/approve/produce/compile
│   └── integrations.py        # publish/archive/accounts/cleanup entry points
├── core/
│   ├── settings.py            # runtime settings only
│   ├── models.py              # canonical typed contracts (split by domain if needed)
│   ├── project.py             # project/chapter store and paths
│   ├── storage.py             # atomic I/O, locks, hashing
│   ├── workflow.py            # orchestration, checkpoint transitions and gates
│   └── review.py              # approval and fingerprints
├── ingest/
│   ├── crawler.py
│   ├── adapters/               # existing site-specific adapters/profiles
│   ├── segment.py             # streaming segmentation, repack, contact sheets
│   └── decision.py            # domain panel routing and cache
├── media/
│   ├── ffmpeg.py              # shared command execution/probing
│   ├── tts/                   # VieNeu backend, catalog, batching/CUDA worker
│   └── render/                # visual policy, renderer, audio, subtitles, watermark
├── providers/
│   ├── decision.py            # HTTP client only
│   ├── kaggle.py              # SDK client and job lifecycle
│   ├── huggingface.py         # HF transport and archive lifecycle
│   └── youtube.py             # OAuth/client/upload verification
├── cloud/
│   ├── payload.py             # package, validate, export, sync
│   └── worker.py              # real executable worker entry point
└── compat/
    └── migrate.py             # legacy project/schema/CLI migration only
```

Dependency direction:

```text
Skill → CLI → Workflow → Core / Ingest / Media / Providers
                       → Cloud runner → same Media pipeline
```

Core does not import CLI exceptions or provider SDKs. Providers return typed results or domain errors. CLI maps these to the existing machine-readable envelope/exit-code contract. Avoid a universal base provider: the services have different lifecycles. Use dependency injection only at meaningful boundaries (HTTP/SDK client, clock/sleep, filesystem, process execution) to enable tests.

Package rename `src.*` → `manga_recap.*` is optional until import/package compatibility is tested. Do it as one isolated change if worthwhile, not interleaved with behavioral changes.

## 5. Canonical Skill workflow

| Stage | Owner | Operation | Completion evidence |
|---|---|---|---|
| 1. Setup | Agent + user + Python | Confirm source/range, compute, voice, speed, subtitle, destination/privacy, quality, BGM/gains | Valid user-confirmed setup + approved voice |
| 2. Prepare | Python | Crawl/import → segment → Decision → inspect | Valid manifest and contact sheets/source crops |
| 3. Author | Agent | Read evidence, characters, story summary; write Vietnamese recap in batches | Valid `script.json`; unresolved evidence explicitly marked |
| 4. Preview | Python | TTS → visual plan → render Chapter 1 preview + review pack | Valid preview and fingerprints |
| 5. Review | User + Python | Human watches preview and approves exact artifacts | Verified approval bound to current inputs |
| 6. Produce | Agent + Python | Prepare/author remaining chapters as needed; produce via local/Kaggle; compile | Verified chapter/feature outputs |
| 7. Release | Agent + user + Python | Prepare thumbnail/metadata; confirm upload destination; optional publish/archive/cleanup | Requested outputs and verified receipts |

### 5.1 Skill files

```text
.agents/skills/manga-recap/
├── SKILL.md
└── references/
    ├── vision.md
    ├── narration.md
    └── characters.md
```

- `SKILL.md`: canonical steps, input/output, command for each step, gates, stop conditions, resume behavior. Integrate the small human-review and publishing rule files.
- `vision.md`: merge `vision_analysis.md` and `vision_ocr.md`; preserve OCR, evidence, panel roles, uncertainty, crop anomalies and framing. Clarify Decision's closed-set role versus agent interpretation. Do not mandate redundant OCR artifacts unless a runtime consumer requires them.
- `narration.md`: merge `recap_scripting.md` and `narrative_recap.md`; preserve faithful storytelling, direct dialogue, N:M mapping, essential panels, hook and pacing as soft guidance, supported emotion cues. Canonical production uses one confirmed narrator; remove contradictory multi-voice instructions only after compatibility review.
- `characters.md`: naming, visual features, memory and chapter continuity.
- `AGENTS.md`: bootstrap to Skill and repo-development conventions, not another production workflow.
- Review templates against actual schemas. Keep only templates that are consumed or materially help the agent; generate canonical examples from models where feasible.
- Runtime validations remain code-enforced; documentation must not be the only protection for destructive/external operations.

### 5.2 CLI surface

Proposed canonical commands (final spelling can be adjusted to preserve usability):

```bash
manga-recap setup record ...
manga-recap prepare --project <slug> --chapter <N> --json
# Agent writes script.json from inspected visual evidence.
manga-recap validate --project <slug> --chapter <N> --json
manga-recap preview --project <slug> --chapter 1 --json
manga-recap approve --project <slug> ... --json
manga-recap produce --project <slug> --chapters <range> --json
manga-recap status --project <slug> --json
manga-recap publish ...
manga-recap archive push|verify|restore ...
manga-recap clean ...
```

High-level commands call existing domain operations rather than shelling out to other CLI commands. `prepare` must never write the creative narration; `produce` must stop/return actionable missing-script or review state when agent input is required. Preserve lower-level `crawl`, `segment`, `decide`, `repack`, `panel-inspect`, `inspect`, `tts`, `render`, `compile`, `batch`, `cloud`, `review-pack`, `review-approve`, `youtube` and benchmark commands as compatibility/debug entry points while consumers migrate. Do not introduce duplicated orchestration implementations. Remove obsolete aliases only after explicit migration/deprecation decision.

## 6. Implementation phases

Work in small, reviewable commits on a dedicated refactor branch. Each phase must pass its relevant tests and the agreed baseline gates before progressing. Do not perform a large rewrite with one final test run.

### Phase 0 — Baseline, contracts, inventory (P0)

- [ ] Inspect HEAD, working tree, README/AGENTS/Skill, current CI and dependency lock.
- [ ] Run baseline tests and compile check in a reproducible environment; record pass/fail/skip counts and pre-existing failures without silently fixing unrelated issues.
- [ ] Inventory CLI commands/flags, JSON envelope/exit codes, public Python imports, schema versions, file layout, environment variables and provider credentials.
- [ ] Identify all call sites of `StorageManager`, legacy CSV/token/voice aliases, alternate path functions, FFmpeg wrappers, and provider-specific state writes. Use static search and tests; do not infer dead code from filenames alone.
- [ ] Capture golden fixtures: representative local project, manifest, script, visual plan, approval, final artifact metadata, provider payload/receipt and old-schema fixtures where available. Sanitize credentials and copyrighted/private assets.
- [ ] Record baseline code/file counts, skill text sizes, relevant media performance metrics and selected render/TTS behavior. Use identical workload/hardware/settings for future comparison.

**Exit gate:** Baseline report exists; feature/command/schema compatibility matrix agreed; no production services invoked.

### Phase 1 — Storage and canonical contracts (P0)

- [ ] Separate generic JSON I/O, atomic writes, checksums, file locks and path resolution from checkpoint and cleanup policy.
- [ ] Introduce a single project/chapter store API where it eliminates repeated path/read/validate/write sequences. Prefer functions if a class does not improve clarity; do not maintain both public facades permanently.
- [ ] Replace duplicate path helpers with one authoritative resolver; preserve existing path and env behavior until migrated.
- [ ] Centralize checkpoint updates and status validation; no raw ad-hoc status mutation scattered across CLI handlers.
- [ ] Move cleanup eligibility to its own domain operation; keep path safety, final outputs, dry-run and current receipt requirements intact.
- [ ] Split schema responsibilities only where it improves coherence. Preserve Pydantic constraints and stable IDs, and distinguish app version from schema version.
- [ ] Move domain exceptions out of `src/cli/exit_codes.py`; CLI remains the mapping layer.

**Acceptance:** Atomic write/locking/checkpoint/cleanup tests pass. Existing projects read correctly. Crash/invalid-input and concurrent-write tests preserve files. No new bypass of approval or cleanup. Remove redundant wrappers only when call sites are migrated.

### Phase 2 — Legacy migration and settings (P0)

- [ ] Introduce explicit legacy normalization/migration at load boundary, covering only formats found in supported data or tests: old stage names, script/CSV, old voice aliases, legacy token paths and schema versions.
- [ ] Current runtime operates on canonical models; no scattered V3/V4 conditionals. Preserve old input support via the migration layer until deprecation is approved.
- [ ] Migration must support backup/dry run or non-destructive read conversion, validate before replacement, be idempotent, and preserve unknown user data or fail explicitly rather than discard it.
- [ ] Replace Q-number mental model with named settings (`source`, `compute`, `voice`, `speed`, `subtitles`, `publish`, `quality`, `audio`); retain old Q01–Q10 record parsing. Keep provenance (`answer_source=user`) and approval hashes.
- [ ] Explicitly separate `RuntimeSettings` (paths/logging/timeouts), `ProjectConfig` (confirmed user choices), and provider credentials. Resolve configuration once at composition root, not at import time in every module.
- [ ] Preserve existing env names via a compatibility mapping, document precedence, and update `.env.example` without actual secret values. Remove stale V3 claims.
- [ ] Establish `pyproject.toml` as dependency authority; optional extras for providers/dev where useful. Keep cloud runtime dependency export generated from the same source or a verified lock, not a second manually maintained list. Update setup scripts and `uv.lock` consistently.

**Acceptance:** Old fixtures migrate deterministically; setup cannot be bypassed using defaults; conflicting config fails clearly; local-only install works without optional provider SDKs; cloud runtime installation remains reproducible; no credentials included in package/payload/archive.

### Phase 3 — Workflow and thin CLI (P0)

- [ ] Extract business operations from `*_cmd.py` into domain workflow functions.
- [ ] Define canonical stage transitions and prerequisites once. Retain existing operational stage semantics (`INGEST`, `AGENT`, `AUDIO`, `RENDER`, `EXPORT`) where appropriate; keep provider job states separate.
- [ ] Refactor `cli/main.py` using command registration/dispatch rather than a long duplicated manual chain. Keep argparse or current lightweight stack unless a migration has measurable benefit.
- [ ] Implement high-level `prepare`, `preview`, `produce`, `approve`, `publish` as thin orchestrators over shared functions. Use one execution path for single/batch/local/cloud, with provider-specific execution only at the appropriate boundary.
- [ ] Make `status` derive completion from authoritative state plus artifact evidence, and report `next_action`/blocker for the agent without guessing success.
- [ ] Consolidate review pack construction: one data model feeding JSON/Markdown/HTML renderers. Preserve useful evidence, omitted panel list, mappings, hashes and feedback loop.
- [ ] Preserve existing JSON envelope, exit codes, aliases, and old flags until compatibility tests demonstrate safe replacement.

**Acceptance:** Canonical and legacy CLI routes produce equivalent results on fixtures; retries skip valid cached artifacts; failures are isolated; no command marks a stage complete without validated output; Chapter 1 approval remains fail-closed.

### Phase 4 — Decision API (P1)

- [ ] Extract provider HTTP transport to `providers/decision.py`: endpoint/model, auth, request serialization, response parsing, timeout, retry policy.
- [ ] Keep decision questions, panel routing, split/merge, crop, ordering and cache in `ingest/decision.py`.
- [ ] Use a single maintained HTTP stack (`httpx` is already present) where compatible. Do not change external API semantics without tests.
- [ ] Centralize key resolution (`GEMINI_API_KEY`/documented aliases), never expose tokens or request headers in logs.
- [ ] Implement bounded retry for retryable rate limits/transient errors with backoff; fail fast for auth/invalid request. Validate response shape before mutation.
- [ ] Cache keys must include relevant image hash, model/question version and routing configuration as needed; preserve valid cache semantics or document migration. Do not double-call already completed work unnecessarily.
- [ ] Keep creative writing/OCR outside Decision transport. Do not use a fallback creative API or fabricate a successful routing result when provider fails.

**Acceptance:** Mocked API tests cover success, cache hit, invalid response, 429, timeout, auth failure, partial batch, split/merge/crop. Golden decision results preserve panel order/evidence and do not incur redundant requests.

### Phase 5 — Kaggle integration (P0/P1)

- [ ] Separate SDK client (`authenticate`, quota, submit, status, cancel, download) from runner lifecycle (select account, submit, wait, sync, retry policy).
- [ ] Replace generated string-list worker code with a real importable/testable worker module and minimal bootstrap. Bootstrap must locate verified runtime payload, validate it before execution, and work on Kaggle's actual script/dataset entry mechanism. Do not assume `python -m` alone is sufficient until verified.
- [ ] Define typed `CloudJob`/`CloudResult`/job state, including provider job ID, account identity (not token), payload hash, input hashes and result manifest. Persist enough state for `pull`/resume after interruption.
- [ ] Reuse manifest, safe extraction, checksum and path-whitelist helpers where semantics match; retain independent trust-boundary validation at worker and client sync. Reject symlinks/traversal, unexpected files and stale results.
- [ ] Preserve multi-account registry and quota/queue fallback. Select only explicitly configured, authorized accounts. Never use fallback to bypass provider rules. Retry only known retryable/pre-submit failures; reconcile ambiguous submissions and cancel/confirm prior jobs before switching accounts to prevent duplicate compute jobs.
- [ ] Do not hide artifact corruption, invalid configuration, schema mismatch or deterministic execution errors behind account fallback. Bound attempts, polling, queue and overall timeout; return structured stall/failure status.
- [ ] Keep token/account isolation. If Kaggle SDK requires temporarily mutating process env, guard it appropriately, restore it on all paths and ensure no cross-account concurrency race. Do not include credentials in runtime bundle.
- [ ] Preserve single-GPU and Dual-T4 orchestration: TTS/encoding overlap, native batching, GPU release/reuse, hardware profile and final validation. Do not rewrite optimized scheduling without regression/benchmark evidence.
- [ ] Cloud worker calls the same production domain code as local mode; only hardware/job transport differs. Validate local/cloud result parity, including watermark assets and subtitle scaling.

**Acceptance:** Fake SDK tests cover auth failure, quota exhaustion, queue timeout, cancellation, submission ambiguity, remote failure, result mismatch, stale input, pull/resume and safe sync. Single/Dual GPU contract tests pass. Live Kaggle smoke/benchmark only after explicit authorization and with limited workload; record actual job IDs and timings without tokens.

### Phase 6 — Hugging Face and YouTube (P0/P1)

#### Hugging Face

- [ ] Retain coherent archive implementation where possible; separate transport from archive manifest policy only if it reduces duplication.
- [ ] Resolve `HF_TOKEN`/SDK credential fallback in one documented place; make dataset ID configurable, private by default. Preserve authenticated repo ownership/identity checks and do not expose secrets.
- [ ] Keep exact allowlist, path/symlink rejection, pinned commit revision, file checksums, validated receipt and atomic restore. Verify receipt matches current content before using it to authorize deletion.
- [ ] Preserve push/verify/restore commands; do not claim successful archive until remote evidence is validated. Handle partial upload and retry without overwriting unrelated remote files.

#### YouTube

- [ ] Move OAuth, account registry and channel resolution out of `youtube_cmd.py` into provider/account code. Keep multi-channel selection, token refresh and bounded interactive authorization; headless mode returns actionable auth-needed error.
- [ ] Move metadata/timestamps generation, upload readiness, thumbnail/subtitle path resolution and receipt generation into appropriate domain functions. CLI should just parse arguments and invoke the operation.
- [ ] Reconfirm actual channel/destination and privacy immediately before upload; verify against user-confirmed setup. No silent default/fallback channel or automatic public upload.
- [ ] Preserve agent-generated 16:9 thumbnail, captions, metadata, attach/verify behavior and bounded processing polling.
- [ ] Treat upload as non-idempotent: persist video ID as soon as known; recover/verify instead of blindly uploading again after interruption. Distinguish upload succeeded but attachment/verification pending from completed publish.
- [ ] Normalize provider errors to structured application errors. Preserve receipt evidence and fail-closed semantics for required attachments and readiness checks.

**Acceptance:** Mocked provider tests cover credential resolution, wrong account, no-upload config, expired OAuth, publish confirmation, upload interruption, thumbnail/captions failure, processing timeout, HF revision mismatch, restore and cleanup denial. Real push/publish only after explicit user authorization; never test by publicly uploading unintended media.

### Phase 7 — Skill consolidation (P1)

- [ ] Merge redundant vision and narration files following Section 5; reconcile contradictory voice/camera/quality guidance with actual current implementation and user intent.
- [ ] Rewrite `SKILL.md` with seven canonical stages, required inputs/outputs, minimal commands, human/provider stop conditions, resume guidance and references loaded only when needed.
- [ ] Keep creative guidance and deterministic validation distinct. Do not encode hardware internals in agent instructions; do not introduce automated creative scoring if current product intentionally relies on human review.
- [ ] Remove duplicated instructions in `AGENTS.md`, README, rules and prompts; keep README as user/developer quickstart, not a second agent workflow.
- [ ] Document exact setup form contract and fallback behavior when structured user-choice tool is unavailable. Never invent user approval.
- [ ] Test complete workflow mentally and with a fixture: fresh series, missing credentials, incomplete setup, ambiguous panels, rejected preview, interrupted job, resume, local-only export, authorized publication/archive.

**Acceptance:** No conflicting canonical instructions; every required feature has one clear workflow owner; Skill contains no obsolete commands/paths; a new agent can execute from setup to release without reading implementation internals.

### Phase 8 — Media boundary and targeted optimization (P2)

- [ ] Consolidate shared FFmpeg process execution, ffprobe, duration helpers, dependency errors, timeout and error reporting. Retain path escaping, subprocess argument safety and hardware-specific flags.
- [ ] Standardize one public TTS API; remove duplicate service/engine wrappers only when legacy consumers migrated. Centralize catalog/alias resolution and audio normalization.
- [ ] Keep optimized CUDA worker, native batch, length bucketing, precision handling, OOM backoff, CPU post-processing and worker heartbeat. Move diagnostic/benchmark-only code out of production paths where safe.
- [ ] Preserve canonical visual plan and evidence validation; isolate FFmpeg shot execution from camera/pacing/subtitle/audio policy without rewriting the algorithms.
- [ ] Preserve static-hold, pre-scaled pan strip, cache, NVENC and optimized subtitle fast paths; verify output frame/audio properties before and after.
- [ ] Profile before changing performance-sensitive code. Optimize only proven duplication or bottlenecks; do not impose arbitrary file-length caps.

**Acceptance:** Golden visual plans and timing equivalent where behavior must stay stable. Valid MP4/SRT/WAV, no missing/reordered shots, no subtitle/voice sync regression, correct BGM/watermark, no mock TTS in production. Benchmark on same inputs/hardware; compare cold and warm paths separately.

### Phase 9 — Test suite and final cleanup (P1)

- [ ] Organize tests by current behavior (`core`, `ingest`, `media`, `providers`, `workflow`, `integration`, `e2e`), not historical phase numbers.
- [ ] Replace stale milestone descriptions with current invariants. Consolidate true duplicate scenarios using parameterization, but preserve distinct edge cases and assertions.
- [ ] Separate unit, mocked integration, real media and authorized live-provider tests. Move one-off benchmark/adversarial scripts to explicit tooling when not part of regression suite.
- [ ] Retain migration and compatibility fixtures for agreed support window; delete only proven dead code/files after import/CLI/test/Skill references are clear.
- [ ] Update README, setup, provider credentials instructions and CI. Check package installs, `--help`, command JSON, and cross-platform paths.
- [ ] Review final diff for genuinely reduced complexity: repeated code removed, responsibilities clarified, fewer sources of truth, no new giant orchestrator or overengineered abstraction.

**Acceptance:** All agreed regression gates green; no unexplained test count/coverage loss; no secrets, production data, temp artifacts or generated caches committed; documentation matches runtime.

## 7. Shared provider conventions

Use small provider-specific clients, not a large generic framework.

| Provider | Inputs | Result | Retry boundary |
|---|---|---|---|
| Decision | Images + closed-set questions | Validated decision response | Retry transient transport/rate limit; no fabricated result |
| Kaggle | Verified payload + job configuration | Job handle + validated result manifest | Reconcile submit/status; account fallback only on eligible failures |
| HF | Archive manifest + files | Pinned revision + verified receipt | Retry safe transfer/reconciliation; never skip checksum |
| YouTube | Confirmed channel/privacy + final assets | Video ID + attachment/processing verification + receipt | Resume existing upload/verification; avoid duplicate publication |

Shared conventions: one settings resolution boundary, secret redaction, bounded timeouts/retries, explicit typed errors, testable transport dependency, no raw exceptions exposed as success. Classify errors as validation, auth, rate limit, quota, timeout, remote execution or integrity where meaningful. CLI maps them to compatible exit codes and actionable JSON responses.

## 8. Regression and performance gates

Run from a clean environment, adapting exact commands to the retained package setup:

```bash
python -m compileall -q src tests
python -m pytest -q
manga-recap --help
```

| Gate | Required evidence |
|---|---|
| G1: Contracts | Schema/legacy fixtures, stable project layout, CLI JSON/exit codes |
| G2: Ingest/author | Source ordering, panel routing/evidence, N:M, uncertainty, essential panel review |
| G3: Approval | Missing/incomplete setup rejected; stale/unsigned preview rejected; explicit destination confirmation |
| G4: Media | Synthetic and representative real-media fixtures; playable output, correct duration/frames/audio/subtitles |
| G5: Recovery | Crash/retry/resume, partial write, concurrency, stale artifact and provider job recovery |
| G6: Provider integrity | Mocked failures, secret isolation, verified payload/results/receipts, no duplicate external side effect |
| G7: Archive/cleanup | Private archive, pinned revision, verified restore; dry run and unsafe-deletion denial |
| G8: Performance | Compare TTS/render local and Kaggle on same workload/profile where available; investigate any regression |
| G9: Skill | Canonical workflow walkthrough; no stale command, contradictory rule or duplicated procedure |

No universal percentage improvement is required. Set numeric thresholds only after measuring baseline variance. Compare median and range across repeated runs where practical; document hardware, dependency versions, cache state and workload. For external services unavailable in the test environment, use mocked contract coverage and mark live verification as pending, not PASS.

## 9. Delivery strategy & definition of done

- [ ] Create a dedicated refactor branch and incremental reviewable commits, keeping the current main usable.
- [ ] Record a concise change log: retained features, removed duplication, migrations, behavioral changes (if any), commands and tests actually run.
- [ ] Maintain an old-path/command compatibility mapping until migration is confirmed; avoid permanent dual implementations.
- [ ] Do not silently change render appearance, narration strategy, provider model, compute profile, publication privacy, archive destination or cleanup permissions.
- [ ] No unrelated product features, no new cloud service, no model replacement, no arbitrary rewrite of optimized media code.
- [ ] No unresolved regression against baseline; any pre-existing failures or unverified live checks are documented.
- [ ] Final repo structure reflects domain responsibilities; CLI is thin; provider transports do not leak into core; Skill has one canonical workflow; config/dependency sources are clear.
- [ ] Provide final metrics (actual before/after source size and LOC, file count, Skill size, test results, benchmark results where measured). A reduction target of roughly 20–30% source size is an exploratory estimate from audit, not a mandatory success criterion. Do not sacrifice clarity or safety to hit it.

## 10. Implementation instruction for coding agent

Implement the plan incrementally. First inspect the current repository, establish the baseline and propose any necessary deviation from the plan with concrete evidence. Prefer deletion, consolidation and simple functions over added abstraction. Preserve current behavior and contracts unless a change is explicitly approved. Run relevant tests after each phase; never treat a mocked provider success as live verification. Stop on a blocking regression and report it instead of masking it. Do not run real external jobs, publish YouTube videos, upload archives, delete production assets or modify credentials without explicit authorization. At completion, summarize changes, tests, actual metrics, compatibility status and remaining risks.

