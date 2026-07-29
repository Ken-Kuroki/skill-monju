---
name: monju
description: Launch three independent, parallel, read-only Cursor CLI reviews asynchronously in a Codex background terminal with the highest non-Fast reasoning variants of Kimi K3, Grok 4.5, and Claude Fable 5, then collect and verify their results in a later conversation turn. Require structured but unexecuted experiment proposals and save private, collision-free review artifacts. Use for multi-model review, second opinions, plan review, implementation or code review, document or design review, and risk analysis.
---

# Monju

Generate one neutral review brief and give the same effective prompt to three fixed
Cursor models. Keep the foreground supervisor alive in a Codex background terminal;
never wait for completion in the launch turn. Inspect and aggregate results only in
a later conversation turn.

## Authorization semantics

Monju may send the review brief and target-file contents read by reviewers to
external Cursor/LLM services. Treat an explicit invocation as authorization for
this scoped read-only workflow, its preflight and private artifacts, and, when
required, Cursor Workspace Trust for the exact workspace with `--trust`. This
authorization does not override Codex platform or Auto-review policy. Never ask
for a conversational confirmation such as "Should I proceed?" before taking
those actions.

If filesystem sandbox escalation is required, submit the tool call immediately
with a precise platform approval request. Let the configured platform approval
path handle that decision; do not precede it with a chat question or consume a
separate turn. Stop only when the user must actually provide missing information
or complete an action, such as browser authentication or SSO.

## Launch turn

1. Identify the workspace and exact review scope. Read enough source material to
   write a self-contained brief without including Codex's conclusions.
2. Resolve this skill directory as `<monju-skill-dir>` and the default output root
   as `<absolute-workspace>/monju-reviews`.
3. Run the deterministic preflight before creating a run:

   ```bash
   AGENT_CLI_CREDENTIAL_STORE=file \
   uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
     --preflight \
     --workspace "<absolute-workspace>"
   ```

4. Handle preflight failures without weakening Cursor protections:
   - For `PREFLIGHT=cursor_state_unwritable`, immediately submit an escalated
     tool call to rerun the preflight outside the filesystem sandbox. Do not ask
     for permission in chat first; the configured platform approval path is the
     only approval step. Use the same outside-sandbox execution for the eventual
     foreground launch. Do not wait for Cursor to fail with `EPERM` under
     `~/.cursor/projects`.
   - For `PREFLIGHT=workspace_trust_required`, do not ask the user to open a
     terminal or ask for conversational confirmation. The skill invocation
     authorizes trust registration for this exact workspace. Immediately submit
     a narrowly scoped platform approval request to run this exact command
     outside the filesystem sandbox with a PTY:

     ```bash
     AGENT_CLI_CREDENTIAL_STORE=file \
     agent --workspace "<absolute-workspace>" --trust
     ```

     State in the approval reason that this persistently trusts exactly the
     displayed workspace in Cursor. Do not request a reusable broad command
     approval. Once the Cursor Agent reaches its initial prompt, interrupt and
     close that process without submitting a prompt, then rerun the preflight
     outside the sandbox. If trust is still absent, report the command output and
     stop instead of retrying indefinitely. Never suggest `--yolo` or `--force`,
     and never create or edit Cursor's trust marker directly.
   - If authentication fails, stop and ask the user to run this command and
     complete browser authentication:

     ```bash
     AGENT_CLI_CREDENTIAL_STORE=file agent login
     ```

     Never request or handle the user's password. Rerun the preflight after the
     user confirms.

5. Prepare a unique run directory after `PREFLIGHT=ok`:

   ```bash
   uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
     --prepare \
     --workspace "<absolute-workspace>" \
     --output-root "<absolute-output-root>"
   ```

6. Write the brief to the exact `PROMPT_FILE` printed by the command. Keep it
   inside that run directory. Include the objective, review type, exact scope,
   requirements, constraints, criteria, exclusions, and required questions.
   Include the actual support matrix and risk tolerance when known; do not imply
   enterprise-scale, safety-critical, or speculative future requirements.
   Exclude `monju-reviews/` and the current run's generated artifacts from the
   reviewers' inspection scope.
7. Start the run using one Codex unified `exec_command` session with a short
   initial yield. Keep the runner itself in that session's foreground. If step 4
   required outside-sandbox execution, directly request or reuse the platform
   approval on this tool call without asking in chat first:

   ```bash
   AGENT_CLI_CREDENTIAL_STORE=file \
   uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
     --notify auto \
     --workspace "<absolute-workspace>" \
     --run-dir "<absolute-run-dir>" \
     --prompt-file "<absolute-prompt-file>"
   ```

   Set `yield_time_ms` to about 3000 so immediate failures can finish visibly.
   A still-running tool result must return a live exec session ID; that Codex
   background terminal, not a detached Python subprocess, owns the supervisor
   until completion. Do not append `&`, use `nohup` or `setsid`, pass
   `--background`, or send the supervisor into another process tree. If unified
   exec cannot retain a live session, stop instead of using a detach fallback.
