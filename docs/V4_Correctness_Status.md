# V4 correctness evidence and acceptance status

Date: 2026-11-11 (Asia/Ho_Chi_Minh). **Draft; not release ready.**

The controlling plan is [V4_Correctness_Improvement_Plan.md](V4_Correctness_Improvement_Plan.md). The older [implementation plan](V4_Implementation_Plan.md) is historical. A local test, an implemented function, and a real/live acceptance gate are different evidence levels.

## Ordered phase ledger

No P0–P7 phase is declared complete by this change. The ordered gate chain is **OPEN**. Missing evidence is a blocker, never an assumed PASS.

| Phase | Implementation evidence | Gate acceptance still required |
|---|---|---|
| P0 | Pulled `13885786f390e3f21ff1fdbe506875e27af1204d`; merged `origin/main` in `18a0550`; retracted historical release/speed claims; fixed README link; exported canonical schema snapshots | Resolve/migrate contradictory legacy regression expectations before accepting the repository-wide contract freeze |
| P1 | Central router policy; explicit exemptions; unknown commands denied; direct handlers and legacy entrypoints guarded; paired fsync/rename setup persistence; revision, snapshot, identity and mapped settings checked | Finish regression migration; no production acceptance is inferred from the focused policy tests |
| P2 | Shared `src/core/visual_contract.py`; separate `evidence_panel_ids`; strict script fields; source/config fingerprint; authored N:M visual selection preserved | Migrate old source-string/legacy-contract tests; verify real chapter compatibility |
| P3 | Three production motions only; normalized scroll cap 0.12/s; H/W > 2.4; conservative text handling; reasoned substitutes; timeline/frame feasibility and vertical-order validation | Real source/agent evidence review and acceptance of preceding contracts; L1 fixtures alone do not close this gate chain |
| P4 | Local and worker call the same `render_visual_plan`; legacy chapter compositors removed/disabled; component MP4s are actually encoded and decoded | **G4 OPEN:** a real chapter with real neural WAVs must render under a directly confirmed series setup |
| P5 | Native PCM energy/header/count checks; decoded video/frame count/streams/energy checks; strict SRT timing; source/script/audio/plan/video fingerprints; cache and status revalidation | **G5 OPEN:** real end-to-end chapter validation; legacy suite is not green |
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

## What was tested

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

Focused result at the latest source checkpoint: **155 passed**. Syntax compilation, frozen schema drift check and patch whitespace check passed locally. GitHub CI has not been run for this local change. The final committed-source full regression result is recorded in `docs/verification/v4_correctness_checks_2026-11-11.json`; a non-green result blocks acceptance.

Earlier repository-wide runs were red, including tests that assert flag-based approval bypass, source-string details of the removed cloud renderer, status-only approval, mock production synthesis and fake/header-only artifact acceptance. Some failures also predate this work. These failures are retained and must be reviewed/migrated; they are not suppressed with skip/xfail or reported as passing verification.

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

The neural adapter uses the SDK's native `save` contract, validates that audio and then normalizes to PCM16 mono 48 kHz when needed. The native API is documented in the [upstream VieNeu README](https://github.com/pnnbao97/VieNeu-TTS/blob/main/README.md). SDK/model execution is **not live verified** here: `vieneu` is absent from the current Python environment. The adapter fixture proves rate conversion, not neural quality or model availability.

There is no directly confirmed `series_setup_answers.json` in the current real project directories. The mandatory `ask_question` tool is unavailable in this session. Per [AGENTS.md](../AGENTS.md), “Incomplete/invalid/unavailable `ask_question` => STOP without starting jobs.” No real series, cloud production, upload, destructive cleanup, or human approval was started or fabricated to bypass this restriction.
