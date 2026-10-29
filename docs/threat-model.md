# Threat model

This document states what each control in this repository prevents, what it does not prevent, and which test checks the claim.

## Setting

A developer workstation running Linux or WSL2. Claude Code and the Codex CLI run as the developer's own user. Without further controls, an agent process can read every file that user can read: other repositories, `.env` files, SSH keys, cloud and GitHub credentials, other Claude Code profiles and, on WSL, the Windows drives under `/mnt`.

## Assets

1. Source code and data in repositories the agent was not asked to work on, including repositories with confidentiality or data-governance obligations.
2. Credentials: `.env` files, cloud and GitHub tokens, SSH keys, Claude Code OAuth tokens.
3. The quality of the change set an agent leaves behind when it stops.
4. Separation between accounts, for example a personal subscription and a client or employer account.

## Threats in scope

| ID | Threat |
|---|---|
| T1 | An agent reads files outside the task's working root. The cause can be the agent's own exploration, a prompt injection in content it reads, or an instruction error. |
| T2 | An agent reads secrets inside the working root, such as `.env` files. |
| T3 | Content an agent reads is sent to the model provider as part of a prompt. This follows from T1 and T2: anything read can leave the machine. |
| T4 | Claude stops with failing tests, or with changes no second reviewer has seen. |
| T5 | A review loop does not converge, and the agent is held in it. |
| T6 | A session runs under the wrong account, or shares settings, memory, permissions and transcripts with another engagement. |

Out of scope: a malicious local user, kernel or bubblewrap exploits, a compromised Codex or Claude Code binary, and network exfiltration controls.

## 1. codex-sandbox (`bin/codex-sandbox`)

### Finding that motivated it

The header of `bin/codex-sandbox` records a probe from 2026-08-30. Codex was started with `--sandbox read-only`. It read a file at an arbitrary absolute path outside its `--cd` directory and listed the root of an unrelated repository. The read-only policy restricts writes. It does not restrict reads. Project trust settings in Codex's `config.toml` govern approval prompts. They do not set filesystem scope. Neither mechanism bounds what Codex can read.

Gap: the Codex version used in the probe was not recorded. Codex behavior may have changed since that date.

Inference: under the read-only policy, any file the user can read can become part of a Codex prompt (T3).

### What it prevents

The wrapper starts Codex in a bubblewrap mount namespace. A path that is not bound into the namespace does not exist for Codex.

| Threat | Mechanism | Test in tests/test_codex_sandbox.py |
|---|---|---|
| T1: reads outside the working root | Only the configured root that contains the current directory is bound. Other roots and repositories are absent. Codex refuses to start outside every root. | test_files_outside_the_root_do_not_exist, test_refuses_a_directory_outside_every_root |
| T1: reads of dotfiles in `$HOME` | `/tmp`, `/home` and `$HOME` are empty tmpfs mounts. Only `tool` and `state` paths from the config are bound back. | test_home_mnt_and_wsl_entry_points_are_empty_or_absent, test_home_and_tmp_start_empty |
| T1: partial access to a governed repository | `ro <root> <subtree>...` binds only the listed subtrees. `.git` is absent, so the history of unlisted directories is unreachable. | test_subtree_root_binds_only_the_listed_subtrees, test_subtree_root_refuses_a_directory_outside_its_subtrees |
| T2: `.env` files inside the root | Every regular file named `.env*`, or matching a `mask` pattern, is overmounted with `/dev/null`. Reads of it fail. The search prunes directories named `node_modules`, `.git`, `.venv`, `venv` and `__pycache__`; files inside them are not masked. | test_masked_env_file_content_is_not_readable, test_env_files_are_masked_and_dependency_dirs_pruned, test_mask_lines_add_patterns |
| T2: credentials in environment variables | `unsetenv` lines remove named variables. | test_state_tool_and_environment_lines |
| Writes to a reference repository | `ro` roots are read-only binds. | test_read_only_root_refuses_writes |
| WSL: Windows drives, the interop socket | `/mnt` is an empty tmpfs and `/run` is absent, except that the target of `/etc/resolv.conf`, when it resolves outside `/etc`, is bound read-only as one file and can lie under `/mnt` or `/run`. `/init` is absent. `WSL_INTEROP` and `WSLENV` are unset. The tests check that `/run` and `/init` are not bound, that `/init` is absent inside the namespace, and that the two variables are unset. | test_wsl_routes_are_removed, test_home_mnt_and_wsl_entry_points_are_empty_or_absent |

