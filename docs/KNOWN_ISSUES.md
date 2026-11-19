# Known Issues & Operational Backlog

## 1. Kaggle Cloud GPU `KernelWorkerStatus.QUEUED` Stalls (RESOLVED)
- **Observed:** On 2026-11-18 (`cuongnguyen1802/mr-tro-thanh-ro-c1-98762`), Kaggle GPU kernel remained stuck in `KernelWorkerStatus.QUEUED` for ~20 minutes (`17:46:32` → `18:06:03`) without transitioning to `RUNNING`. Also observed an earlier 709s (~12 min) queue wait on `mr-tro-thanh-ro-c1-40054`.
- **Verified Root Cause (`src/cloud/kaggle_service.py`):**
  1. `dispatch_chapter_job()` and `_poll_kernel_until_complete()` had no dedicated fail-fast `queue_timeout_seconds`, blocking `1800s`–`3600s` whenever Kaggle's `NvidiaTeslaT4` queue stalled or hit per-account concurrency/quota limits, logged zero queue heartbeats, missed `"CANCEL"` failure status in `dispatch_chapter_job()`, and did not persist `chapters/<n>/cloud_job.json`.
  2. Multi-account fallback (`with_account_fallback`) was never wired to CLI callers (`tts_cmd`, `render_cmd`, `batch_cmd`, `cloud_cmd`), leaving 3 additional healthy Kaggle accounts (`account_2`, `account_3`, `account_4`, `29.68h`–`30.00h` GPU quota remaining) idle whenever `existing` (`19.14h` remaining) queued.
  3. No GPU quota preflight (`get_accelerator_quota_statistics()`, returning `datetime.timedelta` fields) existed before kernel submission.
- **Fix Implemented:**
  - Added `get_gpu_quota_status()` (parsing `datetime.timedelta` and duration strings) and `list_accounts_quota()`, raising `KaggleQuotaExhaustedError` pre-push when weekly GPU quota is exhausted.
  - Unified polling in `_poll_kernel_until_complete()` with `queue_timeout_seconds=300.0` (5-min fail-fast), 30s queue heartbeats, `"CANCEL"` failure handling, and atomic `chapters/<n>/cloud_job.json` persistence (raising `KaggleQueueTimeoutError` on `QUEUED` stalls).
  - Exposed `dispatch_chapter_job_with_fallback()` / `dispatch_series_job_with_fallback()` and wired all CLI cloud callers (`tts_cmd`, `render_cmd`, `batch_cmd`, `cloud_cmd`) + `cloud info` quota reporting.

## 2. YouTube OAuth `InstalledAppFlow.run_local_server(port=0)` Unbounded Wait
- **Observed:** When token refresh fails with `invalid_grant`, `src/youtube/account_manager.py:314` launches an interactive local HTTP server with no timeout, hanging headless agent invocations indefinitely.
- **Proposed Fix (for future PR):**
  - Fail fast with exit code `4` during non-interactive uploads when refresh fails, prompting `manga-recap youtube accounts --account-action add` (or add `timeout_seconds=300` for interactive `-i` mode).
