# Manga Recap Agentic Skill

Agent-native Vietnamese manga recap tooling: the coding agent reads images and
writes narration; deterministic Python handles ingestion, segmentation, local
VieNeu-TTS audio, VisualPlan rendering and artifact validation. Persistence is
local JSON/Markdown under `data/projects/`. The core has no cloud LLM/TTS API or
database dependency. Kaggle can run the same neural/media runtime on a remote GPU.

**V4 P0–P7 are PASS and FROZEN on exact tested code tree `d1b032eaf5f2ed178fc28e71740664bb778dacd5` (`90f1786e31f45ddcac38376a3161ac9a7fff615b`). Branch not yet release ready overall while P8–P15 remain OPEN / IN PROGRESS.**
Exact-HEAD deterministic checks (`794 passed`, zero failures/skips), GitHub Actions CI
([PR #1 run `43083930743`](https://github.com/nxc1802/Manga-Recap-Skill/actions/runs/43083930743)
and [`push` run `43083921539`](https://github.com/nxc1802/Manga-Recap-Skill/actions/runs/43083921539)),
and real Chapter 1 E2E verification (G0–G7: real VieNeu-TTS synthesis, canonical
preview render, explicit human approval, bit-for-bit final promotion, live Kaggle
Tesla T4 semantic parity, and 8/8 failure-path checks) passed and are recorded in
[docs/verification/v4_e2e_2026-11-12/](docs/verification/v4_e2e_2026-11-12/README.md)
(superseding the initial live checkpoint in
[docs/verification/v4_e2e_2026-11-11/](docs/verification/v4_e2e_2026-11-11/README.md)).
Future evidence/documentation commits do not alter canonical P0–P7 correctness
code unless explicitly re-opened; P8–P15 (Dual-T4, benchmarks, HF archive/restore,
YouTube verification, receipt-gated cleanup, test pyramid, and canonical release
state machine) remain OPEN / IN PROGRESS next. See the canonical
[gate ledger](docs/V4_Correctness_Status.md),
[controlling plan](docs/V4_Correctness_Improvement_Plan.md) and
[remaining fixes / E2E handoff](docs/V4_Remaining_Fixes_and_E2E_Handoff.md).

## Branch contract

| Scope | Current status |
|---|---|
| V3 stable / compatibility | Existing V3 schema reads and utility APIs remain supported where explicitly allowed by strict schemas. Historical V3 reports do not establish V4 acceptance. |
| V4 deterministic baseline & CI (P0–P7) | Runtime Q01–Q10 checks, canonical contracts, shared Local/Cloud renderer, media validators, hash-bound approval and exact Chapter 1 promotion. Deterministic L1/L2 suite green (`794 passed` at `90f1786`); GitHub Actions CI (`43083930743`, `43083921539`) PASS. |
| V4 P0–P7 live-verified & FROZEN | Exact-HEAD Chapter 1 E2E closed **G0–G7 PASS** on tested code tree `d1b032ea...` (`90f1786`; [v4_e2e_2026-11-12](docs/verification/v4_e2e_2026-11-12/README.md)): VieNeu-TTS v3 Turbo (`"Ngọc Huyền"`, 37 clips, `303.68s`), preview render (`9110` frames, `187` SRT cues), human approval (`cuongnguyen1802`), bit-for-bit final promotion (`d6d8f75b...`), live Kaggle T4 run (`cuongnguyen1802/mr-nhan-vat-phu-c1-40228`, `1756s`, `389` files synced, `semantic_parity: true`), and `8/8` failure-path checks. Future evidence/doc commits do not alter canonical P0–P7 correctness code unless explicitly re-opened. |
| Open P8–P15 roadmap | Dual-T4 multiprocessing & benchmarks (P8–P9) and HF private archive/restore, YouTube processed verification, receipt-gated cleanup, test pyramid & canonical release state machine (P10–P15) remain OPEN / IN PROGRESS next. |

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
