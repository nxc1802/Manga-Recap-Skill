# V4 Remaining Fixes & Real E2E Verification Handoff

> Repository: `nxc1802/Manga-Recap-Skill`  
> Branch: `feat/v4-strict-form-motion-cloud-archive`  
> Controlling plan: `docs/V4_Correctness_Improvement_Plan.md`  
> Current evidence: `docs/V4_Correctness_Status.md`  
> Status: **handoff for remaining correctness work and real E2E verification**  
> Release state: **NOT RELEASE READY — gate chain remains OPEN**

---

## 1. Purpose

This document is the next-task handoff after the first P0-P7 implementation pass.

The immediate goal is **not** to add P8 Dual-T4 optimization or publish performance numbers. The next work must:

1. fix the remaining correctness defects found during review,
2. classify and resolve the repository-wide regression failures,
3. run a **real local Chapter 1 E2E** with neural VieNeu-TTS and real media,
4. obtain a **real human approval** for the exact Chapter 1 preview,
5. run a **real Kaggle chapter E2E** and close Local/Cloud semantic parity,
6. publish reproducible evidence before any gate is marked PASS.

Core rule:

```text
No green regression baseline
-> no release claim

No real neural audio / real rendered video
-> no G4/G5 acceptance

No human watch + explicit approval
-> no G6 acceptance

No live Kaggle run
-> no G7 acceptance

No G7
-> do not start P8/P9 performance acceptance
```

---

# 2. Current Verified State

At the current source checkpoint recorded in the repository:

```text
Focused L1/L2:
155 passed
0 failed

Full regression:
694 passed
168 failed
15 errors
7 skipped

GitHub CI:
not run for the recorded local checkpoint

Real Chapter 1:
not run

Human approval:
not performed

Live Kaggle:
not run

Benchmark:
not run / not published
```

The focused suite is useful implementation evidence only. It is **not** production acceptance.

---

# 3. Remaining Correctness Issues

## R1 — Tall-panel authored VisualPlan can lose top-down ordering guarantees

### Problem

`generate_visual_plan()` creates tall-panel crops in monotonic top -> bottom order.

However, `validate_visual_plan()` currently validates authored plans mainly against a per-panel reference and only performs vertical reversal checks for consecutive `SLOW_PAN_DOWN` shots.

Tall panels generated as multiple `STATIC` crops can therefore lose the required top-down ordering guarantee when an authored plan is edited.

The baseline lookup also collapses multiple shots with the same `source_panel_id` into one entry:

```python
by_panel = {s.source_panel_id: s for s in expected_shots}
```

This is insufficient for validating multiple canonical crop regions from one tall panel.

### Required fix

Validation must preserve the canonical sequence of crop regions for each source panel.

For repeated shots from the same tall panel, require monotonic vertical progression:

```text
current.source_box.y_min >= previous.source_box.y_min
current.source_box.y_max >= previous.source_box.y_max
```

or an equivalent canonical-region ordering rule.

Authored plans may omit evidence visually, but if they use multiple crops from the same tall panel they must not reorder them bottom -> top.

### Required adversarial tests

Add explicit tests for:

- canonical top -> middle -> bottom sequence passes,
- bottom -> middle -> top fails,
- one static full-panel hold remains valid,
- one authored crop remains valid only if it satisfies safe-region constraints,
- repeated tall-panel shots cannot use arbitrary non-canonical regions.

### Gate impact

Blocks final acceptance of **P3 / G3**.

---

## R2 — Short-sliver contract is inconsistent

### Problem

`detect_omit_visual_band(..., min_height_px=80)` exposes `min_height_px`, but current implementation does not use it.

The full regression contains failures for short panel/sliver cases at approximately:

```text
1 px
10 px
45 px
79 px
```

This leaves an ambiguous contract:

- either short slivers are still supposed to be automatically omitted,
- or that behavior was intentionally removed.

The code, function signature, old tests, and current V4 contract do not yet agree.

### Required decision

Choose exactly one behavior and freeze it.

**Preferred option:** keep the conservative sliver guard because tiny bands are normally not useful visual shots.

Example policy:

```text
height < 80 px
-> OMIT_VISUAL
-> reason = "short_sliver"
```

If the feature is intentionally removed instead:

- remove `min_height_px`,
- update the canonical contract,
- migrate the old tests,
- document why the new behavior is safe.

