"""Tests for bin/codex-sandbox.

The dry-run tests check the bwrap arguments the wrapper builds. The tests marked
needs_bwrap run a stand-in Codex inside a real namespace and check what it can
and cannot reach. They are skipped where the wrapper cannot create its
namespace, unless CODEX_SANDBOX_REQUIRE_NAMESPACE=1, which makes them run and
fail there instead.
"""

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "bin" / "codex-sandbox"
# Stand-in for Codex: runs its first argument as a shell command.
FAKE_CODEX = '#!/usr/bin/env bash\nexec bash -c "$1"\n'
BIND_FLAGS = {"--bind", "--ro-bind", "--ro-bind-try"}


def namespace_probe_failure() -> str | None:
    """Run the wrapper itself, with its real namespace flags, on `true`.

    Returns None when the namespace starts, otherwise the reason it did not.
    """
    if shutil.which("bwrap") is None:
        return "bwrap is not installed"
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        tools, home, work = base / "tools", base / "home", base / "work"
        for path in (tools, home, work):
            path.mkdir()
        codex = tools / "codex"
        codex.write_text(FAKE_CODEX)
        codex.chmod(0o755)
        config = base / "sandbox.conf"
        config.write_text(f"tool {tools}\nrw {work}\n")
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "CODEX_BIN": str(codex),
            "CODEX_SANDBOX_CONFIG": str(config),
        }
        result = subprocess.run(
            [str(WRAPPER), "true"], cwd=work, env=env, capture_output=True, text=True
        )
    if result.returncode == 0:
        return None
    return f"the wrapper's namespace did not start: {result.stderr.strip()[:200]}"


PROBE_FAILURE = namespace_probe_failure()
needs_bwrap = pytest.mark.skipif(
    PROBE_FAILURE is not None and os.environ.get("CODEX_SANDBOX_REQUIRE_NAMESPACE") != "1",
    reason=f"namespace tests skipped: {PROBE_FAILURE}",
)


@dataclass
class Sandbox:
    base: Path
    home: Path
    tools: Path
    codex: Path

    def make_dir(self, relative: str) -> Path:
        path = self.base / relative
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write(self, relative: str, text: str = "x\n") -> Path:
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def configure(self, *lines: str) -> Path:
        config = self.base / "sandbox.conf"
        config.write_text("\n".join([f"tool {self.tools}", *lines]) + "\n")
        return config

    def run(self, cwd: Path, *args: str, dry_run: bool = True, **extra_env: str):
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "CODEX_BIN": str(self.codex),
            "CODEX_SANDBOX_CONFIG": str(self.base / "sandbox.conf"),
        }
        if dry_run:
            env["CODEX_SANDBOX_DRY_RUN"] = "1"
        env.update(extra_env)
        return subprocess.run(
            [str(WRAPPER), *args], cwd=cwd, env=env, capture_output=True, text=True
        )


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    home = tmp_path / "home"
    tools = tmp_path / "tools"
    home.mkdir()
    tools.mkdir()
    codex = tools / "codex"
    codex.write_text(FAKE_CODEX)
    codex.chmod(0o755)
    return Sandbox(tmp_path, home, tools, codex)


def argv_of(result) -> list[str]:
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def binds(argv: list[str], flag: str) -> list[str]:
    """Destinations bound with exactly this flag."""
    return [argv[i + 2] for i, arg in enumerate(argv) if arg == flag]


def masked(argv: list[str]) -> list[str]:
    return [
        argv[i + 2]
        for i, arg in enumerate(argv)
        if arg == "--ro-bind" and argv[i + 1] == "/dev/null"
    ]


def test_refuses_a_directory_outside_every_root(sandbox):
    app = sandbox.make_dir("work/app")
    other = sandbox.make_dir("elsewhere")
    sandbox.configure(f"rw {app}")
    result = sandbox.run(other, "exec", "x")
    assert result.returncode == 1
    assert "outside every configured root" in result.stderr


def test_rw_root_is_bound_read_write(sandbox):
    app = sandbox.make_dir("work/app")
    sandbox.configure(f"rw {app}")
    argv = argv_of(sandbox.run(app))
    assert str(app) in binds(argv, "--bind")
    assert argv[argv.index("--chdir") + 1] == str(app)


def test_ro_root_is_bound_read_only(sandbox):
    lib = sandbox.make_dir("work/lib")
    sandbox.configure(f"ro {lib}")
    argv = argv_of(sandbox.run(lib))
    assert str(lib) in binds(argv, "--ro-bind")
    assert str(lib) not in binds(argv, "--bind")


def test_subtree_root_binds_only_the_listed_subtrees(sandbox):
    gov = sandbox.make_dir("work/gov")
    for name in ("docs", "src", "confidential", ".git"):
        sandbox.make_dir(f"work/gov/{name}")
    sandbox.configure(f"ro {gov} docs src")
    argv = argv_of(sandbox.run(gov, "exec", "review"))
    ro = binds(argv, "--ro-bind")
    assert str(gov / "docs") in ro and str(gov / "src") in ro
    for absent in (gov, gov / "confidential", gov / ".git"):
        assert str(absent) not in ro and str(absent) not in binds(argv, "--bind")
    assert argv[-3:] == ["exec", "--skip-git-repo-check", "review"]


