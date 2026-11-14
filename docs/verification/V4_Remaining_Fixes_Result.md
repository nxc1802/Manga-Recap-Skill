# Remaining fixes: verification result

> **Historical checkpoint notice:** The section below preserves the exact `0d10ff1` / `c890afe` deterministic checkpoint where G4–G7 were **OPEN**. For subsequent live Chapter 1 E2E verification (`055d17f`), see [Subsequent Verification Checkpoint](#subsequent-verification-checkpoint) and the canonical gate ledger in [`docs/V4_Correctness_Status.md`](../V4_Correctness_Status.md).

## Historical Checkpoint (`0d10ff1` / `c890afe` — G4–G7 OPEN)

Implementation: `0d10ff13b18313f76eddb9b81271208ab68a5dcd`.

The full deterministic suite passed **793 tests**, with no failures, errors or
skips. Compile, frozen-schema and patch checks passed. This validates L1/L2
contracts and actual-format media handling in isolated fixtures. It does not
establish neural speech quality, human review or live Kaggle behavior.

The [migration ledger](v4_regression_migration.json) classifies every original
failed/error node and maps consolidated legacy scenarios to their canonical
replacements. Test counts decreased because obsolete duplicate implementation
assertions were consolidated and deferred/live scopes were separated. No
removed or deferred case is counted as passing. Existing legacy receipt tests
remain protocol fixture checks, not verified HF/YouTube acceptance.

Real E2E is blocked at E1: the required `ask_question` tool is unavailable and
no real series has a complete directly confirmed setup. AGENTS.md says
“Incomplete/invalid/unavailable `ask_question` => STOP without starting jobs.”
No alternative question tool, hand-authored provenance or fixture approval was
used to bypass that requirement.

Available neural environment: `venv/bin/python`, Python 3.12.13, VieNeu 3.8.3,
Turbo cache directory observed. Model integrity, model loading, selected device
and actual synthesis remain unverified. Test execution used Python 3.14.8.

Resume with the exact E1 Q01–Q10 interaction, validate setup, then execute
[E2–E8 and K0–K3](../V4_Remaining_Fixes_and_E2E_Handoff.md). A person must watch
the resulting real preview before its exact hashes are approved. G4–G7 remain
**OPEN**, and Dual-T4/production benchmark work remains blocked until G7 PASS.

## Subsequent Verification Checkpoint

At the later live E2E checkpoint (`055d17f`, with runtime `environment.json` recorded at `c890afeeeb2512e91f9044e7357d01568335120f`), real Chapter 1 E2E verification completed for project `nhan-vat-phu-trong-the-gioi-dien-loan` (chapter `1`) and recorded **G1** and **G4–G7** as **PASS** on the initial live run:

- **Canonical gate ledger:** [`docs/V4_Correctness_Status.md`](../V4_Correctness_Status.md)
- **Live E2E evidence package:** [`docs/verification/v4_e2e_2026-11-11/`](v4_e2e_2026-11-11/README.md) (real VieNeu-TTS v3 Turbo `"Ngọc Huyền"` synthesis across 37 clips / 304.24s, 9127-frame canonical preview render, explicit human approval by `cuongnguyen1802`, bit-for-bit final promotion, live Kaggle T4 run `cuongnguyen1802/mr-nhan-vat-phu-c1-6793` with `semantic_parity: true`, and 5/5 failure-path checks)

Because commit `055d17f` subsequently modified `src/cloud/kaggle_service.py` and added `scripts/wait_and_sync_kaggle.py`, an exact-HEAD P0–P7 E2E re-pin after CI and documentation cleanup remains pending before freezing P0–P7 and opening P8–P15 (Dual-T4, benchmarks, HF archive/restore, YouTube verification, receipt-gated cleanup, and canonical release state machine).
