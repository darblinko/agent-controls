#!/usr/bin/env python3
"""Helpers for codex-review-gate.sh.

review_gate.py verdict <review_file>
    Print "<STATUS> <blocking_count>". STATUS is APPROVED, CHANGES_REQUESTED
    or UNKNOWN. An APPROVED verdict that lists blocking issues is reported as
    CHANGES_REQUESTED.

review_gate.py decision <timestamp> <branch> <commit> <round> <outcome>
                        <prompt_bytes> <reason> <review_file> <summary_file>
    Print one JSON Lines decision entry.
"""

import json
import re
import sys
from pathlib import Path

VALID_STATUSES = {"APPROVED", "CHANGES_REQUESTED"}
DISPUTE_HEADING = "## Disputed Issues"
MAX_RAW_CHARS = 2000
MAX_DISPUTE_CHARS = 3000


def load_review(path: Path) -> dict | None:
    """Parse the review file; accept JSON wrapped in other text."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def verdict(review: dict | None) -> tuple[str, int]:
    if review is None:
        return "UNKNOWN", 0
    issues = review.get("blocking_issues")
    count = len(issues) if isinstance(issues, list) else 0
    status = review.get("status")
    if status not in VALID_STATUSES:
        return "UNKNOWN", count
    if status == "APPROVED" and count > 0:
        return "CHANGES_REQUESTED", count
    return status, count


def dispute_section(summary_path: Path) -> str | None:
    """Return the '## Disputed Issues' section of Claude's summary, if any."""
    try:
        text = summary_path.read_text(encoding="utf-8")
    except OSError:
        return None
    start = text.find(DISPUTE_HEADING)
    if start < 0:
        return None
    end = text.find("\n## ", start + 1)
    section = text[start:end] if end > start else text[start:]
    return section.strip()[:MAX_DISPUTE_CHARS]


def raw_review(path: Path) -> dict:
    try:
        return {"_raw": path.read_text(encoding="utf-8").strip()[:MAX_RAW_CHARS]}
    except OSError:
        return {}


def decision_entry(args: list[str]) -> dict:
    timestamp, branch, commit, round_num, outcome, prompt_bytes, reason, review, summary = args
    review_path = Path(review)
    parsed = load_review(review_path)
    return {
        "timestamp": timestamp,
        "branch": branch,
        "commit": commit,
        "round": int(round_num),
        "outcome": outcome,
        "reason": reason,
        "codex_review": parsed if parsed is not None else raw_review(review_path),
        "claude_dispute": dispute_section(Path(summary)),
        "prompt_bytes": int(prompt_bytes),
    }


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "verdict":
        status, count = verdict(load_review(Path(argv[1])))
        print(f"{status} {count}")
        return 0
    if len(argv) == 10 and argv[0] == "decision":
        print(json.dumps(decision_entry(argv[1:])))
        return 0
    print(__doc__, file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
