# V4 Correctness Improvement Plan

> Repository: `nxc1802/Manga-Recap-Skill`  
> Target branch: `feat/v4-strict-form-motion-cloud-archive`  
> Status: Implementation plan  
> Primary goal: make the pipeline provably correct end-to-end before adding further optimization or feature work.

## 1. Final Goal

The project is considered production-ready only when this real-artifact path is verified:

```text
Series Setup
  -> Crawl / Import Manga
  -> Panel Evidence
  -> Script + N:M Evidence Mapping
  -> Real TTS
  -> Canonical VisualPlan
  -> Real Rendering
  -> Chapter 1 Human Preview
  -> APPROVED
  -> Full Series Production
  -> YouTube Upload + Verify
  -> HF Private Archive
  -> Archive Verify
  -> Restore Verification
  -> Safe Cleanup
```

Core invariants:

```text
No evidence      -> no success
No real artifact -> no benchmark
No verification  -> no destructive cleanup
No human approval -> no full-series production
```

Implementation status must be tracked explicitly as:

```text
planned
-> implemented
-> unit_verified
-> integration_verified
-> live_verified
-> release_ready
```

Do not treat `implemented` as equivalent to `production ready`.

---

## 2. Critical Path

The immediate critical path is:

```text
P0 Repository Stabilization
-> P1 Central Setup Gate
-> P2 Canonical Contracts
-> P3 VisualPlan Correctness
-> P4 Canonical Local Renderer
-> P5 Artifact Validators
-> P6 Human Preview Gate
-> P7 Single-session Kaggle Correctness
```

**P3 -> P7 is the highest-priority implementation path.**

Do not prioritize Dual-T4 benchmarking, archive optimization, or new product features until a real panel can move through:

```text
Real panel
-> VisualPlan
-> Canonical renderer
-> Real frames
-> Real WAV
-> Real MP4
-> Artifact validation
-> Local PASS
-> Cloud PASS
```

---

# P0 — Repository Stabilization and Contract Freeze

## Goals

Remove ambiguity between `main`, the V4 feature branch, documentation, schemas, and release claims.

## Tasks

- Reconcile the V4 branch with current `main` and resolve branch divergence without overwriting already-merged safety/policy fixes.
- Fix stale documentation links such as references to missing `docs/plan.md`.
- Keep `docs/V4_Implementation_Plan.md` as the historical V4 plan and use this document as the correctness-remediation plan.
- Freeze/version the canonical schemas for:
  - Series setup
  - Script/evidence mapping
  - VisualPlan / Shot
  - Archive receipt
  - YouTube receipt
  - Release state
- Separate repository claims into:
  - implemented
  - tested
  - integration verified
  - live verified
  - blocked
- Remove or clearly mark any benchmark/live claim that cannot be reproduced from real artifacts.

## Gate G0

PASS only when branch state, documentation, canonical schemas, and verification terminology are consistent.

---

# P1 — Centralize the Strict Series Setup Gate

Current gating must not depend on each CLI handler remembering to call `require_series_setup()`.

## Tasks

Create a centralized execution policy, for example:

```text
src/core/execution_policy.py
```

with a command requirement map such as:

```python
COMMAND_REQUIREMENTS = {
    "crawl": ["series_setup"],
    "segment": ["series_setup"],
    "tts": ["series_setup"],
    "render": ["series_setup"],
    "compile": ["series_setup"],
    "cloud": ["series_setup"],
    "youtube": ["series_setup"],
    "thumbnail": ["series_setup"],
}
```

The CLI router must validate the policy before dispatching the command.

Commands that currently bypass or partially bypass setup validation must be brought under the same gate, including `segment`, `tts`, `compile`, `youtube`, and `thumbnail`.

## Setup persistence correctness

`series_setup_answers.json` and `project.json` currently share a setup revision, but two independent atomic writes are not a true transaction.

Required behavior:

