#!/usr/bin/env bash
# codex-review-gate.sh: Claude Code Stop hook. Before Claude may stop, run the
# project tests and a Codex review of the uncommitted changes.
#
# Round 1 is a full review. Later rounds verify only that the blockers from the
# previous round were fixed. When the last allowed round still requests
# changes, the gate lets Claude stop and records the unresolved blockers as an
# override in the decision log. A round is counted only when Codex returns a
# verdict. A change in `git status --short`, git add included, restarts at round 1.
#
# Exit 0: Claude may stop.
# Exit 2: Claude must act on stderr (failed tests or requested changes).
# Exit 1: the gate could not run (setup error, Codex failure, unparseable
#         review, files changed during the tests or the review). Claude Code
#         shows stderr to the user and does not block, so a broken reviewer
#         cannot hold Claude in a loop. No approval is written.
#
# The working state is hashed before the tests run. An approval records that
# hash, and only when the state is unchanged after the review.
#
# Settings (environment):
#   REVIEW_GATE_DISABLE=1           pass through; so does the file <reports>/disabled
#   REVIEW_GATE_TEST_CMD            test command, run with bash -c; unset means none
#   REVIEW_GATE_SKIP_TESTS=1        skip the test command
#   REVIEW_GATE_MAX_ROUNDS          review rounds per batch, default 2
#   REVIEW_GATE_MAX_PROMPT_BYTES    prompt size cap, default 204800
#   REVIEW_GATE_LOCK_STALE_SECONDS  age after which a live lock is taken over, default 1500
#   REVIEW_GATE_CODEX               Codex executable, default codex
#   REVIEW_GATE_DIR                 directory holding codex_reviewer.md and
#                                   codex_review_schema.json, relative to the
#                                   project root, default .ai
set -euo pipefail

HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/review_gate.py"
BINARY_EXT_PATTERN='\.(parquet|pkl|pickle|joblib|h5|hdf5|onnx|pt|pth|bin|so|dylib|whl|gz|zip|tar|bz2|xz|png|jpg|jpeg|gif|ico|woff|woff2|ttf|eot|pdf|npy|npz|safetensors|ckpt|model)$'
MAX_UNTRACKED_FILE_BYTES=51200
FENCE='```'
ROUND=0
PROMPT_BYTES=0
CURRENT_STATE=""
VERDICT_STATUS=""
VERDICT_BLOCKING=0

setup_paths() {
  local start=${CLAUDE_PROJECT_DIR:-$PWD}
  if ! PROJECT_ROOT=$(git -C "$start" rev-parse --show-toplevel 2>/dev/null); then
    echo "codex-review-gate: $start is not inside a git repository." >&2
    exit 1
  fi
  cd "$PROJECT_ROOT"
  GATE_DIR=${REVIEW_GATE_DIR:-.ai}
  REPORT_DIR="$GATE_DIR/reports"
  EXCLUDE=":(exclude)$REPORT_DIR"
  REVIEWER_FILE="$GATE_DIR/codex_reviewer.md"
  SCHEMA_FILE="$GATE_DIR/codex_review_schema.json"
  SUMMARY_FILE="$REPORT_DIR/claude_last.md"
  REVIEW_FILE="$REPORT_DIR/codex_review.json"
  PROMPT_FILE="$REPORT_DIR/codex_review_prompt.md"
  CODEX_LOG="$REPORT_DIR/codex_exec.log"
  TEST_LOG="$REPORT_DIR/test_output.log"
  APPROVAL_FILE="$REPORT_DIR/codex_approved"
  ROUND_FILE="$REPORT_DIR/codex_round_count"
  BATCH_FILE="$REPORT_DIR/codex_batch_id"
  LOCK_FILE="$REPORT_DIR/codex_review_gate.lock"
  ACTIVITY_LOG="$REPORT_DIR/qa_activity.log"
  DECISION_LOG="$REPORT_DIR/qa_decisions.jsonl"
  MAX_ROUNDS=${REVIEW_GATE_MAX_ROUNDS:-2}
  MAX_PROMPT_BYTES=${REVIEW_GATE_MAX_PROMPT_BYTES:-204800}
  mkdir -p "$REPORT_DIR"
}

