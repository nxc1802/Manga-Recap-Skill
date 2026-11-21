# Manga Recap V5 Slim

Read the manga-recap skill and rulebooks for chapter production. The coding agent owns OCR, story and character comprehension, Vietnamese narration, and framing. Python handles deterministic ingestion, VieNeu-TTS, and FFmpeg. Use atomic local JSON writes and chapter checkpoints. Gemini Decision is limited to closed-set panel routing. Creative cloud APIs, cloud speech APIs, and external databases are out of scope. [Manga Recap skill](.agents/skills/manga-recap/SKILL.md)

## Setup and review

Before starting a new series, use `ask_question` or the host's structured question tool to collect Q01–Q05 and Q08–Q10. Stop before starting jobs if an answer is missing or the form is unavailable. Persist all eight choices with `setup record`. New series use adaptive motion and agent-generated thumbnails.

Record the approved single narrator in `voice_selection.json`. Chapter 1 requires actual human preview approval before final promotion and later-chapter production. After the user watches the preview, use the fingerprints from `review-pack` with `review-approve`. Immediately before YouTube upload, list the channels and reconfirm the destination and privacy. Check Kaggle and YouTube jobs after five minutes and report queue or OAuth stalls.

## Chapter pipeline

Pass `--json` to CLI calls. Run crawl → segment → decide → inspect. Read the contact sheets and character registry before writing `script.json`. Use natural Vietnamese dialogue and retain essential story panels. Mark ambiguous content `needs_review` with `[CAN_XAC_MINH: <lý do>]`. Save each scripting batch, validate, synthesize, and render. Use `status` to resume.
