# Human review gates

1. Collect and record the eight explicit setup choices from Stage 0.
2. Confirm the single narrator and speed. Store the approval in `voice_selection.json`. Use `tts_benchmark` for listening samples when needed.
3. Ask the user to watch Chapter 1's preview. After approval, pass the config and artifact fingerprints from `review-pack` to `review-approve`. The approved MP4 becomes Chapter 1 final. Later chapters inherit the gate.
4. Immediately before YouTube upload, list the accounts and confirm destination and privacy. Check for Kaggle queue and OAuth stalls after five minutes.