log_event() {
  printf '%s | round=%s | %s | %s\n' "$(date -Iseconds)" "$ROUND" "$1" "${2:-}" >>"$ACTIVITY_LOG"
}

log_decision() {
  local outcome=$1 reason=$2 review=${3:-$REVIEW_FILE}
  python3 "$HELPER" decision "$(date -Iseconds)" \
    "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)" \
    "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
    "$ROUND" "$outcome" "$PROMPT_BYTES" "$reason" "$review" "$SUMMARY_FILE" >>"$DECISION_LOG"
}

check_setup() {
  if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "codex-review-gate: no commit yet; create a baseline commit first." >&2
    exit 1
  fi
  local file
  for file in "$REVIEWER_FILE" "$SCHEMA_FILE"; do
    if [[ ! -f $file ]]; then
      echo "codex-review-gate: missing $file; copy it from the templates directory." >&2
      exit 1
    fi
  done
}

is_disabled() {
  [[ ${REVIEW_GATE_DISABLE:-0} == 1 || -f $REPORT_DIR/disabled ]]
}

# Run "$@" while holding an exclusive flock on the guard file, so that the
# check and the update of the lock file are one atomic step across processes.
# The guard is released before the tests and the review run.
with_guard() {
  local guard_fd status=0
  exec {guard_fd}>>"$LOCK_FILE.guard"
  if ! flock -w 30 "$guard_fd"; then
    exec {guard_fd}>&-
    echo "codex-review-gate: could not lock $LOCK_FILE.guard within 30 s." >&2
    exit 1
  fi
  "$@" || status=$?
  exec {guard_fd}>&-
  return "$status"
}

acquire_lock() {
  with_guard take_lock
  trap 'with_guard release_lock' EXIT
}

# Remove the lock only while it is still this process's; a stale takeover by
# a later stop replaces it with that stop's pid.
release_lock() {
  if [[ -f $LOCK_FILE && $(sed -n 1p "$LOCK_FILE") == "$$" ]]; then
    rm -f "$LOCK_FILE"
  fi
}

take_lock() {
  local stale=${REVIEW_GATE_LOCK_STALE_SECONDS:-1500} lock_pid lock_time elapsed
  if [[ -f $LOCK_FILE ]]; then
    lock_pid=$(sed -n 1p "$LOCK_FILE")
    lock_time=$(sed -n 2p "$LOCK_FILE")
    [[ $lock_time =~ ^[0-9]+$ ]] || lock_time=0
    elapsed=$(($(date +%s) - lock_time))
    if [[ $lock_pid =~ ^[0-9]+$ ]] && kill -0 "$lock_pid" 2>/dev/null && ((elapsed < stale)); then
      log_event HOOK_SKIP "lock held by live pid=$lock_pid (${elapsed}s old)"
      exit 0
    fi
    log_event LOCK_CLEARED "previous lock pid=${lock_pid:-unknown} age=${elapsed}s"
    rm -f "$LOCK_FILE"
  fi
  printf '%s\n%s\n' "$$" "$(date +%s)" >"$LOCK_FILE"
}

project_changes() {
  git status --short --untracked-files=all -- . "$EXCLUDE"
}

# NUL-separated untracked paths, unquoted whatever their characters.
untracked_paths() {
  git ls-files -z --others --exclude-standard -- . "$EXCLUDE"
}

# One "<path> <blob id>" line per untracked file. Returns 1 if a file cannot
# be hashed, so a failure cannot leave that file out of the state.
untracked_state() {
  local file digest
  while IFS= read -r -d '' file; do
    if [[ -L $file ]]; then
      digest="symlink:$(readlink -- "$file")" || return 1
    else
      digest=$(git hash-object -- "$file") || return 1
    fi
    printf '%s %s\n' "$file" "$digest"
  done < <(untracked_paths)
}

