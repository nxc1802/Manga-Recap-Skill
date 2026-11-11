# Remaining fixes: verification result

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