```text
write setup.tmp
write project.tmp
fsync
rename setup.tmp -> setup file
rename project.tmp -> project file
verify setup_revision equality
```

On startup, any revision mismatch must fail closed.

## Required crash/failure tests

Cover at least:

- crash after setup write
- crash before project write
- crash after project write
- corrupted JSON
- revision mismatch
- missing Q01-Q10
- invalid migrated configuration

## Gate G1

No production-sensitive CLI path can execute without a valid Q01-Q10 setup.

---

# P2 — Canonical Data Contracts

Evidence, narration mapping, and rendering instructions must be separate concepts.

## 2.1 Panel evidence

`PanelItem` should represent source evidence only.

Recommended conceptual fields:

```text
panel_id
source_page
bbox
image_path
width
height
evidence_metadata
```

Do not use `PanelItem` as the rendering instruction itself.

## 2.2 Script evidence mapping

Each narration segment should preserve the panels that justify the narration:

```python
NarrationSegment:
    segment_id
    narrator_text
    evidence_panel_ids: list[str]
```

This means "which panels support this narration", not "which shots must be rendered".

## 2.3 VisualPlan rendering contract

Canonical model:

```python
VisualPlan:
    chapter_id
    shots
    fps
    target_width
    target_height
    expected_duration
```

```python
Shot:
    shot_id
    segment_id
    source_panel_id
    substitute_panel_id
    source_box
    focus_regions
    avoid_regions
    visual_role
    visual_omit_reason
    substitution_reason
    motion
    duration
```

The system must support:

```text
Evidence != Rendering
```

For example, narration evidence may be `[P12, P13, P14]` while the renderer uses `P12-top`, `P12-bottom`, and `P14`.

## Gate G2

All local/cloud rendering code and tests depend on the same canonical contract.

---

# P3 — Complete VisualPlan Correctness

This is the highest-priority technical area.

## 3.1 Strict motion policy

V4 production mode should permit only:

```text
STATIC
SLOW_PAN_DOWN
STATIC_FOCUS
```

Legacy motions such as left/right/up pan, zoom, subject pan, or jitter must not silently pass through or be aliased into a valid motion. Invalid production motion should fail validation.

## 3.2 Scale-invariant motion limits

Do not use a fixed value such as `120 px/s`.

Use a normalized constraint, for example:

```text
scroll_fraction_per_second <= 0.12
```

and derive pixels per second from the image height.

## 3.3 Tall panel decomposition

Choose one canonical threshold and make Plan + code + tests match. Recommended:

```text
H / W > 2.4
```

Required boundary tests:

- 2.39
- 2.40
- 2.41
- extreme tall panel

Tall-panel crops must remain monotonic top -> middle -> bottom and never reverse vertically.

## 3.4 Speech bubble / text avoidance

Add explicit rendering metadata:

```text
focus_regions
avoid_regions
```

Visual planning should prefer safe regions. If text/speech detection is uncertain, use a conservative static/full panel rather than an aggressive crop that can destroy information.

## 3.5 Omitted visual substitution

Do not select a substitute purely from the first approved panel or nearest identifier.

Preferred fallback order:

```text
same segment
-> adjacent evidence panel
-> same source page
-> previous approved panel
-> neutral/static fallback
```

Every substitution must record its reason.

## Gate G3

VisualPlan generation is deterministic, schema-valid, policy-valid, and covered by edge-case tests.

---

# P4 — Make the Local Renderer the Reference Implementation

The canonical renderer must consume `VisualPlan` directly.

Recommended module:

```text
src/render/visual_plan_renderer.py
```

Suggested boundaries:

```text
render_shot()
render_frame()
render_visual_plan()
```

Architecture:

```text
VisualPlan
   -> Canonical VisualPlanRenderer
   -> Frames
   -> Encoder backend
   -> MP4
```

Local and cloud must not maintain separate interpretations of `VisualPlan`.