def test_subtree_root_refuses_a_directory_outside_its_subtrees(sandbox):
    gov = sandbox.make_dir("work/gov")
    sandbox.make_dir("work/gov/docs")
    hidden = sandbox.make_dir("work/gov/confidential")
    sandbox.configure(f"ro {gov} docs")
    result = sandbox.run(hidden)
    assert result.returncode == 1
    assert "outside its bound subtrees" in result.stderr


@pytest.mark.parametrize("subtree", ["docs", "docs/inner"])
def test_subtree_that_resolves_through_a_symlink_is_refused(sandbox, subtree):
    gov = sandbox.make_dir("work/gov")
    other = sandbox.make_dir("work/other-repo")
    sandbox.make_dir("work/other-repo/inner")
    (gov / "docs").symlink_to(other)
    sandbox.configure(f"ro {gov} {subtree}")
    result = sandbox.run(gov)
    assert result.returncode == 1
    assert "resolves through a symbolic link" in result.stderr
    assert str(other) not in result.stdout


def test_subtrees_are_rejected_on_read_write_roots(sandbox):
    app = sandbox.make_dir("work/app")
    sandbox.configure(f"rw {app} src")
    result = sandbox.run(app)
    assert result.returncode == 1
    assert "subtrees are supported on ro roots only" in result.stderr


def test_subtree_with_parent_reference_is_rejected(sandbox):
    gov = sandbox.make_dir("work/gov")
    sandbox.configure(f"ro {gov} ../app")
    result = sandbox.run(gov)
    assert result.returncode == 1
    assert "must not contain .." in result.stderr


def test_env_files_are_masked_and_dependency_dirs_pruned(sandbox):
    app = sandbox.make_dir("work/app")
    secrets = [sandbox.write("work/app/.env"), sandbox.write("work/app/api/.env.local")]
    skipped = [
        sandbox.write("work/app/node_modules/pkg/.env"),
        sandbox.write("work/app/.venv/.env"),
    ]
    sandbox.configure(f"rw {app}")
    hidden = masked(argv_of(sandbox.run(app)))
    assert sorted(hidden) == sorted(str(path) for path in secrets)
    assert not set(hidden) & {str(path) for path in skipped}


def test_mask_lines_add_patterns(sandbox):
    app = sandbox.make_dir("work/app")
    key = sandbox.write("work/app/certs/server.pem")
    sandbox.configure(f"rw {app}", "mask *.pem")
    assert str(key) in masked(argv_of(sandbox.run(app)))


def test_longest_matching_root_wins(sandbox):
    work = sandbox.make_dir("work")
    lib = sandbox.make_dir("work/lib")
    sandbox.configure(f"rw {work}", f"ro {lib}")
    argv = argv_of(sandbox.run(lib))
    assert str(lib) in binds(argv, "--ro-bind")
    assert str(work) not in binds(argv, "--bind")


def test_home_and_tmp_start_empty(sandbox):
    app = sandbox.make_dir("work/app")
    sandbox.configure(f"rw {app}")
    tmpfs = binds_tmpfs(argv_of(sandbox.run(app)))
    assert {"/tmp", "/home", str(sandbox.home)} <= set(tmpfs)


def binds_tmpfs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--tmpfs"]


def test_wsl_routes_are_removed(sandbox):
    app = sandbox.make_dir("work/app")
    sandbox.configure(f"rw {app}")
    argv = argv_of(sandbox.run(app))
    assert "/mnt" in binds_tmpfs(argv)
    unset = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--unsetenv"]
    assert {"WSL_INTEROP", "WSLENV"} <= set(unset)
    sources = [argv[i + 1] for i, arg in enumerate(argv) if arg in BIND_FLAGS]
    assert not {"/init", "/run", "/mnt", "/"} & set(sources)
    assert not [src for src in sources if src.startswith("/mnt/") and "resolv" not in src]


def test_state_tool_and_environment_lines(sandbox):
    app = sandbox.make_dir("work/app")
    sandbox.configure(
        f"rw {app}",
        "state ~/.codex",
        "setenv UV_CACHE_DIR ~/.cache/uv-codex",
        "unsetenv GH_TOKEN",
    )
    argv = argv_of(sandbox.run(app))
    assert (sandbox.home / ".codex").is_dir()
    assert str(sandbox.home / ".codex") in binds(argv, "--bind")
    assert str(sandbox.tools) in binds(argv, "--ro-bind")
    setenv = argv.index("--setenv")
    assert argv[setenv + 1 : setenv + 3] == ["UV_CACHE_DIR", str(sandbox.home / ".cache/uv-codex")]
    assert "GH_TOKEN" in [argv[i + 1] for i, arg in enumerate(argv) if arg == "--unsetenv"]


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("allow /tmp", "line 2: unknown keyword: allow"),
        ("rw relative/path", "path must be absolute"),
        ("setenv ONLY_NAME", "setenv takes 2 argument(s)"),
        ("rw /", "refusing / as a root"),
    ],
)
def test_invalid_config_lines_are_rejected(sandbox, line, message):
    sandbox.configure(line)
    result = sandbox.run(sandbox.base)
    assert result.returncode == 1
    assert message in result.stderr


