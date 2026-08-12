---
name: monju
description: Launch explicitly selected independent, parallel, read-only Cursor or OpenCode Go reviews in a tracked persistent tmux session, then collect and verify their results in a later conversation turn. Use for multi-model review, second opinions, plan or implementation review, document or design review, and risk analysis. Require structured but unexecuted experiment proposals and save private, collision-free artifacts.
---

# Monju

Give one neutral brief to every explicitly selected reviewer. Run the
foreground supervisor inside a run-ID-named tmux session, end the launch turn
after startup, and inspect results only in a later turn.

## Authorization

Monju may send the brief and target files read by reviewers to Cursor or
OpenCode Go and their external LLM services. Treat an explicit skill invocation as authorization
for this scoped read-only workflow, preflight, private artifacts, and its exact
run-ID-named tmux session. Do not ask for an additional conversational
confirmation after the user has selected the exact backend and models. This authorization does not override Codex platform or
Auto-review policy.

## Required backend and model choice

Never choose a backend or model implicitly. At the start of every launch, inspect
`reviewers.json` and `cursor_reviewers.json`, show the available exact
backend/model pairs, and ask the user which pairs to run. If the request already
contains every exact pair, restate the selection and continue without asking
again. A reviewer key alone is insufficient in conversation unless it is shown
together with its exact model ID and variant.

One automated run uses exactly one backend. If the user wants models from both
backends, create and launch one prepared run per backend and keep their artifacts
and provenance separate. Never substitute the same-named model through the other
backend.

When filesystem escalation is needed, submit the tool call immediately with a
precise platform approval request. Stop only when the user must supply missing
information or complete backend authentication or Cursor Workspace Trust.

## Launch turn

Treat a four-reviewer OpenCode run at the shipped highest reasoning variants as
likely to consume roughly an entire five-hour OpenCode Go usage allowance in a
single launch. Tell the user this before launching when it is not already clear,
but do not turn the warning into an extra confirmation step. Reduce the entries
in `reviewers.json`, or select a smaller subset. The runner requires at least one
repeated `--reviewer KEY` option and never defaults to all reviewers. Use keys
from the chosen backend's configuration, preserve the user's option order, and
pass the same `--backend` and reviewer selection to preflight and launch.

1. Identify the workspace and exact review scope. Read enough source material to
   write a self-contained neutral brief without including Codex's conclusions.
2. Resolve this skill directory as `<monju-skill-dir>` and use
   `<absolute-workspace>/monju-reviews` as the default output root.
3. Run preflight before creating a run:

   ```bash
   uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
     --preflight \
     --backend <cursor-or-opencode> \
     --reviewer <configured-key> \
     --workspace "<absolute-workspace>"
   ```

   Append one `--reviewer <configured-key>` per selected model. For Cursor, the
   default configuration is `cursor_reviewers.json`; for OpenCode it is
   `reviewers.json`. Repeat the exact backend and reviewer options on launch.

4. Handle preflight failures without weakening protections:
   - For `PREFLIGHT=opencode_state_unwritable`, immediately rerun preflight
     outside the filesystem sandbox through the platform approval path. Use the
     same approval boundary for the eventual launch. OpenCode writes under
     `~/.local/share/opencode` and `~/.cache/opencode` even for simple commands.
   - For `PREFLIGHT=tmux_state_unwritable`, rerun preflight and launch through
     the same outside-sandbox approval. tmux uses its user-owned socket under a
     temporary directory. Treat a missing tmux executable as a local dependency
     failure; do not replace it with `nohup`, `setsid`, `&`, or `--background`.
     `TMUX_SERVER=existing` uses the current user server;
     `TMUX_SERVER=absent` is not an error because `--tmux` creates it with the
     review session.
   - For `PREFLIGHT=opencode_auth_required`, ask the user to complete:

     ```bash
     opencode auth login --provider opencode-go
     ```

     Never request or handle an API key or password. Rerun preflight afterward.
   - For `PREFLIGHT=cursor_state_unwritable`, rerun preflight and launch through
     the same outside-sandbox approval. Cursor writes under `~/.cursor`.
   - For `PREFLIGHT=workspace_trust_required`, run only the exact trust command
     printed by preflight with a PTY, stop after the initial prompt, then rerun
     preflight. Never use `--yolo` or `--force`.
   - For `PREFLIGHT=cursor_auth_required`, ask the user to complete the printed
     `AGENT_CLI_CREDENTIAL_STORE=file agent login` flow. Never request or handle
     credentials.
   - Treat missing models or variants as configuration failures. Never substitute
     a model, lower reasoning effort, use automatic routing, or retry indefinitely.