The only backend-specific difference should be frame encoding / device execution, for example local ffmpeg vs cloud NVENC.

## Gate G4

A real chapter can be rendered locally from the canonical VisualPlan without legacy rendering logic bypassing the contract.

---

# P5 — Artifact Correctness Validators

A file existing is not proof that an artifact is correct.

The previous silent-WAV and black-video failures show that every stage needs semantic validation.

## 5.1 Audio validator

Validate at least:

- WAV/container header
- valid sample count
- duration > minimum threshold
- PCM not all-zero
- RMS/energy above a conservative threshold

The validator does not need to judge voice quality; it only needs to prove that the output is real non-silent audio.

## 5.2 Video validator

Validate at least:

- ffprobe succeeds
- duration > 0
- frame count > 0
- expected width/height
- video stream exists
- audio stream exists when required
- sampled frames have non-trivial pixel variance
- sampled frames are not all black

Sampling frames around 10%, 50%, and 90% is sufficient for the initial guard.

## 5.3 Subtitle validator

Validate:

- cue count > 0
- non-empty text
- monotonic timestamps
- cue end <= final media duration + tolerance

## 5.4 Final artifact validator

Verify consistency across:

- final video duration
- narration/audio duration
- streams present
- subtitle timing
- chapter metadata
- artifact hashes / fingerprints

Only after the complete validation may the chapter be marked `completed`.

## Gate G5

Silent WAV, black/fake MP4, malformed subtitles, or mismatched final artifacts cannot transition to completed state.

---

# P6 — Human Preview Gate

Human quality review remains a real human gate and must not be replaced by an automated quality score.

For Chapter 1:

```text
Generate
-> Technical Validation
-> Review Pack
-> Human Review
-> APPROVED
```

The human review should cover story fidelity, panel relevance, cropping, motion comfort, speech-bubble handling, voice, subtitles, BGM, and thumbnail.

Approval must be tied to the exact reviewed artifact/config:

```text
human_review_status
approved_at
reviewer
config_fingerprint
artifact_hash
```

Any relevant change to source, voice, VisualPlan, rendering settings, or final artifact must invalidate the previous approval.

## Gate G6

Full-series production cannot start unless the exact Chapter 1 configuration/artifact being used has a valid approval.

---

# P7 — Single-Session Kaggle Correctness Before Dual-T4

Cloud correctness must be proven before GPU optimization.

The first target is:

```text
cloud semantic output == local semantic output
```

not "cloud is faster".

## 7.1 Required cloud flow

```text
package source
-> upload Kaggle payload
-> launch kernel
-> load canonical contracts
-> real TTS
-> canonical VisualPlan rendering
-> artifact validation
-> download artifacts
```

A single GPU implementation is acceptable at this stage.

## 7.2 Self-contained payload

The package manifest must declare all required source/runtime/project assets and hashes.

The worker must run a startup verification such as:

```text
verify_payload_manifest()
```

Missing source/package/assets must fail closed. Never generate fake artifacts to satisfy a downstream step.

## 7.3 Local/cloud parity

Given the same fixture, local and cloud must agree on:

- shot sequence
- source_panel_id
- source_box
- motion
- duration/timeline
- frame dimensions
- semantic artifact expectations

MP4 bytes do not need to be identical because encoder backends differ.

## Gate G7

A real cloud chapter passes semantic parity and artifact validation.

**Do not begin Dual-T4 performance acceptance before G7.**

---

# P8 — True Dual-T4 Architecture

After cloud correctness is proven, replace the current thread-oriented orchestration with genuine process isolation for CUDA workloads.

Target:

```text
Main Orchestrator
  |- Process 0
  |    CUDA_VISIBLE_DEVICES=0
  |    TTS
  |
  |- Process 1
       CUDA_VISIBLE_DEVICES=1
       Render / NVENC
```

Use multiprocessing or subprocess workers rather than two CUDA workloads in ordinary Python threads.

