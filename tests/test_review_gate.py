"""Tests for hooks/codex-review-gate.sh and hooks/review_gate.py.

Each test builds a throwaway git repository and replaces Codex with a stub that
records the prompt it receives and returns a canned verdict.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "hooks" / "codex-review-gate.sh"
FAKE_CODEX = """#!/usr/bin/env bash
set -euo pipefail
out=""
while [[ $# -gt 0 ]]; do
  if [[ $1 == --output-last-message ]]; then out=$2; shift; fi
  shift
done
calls=$(( $(cat "$FAKE_STATE/calls" 2>/dev/null || echo 0) + 1 ))
echo "$calls" > "$FAKE_STATE/calls"
cat > "$FAKE_STATE/prompt-$calls.md"
if [[ ${FAKE_CODEX_EXIT:-0} != 0 ]]; then echo "stub failure" >&2; exit "$FAKE_CODEX_EXIT"; fi
if [[ -n ${FAKE_NO_OUTPUT:-} ]]; then exit 0; fi
cp "$FAKE_STATE/verdict.json" "$out"
"""
BLOCKER = {
    "severity": "P1",
    "file": "app.py",
    "issue": "off-by-one in total()",
    "recommended_fix": "sum the full range",
}


def review(status: str, blockers: list | None = None) -> dict:
    return {
        "status": status,
        "round": 1,
        "blocking_issues": blockers or [],
        "non_blocking_suggestions": [],
        "summary": "stub review",
    }


APPROVED = review("APPROVED")
CHANGES = review("CHANGES_REQUESTED", [BLOCKER])


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@dataclass
class Project:
    root: Path
    state: Path
    codex: Path

    @property
    def reports(self) -> Path:
        return self.root / ".ai" / "reports"

    def edit(self, name: str = "app.py", text: str = "def total():\n    return 2\n") -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run(self, verdict: dict | str = APPROVED, **extra_env: str):
        text = verdict if isinstance(verdict, str) else json.dumps(verdict)
        (self.state / "verdict.json").write_text(text)
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.state),
            "CLAUDE_PROJECT_DIR": str(self.root),
            "REVIEW_GATE_CODEX": str(self.codex),
            "FAKE_STATE": str(self.state),
        }
        env.update(extra_env)
        return subprocess.run(
            ["bash", str(GATE)], cwd=self.root, env=env, capture_output=True, text=True
        )

    def calls(self) -> int:
        path = self.state / "calls"
        return int(path.read_text()) if path.exists() else 0

    def prompt(self, number: int) -> str:
        return (self.state / f"prompt-{number}.md").read_text()

    def decisions(self) -> list[dict]:
        log = self.reports / "qa_decisions.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()]

    def approval(self) -> dict:
        lines = (self.reports / "codex_approved").read_text().splitlines()
        return dict(line.split("=", 1) for line in lines)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "project"
    state = tmp_path / "state"
    (root / ".ai").mkdir(parents=True)
    state.mkdir()
    for name in ("codex_reviewer.md", "codex_review_schema.json"):
        shutil.copy(REPO / "templates" / name, root / ".ai" / name)
    (root / ".gitignore").write_text(".ai/reports/\n")
    (root / "app.py").write_text("def total():\n    return 1\n")
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    codex = tmp_path / "codex"
    codex.write_text(FAKE_CODEX)
    codex.chmod(0o755)
    return Project(root, state, codex)


def test_clean_tree_passes_without_review(project):
    result = project.run()
    assert result.returncode == 0
    assert project.calls() == 0


@pytest.mark.parametrize("how", ["env", "marker"])
def test_disabled_gate_passes_without_review(project, how):
    project.edit()
    env = {}
    if how == "env":
        env["REVIEW_GATE_DISABLE"] = "1"
    else:
        project.reports.mkdir(parents=True)
        (project.reports / "disabled").touch()
    result = project.run(**env)
    assert result.returncode == 0
    assert project.calls() == 0


def test_approval_lets_claude_stop_and_is_recorded(project):
    project.edit()
    result = project.run(APPROVED)
    assert result.returncode == 0, result.stderr
    assert project.approval()["auto_approved"] == "false"
    assert [d["outcome"] for d in project.decisions()] == ["APPROVED"]


def test_approved_state_is_not_reviewed_twice(project):
    project.edit()
    project.run(APPROVED)
    result = project.run(CHANGES)
    assert result.returncode == 0
    assert project.calls() == 1


def test_edit_after_approval_triggers_a_new_review(project):
    project.edit()
    project.run(APPROVED)
    project.edit(text="def total():\n    return 3\n")
    result = project.run(CHANGES)
    assert result.returncode == 2
    assert project.calls() == 2


def test_changes_requested_blocks_with_the_review_on_stderr(project):
    project.edit()
    result = project.run(CHANGES)
    assert result.returncode == 2
    assert "round 1 of 2" in result.stderr
    assert BLOCKER["issue"] in result.stderr
    assert not (project.reports / "codex_approved").exists()


def test_second_round_verifies_fixes_then_overrides_at_the_cap(project):
    project.edit()
    assert project.run(CHANGES).returncode == 2
    project.edit(text="def total():\n    return 4\n")
    result = project.run(CHANGES)
    assert result.returncode == 0, result.stderr
    second_prompt = project.prompt(2)
    assert "Fix-verification round" in second_prompt
    assert BLOCKER["issue"] in second_prompt
    approval = project.approval()
    assert approval["auto_approved"] == "true"
    assert approval["override_reason"] == "max_rounds_reached"
    outcomes = [d["outcome"] for d in project.decisions()]
    assert outcomes == ["CHANGES_REQUESTED", "AUTO_APPROVED"]


def test_round_cap_is_configurable(project):
    project.edit()
    assert project.run(CHANGES, REVIEW_GATE_MAX_ROUNDS="1").returncode == 0
    assert project.approval()["auto_approved"] == "true"


def test_new_file_set_restarts_at_round_one(project):
    project.edit()
    assert project.run(CHANGES).returncode == 2
    project.edit("helper.py", "VALUE = 1\n")
    result = project.run(CHANGES)
    assert result.returncode == 2
    assert "round 1 of 2" in result.stderr
    assert "Fix-verification round" not in project.prompt(2)


def test_failing_tests_block_without_using_a_round(project):
    project.edit()
    failed = project.run(CHANGES, REVIEW_GATE_TEST_CMD="echo test-marker; exit 3")
    assert failed.returncode == 2
    assert "Tests failed" in failed.stderr and "test-marker" in failed.stderr
    assert project.calls() == 0
    reviewed = project.run(CHANGES, REVIEW_GATE_TEST_CMD="true")
    assert reviewed.returncode == 2
    assert "round 1 of 2" in reviewed.stderr


def test_test_output_is_included_in_the_prompt(project):
    project.edit()
    project.run(APPROVED, REVIEW_GATE_TEST_CMD="echo 3 passed")
    assert "3 passed" in project.prompt(1)


@pytest.mark.parametrize(
    ("env", "verdict"),
    [
        ({"FAKE_CODEX_EXIT": "1"}, APPROVED),
        ({"FAKE_NO_OUTPUT": "1"}, APPROVED),
        ({}, "this is not json"),
        ({}, {"status": "MAYBE", "blocking_issues": []}),
    ],
    ids=["codex-fails", "no-review-file", "not-json", "unknown-status"],
)
def test_reviewer_failures_neither_block_nor_approve(project, env, verdict):
    project.edit()
    result = project.run(verdict, **env)
    assert result.returncode == 1
    assert "no approval was recorded" in result.stderr
    assert not (project.reports / "codex_approved").exists()


def test_reviewer_failure_does_not_use_a_round(project):
    project.edit()
    assert project.run(APPROVED, FAKE_CODEX_EXIT="1").returncode == 1
    assert "round 1 of 2" in project.run(CHANGES).stderr


def test_approved_verdict_with_blockers_counts_as_changes_requested(project):
    project.edit()
    result = project.run(review("APPROVED", [BLOCKER]))
    assert result.returncode == 2


def test_live_lock_skips_the_review(project):
    project.edit()
    project.reports.mkdir(parents=True)
    lock = project.reports / "codex_review_gate.lock"
    lock.write_text(f"{os.getpid()}\n{int(time.time())}\n")
    result = project.run(CHANGES)
    assert result.returncode == 0
    assert project.calls() == 0


def test_lock_of_a_dead_process_is_cleared(project):
    project.edit()
    project.reports.mkdir(parents=True)
    finished = subprocess.Popen(["true"])
    finished.wait()
    lock = project.reports / "codex_review_gate.lock"
    lock.write_text(f"{finished.pid}\n{int(time.time())}\n")
    result = project.run(APPROVED)
    assert result.returncode == 0
    assert project.calls() == 1
    assert not lock.exists()


def test_prompt_is_capped(project):
    project.edit("notes.txt", "line of text\n" * 2000)
    project.run(APPROVED, REVIEW_GATE_MAX_PROMPT_BYTES="4000")
    prompt = project.prompt(1)
    assert len(prompt.encode()) < 4100
    assert "[TRUNCATED: prompt exceeded 4000 bytes]" in prompt


def test_binary_untracked_files_are_not_inlined(project):
    project.edit("model.PKL", "binary-marker")
    project.run(APPROVED)
    prompt = project.prompt(1)
    assert "[binary, skipped]" in prompt
    assert "binary-marker" not in prompt


def test_disputed_issues_are_logged(project):
    project.edit()
    project.reports.mkdir(parents=True)
    summary = "## Summary\nDone.\n\n## Disputed Issues\nThe range is inclusive.\n"
    (project.reports / "claude_last.md").write_text(summary)
    project.run(CHANGES)
    dispute = project.decisions()[0]["claude_dispute"]
    assert dispute.startswith("## Disputed Issues")
    assert "The range is inclusive." in dispute


def test_missing_reviewer_template_is_a_setup_error(project):
    (project.root / ".ai" / "codex_reviewer.md").unlink()
    result = project.run()
    assert result.returncode == 1
    assert "missing .ai/codex_reviewer.md" in result.stderr


def test_directory_outside_git_is_a_setup_error(project, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = project.run(CLAUDE_PROJECT_DIR=str(plain))
    assert result.returncode == 1
    assert "not inside a git repository" in result.stderr


def load_helper():
    spec = importlib.util.spec_from_file_location("review_gate", REPO / "hooks" / "review_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_helper_reads_json_wrapped_in_text(tmp_path):
    helper = load_helper()
    path = tmp_path / "review.json"
    path.write_text("Here is the review:\n" + json.dumps(CHANGES) + "\nEnd.")
    assert helper.verdict(helper.load_review(path)) == ("CHANGES_REQUESTED", 1)


def test_helper_rejects_non_object_json(tmp_path):
    helper = load_helper()
    path = tmp_path / "review.json"
    path.write_text("[1, 2]")
    assert helper.verdict(helper.load_review(path)) == ("UNKNOWN", 0)