The five tests that start a real namespace are skipped where bubblewrap cannot create one. CI enables unprivileged user namespaces so that they run there.

### What it does not prevent

- **Network egress.** The network is shared, because the Codex API needs it. Anything inside the bound root can be sent anywhere the machine can reach.
- **Environment variables.** The calling shell's environment passes through, except `WSL_INTEROP`, `WSLENV` and the `unsetenv` entries.
- **Secrets under other names or in pruned directories.** Masking matches regular file names when Codex starts. A secret in a file with another name, a file created after launch, a symbolic link, or a `.env*` file under a directory named `node_modules`, `.git`, `.venv`, `venv` or `__pycache__` is not masked.
- **Windows executables through binfmt_misc.** `/init` is absent. Whether that disables the WSLInterop binfmt_misc handler depends on the handler's registration flags: with the `F` flag, the kernel opens the interpreter when the handler is registered (Linux kernel binfmt_misc documentation). The flags were not checked, and no test runs a Windows executable inside the namespace.
- **Everything that is bound.** The working root, every `tool` path and every `state` path are visible. `~/.codex`, which holds Codex's own credentials, is usually a read-write `state` path. `/etc` is bound read-only in full.
- **Bypass.** `CODEX_UNCONFINED=1`, or running the real Codex binary by its absolute path, skips the namespace.
- **Other agents.** Claude Code is not confined by this wrapper. It has its own permission system.

### Reproducing the finding

Run this yourself; the outcome depends on the Codex version installed.

```bash
mkdir -p /tmp/probe/work /tmp/probe/elsewhere
echo probe-marker > /tmp/probe/elsewhere/notes.txt
cd /tmp/probe/work && git init -q
CODEX_UNCONFINED=1 codex exec --sandbox read-only "Print the contents of /tmp/probe/elsewhere/notes.txt"
```

Then add `rw /tmp/probe/work` to the sandbox config and run the same command without `CODEX_UNCONFINED`. Inside the namespace, `/tmp/probe/elsewhere` does not exist.

## 2. codex-review-gate (`hooks/codex-review-gate.sh`)

A Claude Code Stop hook. When Claude tries to stop, the gate runs the project's test command and then a Codex review of the uncommitted changes.

### What it prevents

| Threat | Mechanism | Test in tests/test_review_gate.py |
|---|---|---|
| T4: stopping with failing tests | A failing `REVIEW_GATE_TEST_CMD` returns exit 2 with the test output, and Claude continues. | test_failing_tests_block_without_using_a_round |
| T4: stopping with unreviewed changes | Codex reviews the staged, unstaged and untracked changes. `CHANGES_REQUESTED` returns exit 2 with the review. | test_changes_requested_blocks_with_the_review_on_stderr |
| T4: approving a state that changed | The approval records a hash of HEAD, the status, the diff against HEAD and the untracked file contents. Any edit invalidates it. | test_approved_state_is_not_reviewed_twice, test_edit_after_approval_triggers_a_new_review |
| T4: an inconsistent verdict | An `APPROVED` verdict that lists blocking issues is treated as `CHANGES_REQUESTED`. | test_approved_verdict_with_blockers_counts_as_changes_requested |
| T5: a review loop that does not converge | `REVIEW_GATE_MAX_ROUNDS` (default 2 in `hooks/codex-review-gate.sh`) caps the rounds per batch, identified by a hash of the sorted `git status --short` output. Rounds after the first are fix-verification only. After the last round the gate lets Claude stop and logs the unresolved blockers as an override. | test_second_round_verifies_fixes_then_overrides_at_the_cap, test_round_cap_is_configurable |
| T5: a broken reviewer holding Claude | A Codex failure, a missing review file or an unparseable review returns exit 1. Claude Code shows the error to the user and does not block. No approval is written, and no round is used. | test_reviewer_failures_neither_block_nor_approve, test_reviewer_failure_does_not_use_a_round |
| Concurrent stops | A PID lock. A lock older than `REVIEW_GATE_LOCK_STALE_SECONDS` (default 1500, above the 1200-second Stop hook timeout in `examples/claude-settings.json`), or held by a dead process, is cleared. | test_live_lock_skips_the_review, test_lock_of_a_dead_process_is_cleared, test_live_lock_is_taken_over_by_age_only_after_the_hook_timeout |