# Hash of HEAD, the status, every index entry, the worktree diff against the
# index and every untracked file. Staged and unstaged content are hashed
# separately, because a diff against HEAD cannot tell them apart.
working_state_hash() {
  local listing digest
  listing=$(mktemp) || return 1
  if ! {
    git rev-parse HEAD &&
      project_changes &&
      git ls-files --stage -- . "$EXCLUDE" &&
      git diff --binary -- . "$EXCLUDE" &&
      untracked_state
  } >"$listing"; then
    rm -f "$listing"
    return 1
  fi
  digest=$(git hash-object --stdin <"$listing") || digest=""
  rm -f "$listing"
  [[ -n $digest ]] && printf '%s\n' "$digest"
}

# Set CURRENT_STATE to the working state hash, or exit 1 when it cannot be computed.
read_current_state() {
  if ! CURRENT_STATE=$(working_state_hash); then
    log_event HASH_ERROR "could not hash the working state"
    echo "codex-review-gate: could not hash the working state; no approval was recorded." >&2
    exit 1
  fi
}

already_approved() {
  [[ -f $APPROVAL_FILE ]] || return 1
  local approved
  approved=$(sed -n 's/^approved_state=//p' "$APPROVAL_FILE")
  if [[ -n $approved && $approved == "$CURRENT_STATE" ]]; then
    return 0
  fi
  rm -f "$APPROVAL_FILE"
  return 1
}

start_round() {
  local batch previous="" completed=0
  batch=$(project_changes | sort | git hash-object --stdin)
  [[ -f $BATCH_FILE ]] && previous=$(<"$BATCH_FILE")
  if [[ $batch != "$previous" ]]; then
    rm -f "$ROUND_FILE" "$REVIEW_FILE"
    printf '%s\n' "$batch" >"$BATCH_FILE"
  fi
  [[ -f $ROUND_FILE ]] && completed=$(<"$ROUND_FILE")
  [[ $completed =~ ^[0-9]+$ ]] || completed=0
  ROUND=$((completed + 1))
  log_event HOOK_START "round=$ROUND/$MAX_ROUNDS"
}

run_tests() {
  if [[ ${REVIEW_GATE_SKIP_TESTS:-0} == 1 ]]; then
    echo "Tests skipped because REVIEW_GATE_SKIP_TESTS=1." >"$TEST_LOG"
    log_event TESTS_SKIPPED
    return 0
  fi
  if [[ -z ${REVIEW_GATE_TEST_CMD:-} ]]; then
    echo "No test command configured (REVIEW_GATE_TEST_CMD is unset)." >"$TEST_LOG"
    log_event TESTS_NOT_CONFIGURED
    return 0
  fi
  printf 'Running test command:\n%s\n\n' "$REVIEW_GATE_TEST_CMD" >"$TEST_LOG"
  if ! bash -c "$REVIEW_GATE_TEST_CMD" >>"$TEST_LOG" 2>&1; then
    log_event TESTS_FAILED "see $TEST_LOG"
    {
      echo "Tests failed. Fix the failures below, then stop again:"
      cat "$TEST_LOG"
    } >&2
    exit 2
  fi
  log_event TESTS_PASSED
}

# shellcheck disable=SC2329 # invoked through fenced_section
bounded_diff() {
  git diff "$@" -- . "$EXCLUDE" | head -c "$MAX_PROMPT_BYTES" || true
}

untracked_files_section() {
  local file size
  while IFS= read -r -d '' file; do
    # A symlink is shown as its link text; its target is never read.
    if [[ -L $file ]]; then
      printf '\n### %s\n[symbolic link to %s]\n' "$file" "$(readlink -- "$file")"
      continue
    fi
    [[ -f $file ]] || continue
    printf '\n### %s\n' "$file"
    if [[ ${file,,} =~ $BINARY_EXT_PATTERN ]]; then
      echo "[binary, skipped]"
      continue
    fi
    size=$(wc -c <"$file")
    if ((size > MAX_UNTRACKED_FILE_BYTES)); then
      printf '[%s bytes, first 200 lines]\n%s\n' "$size" "$FENCE"
      head -200 "$file"
      echo "$FENCE"
    else
      echo "${FENCE}diff"
      git diff --no-index -- /dev/null "$file" || true
      echo "$FENCE"
    fi
  done < <(untracked_paths)
}

