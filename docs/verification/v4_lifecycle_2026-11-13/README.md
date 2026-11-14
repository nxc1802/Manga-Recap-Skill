# Manga-Recap V4 Lifecycle Verification Report (2026-11-13)

This directory contains cryptographic receipts and execution evidence proving the live, end-to-end completion of **P10 (Hugging Face Archive & Restore)**, **P11 (YouTube Upload & Verification)**, **P12 (Safe Cleanup & Tombstone)**, and **P14/P15 (State Machine & Final Release Gate)** for series `nhan-vat-phu-trong-the-gioi-dien-loan` (Chapter 1).

---

## 1. Phase P11 — YouTube Upload & Verification (Live)

- **Target Channel**: `Đầu Tư Nhàn Rỗi Và Thông Minhh` (`UCpTOfO8g5i1Jxg77gee7T9Q`)
- **Video ID**: `bGpDJ15um7Q` ([Watch Video](https://www.youtube.com/watch?v=bGpDJ15um7Q))
- **Privacy Status**: `unlisted`
- **Video SHA-256**: `d6d8f75bb868736765dfb58afc4c1f25e69183c73c5a327633cae1a842b64b4b`
- **Captions**: Vietnamese SRT (`final.srt`) uploaded & verified (`has_captions: true`)
- **Thumbnail**: Custom high-contrast thumbnail (`thumbnail.jpg`) uploaded & verified (`has_custom_thumbnail: true`)
- **Processing Status**: Polled YouTube Data API until `upload_status: "processed"` and `verified: true`
- **Receipt**: `youtube_receipt.json` emitted as a SHA-256 integrity-bound receipt.

---

## 2. Phase P10 — Hugging Face Private Dataset Archive & Round-Trip Restore (Live)

- **Target Dataset**: Private repository [`Cuong2004/manga-recap-archive`](https://huggingface.co/datasets/Cuong2004/manga-recap-archive)
- **Commit SHA**: `5575202b5f2d28a2bbe2e61f684a5add8b5d83ee`
- **Total Archived**: 1,156 files (514,313,185 bytes / 514.3 MB)
- **Manifest**: `archive_manifest.json` (SHA-256: `a1b9c753b90eeda88fc94422ccca2ec50a953964960bcf90ef444433efb3d251`)
- **Remote Verification**: `manga-recap archive verify` checked all 1,156 files; remote tree matched manifest 100%.
- **Sandbox Restore Round-Trip**:
  - Target directory: `/tmp/manga_recap_restore_verify`
  - Result: 1,156 files downloaded and verified
  - Pydantic Schemas Validated: 19 files
  - Media Streams Validated (ffprobe / PIL / SRT): 1,129 files
  - Semantic Round-Trip Check: **PASSED**
- **Receipt**: `archive_receipt.json` updated with `restore_verified_at` and `restored_verified_files: 1156`.

---

## 3. Phase P12 — Two-Receipt Safe Cleanup Protocol (Live)

- **Two-Receipt Gate**:
  - Gate 1 (`YouTubeReceipt`): Verified for Chapter 1 (`video_id: bGpDJ15um7Q`).
  - Gate 2 (`ArchiveReceipt`): Verified with round-trip restore verification (`restored_revision: 5575202b...`).
- **State Transition**: State machine evaluated and transitioned to `ReleaseState.CLEANUP_ALLOWED`.
- **Cleanup Execution**:
  - Command: `manga-recap clean -p nhan-vat-phu-trong-the-gioi-dien-loan -c 1 --confirm-project nhan-vat-phu-trong-the-gioi-dien-loan --no-dry-run --json`
  - Cleaned files: 39 intermediate clips, audio utterances, and silent video tracks
  - Space reclaimed: 87,645,876 bytes (83.59 MB)
  - Preserved assets: `final.mp4`, `final.srt`, `thumbnail.jpg`, manifests, scripts, project metadata
- **Tombstone**: `tombstone.json` created:
  - Exact size: **795 bytes** (strictly < 2048 bytes requirement)
  - Records remote HF URL, commit SHA, YouTube video mapping, and timestamps.

---

## 4. Phase P14 & P15 — Release State Machine & Final Release Gate

The project successfully traversed all 16 canonical release states without skipping or drifting:

```
DRAFT
  ↓
SETUP_VALID (Q01–Q10 Strict Form verified)
  ↓
SOURCE_READY
  ↓
SCRIPT_READY
  ↓
AUDIO_VALID (VieNeu-TTS neural speech synthesized)
  ↓
VISUAL_PLAN_VALID (Top-down slow motion & smart crop)
  ↓
RENDER_VALID (Zero-reencode FFmpeg render)
  ↓
TECHNICALLY_VALID (ffprobe & PIL artifact validation)
  ↓
HUMAN_APPROVED (Gate 2 preview approval)
  ↓
PRODUCTION_READY (Output packaged)
  ↓
UPLOADED (YouTube video uploaded)
  ↓
YOUTUBE_VERIFIED (Captions, thumbnail, processed status)
  ↓
ARCHIVED (HF commit pushed)
  ↓
ARCHIVE_VERIFIED (Remote SHA-256 manifest check)
  ↓
RESTORE_VERIFIED (Sandbox round-trip media validation)
  ↓
CLEANUP_ALLOWED (Two-receipt gate satisfied, tombstone written)
```

Final state in `release_state.json`: **`CLEANUP_ALLOWED`** with 22 immutable evidence bindings.
