---
description: Turn the Codex review gate on or off, or show its state
argument-hint: on | off | status
---

# Codex review gate

Argument: $ARGUMENTS

The gate reads its state from files under `.ai/reports/` in the project root. An exported environment variable set from a Bash tool call does not reach the Stop hook, so this command uses a marker file.

- `off`: create `.ai/reports/disabled` (run `mkdir -p .ai/reports && touch .ai/reports/disabled`). Report that the Stop hook now passes through without review.
- `on`: remove `.ai/reports/disabled`, `.ai/reports/codex_review_gate.lock`, `.ai/reports/codex_round_count` and `.ai/reports/codex_batch_id`. Report that the next stop runs the tests and a Codex review.
- `status` or no argument: report whether `.ai/reports/disabled` exists, whether `.ai/reports/codex_review_gate.lock` exists (with the PID and age from its two lines), the completed round count from `.ai/reports/codex_round_count`, and the last 5 lines of `.ai/reports/qa_activity.log`.