8. Do not ask for an extra review confirmation at any point in this workflow.
   A platform-enforced approval may surface to the user or go to Auto-review,
   but it must not be preceded by a conversational yes/no question.
9. If the command yields with a live exec session and prints `STATUS=running` plus
   `EXECUTION_MODE=foreground_supervisor`, report the saved run directory and
   end the turn. Do not call `write_stdin`, poll the session, read intermediate
   streams, or start another run. If the command exits during the initial yield,
   report its terminal status as an immediate launch result instead.

## Auto-review denial

Do not preemptively stop merely because `approvals_reviewer` is `auto_review` or
its legacy alias `guardian_subagent`; public or sanitized material may pass.
Proceed to one normal platform approval attempt. If that attempt explicitly
denies the launch as private-data export to an untrusted external destination:

- Treat it as a Codex platform denial, not an authentication, network, trust,
  subscription, or model failure. If the launch `exec_command` is denied before
  process creation, report that no reviewer subprocess started and no review
  content was sent to Cursor/LLM services.
- Do not infer an administrator-managed policy from the phrase `tenant policy`;
  the default generic Auto-review policy can also deny this on personal accounts.
- Do not retry in the same turn, route around the denial, use indirect execution,
  or edit configuration automatically. Conversational reapproval can still meet
  an absolute deny, so do not repeatedly ask for it.
- Explain the counterintuitive recovery plainly: **Approve for me** sends the
  request to Auto-review, whose absolute private-data rule may deny it, while
  **Ask for approval** keeps the same `workspace-write` sandbox but sends the
  decision to the user. The apparently stricter, more interactive setting can
  therefore succeed. This changes who approves, not the sandbox protection.
  Offer this configuration:

  ```toml
  approval_policy = "on-request"
  approvals_reviewer = "user"
  sandbox_mode = "workspace-write"
  ```

  In the currently reported App UI, **Ask for approval** maps to `user` and
  **Approve for me** maps to `auto_review`; verify current UI/documentation
  before stating that mapping because labels may change. After changing it,
  restart the Codex App and start a new thread/session; do not assume an existing
  thread inherited the change.
- If diagnosis is needed, inspect only `approval_policy`, `approvals_reviewer`,
  `sandbox_mode`, and `[auto_review]` read-only; never display a potentially
  secret-bearing `config.toml` in full. If the value returns to
  `guardian_subagent`, describe Desktop write-back only as a possibility. Record
  the App/CLI version, those keys before and after, and the file modification
  time, then report it to the applicable known issue or support.
- Never recommend `danger-full-access`, `--yolo`, or
  `approval_policy = "never"` as the standard fix.

## Result-check turn

1. Check once without waiting:

   ```bash
   uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
     --status \
     --run-dir "<absolute-run-dir>"
   ```

2. If status is `prepared` or `running`, report it and stop. Do not block or poll.
   For `stale_running`, use `STALE_REASON`, `STALE_PROGRESS`, and
   `STAGING_PRESERVED` from the status output:
   - `external_termination_after_startup` or
     `external_termination_after_results` means reviewers made verified progress
     before the supervisor was externally killed; do not call this a startup
     failure.
   - `startup_failure` means no reviewer initialized and meaningful startup
     diagnostics were recorded.
   - `external_termination_before_startup` means the supervisor disappeared
     before useful reviewer evidence was written.

   Inspect preserved event and stderr files only for this stale diagnosis, report
   the distinction, and do not auto-retry.
3. For a terminal manifest status, read the manifest and the three generated review
   `*.md` files. Treat `*.events.jsonl` and stderr logs as diagnostics only.
4. Report:
   - act-now findings supported by multiple successful reviewers;
   - useful act-now findings unique to one reviewer;
   - a compact `Usually defer (YAGNI)` section for premature abstraction,
     unstated compatibility, disproportionate robustness, and low-impact unlikely
     edge cases, even when multiple reviewers mention them;
   - disagreements and uncertainty;
   - proposed experiments, deduplicated, labeled unexecuted, and split into
     act-now versus usually-defer based on present decision value;
   - failed reviewer reasons and the saved run directory.
5. Verify material findings against the reviewed source before presenting them
   as facts. Never present a predicted experiment outcome as observed evidence.
   Do not execute experiments or implement fixes without separate authorization.
6. Reassess each reviewer's disposition rather than copying it mechanically.
   Prioritize current requirements, realistic likelihood and impact, implementation
   cost, conceptual complexity, and readability. Do not promote an item merely
   because several reviewers found it or because a worst case can be imagined.
