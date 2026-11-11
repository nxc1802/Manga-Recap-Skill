# Manga Recap V4 End-to-End Verification Package (2026-11-11)

This directory contains the complete and authentic evidence package for the V4 Chapter 1 End-to-End verification on both Local and Live Kaggle environments.

## Verification Summary

| Gate | Scope | Status | Evidence Document | Key Hash / Metric |
|---|---|---|---|---|
| **G4** | Real VieNeu-TTS Synthesis | **PASS** | `local_run.json` | 37 clips, 304.24s duration, 100% non-silent, RMS > 0.10, "Ngọc Huyền" |
| **G5** | Canonical Render Preview | **PASS** | `local_artifact_report.json` | 9127 frames, 304.24s, Video SHA: `f74608cdf29c1afe130909f01ac7f69f0e1f58bc4b8f468697f64c68c344f330` |
| **G6** | Human Approval Gate | **PASS** | `human_approval.json`, `human_review.md` | Approved by `cuongnguyen1802`, final artifact bit-for-bit matched preview |
| **G7** | Live Kaggle Cloud Run | **PASS** | `kaggle_run.json`, `semantic_parity.json` | Kernel `cuongnguyen1802/mr-nhan-vat-phu-c1-6793`, duration 769s, parity `true` |
| **K3** | Failure-Path Hardening | **PASS** | `failure_path_checks.json` | 5/5 failure scenarios rejected fail-closed |

## Evidence Files Description

- `environment.json`: Host machine OS, Python, ffmpeg/ffprobe, torch/onnxruntime, and VieNeu versions.
- `local_run.json`: Complete record of local Chapter 1 execution, inputs, fingerprints, and outputs.
- `local_artifact_report.json`: Frame variance, RMS per audio segment, and subtitle count for Chapter 1 preview.
- `human_approval.json`: Cryptographically bound review approval record linking config fingerprint and artifact hash.
- `human_review.md`: Qualitative reviewer checklist detailing story fidelity, crop safety, audio, and pacing.
- `kaggle_run.json`: Record of Kaggle cloud execution, kernel ID, payload manifest digest, and sync verification report.
- `kaggle_result_manifest.json`: Downloaded remote execution result manifest and inventory from Kaggle.
- `semantic_parity.json`: Verified semantic parity between local and cloud runs (VisualPlan, script, audio duration, timeline).
- `failure_path_checks.json`: Automated empirical validation of 5 critical security and tamper rejection failure-paths.
- `sha256sums.txt`: SHA-256 digests of all verification files in this package.