Do not leave a dead parameter and contradictory tests.

### Gate impact

Blocks final acceptance of **P3 / G3**.

---

## R3 — Full repository regression is still red

### Problem

The repository-wide run is currently:

```text
694 passed
168 failed
15 errors
7 skipped
```

The failures include a mixture of:

- tests for intentionally removed legacy behavior,
- tests that assert old bypass semantics,
- tests tied to the removed legacy cloud renderer,
- tests allowing mock/synthetic production artifacts,
- CLI/error-code compatibility regressions,
- segmentation/sliver behavior,
- old E2E workflows,
- old Kaggle/Dual-T4 assumptions,
- cleanup / YouTube / production lifecycle tests,
- potential true defects.

### Required workflow

Do **not** mass-skip or xfail the failures.

Every failure must be classified into one of:

| Class | Meaning | Action |
|---|---|---|
| A | True defect in current V4 contract | Fix production code and add regression test |
| B | Legacy test contradicts intentionally frozen V4 contract | Rewrite/remove the obsolete assertion with rationale |
| C | Test fixture bypasses mandatory setup/human gates | Migrate fixture to canonical setup/approval flow |
| D | Test belongs to P8+ functionality intentionally disabled | Move to future/P8 acceptance scope; do not report as passing |
| E | Environment/live dependency failure | Keep separate from deterministic L1-L3 CI and document prerequisite |

### Required result

Before claiming repository regression correctness:

```text
pytest -q --disable-warnings
-> exit code 0
```

Any intentionally excluded live test must be clearly separated into L4 instead of silently skipped as production evidence.

### Gate impact

Blocks **G0**, release acceptance, and merge readiness.

---

## R4 — README is materially stale relative to the V4 branch

### Problem

The current README still contains statements such as:

- V4 runtime enforcement is only planned,
- Gate 0 is not a CLI runtime guard,
- Chapter 1 runtime approval protection is only planned,
- the renderer supports the legacy 8-motion model,
- `docs/plan.md` is referenced even though it is not the active plan,
- historical test counts such as 306/306 are shown.

Those statements conflict with the current branch, which already has:

- centralized execution policy,
- strict Q01-Q10 runtime enforcement,
- canonical three-motion production contract,
- artifact-bound human approval,
- canonical VisualPlan renderer,
- current correctness plan/status documents.

### Required fix

Rewrite the README after the regression contract is stabilized.

The README must distinguish:

```text
V3 stable behavior
V4 implemented-but-not-live-verified behavior
V4 live-verified behavior
future P8+ behavior
```

Do not publish old test counts or old architecture descriptions as current truth.

### Gate impact

Blocks clean **P0 documentation consistency**.

---

## R5 — Public CLI still exposes legacy/dangerous-looking bypass flags

### Current examples

```text
cloud produce-series --skip-preview
clean --bypass-receipts
produce --tts-engine mock
```

Current production code does fail closed in important places:

- `cloud produce-series` calls preview verification with `skip_preview=False`,
- destructive `clean --bypass-receipts` is rejected when not dry-run,
- production TTS paths reject non-neural execution.

That is good, but the public CLI still suggests that these are legitimate production escape hatches.

### Required fix

Prefer one of:

1. remove obsolete bypass flags from the production CLI, or
2. rename them to explicit test-only/internal interfaces and hard-block them outside test mode.

At minimum, help text must state that they **cannot bypass production correctness gates**.

Add tests proving:

```text
--skip-preview cannot bypass G6
--bypass-receipts cannot delete files
mock TTS cannot create a production-completed chapter
```

### Gate impact

Not necessarily a current bypass defect, but required before release to avoid contract ambiguity.

---

## R6 — Setup provenance remains assertion-based

### Problem

The persisted record guarantees:

```text
answer_source == "user"
Q01-Q10 complete
snapshot hash valid
project/setup revisions match
```

but the JSON itself cannot cryptographically prove that the answers came through the coding-agent `ask_question` interaction.

The current agent correctly stopped instead of fabricating a setup when `ask_question` was unavailable. Preserve this behavior.

### Recommended improvement

If strict provenance is required, optionally add:

```text
interaction_id
question_set_hash
agent_session_id or provenance token
recorded_by
```

This is lower priority than R1-R5 and the real E2E run.

### Gate impact

No immediate blocker if the Agent policy remains enforced and no setup is fabricated.