5. Prepare a unique run directory after `PREFLIGHT=ok`:

   ```bash
   uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
     --prepare \
     --workspace "<absolute-workspace>" \
     --output-root "<absolute-output-root>"
   ```

6. Write the neutral brief to the exact `PROMPT_FILE`. Include the objective,
   review type, exact scope, requirements, constraints, exclusions, risk
   tolerance, and questions to answer. Exclude `monju-reviews/` and generated
   artifacts from inspection.
7. Ask the runner to create one run-ID-named tmux session containing the
   foreground supervisor, using the same outside-sandbox approval if preflight
   required it:

   ```bash
   uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
     --tmux \
     --backend <cursor-or-opencode> \
     --reviewer <configured-key> \
     --notify auto \
     --workspace "<absolute-workspace>" \
     --run-dir "<absolute-run-dir>" \
     --prompt-file "<absolute-prompt-file>"
   ```

   Insert the same backend and repeated reviewer options used for preflight.

   Do not wrap this command in another tmux command. Never append `&`, use
   `nohup` or `setsid`, or pass `--background`. The launcher checks the exact
   session name, starts the supervisor without a shell, and waits only for the
   running manifest. The tmux session closes automatically when the supervisor
   reaches a terminal state.

   The supervisor emits a flushed `MONJU_HEARTBEAT` about every 30 seconds to
   its owner-only runner log. This is internal liveness evidence, not Codex
   polling. tmux removes the Codex exec-session TTL from the review process, but
   cannot survive the tmux server, host, or user session being terminated.
8. Do not ask for an extra review confirmation. Platform approval may still be
   shown to the user or Auto-review.
9. When the command returns `STATUS=running`,
   `EXECUTION_MODE=tmux_supervisor`, and `TMUX_SESSION_ALIVE=yes`, report the run
   directory and session name, then end the turn. `STATUS=tmux_starting` is also
   non-terminal; report it and stop. Do not poll, read intermediate streams, or
   launch a second run. Report a startup or terminal result returned directly.

## Manual handoff

Use a manual handoff when the user has access to a reviewer through a separate
subscription or UI but no callable CLI/API. It is independent of the automated
reviewer list and does not require OpenCode preflight:

```bash
uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
  --manual-handoff <safe-key> \
  --manual-display-name "<human-readable model name>" \
  --workspace "<absolute-workspace>" \
  --run-dir "<absolute-run-dir>" \
  --prompt-file "<absolute-prompt-file>"
```

The command makes owner-only prompt, response, and provenance files without
invoking OpenCode, a network, or an external model. Give the human the exact
`MANUAL_PROMPT_FILE`; they must separately attach or provide only the in-scope
files required by the brief, then paste the model's unedited answer into
`MANUAL_RESPONSE_FILE`. Monju never packages or uploads workspace files for this
path. Existing handoff files are never overwritten.

Manual responses are not part of the automated manifest, recovery, notification,
or success/failure classification. `--status` reports non-empty response files as
`MANUAL_RESULT`. Read them during aggregation, verify material findings against
source, and label their model identity and provenance as manually supplied and
unverified. A missing manual response is not an automated reviewer failure.

## Auto-review denial

Do not stop merely because `approvals_reviewer` is `auto_review` or its legacy
alias `guardian_subagent`; public or sanitized material may pass. Attempt the
normal platform approval once. If it explicitly denies private-data export to an
untrusted external destination:

- Report a Codex platform denial, not an OpenCode authentication, network,
  subscription, or model failure. If denied before process creation, say no
  reviewer started and no review content was sent.
- Do not infer an administrator-managed policy from `tenant policy`; default
  Auto-review policy can deny this on personal accounts.
- Do not retry in the same turn, route around the denial, use indirect execution,
  or edit configuration automatically. Do not repeatedly request conversational
  approval after an absolute deny.
