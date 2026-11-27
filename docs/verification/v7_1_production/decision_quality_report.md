# V7.1 Decision Quality Report

- **Model:** `gemini-decision-1` (`4sided` policy)
- **Chapters 1–5:** Prepared, visually inspected via `contact_sheets/`, repacked cleanly, and validated with `validate --pacing --json` + `validate_narration_evidence`.
- **Chapters 6–10:**
  - Ch. 6: `92` panels (`65` active, `14` context_only, `13` skip after repack).
  - Ch. 7: `115` panels (`72` active, `4` context_only, `39` skip).
  - Ch. 8: `129` panels (`70` active, `9` context_only, `50` skip).
  - Ch. 9: `120` panels (`68` active, `15` context_only, `37` skip).
  - Ch. 10: `127` panels (`73` active, `11` context_only, `43` skip, `0` needs_review).
- All Chapters 6–10 passed `validate --pacing --json` (`0` errors, `0` warnings) and `validate_narration_evidence` prior to Kaggle dispatch.