---

# 4. Regression-Fix Priority

Work in this order:

```text
R1 Tall-panel authored order
-> R2 Sliver contract
-> classify full regression
-> fix all Class A defects
-> migrate Class B/C tests
-> isolate Class D/E from deterministic CI
-> README / public CLI cleanup
-> full regression green
```

Do not proceed to release acceptance merely because the focused 155-test suite remains green.

---

# 5. Required Test Layers Before Real E2E

## L1 — Unit

Must remain green for:

- Q01-Q10 setup
- setup split-brain/crash handling
- canonical schemas
- motion policy
- tall-panel order
- sliver policy
- evidence/substitution
- audio validation
- video black-frame validation
- subtitle validation
- human approval binding
- cloud payload hashing

## L2 — Integration

Must cover:

```text
Script -> VisualPlan
VisualPlan -> canonical renderer
real-format WAV -> validation
rendered frames -> MP4 validation
preview -> review-pack binding
cloud payload -> worker -> result-sync semantic validation
```

## L3 — Local real chapter

Must use:

- real manga/webtoon panels,
- real canonical script/evidence mapping,
- real VieNeu neural synthesis,
- real ffmpeg rendering,
- real media validation,
- a human-watched preview.

No mock audio, black frames, fake MP4, or fixture-only approval is allowed as L3 evidence.

## L4 — Live external

Separate workflow for:

- Kaggle,
- later HF private archive/restore,
- later YouTube upload/verify.

For the current handoff, L4 priority is **Kaggle G7 only**.

---

# 6. Real Local Chapter 1 E2E Runbook

Use a small real chapter that is easy for a human to inspect. Do not begin with a large multi-chapter series.

Suggested scope:

```text
1 real series
1 real chapter
small but representative panel count
contains at least one normal panel
contains at least one tall panel if possible
contains visible text/speech regions if possible
```

## Step E0 — Environment preflight

Record:

```text
git SHA
Python version
OS
ffmpeg version
ffprobe version
VieNeu package/model version
device (CPU/MPS/CUDA)
```

Run deterministic checks first:

```bash
python -m compileall -q src tests
PYTHONPATH=. python scripts/freeze_contracts.py --check
git diff --check
pytest -q --disable-warnings
```

**Stop if the deterministic regression suite is not green**, except for explicitly separated L4 tests.

---

## Step E1 — Collect real Q01-Q10 setup

Use the mandatory agent `ask_question` flow.

Do not hand-author `answer_source=user` merely to unblock the run.

Persist the resulting answers with the canonical setup command, then validate:

```bash
manga-recap setup validate -p <slug> --json
```

Evidence to save:

```text
series_setup_answers.json
project.json
setup_revision
setup_snapshot_hash
CLI validation JSON
```

Expected:

```text
Q01-Q10 complete
project/setup revision match
snapshot hash valid
no implicit defaults
```

---

## Step E2 — Ingest real source

For local source:

```bash
manga-recap crawl -p <slug> -c 1 --import-dir <real_source_dir> --json
```

For supported web source, use the confirmed Q01 URL instead.

Then segment if required:

```bash
manga-recap segment -p <slug> -c 1 --json
```

Inspect and validate:

```bash
manga-recap inspect -p <slug> -c 1 --json
manga-recap validate -p <slug> -c 1 --strict --json
```

Evidence to save:

```text
manifest.json
panel hashes
panel dimensions
contact sheets
CLI outputs
```

No panel marked `needs_review` may silently continue into production.

---

## Step E3 — Produce canonical narration/evidence mapping

Create the real Chapter 1 script through the normal Agent workflow.

Requirements:

```text
every narration segment has evidence_panel_ids
no unresolved [CAN_XAC_MINH]
no embedded legacy render shots
project/chapter identity matches
```

Run strict validation before TTS.

Save:

```text
script.json
script SHA-256
evidence mapping review notes
```

---

## Step E4 — Real VieNeu neural TTS

Ensure the real neural VieNeu runtime/model is installed and available.

Run:

```bash
manga-recap tts -p <slug> -c 1 --json
```

Production acceptance requires:

```text
real neural engine
non-zero PCM
valid duration
valid sample count
RMS > silence threshold
segment checksum matches WAV
measured WAV duration written to script
```

Save:

```text
audio/clips/*.wav
script.json after measured durations/checksums
TTS CLI JSON
device/runtime metadata
```