7. Account for every non-duplicate reviewer finding as act-now, usually-defer,
   disagreement or uncertainty, or rejected after source verification. Deduplicate
   overlaps and keep non-actionable categories compact, but do not silently drop
   premature-generalization, compatibility, robustness, or edge-case findings.
8. Keep likely correctness, security, irreversible data-loss, and explicit-contract
   problems in the act-now section. Also keep a current readability problem there
   when a small localized correction reduces conceptual complexity; defer stylistic
   preferences and high-churn rewrites. Put speculative future-proofing elsewhere
   and omit it from recommended next actions unless the user explicitly asks to
   implement deferred work.

## Fixed review configuration

The runner alone constructs reviewer commands:

```text
agent -p --mode=ask --model <fixed-model-spec> --output-format stream-json <effective-prompt>
```

```text
Kimi K3:        kimi-k3-max
Grok 4.5:       cursor-grok-4.5-high
Claude Fable 5: claude-fable-5-thinking-max
```

Run exactly these three top-level reviewers concurrently. Do not use Fast,
UltraCode-like parallel-compute variants, Auto routing, lower-effort fallback,
replacement models, bracket overrides, or extra reviewer agents. If a requested
model is unavailable, preserve that failure without substitution.

## Guarantees and failures

- Use Ask mode and prohibit reviewer file changes, mutating commands, and
  subagent delegation.
- Keep reviewer streams outside the workspace until all reviewers finish to
  reduce accidental cross-review contamination. Treat this as workflow
  separation, not a hard same-user security boundary.
- Require each reviewer either to provide structured, unexecuted experiment
  proposals with procedures, side effects, multiple outcomes, and
  interpretations, or to state that no experiment is warranted.
- Require YAGNI-aware disposition without suppressing findings. Favor the smallest
  coherent fix and a clear conceptual surface over premature generalization or
  maximum theoretical robustness.
- Verify the `system/init` model against the normalized allowlist of observed
  maximum-quality model IDs and display names. Fail visibly on any unknown name.
- Keep the supervisor in the foreground of a Codex unified-exec background
  terminal. Never rely on `start_new_session`, `nohup`, `setsid`, or a detached
  Python child to outlive the launch turn. Treat the live exec session ID as the
  launch-lifetime guarantee and do not poll it.
- Before launch, probe actual write access to the relevant Cursor project state
  directory, verify the workspace-specific trust marker, and check file-store
  authentication. On an untrusted workspace, let Codex run Cursor's dedicated
  `--trust` operation through a narrowly approved outside-sandbox PTY; do not
  require the user to open a terminal. Never bypass trust with `--yolo` or
  `--force` or by editing the marker directly.
- Treat WSL2 as the supported Windows execution path; it uses Linux/POSIX
  behavior. Prefer the WSL Linux filesystem for the workspace and output root,
  and prefer a webhook when completion must be visible outside the WSL session.
  Do not present native PowerShell or CMD execution as supported end to end.
- Save run artifacts with owner-only modes on POSIX and preserve staging on
  publication failure or interruption. Native-Windows branches are best effort
  only; if used, require an output root protected by an owner-only DACL.
- Treat completion notifications as best effort. `--notify auto` uses the webhook
  URL in `MONJU_NOTIFY_WEBHOOK_URL` when configured; otherwise it uses the local
  desktop mechanism. In WSL2 this is the Linux backend and may not surface on the
  Windows desktop; native `msg.exe` support is best effort only. Never place a
  webhook URL or token in command arguments, briefs, logs, or review artifacts.
  The runner removes this variable from each reviewer subprocess environment.
- Send only the terminal status, run ID, and result directory in notifications.
  Record notification success or failure in the manifest without changing the
  review status or exit code.
- Surface authentication, network, subscription, regional, retention, timeout,
  invalid-model, supervisor, and malformed-result failures without fallback.
- In the best-effort native-Windows path, state that interruption reliably
  terminates direct reviewer processes but descendant cleanup depends on Cursor
  CLI behavior. WSL2 uses POSIX process-group termination.
- The Cursor CLI currently accepts the effective prompt only as an argument. The
  runner rejects oversized prompts but local process listings may expose brief
  text; keep briefs scoped and avoid embedding unnecessary secrets.

## Runner commands

```text
--preflight             Check Cursor state access, workspace trust, and authentication
--prepare               Create a unique run directory and empty brief
--background            Rejected; use a foreground Codex background terminal
--status                Read run status without waiting
--run-dir PATH          Prepared run directory
--agent-bin PATH        Cursor CLI executable; default: agent
--timeout-seconds N     Per-reviewer timeout; default: 3600
--notify MODE           none, auto, desktop, or webhook; default: none
--dry-run               Print reviewer command templates without invoking Cursor
```