round_section() {
  echo "## Review Round"
  echo
  echo "This is review round $ROUND of $MAX_ROUNDS maximum."
  if ((ROUND > 1)) && [[ -f $REVIEW_FILE ]]; then
    echo
    echo "**Fix-verification round.** Verify only that the blocking issues from the previous review were fixed. Do not raise new blockers unless the fix caused a regression."
    echo
    echo "## Previous Review"
    echo
    echo "${FENCE}json"
    cat "$REVIEW_FILE"
    echo "$FENCE"
  fi
}

# Print a "## <title>" section holding a command's output in a code fence.
fenced_section() {
  local title=$1 lang=$2
  shift 2
  printf '\n## %s\n\n%s%s\n' "$title" "$FENCE" "$lang"
  "$@"
  printf '%s\n' "$FENCE"
}

summary_or_note() {
  if [[ -f $SUMMARY_FILE ]]; then
    cat "$SUMMARY_FILE"
  else
    echo "No summary file was found."
  fi
}

write_prompt() {
  {
    echo "# Codex Review Request"
    echo
    cat "$REVIEWER_FILE"
    echo
    round_section
    printf '\n## Project Scope\n\nReview only changes under: %s\n' "$PROJECT_ROOT"
    printf '\n## Claude Implementation Summary\n\n'
    summary_or_note
    fenced_section "Test Output" text cat "$TEST_LOG"
    fenced_section "Git Status" text project_changes
    fenced_section "Staged Diff" diff bounded_diff --cached
    fenced_section "Unstaged Diff" diff bounded_diff
    printf '\n## Untracked Files\n'
    untracked_files_section
  } >"$PROMPT_FILE"
  PROMPT_BYTES=$(wc -c <"$PROMPT_FILE")
  if ((PROMPT_BYTES > MAX_PROMPT_BYTES)); then
    log_event PROMPT_TRUNCATED "original=${PROMPT_BYTES} bytes, cap=${MAX_PROMPT_BYTES}"
    local notice keep
    notice=$(printf '\n\n[TRUNCATED: prompt exceeded %s bytes]' "$MAX_PROMPT_BYTES")
    # The notice and its newline count toward the cap. The notice is ASCII, so
    # its length in characters is its length in bytes.
    keep=$((MAX_PROMPT_BYTES - ${#notice} - 1))
    ((keep >= 0)) || keep=0
    head -c "$keep" "$PROMPT_FILE" >"$PROMPT_FILE.tmp"
    printf '%s\n' "$notice" >>"$PROMPT_FILE.tmp"
    mv "$PROMPT_FILE.tmp" "$PROMPT_FILE"
    PROMPT_BYTES=$(wc -c <"$PROMPT_FILE")
  fi
}

run_codex() {
  local codex=${REVIEW_GATE_CODEX:-codex} new_review="$REVIEW_FILE.new"
  rm -f "$new_review"
  log_event CODEX_INVOKE "prompt=${PROMPT_BYTES} bytes"
  if ! "$codex" exec --ephemeral --sandbox read-only --cd "$PROJECT_ROOT" \
    --output-schema "$SCHEMA_FILE" --output-last-message "$new_review" \
    - <"$PROMPT_FILE" >"$CODEX_LOG" 2>&1; then
    log_event CODEX_ERROR "see $CODEX_LOG"
    {
      echo "codex-review-gate: Codex review failed; no approval was recorded. Last output:"
      tail -20 "$CODEX_LOG"
    } >&2
    exit 1
  fi
  if [[ ! -f $new_review ]]; then
    log_event CODEX_ERROR "no review file written"
    echo "codex-review-gate: Codex wrote no review to $new_review; no approval was recorded." >&2
    exit 1
  fi
  read -r VERDICT_STATUS VERDICT_BLOCKING < <(python3 "$HELPER" verdict "$new_review") || true
  # An unparseable review must not replace the last valid one, which the next
  # fix-verification round needs.
  if [[ $VERDICT_STATUS != APPROVED && $VERDICT_STATUS != CHANGES_REQUESTED ]]; then
    log_event PARSE_ERROR "status=${VERDICT_STATUS:-none}"
    log_decision PARSE_ERROR "unparseable review" "$new_review"
    echo "codex-review-gate: could not parse the Codex review; no approval was recorded." >&2
    cat "$new_review" >&2
    exit 1
  fi
  mv -f "$new_review" "$REVIEW_FILE"
  log_event CODEX_COMPLETE
}

# Exit 1 without an approval when the working state differs from the one
# hashed before the tests ran, because Codex saw some other state.
require_reviewed_state() {
  local reviewed=$CURRENT_STATE
  read_current_state
  [[ $CURRENT_STATE == "$reviewed" ]] && return 0
  log_event STATE_CHANGED "files changed during the tests or the review"
  log_decision STATE_CHANGED "files changed during the review; no approval recorded"
  echo "codex-review-gate: files changed while Codex reviewed them; no approval was recorded. The next stop reviews the current state." >&2
  exit 1
}

write_approval() {
  local auto=$1 reason=${2:-}
  require_reviewed_state
  {
    echo "approved_at=$(date -Iseconds)"
    echo "approved_commit=$(git rev-parse HEAD)"
    echo "approved_state=$CURRENT_STATE"
    echo "rounds=$ROUND"
    echo "auto_approved=$auto"
    [[ -z $reason ]] || echo "override_reason=$reason"
  } >"$APPROVAL_FILE"
  rm -f "$ROUND_FILE" "$BATCH_FILE"
}

route_verdict() {
  local status=$VERDICT_STATUS blocking=$VERDICT_BLOCKING
  case $status in
    APPROVED)
      write_approval false
      log_event APPROVED "after $ROUND round(s)"
      log_decision APPROVED "Codex approved"
      echo "Codex approved the current diff after $ROUND round(s)."
      exit 0
      ;;
    CHANGES_REQUESTED)
      printf '%s\n' "$ROUND" >"$ROUND_FILE"
      if ((ROUND >= MAX_ROUNDS)); then
        write_approval true max_rounds_reached
        log_event AUTO_APPROVED "$blocking unresolved blocker(s) after $ROUND round(s)"
        log_decision AUTO_APPROVED "$blocking blocker(s) unresolved after $ROUND of $MAX_ROUNDS rounds"
        echo "Codex still requests changes after round $ROUND of $MAX_ROUNDS."
        echo "Round limit reached: stopping is allowed. Unresolved blockers are in $DECISION_LOG."
        exit 0
      fi
      log_event CHANGES_REQUESTED "$blocking blocking issue(s)"
      log_decision CHANGES_REQUESTED "$blocking blocking issue(s) in round $ROUND"
      echo "Codex requested changes (round $ROUND of $MAX_ROUNDS). Fix these, then stop again:" >&2
      cat "$REVIEW_FILE" >&2
      exit 2
      ;;
  esac
}

main() {
  setup_paths
  if is_disabled; then
    log_event HOOK_DISABLED
    echo "codex-review-gate: disabled." >&2
    exit 0
  fi
  check_setup
  acquire_lock
  if [[ -z $(project_changes) ]]; then
    log_event HOOK_SKIP "no uncommitted changes"
    exit 0
  fi
  read_current_state
  if already_approved; then
    log_event HOOK_SKIP "working state already approved"
    exit 0
  fi
  start_round
  run_tests
  write_prompt
  run_codex
  route_verdict
}

main "$@"