Listen to at least several representative clips. Automated RMS validation proves non-silence, not narration quality.

---

## Step E5 — Generate and inspect canonical VisualPlan

Run:

```bash
manga-recap visual-plan -p <slug> -c 1 --json
```

Inspect:

```text
visual_plan.json
shot order
evidence_panel_ids vs rendered shots
tall-panel crop direction
substitution reasons
focus/avoid regions
motion types
timeline == measured audio durations
```

Acceptance:

```text
only static / slow_pan_down / static_focus
no reverse vertical order
no unsafe crop through avoid regions
no unexplained substitute
no stale input fingerprint
```

---

## Step E6 — Render a real Chapter 1 preview

Run:

```bash
manga-recap render -p <slug> -c 1 --quality preview --json
```

Expected output must pass semantic artifact validation:

```text
real decodable video
expected resolution/fps
expected frame count
non-black sampled frames
real non-silent audio stream
subtitle timeline valid
video/audio duration consistent
VisualPlan/source/script fingerprints consistent
```

Save:

```text
video/preview.mp4
subtitles/preview.srt
artifact_validation.json
visual_plan.json
render CLI JSON
ffprobe summary
SHA-256 of preview
```

A successful ffmpeg exit code alone is not sufficient.

---

## Step E7 — Generate review pack and perform real human review

Generate:

```bash
manga-recap review-pack -p <slug> -c 1 --format all --json
```

A real human must watch the exact `video/preview.mp4` and check:

- story fidelity,
- panel relevance,
- tall-panel reading order,
- crop safety,
- speech-bubble handling,
- motion comfort,
- voice quality,
- subtitle timing/readability,
- BGM if configured,
- thumbnail if Q07 requires it.

Do not create approval before the human has watched the artifact.

Record the exact review-pack:

```text
config_fingerprint
artifact_hash
artifact_path
reviewer identity/name
review notes
```

Approve only using the exact values emitted by the review pack:

```bash
manga-recap review-approve \
  -p <slug> \
  --reviewer "<reviewer>" \
  --config-fingerprint <hash> \
  --artifact-hash <hash> \
  --json
```

Then verify that the approval gate passes and that changing any reviewed source/config/media invalidates it.

This closes **G6** only when a real person reviewed the exact artifact.

---

## Step E8 — Chapter 1 final promotion correctness

After approval:

```bash
manga-recap render -p <slug> -c 1 --quality final --json
```

Required:

```text
final Chapter 1 bytes == approved preview bytes
final SHA-256 == approved artifact_hash
no unreviewed reframe
no unreviewed re-encode
```

Save final hash evidence.

---

# 7. Live Kaggle G7 E2E Runbook

Do this only after the local workflow is technically valid.

## Step K0 — Preflight

Record:

```text
Kaggle account
Kaggle API auth result
kernel image/runtime
GPU model
CUDA version
ffmpeg/NVENC availability
VieNeu model provisioning method
source git SHA
payload manifest SHA
```

Run:

```bash
manga-recap cloud info --provider kaggle --json
```

No credentials/tokens may appear in generated payloads, logs, kernel metadata, or evidence reports.

---

## Step K1 — Live Chapter 1 cloud preview

Use the canonical single-chapter cloud path, not Dual-T4 series production.

```bash
manga-recap cloud run -p <slug> -c 1 --provider kaggle --json
```

This must exercise:

```text
self-contained payload packaging
private per-job dataset
payload inventory/hash verification
real neural TTS
canonical VisualPlan runtime
canonical renderer
real ffmpeg/NVENC or accepted encoder
artifact validators
downloaded result hash verification
local/cloud semantic parity validation
result publication only after validation
```

### Required evidence

Save:

```text
payload_manifest.json
payload manifest SHA-256
Kaggle dataset reference
kernel reference
kernel status history
worker logs
result_manifest.json
downloaded result hashes
VisualPlan semantic parity result
artifact_validation.json
cloud CLI JSON
```

Do not publish a PASS if the kernel merely starts or if files merely download.

---

## Step K2 — Required semantic parity

Local and cloud MP4 bytes may differ.

They must match semantically on:

```text
project/chapter identity
script segment sequence
narrator text
evidence_panel_ids
shot sequence semantics
source_panel_id
substitute_panel_id
source_box
motion
duration/timeline
target dimensions
fps
input fingerprints
artifact validity
```

