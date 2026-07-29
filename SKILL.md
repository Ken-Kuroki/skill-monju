---
name: monju
description: Launch three independent, parallel, read-only Cursor CLI reviews asynchronously with the highest non-Fast reasoning variants of Kimi K3, Grok 4.5, and Claude Fable 5, then collect and verify their results in a later conversation turn. Require structured but unexecuted experiment proposals and save private, collision-free review artifacts. Use for multi-model review, second opinions, plan review, implementation or code review, document or design review, and risk analysis.
---

# Monju

Generate one neutral review brief and give the same effective prompt to three fixed
Cursor models. Launch the review in the background; never wait for completion in
the launch turn. Inspect and aggregate results only in a later conversation turn.

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
   - For `PREFLIGHT=cursor_state_unwritable`, request platform approval to rerun
     the preflight outside the filesystem sandbox. Use the same outside-sandbox
     execution for the eventual `--background` launch. Do not wait for Cursor to
     fail with `EPERM` under `~/.cursor/projects`.
   - For `PREFLIGHT=workspace_trust_required`, do not ask the user to open a
     terminal. Request narrowly scoped platform approval to run this exact
     command outside the filesystem sandbox with a PTY:

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
7. Start the run using the `RUN_DIR` and `PROMPT_FILE` from step 5. If step 4
   required outside-sandbox execution, request or reuse that approval here:

   ```bash
   AGENT_CLI_CREDENTIAL_STORE=file \
   uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
     --background \
     --notify auto \
     --workspace "<absolute-workspace>" \
     --run-dir "<absolute-run-dir>" \
     --prompt-file "<absolute-prompt-file>"
   ```

8. Do not ask for an extra review confirmation. Invoking this skill authorizes
   the read-only review and creation of generated artifacts under the output
   directory; still respect any platform-enforced approval.
9. Interpret the short startup result, then end the turn:
   - `STATUS=running` with `STARTUP=confirmed` means all three reviewers emitted
     verified model initialization events.
   - `STATUS=running` with `STARTUP=pending` means the supervisor is alive but
     initialization was not confirmed within the short grace period; report that
     distinction without polling.
   - A terminal `STATUS` with `STARTUP=failed` is an immediate launch failure, not
     a successful background start. Report it and the saved run directory.

   Never wait for review completion, read intermediate streams, or start another
   run in the launch turn.

## Result-check turn

1. Check once without waiting:

   ```bash
   uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
     --status \
     --run-dir "<absolute-run-dir>"
   ```

2. If status is `prepared` or `running`, report it and stop. Do not block or poll.
   If status is `stale_running`, inspect the runner stderr log, report that the
   detached supervisor exited without a terminal manifest, and do not auto-retry.
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
--background            Start the detached supervisor and return immediately
--status                Read run status without waiting
--run-dir PATH          Prepared run directory
--agent-bin PATH        Cursor CLI executable; default: agent
--timeout-seconds N     Per-reviewer timeout; default: 3600
--notify MODE           none, auto, desktop, or webhook; default: none
--dry-run               Print reviewer command templates without invoking Cursor
```
