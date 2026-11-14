# Acceptance scope outside deterministic CI

`pytest -q --disable-warnings` collects `tests/` (L1/L2). It must not start
network crawls, real chapter jobs, Kaggle jobs, or performance workloads.

- `p8/`: historical Dual-T4/benchmark specifications, deferred until real G7 PASS.
  These files are **unexecuted and unaccepted**, not passing tests. The old
  source-string assertions and fake throughput fixtures need migration when P8
  is authorized; do not use them for benchmark claims.
- `l4/`: historical network/workspace-dependent specifications. They are not a
  current real-chapter acceptance runner. Their old bypass assumptions must be
  replaced with the exact Q01–Q10 and human approval flow before execution.

Current real Local/Cloud acceptance uses
[`V4_Remaining_Fixes_and_E2E_Handoff.md`](../docs/V4_Remaining_Fixes_and_E2E_Handoff.md),
steps E0–E8 and K0–K3. It requires actual user setup, installed neural runtime,
real source/media, human review and a live Kaggle run. Missing prerequisites
leave gates OPEN/BLOCKED. Historical specifications supply no gate evidence.

Current deterministic tests explicitly verify that Dual-T4 entry points remain
disabled and that compatibility flags cannot bypass G6. No xfail or mass skip
is used to hide regressions.