Design rationale (inference, not measured): without a cap, a reviewer that raises a new blocker in each round can keep Claude working indefinitely. The cap trades that risk for the risk in the next list.

### What it does not prevent

- **Unresolved blockers after the cap.** Claude may stop with P0 or P1 issues still open. They are recorded in `.ai/reports/qa_decisions.jsonl`, which a person has to read.
- **Committed changes.** The gate reviews uncommitted changes. Work Claude commits before it stops is not reviewed.
- **Partial review of large diffs.** The prompt is capped at `REVIEW_GATE_MAX_PROMPT_BYTES` (default 204800 bytes). A larger prompt is truncated, and the truncation is marked in the prompt.
- **Test failures Claude cannot fix.** Test failures are not capped. They block until the code is fixed, the user intervenes or the gate is disabled.
- **New file sets and staging.** The cap applies per batch, and the batch hash includes the two status letters of each `git status --short` line. A fix that touches a new file starts again at round 1. `git add` with no content change has the same effect, because staging changes the status letters. Staging files between rounds therefore resets the round count.
- **Concurrent stops.** A stop that finds the lock held by a live process younger than `REVIEW_GATE_LOCK_STALE_SECONDS` exits 0 without running the tests or a review, and Claude stops. The skip is recorded only in `.ai/reports/qa_activity.log`.
- **Disabling by the agent.** Claude can create `.ai/reports/disabled`. Deny writes to that path in Claude Code permissions if this matters.
- **T3 for the review itself.** Codex receives the diff and the test output. Run the gate's Codex call through codex-sandbox to bound what else it can read.

## 3. claude_profile (`shell/claude-profiles.sh`)

### What it prevents

| Threat | Mechanism | Test in tests/test_claude_profiles.py |
|---|---|---|
| T6: shared settings, memory, permissions and transcripts | Each profile gets its own `CLAUDE_CONFIG_DIR` (`~/.claude-<name>`). | test_profile_selects_its_config_dir_and_passes_arguments |
| T6: the wrong account | Each profile can carry its own long-lived OAuth token in `CLAUDE_TOKEN_DIR/<name>`. A profile never reads another profile's token file. | test_token_file_is_loaded_from_the_default_directory, test_other_profiles_tokens_are_not_used |
| A profile name that points elsewhere | Names are restricted to letters, digits, dot, dash and underscore, starting with a letter or digit. | test_unsafe_profile_names_are_refused |
| T6: the wrong GitHub identity | A `GH_CONFIG_DIR=...` prefix on the alias reaches `claude` and the `gh` commands it runs. | test_prefix_assignments_reach_claude |

### What it does not prevent

- **Token exposure in the environment.** The token is passed as `CLAUDE_CODE_OAUTH_TOKEN`. Processes of the same user can read it from `/proc`, and child processes inherit it. Inference: this includes Codex when Claude Code starts it; `examples/sandbox.conf` unsets it inside the namespace.
- **Tokens at rest.** Token files are plain text. Their protection is the file mode (600) and the directory mode (700).
- **Filesystem separation.** A session under one profile can still read another profile's directory. Combine profiles with a sandbox when that matters.
