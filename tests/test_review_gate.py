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
echo call >> "$FAKE_STATE/invocations"
calls=$(wc -l < "$FAKE_STATE/invocations")
printf '%s\\n' "$@" > "$FAKE_STATE/args-$calls"
out=""
while [[ $# -gt 0 ]]; do
  if [[ $1 == --output-last-message ]]; then out=$2; shift; fi
  shift
done
cat > "$FAKE_STATE/prompt-$calls.md"
if [[ -n ${FAKE_SLEEP:-} ]]; then sleep "$FAKE_SLEEP"; fi
if [[ -n ${FAKE_EDIT:-} ]]; then echo "# edited during the review" >> "$FAKE_EDIT"; fi
if [[ -n ${FAKE_TAKEOVER:-} ]]; then printf '1\\n%s\\n' "$(date +%s)" > "$FAKE_TAKEOVER"; fi
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

    def env(self, verdict: dict | str, extra_env: dict) -> dict:
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
        return env

    def run(self, verdict: dict | str = APPROVED, **extra_env: str):
        return subprocess.run(
            ["bash", str(GATE)],
            cwd=self.root,
            env=self.env(verdict, extra_env),
            capture_output=True,
            text=True,
        )

    def start(self, verdict: dict | str = APPROVED, **extra_env: str) -> subprocess.Popen:
        return subprocess.Popen(
            ["bash", str(GATE)],
            cwd=self.root,
            env=self.env(verdict, extra_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def calls(self) -> int:
        path = self.state / "invocations"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def args(self, number: int) -> list[str]:
        return (self.state / f"args-{number}").read_text().splitlines()

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


def test_new_staged_content_behind_the_same_worktree_triggers_a_new_review(project):
    project.edit(text="def total():\n    return 2\n")
    git(project.root, "add", "app.py")
    project.edit(text="def total():\n    return 3\n")
    assert project.run(APPROVED).returncode == 0
    project.edit(text="def total():\n    return 4\n")
    git(project.root, "add", "app.py")
    project.edit(text="def total():\n    return 3\n")
    project.run(APPROVED)
    assert project.calls() == 2


def test_binary_edit_triggers_a_new_review(project):
    (project.root / "data.bin").write_bytes(b"\x00first")
    git(project.root, "add", "data.bin")
    git(project.root, "commit", "-q", "-m", "binary")
    (project.root / "data.bin").write_bytes(b"\x00second")
    assert project.run(APPROVED).returncode == 0
    (project.root / "data.bin").write_bytes(b"\x00third")
    project.run(APPROVED)
    assert project.calls() == 2


@pytest.mark.parametrize("verdict", [APPROVED, CHANGES], ids=["approved", "auto-approved"])
def test_edit_during_the_review_is_not_approved(project, verdict):
    project.edit()
    result = project.run(
        verdict, FAKE_EDIT=str(project.root / "app.py"), REVIEW_GATE_MAX_ROUNDS="1"
    )
    assert result.returncode == 1
    assert "files changed while Codex reviewed them" in result.stderr
    assert not (project.reports / "codex_approved").exists()
    project.run(APPROVED)
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
        ({}, {"status": "APPROVED"}),
        ({}, {**APPROVED, "blocking_issues": ["not an object"]}),
    ],
    ids=[
        "codex-fails",
        "no-review-file",
        "not-json",
        "unknown-status",
        "status-only",
        "string-blocker",
    ],
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


def test_unparseable_review_keeps_the_previous_blockers(project):
    project.edit()
    assert project.run(CHANGES).returncode == 2
    project.edit(text="def total():\n    return 4\n")
    failed = project.run("this is not json")
    assert failed.returncode == 1
    assert "this is not json" in failed.stderr
    assert project.run(CHANGES).returncode == 0
    third_prompt = project.prompt(3)
    assert BLOCKER["issue"] in third_prompt
    assert "this is not json" not in third_prompt
    assert project.decisions()[1]["codex_review"] == {"_raw": "this is not json"}


def test_approved_verdict_with_blockers_counts_as_changes_requested(project):
    project.edit()
    result = project.run(review("APPROVED", [BLOCKER]))
    assert result.returncode == 2


def test_simultaneous_stops_run_one_review(project):
    project.edit()
    project.reports.mkdir(parents=True)
    stops = [project.start(APPROVED, FAKE_SLEEP="2") for _ in range(8)]
    results = [stop.communicate(timeout=60) for stop in stops]
    assert [stop.returncode for stop in stops] == [0] * 8, results
    assert project.calls() == 1
    assert not (project.reports / "codex_review_gate.lock").exists()


def test_a_lock_taken_over_during_the_review_is_left_to_its_new_holder(project):
    project.edit()
    lock = project.reports / "codex_review_gate.lock"
    result = project.run(APPROVED, FAKE_TAKEOVER=str(lock))
    assert result.returncode == 0, result.stderr
    assert lock.read_text().splitlines()[0] == "1"


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


def test_live_lock_is_taken_over_by_age_only_after_the_hook_timeout(project):
    settings = json.loads((REPO / "examples" / "claude-settings.json").read_text())
    hook_timeout = settings["hooks"]["Stop"][0]["hooks"][0]["timeout"]
    project.edit()
    project.reports.mkdir(parents=True)
    lock = project.reports / "codex_review_gate.lock"
    lock.write_text(f"{os.getpid()}\n{int(time.time()) - (hook_timeout - 1)}\n")
    assert project.run(APPROVED).returncode == 0
    assert project.calls() == 0
    lock.write_text(f"{os.getpid()}\n{int(time.time()) - 2 * hook_timeout}\n")
    result = project.run(APPROVED)
    assert result.returncode == 0, result.stderr
    assert project.calls() == 1
    assert not lock.exists()


@pytest.mark.parametrize("cap", [None, 4000], ids=["default", "configured"])
def test_prompt_never_exceeds_the_cap(project, cap):
    limit = cap or 204800
    env = {} if cap is None else {"REVIEW_GATE_MAX_PROMPT_BYTES": str(cap)}
    project.edit(text="line of text\n" * 20000)
    project.run(APPROVED, **env)
    prompt = (project.state / "prompt-1.md").read_bytes()
    assert len(prompt) <= limit
    assert f"[TRUNCATED: prompt exceeded {limit} bytes]".encode() in prompt


def test_untracked_file_with_a_non_ascii_name_is_reviewed_and_hashed(project):
    project.edit("café.py", "VALUE = 'first-marker'\n")
    assert project.run(APPROVED).returncode == 0
    assert "first-marker" in project.prompt(1)
    project.edit("café.py", "VALUE = 'second-marker'\n")
    project.run(APPROVED)
    assert project.calls() == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files of any mode")
def test_unhashable_untracked_file_stops_the_gate_without_approval(project):
    project.edit("locked.txt", "x\n")
    (project.root / "locked.txt").chmod(0)
    result = project.run(APPROVED)
    assert result.returncode == 1
    assert "could not hash the working state" in result.stderr
    assert project.calls() == 0


@pytest.mark.parametrize("repeat", [1, 4000], ids=["small-target", "large-target"])
def test_untracked_symlink_is_shown_as_its_link_text(project, tmp_path, repeat):
    target = tmp_path / "confidential.txt"
    target.write_text("confidential-marker\n" * repeat)
    (project.root / "notes.txt").symlink_to(target)
    assert project.run(APPROVED).returncode == 0
    prompt = project.prompt(1)
    assert "confidential-marker" not in prompt
    assert f"[symbolic link to {target}]" in prompt


def test_retargeted_untracked_symlink_triggers_a_new_review(project, tmp_path):
    (project.root / "link").symlink_to(tmp_path / "first")
    assert project.run(APPROVED).returncode == 0
    (project.root / "link").unlink()
    (project.root / "link").symlink_to(tmp_path / "second")
    project.run(APPROVED)
    assert project.calls() == 2


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


@pytest.mark.parametrize(
    "review_data",
    [
        {"status": "APPROVED"},
        {**APPROVED, "blocking_issues": ["not an object"]},
        {**APPROVED, "blocking_issues": [{**BLOCKER, "severity": "P9"}]},
        {**APPROVED, "blocking_issues": [{"severity": "P1", "file": "app.py"}]},
        {**APPROVED, "blocking_issues": [{**BLOCKER, "extra": "x"}]},
        {**APPROVED, "round": "1"},
        {**APPROVED, "round": True},
        {**APPROVED, "non_blocking_suggestions": [1]},
        {**APPROVED, "summary": None},
        {**APPROVED, "unexpected": 1},
    ],
)
def test_helper_rejects_reviews_that_do_not_match_the_schema(review_data):
    assert load_helper().verdict(review_data) == ("UNKNOWN", 0)


def test_helper_schema_check_matches_the_template_schema():
    schema = json.loads((REPO / "templates" / "codex_review_schema.json").read_text())
    helper = load_helper()
    issue_schema = schema["properties"]["blocking_issues"]["items"]
    assert set(helper.REVIEW_FIELDS) == set(schema["required"]) == set(schema["properties"])
    assert set(helper.ISSUE_FIELDS) == set(issue_schema["required"])
    assert helper.SEVERITIES == set(issue_schema["properties"]["severity"]["enum"])
    assert helper.VALID_STATUSES == set(schema["properties"]["status"]["enum"])