## Pipeline overlap

Desired execution:

```text
GPU0: C1 TTS -> C2 TTS -> C3 TTS -> C4 TTS
GPU1:          C1 render -> C2 render -> C3 render
```

## Dynamic batch selection

Probe candidate TTS batch sizes such as:

```text
8 -> 16 -> 32 -> 64
```

Select the largest stable batch within memory/latency limits, then freeze it for the session. Retain recursive OOM recovery as a safety fallback.

## Gate G8

Both T4 GPUs perform real independent work on real artifacts with process-level isolation and no mock/synthetic output.

---

# P9 — Benchmark Correctness

Benchmarking is allowed only after G7 and G8.

Every report must contain enough information to reproduce the test:

- git SHA
- GPU model/count
- CUDA/driver
- PyTorch version
- TTS model/version
- video resolution
- FPS
- chapter count
- source/audio duration
- TTS duration
- rendering duration
- end-to-end wall-clock duration

The primary performance metric is end-to-end wall time.

Do not use encoder-only FPS as proof of whole-system speedup.

A target such as `>= 1.5x` should remain an engineering target, not a correctness requirement. If the verified system achieves a lower speedup but remains correct, report the measured value rather than forcing the target.

## Gate G9

All published benchmark numbers come from real, valid artifacts and are reproducible.

---

# P10 — Complete HF Archive Correctness

The existing archive safety direction is strong; finish the missing lifecycle coverage.

## Tasks

- Audit all canonical final outputs, including any relevant `output/<slug>` artifacts.
- Prefer manifest roles over directory assumptions.
- Ensure the archive manifest covers the required lifecycle data:
  - source metadata
  - raw/panels as required
  - script
  - audio
  - subtitles
  - VisualPlan
  - final video
  - thumbnail
  - review pack
  - receipts
- Keep strict secret/symlink/traversal guards.
- Preserve pinned commit/revision verification.

Recommended CLI lifecycle:

```text
archive push
archive verify
archive restore
clean
```

Keep destructive cleanup separate from archive creation.

## Required live round trip

```text
local project
-> push to private HF repo
-> verify remote hashes
-> remove/move local test copy
-> restore exact commit SHA
-> verify manifest and media/schema
-> project validation
```

## Gate G10

A real private HF archive can be verified and restored byte-for-byte from an exact revision.

---

# P11 — YouTube Receipt Correctness

Required lifecycle:

```text
upload
-> uploaded receipt
-> YouTube API verify
-> uploadStatus == processed
-> channel matches
-> privacy matches
-> caption requirement verified
-> thumbnail operation recorded/verified where possible
-> verified receipt
```

Improve internal evidence by storing values such as:

```text
thumbnail_local_sha256
thumbnail_upload_at
```

Do not mark a YouTube receipt verified immediately after upload.

## Gate G11

Every required chapter has a verified, processed YouTube receipt tied to the intended account/privacy configuration.

---

# P12 — Destructive Cleanup Safety

Cleanup must remain dry-run by default.

Physical deletion is allowed only when all required conditions are true:

```text
HF archive VERIFIED
AND HF restore VERIFIED
AND YouTube VERIFIED (unless setup explicitly selected no upload)
AND manifest unchanged
AND exact revision/receipt consistency
AND explicit project confirmation
```

Do not add correctness bypasses such as `--skip-verify`, `--force`, or a global yes flag that can disable lifecycle safeguards.

Missing, stale, mismatched, or unverifiable receipts must block deletion.

## Gate G12

A destructive cleanup cannot execute unless the exact project state has passed all lifecycle verification requirements.

---

# P13 — Test Pyramid

Do not equate a large pytest count with production readiness.

Use four levels:

| Level | Purpose | Examples |
|---|---|---|
| L1 | Unit correctness | schemas, setup, motion policy, manifests, receipts |
| L2 | Integration correctness | Script -> VisualPlan, VisualPlan -> Renderer, TTS -> validator |
| L3 | End-to-end project/chapter | real sample chapter -> TTS -> render -> review pack |
| L4 | Live external verification | Kaggle, HF private archive/restore, YouTube |

Normal CI should run L1 + L2 + lightweight L3.

L4 should be an explicit live verification workflow and must never be replaced by mocks while still being reported as live verification.

---

# P14 — Canonical Release State Machine

Introduce or formalize one canonical lifecycle state machine:

```text
DRAFT
-> SETUP_VALID
-> SOURCE_READY
-> SCRIPT_READY
-> AUDIO_VALID
-> VISUAL_PLAN_VALID
-> RENDER_VALID
-> TECHNICALLY_VALID
-> HUMAN_APPROVED
-> PRODUCTION_READY
-> UPLOADED
-> YOUTUBE_VERIFIED
-> ARCHIVED
-> ARCHIVE_VERIFIED
-> RESTORE_VERIFIED
-> CLEANUP_ALLOWED
```

Each transition must have a validator.

Invalid transitions must fail closed.

Avoid scattered ad-hoc checks of status strings throughout the CLI and services.

---

# P15 — Release Gate / Definition of Done

V4 should not be considered fully merge/release ready until all items below have real evidence.

| Requirement | Required |
|---|---:|
| Setup Q01-Q10 cannot be bypassed | YES |
| Setup/project crash consistency | YES |
| Canonical Script/VisualPlan contracts | YES |
| Local renderer consumes VisualPlan | YES |
| Real neural WAV generation | YES |
| Silent-WAV detection | YES |
| Real panel/video rendering | YES |
| Black-video detection | YES |
| Chapter 1 human approval gate | YES |
| Approval tied to artifact/config fingerprint | YES |
| Single-session Kaggle production works | YES |
| Local/cloud semantic parity | YES |
| Two real CUDA processes for Dual-T4 | YES |
| Real Dual-T4 production run | YES |
| Reproducible real benchmark evidence | YES |
| HF private push + remote SHA verify | YES |
| Exact-revision HF restore | YES |
| YouTube processed verification | YES |
| Safe cleanup two-receipt/lifecycle gate | YES |
| At least two real human-reviewed demos | YES |
| No false production/live benchmark claims | YES |

---

# Milestones

## M1 — Core Correct

```text
P0-P5
```

Result: local technical correctness.

## M2 — Workflow Correct

```text
P6
```

Result: real human-gated chapter workflow.

## M3 — Cloud Correct

```text
P7
```

Result: Kaggle produces the same canonical semantics as local.

## M4 — Performance Ready

```text
P8-P9
```

Result: real Dual-T4 optimization and reproducible benchmark evidence.

## M5 — Lifecycle Correct

```text
P10-P12
```

Result: upload/archive/restore/delete lifecycle is safe and verified.

## M6 — Production Ready

```text
P13-P15
```

Result: end-to-end and live verification satisfy the Definition of Done.

---

# Implementation Priority

Recommended order:

```text
P0 Repository stabilization
-> P1 Central Setup Gate
-> P2 Canonical Contracts
-> P3 VisualPlan correctness
-> P4 Local VisualPlan Renderer
-> P5 Artifact Validators
-> P6 Human Preview Gate
-> P7 Single-session Kaggle correctness
-> P8 True Dual-T4 multiprocessing
-> P9 Real benchmark
-> P10 HF archive completeness
-> P11 YouTube verification
-> P12 Safe cleanup
-> P13 Test pyramid / E2E live verification
-> P14 Release state machine
-> P15 Release gate
```

The architectural rule for the remaining V4 work is:

> **Dual-T4 is an optimization layer on top of a renderer that is already correct. It must not be the place where rendering correctness is first implemented.**

The release rule is:

> **Fail closed whenever required evidence, artifact validity, human approval, remote verification, or lifecycle receipts are missing.**
