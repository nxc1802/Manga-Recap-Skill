# V4 correctness evidence and acceptance status

Date: 2026-11-11 (Asia/Ho_Chi_Minh). **Draft; not release ready.**

The controlling plan is [V4_Correctness_Improvement_Plan.md](V4_Correctness_Improvement_Plan.md). The older [implementation plan](V4_Implementation_Plan.md) is historical. A local test, an implemented function, and a real/live acceptance gate are different evidence levels.

## Latest handoff work (2026-11-11)

Pulled `ba11e81ac64733327c4bfc1dbe2bb9845ac6b87c`. Tested implementation contents
are committed as `0d10ff13b18313f76eddb9b81271208ab68a5dcd`.

- **R1 fixed:** authored tall-panel crops retain the full per-panel canonical
  region list; arbitrary regions and reversed y-min/y-max progression are
  rejected, including nonconsecutive static shots. Canonical order, a single
  safe crop and a full static hold have explicit adversarial coverage.
- **R2 frozen:** height <80 px means `short_sliver`; 80 px is not a sliver.
  The argument now affects both source metadata and actual image inputs.
- **R3 deterministic checks green:** `python3 -m pytest -q --disable-warnings`
  (with JUnit output) returned **793 passed, 0 failed/error/skip**, 90.62s.
  All 183 failed/error nodes from the initial run are classified in the
  [migration ledger](verification/v4_regression_migration.json).
  Legacy scenarios were consolidated with explicit replacements, fixtures
  migrated, and deferred/live specifications moved to [acceptance scope](../acceptance/README.md).
  Their removed/skipped counts are not reported as passing acceptance.
- **R4/R5:** README reflects the current branch; production TTS parsers only
  accept neural. Retained compatibility flags cannot bypass approval/receipts.
  CI references active canonical tests and runs the complete deterministic suite.
- Additional fixes: missing project config is no longer swallowed by inspection
  or status; reading a missing setup creates no project directory; missing
  render source metadata fails explicitly; tar links/devices/duplicate members
  are rejected before extraction on all supported Python versions.

Compilation, frozen schemas and whitespace checks pass. See the
[new verification record](verification/v4_remaining_fixes_2026-11-11.json).
GitHub CI has not been run for this source checkpoint.

**Real E2E is BLOCKED before Stage 0.** No `ask_question` tool is available and
there are zero real-workspace setup records. `venv/bin/python` is Python 3.12.13
with VieNeu 3.8.3 installed and a Turbo cache directory present; model integrity,
loading, device and real speech are unverified. The Python 3.14 test interpreter
has no VieNeu package. No real series job, human approval or Kaggle job was
started. **G4–G7 remain OPEN; P8/P9 remain blocked.**

## Ordered phase ledger

No P0–P7 phase is declared complete by this change. The ordered gate chain is **OPEN**. Missing evidence is a blocker, never an assumed PASS.

| Phase | Implementation evidence | Gate acceptance still required |
|---|---|---|
| P0 | Pulled `13885786f390e3f21ff1fdbe506875e27af1204d`; merged `origin/main` in `18a0550`; retracted historical release/speed claims; fixed README link; exported canonical schema snapshots | Deterministic suite green after documented migration; complete ordered contract acceptance review before phase closure |
| P1 | Central router policy; explicit exemptions; unknown commands denied; direct handlers and legacy entrypoints guarded; paired fsync/rename setup persistence; revision, snapshot, identity and mapped settings checked | Deterministic policy and migration checks green; no real production acceptance inferred |
| P2 | Shared `src/core/visual_contract.py`; separate `evidence_panel_ids`; strict script fields; source/config fingerprint; authored N:M visual selection preserved | Legacy tests consolidated with canonical replacements; real chapter compatibility remains unverified |
| P3 | Three production motions only; normalized scroll cap 0.12/s; H/W > 2.4; conservative text handling; reasoned substitutes; timeline/frame feasibility and vertical-order validation | Real source/agent evidence review and acceptance of preceding contracts; L1 fixtures alone do not close this gate chain |
| P4 | Local and worker call the same `render_visual_plan`; legacy chapter compositors removed/disabled; component MP4s are actually encoded and decoded | **G4 OPEN:** a real chapter with real neural WAVs must render under a directly confirmed series setup |
| P5 | Native PCM energy/header/count checks; decoded video/frame count/streams/energy checks; strict SRT timing; source/script/audio/plan/video fingerprints; cache and status revalidation | **G5 OPEN:** real end-to-end chapter validation; green L1/L2 is not G5 acceptance |
| P6 | Explicit `review-approve`; hashes and current source/config binding; no render flag grants approval; exact Chapter 1 artifact promotion; conditional thumbnail evidence required | **G6 OPEN:** an actual human must watch and approve the exact technically valid preview |
| P7 | Source/runtime/project inventory with pinned manifest hash; private per-job dataset; startup verification; shared neural/planning/rendering/validation runtime; downloaded hashes + local semantic parity + dispatch input snapshot checked before state publication | **G7 OPEN:** actual Kaggle run with real media; dependency/model provisioning, NVENC and live dataset behavior remain unverified |