- Explain that **Approve for me** routes to Auto-review, while **Ask for
  approval** routes the decision to the user without disabling the
  `workspace-write` sandbox. Recommend:

  ```toml
  approval_policy = "on-request"
  approvals_reviewer = "user"
  sandbox_mode = "workspace-write"
  ```

  UI labels may change. Verify current behavior before asserting the mapping.
  Restart Codex and begin a new thread after changing it.
- Never recommend `danger-full-access`, `--yolo`, or
  `approval_policy = "never"` as the standard solution.

## Result-check turn

1. Check once without waiting:

   ```bash
   uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
     --status \
     --run-dir "<absolute-run-dir>"
   ```

2. If status is `prepared`, `tmux_starting`, or `running`, report it and stop.
   For tmux runs, use `TMUX_SESSION_ALIVE`; do not infer failure from the old
   Codex exec session or poll repeatedly.
3. For `stale_running`, use `STALE_REASON`, `STALE_PROGRESS`,
   `STAGING_PRESERVED`, and `RECOVERY`. For `artifact_failure` or
   `supervisor_failure`, use the preserved staging and `RECOVERY` fields without
   treating the run as published:
   - If every configured reviewer has a verified terminal marker and
     `RECOVERY_CAN_PUBLISH=yes`, run recovery once:

     ```bash
     uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
       --recover \
       --run-dir "<absolute-run-dir>"
     ```

   - `RECOVERY=ready` means all streams are successful and valid.
   - `RECOVERY=invalid` with `RECOVERY_CAN_PUBLISH=yes` means all reviewer
     processes completed, but one or more verified results will be published as
     a normal failure because of a backend error or malformed output.
   - For `RECOVERY=not_ready`, or invalid with `RECOVERY_CAN_PUBLISH=no`, preserve
     staging and stop. Never infer missing output, relaunch, or auto-retry.

   Recovery reads only preserved artifacts and never invokes Cursor, OpenCode, or an
   external LLM. It verifies frozen reviewer configuration, terminal markers,
   and artifact hashes. It can retry publication after `artifact_failure` or
   `supervisor_failure` only when the same validation succeeds. A genuinely
   published terminal manifest makes repeated recovery a read-only no-op.
   Treat DeepSeek V4 Flash's non-retryable HTTP 403 `RegionError` saying that
   the latest model is hosted only in China and requires explicit opt-in as an
   expected reviewer failure. Keep the reviewer configured, report the failure,
   and aggregate the other results. Do not opt in, retry, or substitute a model
   automatically. Expected here means operationally anticipated, not successful;
   the run remains a normal partial/failure result.
4. For terminal status, read the manifest and all generated review Markdown.
   Also read each non-empty `MANUAL_RESULT` reported by status, while keeping its
   unverified manual provenance distinct from automated results.
   Treat JSONL, stderr, terminal markers, and worker task files as diagnostics.
5. Aggregate:
   - supported act-now findings;
   - useful unique act-now findings;
   - a compact `Usually defer (YAGNI)` section for premature abstraction,
     unstated compatibility, disproportionate robustness, and unlikely
     low-impact edge cases;
   - disagreements and uncertainty;
   - deduplicated, unexecuted experiments split into act-now and usually-defer;
   - reviewer failures and the run directory.
6. Verify material findings against source before presenting them as facts.
   Never present predicted experiment outcomes as observed evidence, execute an
   experiment, or implement fixes without separate authorization.
7. Account for every non-duplicate finding as act-now, usually-defer,
   disagreement/uncertainty, or rejected after source verification. Favor clear,
   direct code and the smallest coherent fix. Keep likely correctness, security,
   irreversible data-loss, explicit-contract, and small high-value readability
   problems in act-now.

## Reviewer configuration

`reviewers.json` is the OpenCode source of truth and
`cursor_reviewers.json` is the Cursor source of truth. Each entry has a unique
safe `key`, `display_name`, and exact model ID. OpenCode entries also carry an
exact `variant`; Cursor entries carry the exact allowed names accepted from the
CLI's `system/init` event. The shipped configurations use:

```text
Kimi K3:          opencode-go/kimi-k3 / max
Grok 4.5:         opencode-go/grok-4.5 / high
DeepSeek V4 Flash: opencode-go/deepseek-v4-flash / max
Qwen3.8 Max:      opencode-go/qwen3.8-max / max

Cursor:
Kimi K3:          kimi-k3-max
Grok 4.5:         cursor-grok-4.5-high
Claude Fable 5:  claude-fable-5-thinking-max
```