def test_missing_config_is_an_error(sandbox):
    result = sandbox.run(sandbox.base)
    assert result.returncode == 1
    assert "config not found" in result.stderr


def test_codex_outside_every_bound_path_is_refused(sandbox):
    app = sandbox.make_dir("work/app")
    sandbox.configure(f"rw {app}")
    stray = sandbox.write("stray/codex", FAKE_CODEX)
    stray.chmod(0o755)
    result = sandbox.run(app, CODEX_BIN=str(stray))
    assert result.returncode == 1
    assert "not inside any bound path" in result.stderr


def test_unconfined_runs_the_real_binary_directly(sandbox):
    elsewhere = sandbox.make_dir("elsewhere")
    result = sandbox.run(elsewhere, "echo unconfined", dry_run=False, CODEX_UNCONFINED="1")
    assert result.returncode == 0
    assert result.stdout.strip() == "unconfined"


def test_default_lookup_skips_the_wrapper_itself(sandbox):
    app = sandbox.make_dir("work/app")
    shim = sandbox.make_dir("shim")
    (shim / "codex").symlink_to(WRAPPER)
    sandbox.configure(f"rw {app}")
    path = f"{shim}:{sandbox.tools}:{os.environ['PATH']}"
    env = {"PATH": path, "HOME": str(sandbox.home), "CODEX_BIN": ""}
    argv = argv_of(sandbox.run(app, "exec", **env))
    assert argv[-2:] == [str(sandbox.codex), "exec"]


# Each negative test first reads a permitted file, so a sandbox that failed to
# start cannot pass it.
READ_PERMITTED = "cat permitted.txt && echo inside-sandbox && "


@needs_bwrap
def test_files_outside_the_root_do_not_exist(sandbox):
    app = sandbox.make_dir("work/app")
    sandbox.write("work/app/permitted.txt", "permitted-marker\n")
    outside = sandbox.write("outside/notes.txt", "outside-marker\n")
    sandbox.configure(f"rw {app}")
    result = sandbox.run(app, f"{READ_PERMITTED}cat {outside}", dry_run=False)
    assert "permitted-marker\ninside-sandbox\n" in result.stdout, result.stderr
    assert result.returncode != 0
    assert "outside-marker" not in result.stdout
    assert "No such file or directory" in result.stderr


@needs_bwrap
def test_masked_env_file_content_is_not_readable(sandbox):
    app = sandbox.make_dir("work/app")
    sandbox.write("work/app/permitted.txt", "permitted-marker\n")
    sandbox.write("work/app/.env", "TOKEN=env-marker\n")
    sandbox.configure(f"rw {app}")
    result = sandbox.run(app, f"{READ_PERMITTED}cat .env", dry_run=False)
    assert "permitted-marker\ninside-sandbox\n" in result.stdout, result.stderr
    assert "env-marker" not in result.stdout + result.stderr


@needs_bwrap
def test_read_only_root_refuses_writes(sandbox):
    lib = sandbox.make_dir("work/lib")
    sandbox.write("work/lib/permitted.txt", "permitted-marker\n")
    sandbox.configure(f"ro {lib}")
    result = sandbox.run(lib, f"{READ_PERMITTED}touch new.txt", dry_run=False)
    assert "permitted-marker\ninside-sandbox\n" in result.stdout, result.stderr
    assert result.returncode != 0
    assert "Read-only file system" in result.stderr
    assert not (lib / "new.txt").exists()


@needs_bwrap
def test_read_write_root_accepts_writes(sandbox):
    app = sandbox.make_dir("work/app")
    sandbox.configure(f"rw {app}")
    result = sandbox.run(app, "echo written > new.txt", dry_run=False)
    assert result.returncode == 0, result.stderr
    assert (app / "new.txt").read_text() == "written\n"


@needs_bwrap
def test_home_mnt_and_wsl_entry_points_are_empty_or_absent(sandbox):
    app = sandbox.make_dir("work/app")
    sandbox.write("home/.ssh/id_test", "key-marker\n")
    sandbox.configure(f"rw {app}")
    script = (
        'ls -A "$HOME"; echo ---; ls -A /mnt; echo ---; test -e /init && echo init-present; true'
    )
    result = sandbox.run(app, script, dry_run=False)
    assert result.returncode == 0, result.stderr
    home_listing, mnt_listing, rest = result.stdout.split("---")
    assert ".ssh" not in home_listing
    assert not set(mnt_listing.split()) - {"wsl"}
    assert "init" not in rest