P8/P9 are not accepted or benchmarked. Dual-T4 package/worker/dispatch/result-sync endpoints remain disabled. No new performance or live-service report is published.

## Canonical contracts

Reviewable JSON Schema snapshots live in `schemas/v4/`. Regenerate intentionally and check drift:

```bash
PYTHONPATH=. python scripts/freeze_contracts.py
PYTHONPATH=. python scripts/freeze_contracts.py --check
```

- Explicit series setup: 4.0.0; project revision and setup snapshot must match.
- Script: new documents use 4.1.0; explicitly identified 3.0.0 documents remain readable only where their fields meet the strict schema. `panel_ids` is a checked evidence alias, not shot instructions. Conflicting aliases and embedded legacy shot plans fail closed.
- VisualPlan/Shot: 4.1.0, unknown fields and legacy motions rejected. A stale/missing input fingerprint on an existing production plan requires regeneration or an explicit authored-plan update.
- Human approval: 4.1.0; old status-only approvals do not authorize production.
- Archive/YouTube receipts: frozen 4.0.0 shapes. Completing their lifecycle validator integration remains P10/P11 work.
- Release record: frozen 4.1.0 shape only. This is not the P14 transition engine and must not be used to assert release readiness.

## Historical verification checkpoint (before this handoff)

The focused L1/L2 suite uses isolated unit fixtures, including explicitly synthetic PCM/image inputs. Those fixtures test failure handling, actual encode/decode, N:M planning, approval binding and result import. **They do not prove real neural speech, actual human approval, a real manga chapter, or a live cloud run.** No fixture was inserted into a user's project to obtain production acceptance. An interrupted cloud artifact copy leaves the chapter running rather than completed; a changed local input snapshot rejects the result before publication.

Focused command:

```bash
python -m pytest -q --disable-warnings \
  tests/unit/test_series_setup.py \
  tests/unit/test_visual_policy.py \
  tests/unit/test_artifact_correctness.py \
  tests/unit/test_canonical_workflow.py \
  tests/unit/cloud/test_canonical_cloud.py \
  tests/unit/test_tts_engine.py \
  tests/unit/test_schemas.py
```

Historical focused result at the earlier source checkpoint: **155 passed**. Syntax compilation, frozen schema drift check and patch whitespace check passed locally. GitHub CI has not been run for this local change. The full regression on source commit `6a16477d0ed6a16fcc39b94552aee07aed6322fb` returned **694 passed, 168 failed, 15 errors, 7 skipped** (exit 1). All focused modules also passed in that full run. That historical suite was **not green**. The latest deterministic result is recorded above; real acceptance remains open. See [machine-readable evidence](verification/v4_correctness_checks_2026-11-11.json) and the [failure inventory](verification/v4_regression_failures_2026-11-11.txt).

Earlier repository-wide runs were red, including tests that assert flag-based approval bypass, source-string details of the removed cloud renderer, status-only approval, mock production synthesis and fake/header-only artifact acceptance. Some failures also predate this work. The migration ledger above records their current dispositions and replacements. Deferred/live specifications are unaccepted; no skip/xfail is used to claim a passing real pipeline.

## Operational behavior and remaining limitations

After a real, explicitly confirmed setup and validated source/script:

```text
real neural TTS
-> visual-plan (or visual-plan --regenerate after edits)
-> render --quality preview for Chapter 1
-> technical validation + review pack
-> actual human review
-> review-approve with the exact review-pack fingerprints
-> final Chapter 1 promotion / later chapter production
```

`visual-plan --regenerate` preserves the prior plan under a content-addressed stale filename. Existing authored plans are validated and preserved, rather than silently replaced with one shot per evidence panel. Changing reviewed Chapter 1 source, narration, voice, setup, plan, audio, thumbnail or BGM invalidates approval. Unrelated chapter registration/bookkeeping does not. A replaced Chapter 1 final also blocks approval; generating a changed preview preserves the old final under a content-addressed stale filename. Final Chapter 1 promotes the exact reviewed preview bytes at the configured target resolution; there is no unreviewed high-resolution reframing after approval.

Canonical production currently supports auto/landscape-fit framing. Unsupported portrait settings fail validation. Cloud per-chapter execution produces Chapter 1 preview or later chapters final. Chapter 1 final promotion is local after download/review; `render --kaggle` rejects a contradictory quality selection. Encoder bytes need not match; the source/shot/timeline contract must match.

The neural adapter uses the SDK's native `save` contract, validates that audio and then normalizes to PCM16 mono 48 kHz when needed. The native API is documented in the [upstream VieNeu README](https://github.com/pnnbao97/VieNeu-TTS/blob/main/README.md). SDK/model execution is **not live verified** here: `vieneu` is absent from the test Python but present as 3.8.3 in the repository Python 3.12 venv. The adapter fixture proves rate conversion, not neural quality or model availability.

There is no directly confirmed `series_setup_answers.json` in the current real project directories. The mandatory `ask_question` tool is unavailable in this session. Per [AGENTS.md](../AGENTS.md), “Incomplete/invalid/unavailable `ask_question` => STOP without starting jobs.” No real series, cloud production, upload, destructive cleanup, or human approval was started or fabricated to bypass this restriction.