Real neural audio may differ at byte level if synthesis is nondeterministic, but every cloud audio file must independently pass validation and match its recorded duration/checksum metadata.

A successful `sync_result()` is necessary evidence, but still record the explicit parity report.

---

## Step K3 — Failure-path checks

After one successful live run, deliberately verify fail-closed behavior with safe non-destructive tests:

- alter one local input after dispatch -> result import must reject,
- corrupt a downloaded result file -> hash check must reject,
- remove a required payload file -> worker startup must reject,
- provide wrong manifest SHA -> worker must reject,
- change VisualPlan semantics -> result sync must reject,
- simulate kernel failure/timeout -> no chapter may become completed.

No fake media should be generated as recovery output.

---

## Gate G7 acceptance

Mark G7 PASS only when:

```text
real Kaggle chapter completed
AND real neural audio validated
AND real rendered frames/video validated
AND payload hashes verified
AND result hashes verified
AND semantic parity verified
AND failed imports cannot publish completed state
```

If any item is missing, G7 remains OPEN.

---

# 8. Evidence Package to Commit

After the real E2E work, add a new evidence directory similar to:

```text
docs/verification/v4_e2e_<YYYY-MM-DD>/
```

Recommended files:

```text
environment.json
local_run.json
local_artifact_report.json
human_review.md
human_approval.json
kaggle_run.json
kaggle_result_manifest.json
semantic_parity.json
failure_path_checks.json
sha256sums.txt
README.md
```

Do **not** commit:

- Kaggle tokens,
- HF tokens,
- YouTube credentials,
- OAuth secrets,
- private manga source files when licensing forbids it,
- large generated media unless explicitly intended.

For private/licensed assets, store only safe hashes/metadata and describe where the reviewer can access the artifact.

---

# 9. Required Status Update After Work

Update:

```text
docs/V4_Correctness_Status.md
docs/verification/v4_correctness_checks_2026-11-11.json
```

or create a new dated verification record.

For every gate, use only:

```text
OPEN
BLOCKED
PASS
```

and attach evidence.

Never derive PASS from code existence or focused unit-test counts alone.

---

# 10. Definition of Done for This Handoff

This handoff task is complete only when all of the following are true:

- [ ] R1 tall-panel authored order is fixed and adversarially tested.
- [ ] R2 sliver behavior is explicitly frozen and tested.
- [ ] Full regression failures are classified.
- [ ] All Class A true defects are fixed.
- [ ] Legacy Class B/C tests are migrated to the canonical V4 contract.
- [ ] Deterministic repository-wide regression is green.
- [ ] README matches the actual V4 branch state.
- [ ] Public bypass-looking CLI flags are removed, renamed, or explicitly hard-blocked.
- [ ] Real Q01-Q10 setup is collected through the required human/agent flow.
- [ ] Real neural VieNeu Chapter 1 TTS succeeds.
- [ ] Real Chapter 1 VisualPlan passes review.
- [ ] Real Chapter 1 preview renders and passes artifact validation.
- [ ] A real human watches and approves the exact preview.
- [ ] Chapter 1 final is the exact approved artifact.
- [ ] A real Kaggle Chapter 1 run completes.
- [ ] Local/cloud semantic parity is verified.
- [ ] Kaggle failure-path checks demonstrate fail-closed behavior.
- [ ] G4, G5, G6, and G7 status is updated with evidence.
- [ ] P8/P9 remains blocked until G7 is genuinely PASS.

---

# 11. Explicit Non-Goals

Do **not** spend this iteration on:

- Dual-T4 performance tuning,
- >=1.5x speedup claims,
- new thumbnail features,
- new YouTube features,
- archive optimization,
- additional motion modes,
- new product features unrelated to correctness.

Those may resume only after the real correctness chain is closed.

---

# 12. Final Handoff Instruction

Implement and verify in this order:

```text
Fix R1/R2
-> classify full regression
-> green deterministic suite
-> update docs/public CLI consistency
-> real local neural Chapter 1
-> artifact validation
-> real human approval
-> exact final promotion
-> live Kaggle Chapter 1
-> semantic parity + failure-path checks
-> publish evidence
-> close G4-G7
```

The key acceptance rule remains:

> **A feature is not accepted because code exists. It is accepted only when the required real artifact and verification evidence exist.**
