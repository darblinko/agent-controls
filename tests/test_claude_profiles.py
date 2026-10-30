"""Tests for shell/claude-profiles.sh, with `claude` replaced by a stub."""

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PROFILES = REPO / "shell" / "claude-profiles.sh"
FAKE_CLAUDE = """#!/usr/bin/env bash
echo "config=${CLAUDE_CONFIG_DIR-unset}"
echo "token=${CLAUDE_CODE_OAUTH_TOKEN-unset}"
echo "gh=${GH_CONFIG_DIR-unset}"
echo "api_key=${ANTHROPIC_API_KEY-unset}"
echo "auth_token=${ANTHROPIC_AUTH_TOKEN-unset}"
echo "args=$*"
"""


@pytest.fixture
def shell_env(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(FAKE_CLAUDE)
    claude.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home)}


def run_profile(env: dict, *args: str, prefix: str = "") -> subprocess.CompletedProcess:
    script = f'. "{PROFILES}"; {prefix} claude_profile "$@"'
    return subprocess.run(
        ["bash", "-c", script, "bash", *args], env=env, capture_output=True, text=True
    )


def parsed(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def write_token(directory: Path, profile: str, value: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / profile).write_text(value)


def test_profile_selects_its_config_dir_and_passes_arguments(shell_env):
    out = parsed(run_profile(shell_env, "work", "--continue", "-p", "hello"))
    assert out["config"] == f"{shell_env['HOME']}/.claude-work"
    assert out["token"] == "unset"
    assert out["args"] == "--continue -p hello"


def test_token_file_is_loaded_from_the_default_directory(shell_env):
    write_token(Path(shell_env["HOME"]) / ".config" / "claude-tokens", "work", "fake-token-1")
    assert parsed(run_profile(shell_env, "work"))["token"] == "fake-token-1"


def test_token_directory_follows_xdg_config_home(shell_env, tmp_path):
    write_token(tmp_path / "xdg" / "claude-tokens", "work", "fake-token-2")
    env = {**shell_env, "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    assert parsed(run_profile(env, "work"))["token"] == "fake-token-2"


def test_claude_token_dir_overrides_the_default(shell_env, tmp_path):
    write_token(tmp_path / "tokens", "work", "fake-token-3")
    env = {**shell_env, "CLAUDE_TOKEN_DIR": str(tmp_path / "tokens")}
    assert parsed(run_profile(env, "work"))["token"] == "fake-token-3"


def test_empty_token_file_is_ignored(shell_env):
    write_token(Path(shell_env["HOME"]) / ".config" / "claude-tokens", "work", "")
    assert parsed(run_profile(shell_env, "work"))["token"] == "unset"


def test_other_profiles_tokens_are_not_used(shell_env):
    write_token(Path(shell_env["HOME"]) / ".config" / "claude-tokens", "personal", "fake-token-4")
    assert parsed(run_profile(shell_env, "work"))["token"] == "unset"


INHERITED_CREDENTIALS = {
    "CLAUDE_CODE_OAUTH_TOKEN": "personal-token",
    "ANTHROPIC_API_KEY": "personal-key",
    "ANTHROPIC_AUTH_TOKEN": "personal-auth",
}


@pytest.mark.parametrize("has_token_file", [False, True], ids=["no-token-file", "token-file"])
def test_inherited_credentials_do_not_reach_another_profile(shell_env, has_token_file):
    if has_token_file:
        write_token(Path(shell_env["HOME"]) / ".config" / "claude-tokens", "work", "fake-token-5")
    out = parsed(run_profile({**shell_env, **INHERITED_CREDENTIALS}, "work"))
    assert out["token"] == ("fake-token-5" if has_token_file else "unset")
    assert out["api_key"] == "unset"
    assert out["auth_token"] == "unset"


def test_profile_run_leaves_the_calling_shell_environment_unchanged(shell_env):
    script = f'. "{PROFILES}"; claude_profile work >/dev/null; echo "$CLAUDE_CODE_OAUTH_TOKEN"'
    result = subprocess.run(
        ["bash", "-c", script],
        env={**shell_env, **INHERITED_CREDENTIALS},
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "personal-token"


def test_prefix_assignments_reach_claude(shell_env):
    out = parsed(run_profile(shell_env, "work", prefix="GH_CONFIG_DIR=/tmp/gh-work"))
    assert out["gh"] == "/tmp/gh-work"


@pytest.mark.parametrize("name", ["", "../escape", "a/b", ".hidden", "-rf", "two words"])
def test_unsafe_profile_names_are_refused(shell_env, name):
    result = run_profile(shell_env, name)
    assert result.returncode == 2
    assert "usage: claude_profile" in result.stderr
    assert "config=" not in result.stdout