The count is configurable rather than fixed in the runner. OpenCode preflight
verifies every selected model and variant against
`opencode models opencode-go --verbose`; Cursor preflight checks `agent models`
and runtime verification checks the exact `system/init` identity. The highest
OpenCode variant is model-specific: Grok 4.5 uses `high`, while the other shipped
reviewers use `max`; do not assume every model uses the literal variant `max`.
Repeated `--reviewer KEY` options explicitly select and order models for one run.
At least one is required. The
runner reassigns ordinals and freezes that selected list and its hash in the
manifest; later edits to the config or CLI selection cannot change recovery.

For OpenCode, the runner constructs commands equivalent to:

```text
opencode --pure run --format json --model <model> --agent monju-review
  --dir <workspace> [--variant <variant>]
```

It passes the prompt through stdin and never passes `--auto`. The inline
`monju-review` agent denies all tools by default, allows read/glob/grep, denies
`.env` and `.env.*`, and permits `.env.example` only.

For Cursor, it constructs commands equivalent to:

```text
agent -p --mode=ask --model <exact-model> --output-format stream-json
  <effective-prompt>
```

Cursor receives the prompt as its final argument, as required by the restored
CLI workflow. Artifacts and manifests redact that argument. The runner verifies
the reported `system/init` model against the selected entry and rejects missing,
mismatched, Fast, Auto, low-effort, or UltraCode-like variants.

## Guarantees and failures

- Freeze backend, reviewer configuration, and catalog verification in schema-v3 running
  manifests so later edits to `reviewers.json` cannot change status or recovery.
- Keep the supervisor as the foreground command of an exact run-ID-named tmux
  session. The launcher itself returns after the running manifest exists; it
  does not remain responsible for process lifetime. Direct foreground execution
  remains available for diagnostics but is not the skill launch path. Handle
  tmux SIGHUP like SIGTERM so session shutdown stops reviewer process groups.
- Run one internal worker per reviewer. Each worker writes an atomic terminal
  sidecar containing the exact model/variant, exit state, timing, and hashes of
  raw artifacts. This enables safe recovery if the supervisor disappears after
  workers finish. It does not guarantee worker survival when the platform kills
  the entire process tree.
- Require non-empty reviewer output to contain every section mandated by the
  review prompt before classifying it as success. Treat progress-only text as a
  reviewer failure rather than a completed review.
- Keep streams outside the reviewed workspace until all reviewers finish. This
  is workflow separation, not a hard same-user security boundary.
- Save artifacts with owner-only POSIX modes and preserve staging on interruption
  or publication failure.
- Remove `MONJU_NOTIFY_WEBHOOK_URL` from reviewer environments. Notifications
  contain only terminal status, run ID, and result directory. For tmux launch,
  transfer a configured webhook through the runner's transient owner-only
  handoff file; the supervisor deletes it before preflight. Never put the secret
  in argv, manifests, published artifacts, or reviewer environments.
  Notification failure never changes review status.
- Surface authentication, workspace trust, state access, network, subscription, region,
  retention, timeout, invalid model/variant, malformed output, supervisor, and
  publication failures without fallback.
- Treat WSL2 as the supported Windows route; native Windows remains best effort.

## Runner commands

```text
--preflight             Check tmux, backend state, auth, and selected models
--prepare               Create a unique run directory and empty brief
--tmux                  Launch the foreground supervisor in a tracked tmux session
--tmux-bin PATH         tmux CLI; default: PATH lookup
--background            Rejected; use --tmux
--status                Read run status without waiting
--recover               Validate and publish completed preserved artifacts
--run-dir PATH          Prepared run directory
--backend NAME          Required launch backend: cursor or opencode
--opencode-bin PATH     OpenCode CLI; PATH then ~/.opencode/bin/opencode
--agent-bin PATH        Cursor CLI; default: agent
--reviewers-file PATH   Backend reviewer JSON; default selected by backend
--reviewer KEY          Select exact configured model; repeat; required
--manual-handoff KEY    Create private prompt/response files; no external call
--manual-display-name   Human-readable name for the manual reviewer
--timeout-seconds N     Per-reviewer timeout; default: 7200
--notify MODE           none, auto, desktop, or webhook; default: none
--dry-run               Print commands without invoking a model
```
