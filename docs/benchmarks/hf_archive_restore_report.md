# Hugging Face archive V4 — Verification audit / NOT YET LIVE VERIFIED

**Review date:** 2026-11-11
**Status:** BLOCKED for destructive cleanup until a genuine private remote round-trip passes.

## Why previous “VERIFIED ROUND-TRIP” wording was withdrawn

The previous document showed an illustrative SHA beginning `9f8e7d6...` and a 10-file / 1,569-byte fixture. It did **not** include independently verifiable evidence of a live private Hugging Face upload and restore. The original code marked the archive receipt as `verified` immediately after upload; checked SHA only for some metadata (silently ignoring failed downloads), and restore checked file **sizes and media headers**, not SHA-256 of every restored asset. None of these are adequate evidence for destroying local originals.

## Code review fixes now on V4 branch

- `archive_push` writes `status=uploaded` and cannot authorize cleanup.
- `archive_verify` pins the uploaded revision, checks manifest identity and downloads/hashes **every** whitelisted remote asset including MP4/WAV/images. It fails if any SHA differs or an item cannot be fetched.
- `archive_restore` checks full restored file SHA-256 in addition to required schema and media headers; only successful matching pinned revision populates restore receipt data.
- `clean` rejects the former destructive `--bypass-receipts` path and requires verified remote + restore evidence.

## Remaining release blockers

1. Execute a real private HF dataset upload with scoped `HF_TOKEN` and verify the repository is private.
2. Test an archive containing **all required original and exported assets**, including files in the separate `output/<slug>/` directory; the current scanner mainly covers `data/projects/<slug>/`.
3. Restore the complete series to a **new empty** directory, check all hashes and playback, and reproduce the original scripts and panel mapping.
4. Confirm YouTube `videos.list` returns the right **processed** video, channel and selected privacy, and that requested captions/thumbnail are present before cleanup.
5. Test interrupted upload, truncated LFS media, stale receipt, changed manifest, mismatched channel, skipped chapter and secret/symlink filtering.
6. Confirm a user-authorized cleanup policy. Keep default `--dry-run`; do not delete whole local series automatically.

**No live HF API operation, upload, download, restore or destructive cleanup was executed as part of this code review.**
