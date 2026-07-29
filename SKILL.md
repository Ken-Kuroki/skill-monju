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
3. Prepare a unique run directory:

   ```bash
   uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
     --prepare \
     --workspace "<absolute-workspace>" \
     --output-root "<absolute-output-root>"
   ```

4. Write the brief to the exact `PROMPT_FILE` printed by the command. Keep it
   inside that run directory. Include the objective, review type, exact scope,
   requirements, constraints, criteria, exclusions, and required questions.
   Include the actual support matrix and risk tolerance when known; do not imply
   enterprise-scale, safety-critical, or speculative future requirements.
   Exclude `monju-reviews/` and the current run's generated artifacts from the
   reviewers' inspection scope.
5. Check the non-Keychain credential store:

   ```bash
   AGENT_CLI_CREDENTIAL_STORE=file agent status
   ```

   If it is not logged in, stop and ask the user to run this command themselves
   and complete browser authentication:

   ```bash
   AGENT_CLI_CREDENTIAL_STORE=file agent login
   ```

   Never request or handle the user's password. Resume after the user confirms
   authentication.
6. Start the run using the `RUN_DIR` and `PROMPT_FILE` from step 3:

   ```bash
   AGENT_CLI_CREDENTIAL_STORE=file \
   uv run --script "<monju-skill-dir>/scripts/run_monju.py" \
     --background \
     --notify auto \
     --workspace "<absolute-workspace>" \
     --run-dir "<absolute-run-dir>" \
     --prompt-file "<absolute-prompt-file>"
   ```

7. Do not ask for an extra review confirmation. Invoking this skill authorizes
   the read-only review and creation of generated artifacts under the output
   directory; still respect any platform-enforced approval.
8. End the turn as soon as the runner prints `STATUS=running`. Report the saved
   run directory and tell the user that results should be checked in a later
   turn. Do not poll, wait, read intermediate streams, or start another run.

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
--prepare               Create a unique run directory and empty brief
--background            Start the detached supervisor and return immediately
--status                Read run status without waiting
--run-dir PATH          Prepared run directory
--agent-bin PATH        Cursor CLI executable; default: agent
--timeout-seconds N     Per-reviewer timeout; default: 3600
--notify MODE           none, auto, desktop, or webhook; default: none
--dry-run               Print reviewer command templates without invoking Cursor
```
