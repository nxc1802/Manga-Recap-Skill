# YouTube publishing

Use `youtube metadata` to create a Vietnamese title, description, tags, and timestamps. Review the title against the story arc and timestamps against the final timeline.

Generate a 16:9 image as the agent-created thumbnail and save it as `thumbnail.jpg` or `thumbnail.png` (or pass its path via `youtube upload --thumbnail <path>`).

Run `youtube accounts --json` and reconfirm the channel and visibility right before upload. Supply `--account` and `--privacy` for headless publishing or use `-i` in a terminal. To renew OAuth, use `youtube accounts --account-action add --account <name>` from a terminal. Use `youtube verify` or upload with `--wait` to check processing and attachments.
