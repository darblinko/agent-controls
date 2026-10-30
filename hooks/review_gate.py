#!/usr/bin/env python3
"""Helpers for codex-review-gate.sh.

review_gate.py verdict <review_file>
    Print "<STATUS> <blocking_count>". STATUS is APPROVED, CHANGES_REQUESTED
    or UNKNOWN. A review that does not match templates/codex_review_schema.json
    is UNKNOWN. An APPROVED verdict that lists blocking issues is reported as
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
SEVERITIES = {"P0", "P1", "P2"}
# Field sets of templates/codex_review_schema.json; every field is required and
# no other field is allowed.
REVIEW_FIELDS = ("status", "round", "blocking_issues", "non_blocking_suggestions", "summary")
ISSUE_FIELDS = ("severity", "file", "issue", "recommended_fix")
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


def valid_issue(issue: object) -> bool:
    return (
        isinstance(issue, dict)
        and set(issue) == set(ISSUE_FIELDS)
        and all(isinstance(issue[field], str) for field in ISSUE_FIELDS)
        and issue["severity"] in SEVERITIES
    )


def valid_review(review: dict) -> bool:
    """True when the review matches the schema in full."""
    if set(review) != set(REVIEW_FIELDS):
        return False
    issues, suggestions = review["blocking_issues"], review["non_blocking_suggestions"]
    return (
        review["status"] in VALID_STATUSES
        and isinstance(review["round"], int)
        and not isinstance(review["round"], bool)
        and isinstance(issues, list)
        and all(valid_issue(issue) for issue in issues)
        and isinstance(suggestions, list)
        and all(isinstance(item, str) for item in suggestions)
        and isinstance(review["summary"], str)
    )


def verdict(review: dict | None) -> tuple[str, int]:
    if review is None or not valid_review(review):
        return "UNKNOWN", 0
    count = len(review["blocking_issues"])
    if review["status"] == "APPROVED" and count > 0:
        return "CHANGES_REQUESTED", count
    return review["status"], count


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
