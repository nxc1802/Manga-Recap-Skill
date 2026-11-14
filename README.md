# Manga Recap Agentic Skill

Agent-native Vietnamese manga recap tooling: the coding agent reads images and
writes narration; deterministic Python handles ingestion, segmentation, local
VieNeu-TTS audio, VisualPlan rendering and artifact validation. Persistence is
local JSON/Markdown under `data/projects/`. The core has no cloud LLM/TTS API or
database dependency. Kaggle can run the same neural/media runtime on a remote GPU.

**V4 P0–P15 ALL PHASES ARE PASS, VERIFIED, AND RELEASE-READY on branch `feat/v4-strict-form-motion-cloud-archive`.**
Exact-HEAD (`b651db3c`) deterministic checks (`896 passed`, zero failures/skips across CI-L1, CI-L2, and CI-L3), GitHub Actions CI
([PR #2 push run `43466223358`](https://github.com/nxc1802/Manga-Recap-Skill/actions/runs/43466223358)
and [PR run `43466235935`](https://github.com/nxc1802/Manga-Recap-Skill/actions/runs/43466235935)),
real Chapter 1 E2E verification (G0–G7: real VieNeu-TTS synthesis, canonical
preview render, explicit human approval, bit-for-bit final promotion, live Kaggle
Tesla T4 semantic parity, and 8/8 failure-path checks) recorded in
[docs/verification/v4_e2e_2026-11-12/](docs/verification/v4_e2e_2026-11-12/README.md),
live Kaggle Dual-T4 multi-chapter production (Gate G8, [docs/verification/v4_g8_2026-11-12/](docs/verification/v4_g8_2026-11-12/)),
real Kaggle Dual-T4 benchmark matrix (Gate G9, B0–B5 evaluated across 12 runs, [docs/benchmarks/kaggle_dual_t4_benchmark_v4.md](docs/benchmarks/kaggle_dual_t4_benchmark_v4.md)
and [docs/verification/v4_p9_2026-11-12/](docs/verification/v4_p9_2026-11-12/README.md)),
and complete lifecycle verification (P10–P15: YouTube upload & verified processed status, HF private dataset archive & round-trip restore, two-receipt safe cleanup with tombstone <2048 bytes, and release state machine `CLEANUP_ALLOWED` traversal, [docs/verification/v4_lifecycle_2026-11-13/](docs/verification/v4_lifecycle_2026-11-13/README.md))
are fully verified. See the canonical [gate ledger](docs/V4_Correctness_Status.md) for detailed gate proofs.

## Branch contract

| Scope | Current status |
|---|---|
| V3 stable / compatibility | Existing V3 schema reads and utility APIs remain supported where explicitly allowed by strict schemas. Historical V3 reports do not establish V4 acceptance. |
| V4 deterministic baseline & CI (P0–P7) | Runtime Q01–Q10 checks, canonical contracts, shared Local/Cloud renderer, media validators, hash-bound approval and exact Chapter 1 promotion. Deterministic L1/L2 suite green (`794 passed` at `90f1786`); GitHub Actions CI (`43083930743`, `43083921539`) PASS. |
| V4 P0–P7 live-verified & FROZEN | Exact-HEAD Chapter 1 E2E closed **G0–G7 PASS** on tested code tree `d1b032ea...` (`90f1786`; [v4_e2e_2026-11-12](docs/verification/v4_e2e_2026-11-12/README.md)): VieNeu-TTS v3 Turbo (`"Ngọc Huyền"`, 37 clips, `303.68s`), preview render (`9110` frames, `187` SRT cues), human approval (`cuongnguyen1802`), bit-for-bit final promotion (`d6d8f75b...`), live Kaggle T4 run (`cuongnguyen1802/mr-nhan-vat-phu-c1-40228`, `1756s`, `389` files synced, `semantic_parity: true`), and `8/8` failure-path checks. |
| V4 Dual-T4 & Benchmarks (P8–P9) | Multi-chapter Dual-T4 production verified (**Gate G8 PASS**, [docs/verification/v4_g8_2026-11-12/](docs/verification/v4_g8_2026-11-12/)) and empirical B0–B5 benchmark matrix verified (**Gate G9 PASS**, [docs/benchmarks/kaggle_dual_t4_benchmark_v4.md](docs/benchmarks/kaggle_dual_t4_benchmark_v4.md)). |
| V4 Lifecycle & Architecture (P10–P15) | HF private archive/restore (P10), YouTube upload & verification (P11), safe receipt-gated cleanup (P12), test pyramid L1/L2/L3 split with 896 tests passing on GitHub Actions (P13), and canonical ReleaseState machine with zero bypasses (P14) are 100% verified live. Final release gate (P15) **PASS & VERIFIED**. |

## Correctness rules

- A new series requires ten explicit user choices through the skill's
  `ask_question` protocol. The CLI validates the persisted record, revision,
  snapshot hash and matching project settings. It cannot prove a human interaction
  from self-asserted JSON. Agents must stop when that tool or any answer is missing.
- Narration uses `evidence_panel_ids`; an authored N:M VisualPlan may omit evidence
  visually while retaining provenance. Missing/altered source evidence fails closed.
- Production motion is `static`, `slow_pan_down` or `static_focus`. Legacy camera
  enums are not accepted by the production VisualPlan.
- Tall panels use the frozen **H/W > 2.4** boundary. Multiple crops follow canonical
  regions in top-to-bottom order, including authored static crops. A full static
  hold is allowed; uncertain text evidence requires conservative framing.
- Bands shorter than **80 px** are omitted as `short_sliver`; narration requires
  an evidenced substitute. Missing substitutes stop plan generation.
- Production audio requires neural VieNeu-TTS and validated non-silent PCM.
  Rendering validates decoded video/audio, dimensions, frame count, timeline,
  subtitle cues and input/media fingerprints before reporting completion.
- Before human approval, a Chapter 1 preview is technically validated only; a person
  must watch the exact artifact before `review-approve` records its hashes.
  Final Chapter 1 copies those reviewed bytes. Changed reviewed inputs invalidate
  approval. `--approved` and `--skip-preview` cannot bypass this gate.
- Cloud payloads include the shared runtime and a hashed inventory. Downloads
  require the trusted dispatch snapshot, result hashes and semantic validation
  before publication. Starting a kernel or downloading files is not acceptance.
- Cleanup requires its receipt protocol; destructive `--bypass-receipts` is rejected.
  Test fixtures, generated signals and fixture approvals never count as real E2E evidence.

## Setup and commands

Python 3.10+, FFmpeg and FFprobe are required. Install the project in an isolated
environment; the neural runtime/model must be provisioned before real synthesis.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m src.cli.main --help
```

Read [AGENTS.md](AGENTS.md) and the
[manga-recap skill](.agents/skills/manga-recap/SKILL.md) before running a series.
These commands assume an explicitly confirmed setup and real chapter assets:

```bash
manga-recap setup validate -p <slug> --json
manga-recap inspect -p <slug> -c 1 --json
manga-recap validate -p <slug> -c 1 --strict --json
manga-recap tts -p <slug> -c 1 --json
manga-recap visual-plan -p <slug> -c 1 --json
manga-recap render -p <slug> -c 1 --quality preview --json
manga-recap review-pack -p <slug> -c 1 --format all --json
```

After a person reviews the exact preview, use the hashes emitted by the review
pack with `review-approve`. Then render `--quality final` for byte-preserving
promotion. The [handoff runbook](docs/V4_Remaining_Fixes_and_E2E_Handoff.md)
describes required evidence and single-chapter Kaggle verification.
Current framing supports `auto` / `landscape_fit`; unsupported portrait settings
fail closed. Consult command help for supported overrides.

## Development checks

```bash
python3 -m compileall -q src tests
PYTHONPATH=. python3 scripts/freeze_contracts.py --check
python3 -m pytest -q --disable-warnings
git diff --check
```

The default suite is deterministic L1/L2; it includes real-format PCM and actual
FFmpeg encoding/decoding with isolated test inputs. It is not neural, human or
live Kaggle acceptance. Legacy assertions consolidated into canonical scenarios
are tracked in [the migration ledger](docs/verification/v4_regression_migration.json).
[Deferred/live specifications](acceptance/README.md) are explicitly outside this
suite and are not reported as passing. Current gate status and exact-HEAD E2E
evidence are tracked in [the canonical gate ledger](docs/V4_Correctness_Status.md)
and `docs/verification/`, not a static test badge.

## Layout

```text
.agents/skills/manga-recap/   Agent workflow, rulebooks and templates
src/cli/                      JSON CLI and guarded entry points
src/core/                     Setup, schemas, storage and human review
src/tts/                      Offline neural synthesis adapter
src/render/                   Visual policy, canonical renderer and validators
src/cloud/                    Payload bootstrap, canonical worker and result sync
schemas/v4/                   Frozen public contract snapshots
tests/                        Deterministic L1/L2 tests
acceptance/                   Deferred/live specifications, not gate evidence
data/projects/<slug>/         Local project configuration and chapter assets
```
