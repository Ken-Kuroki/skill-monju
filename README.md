# Monju

Monju runs the same neutral review through configurable OpenCode Go models in
parallel. It publishes private per-model Markdown and a terminal manifest, then
lets Codex aggregate the reviews in a later conversation turn.

The default reviewers are:

| Reviewer | OpenCode Go model | Reasoning variant |
|---|---|---|
| Kimi K3 | `opencode-go/kimi-k3` | `max` |
| Grok 4.5 | `opencode-go/grok-4.5` | `high` |
| DeepSeek V4 Flash | `opencode-go/deepseek-v4-flash` | `max` |
| GLM 5.2 | `opencode-go/glm-5.2` | `max` |
| Qwen3.8 Max | `opencode-go/qwen3.8-max` | `max` |

Grok 4.5 uses `high`, its highest catalogued variant; the other shipped reviewers
use `max`. Reviewer count, models, and variants are controlled by
[`reviewers.json`](reviewers.json) rather than fixed in the runner.

## Safety model

- Every reviewer receives the same effective prompt through stdin.
- OpenCode runs with `--pure`, never `--auto`, and a Monju-specific primary
  agent that denies all tools except read, glob, and grep.
- `.env` and `.env.*` are denied after the general read allowance;
  `.env.example` remains readable.
- Reviewers cannot edit files, run shell commands, use web tools, or delegate to
  subagents through OpenCode permissions. The prompt repeats those constraints.
- Raw streams stay in owner-only temporary staging until publication.
- `MONJU_NOTIFY_WEBHOOK_URL` is removed from reviewer environments.
- No model, variant, or automatic-routing fallback is used.

Monju can send the brief and target files read by reviewers to OpenCode Go and
external LLM providers. An explicit Monju invocation authorizes this scoped
read-only workflow but does not override Codex platform approval policy.

## Requirements

- Python 3.11 or later, invoked with `uv`.
- tmux. Monju uses one exact run-ID-named session as the persistent owner of the
  foreground supervisor.
- OpenCode CLI. Monju first checks `PATH`, then
  `~/.opencode/bin/opencode` used by the official installer.
- An authenticated OpenCode Go account:

  ```bash
  opencode auth login --provider opencode-go
  ```

- Access to each exact model and variant in `reviewers.json`.
- POSIX or WSL2 for the supported process-group behavior. Native Windows is
  best effort.

OpenCode writes under `~/.local/share/opencode` and `~/.cache/opencode`, even
for commands such as version or model listing. Sandboxed coding agents usually
need an outside-sandbox approval for preflight and the review launch.

## Installation

Install the skill as a symlink for Codex:

```bash
./install.sh --agent codex
```

The installer also supports `cursor` and `claude` as *skill hosts*. A Cursor
installation target does not change the review backend; Monju reviewers still
run through OpenCode Go.

```bash
./install.sh --list
./install.sh --agent all
./install.sh --agent cursor --scope project --project /path/to/repo
./install.sh --agent codex --uninstall
```

## Reviewer configuration

The checked-in configuration has this shape:

```json
{
  "schema_version": 1,
  "provider": "opencode-go",
  "reviewers": [
    {
      "key": "kimi-k3",
      "display_name": "Kimi K3",
      "model_id": "opencode-go/kimi-k3",
      "variant": "max"
    }
  ]
}
```

Add or remove array entries to change reviewer count. Keys must be unique safe
slugs. Exact model/variant duplicates are rejected. Only `opencode-go/...`
models are accepted. Omit `variant` only for a model without a selectable
variant.

Preflight reads the authenticated catalog from:

```bash
opencode models opencode-go --verbose
```

It rejects missing, inactive, or unknown model/variant combinations rather than
silently lowering quality.

## Workflow

### 1. Preflight

```bash
uv run --script scripts/run_monju.py \
  --preflight \
  --workspace /absolute/path/to/workspace
```

Preflight checks:

- tmux executable and access to the user-owned tmux server socket;
- actual write access to OpenCode data and cache directories;
- an OpenCode Go credential;
- every configured model and variant in the current verbose catalog.

Important results include:

- `PREFLIGHT=ok`
- `PREFLIGHT=tmux_state_unwritable`: rerun preflight and launch through the
  same outside-sandbox approval.
- `PREFLIGHT=opencode_state_unwritable`: rerun preflight and launch through a
  narrowly scoped outside-sandbox approval.
- `PREFLIGHT=opencode_auth_required`: complete OpenCode Go login and retry.
- `PREFLIGHT=reviewer_model_invalid`: correct `reviewers.json`; do not use a
  substitute model.

`TMUX_SERVER=existing` means the current user server will own the run.
`TMUX_SERVER=absent` is also valid; `--tmux` starts the server with the review
session.

OpenCode does not use Cursor Workspace Trust. Monju never edits OpenCode
credentials or configuration automatically.

### 2. Prepare and write the brief

```bash
uv run --script scripts/run_monju.py \
  --prepare \
  --workspace /absolute/path/to/workspace \
  --output-root /absolute/path/to/workspace/monju-reviews
```

The command prints a collision-free `RUN_DIR` and `PROMPT_FILE`. Write one
neutral brief to that file. The brief should contain scope, requirements,
constraints, exclusions, risk tolerance, and concrete questions, while
excluding `monju-reviews/` from inspection.

### 3. Launch

```bash
uv run --script scripts/run_monju.py \
  --tmux \
  --notify auto \
  --workspace /absolute/path/to/workspace \
  --run-dir /absolute/path/to/run \
  --prompt-file /absolute/path/to/run/monju-...-00-review-brief.md
```

The runner creates a tmux session whose exact name is the run ID and starts the
supervisor there without a shell. Do not wrap the command in another tmux
invocation, append `&`, use `nohup` or `setsid`, or pass `--background`. After
the launcher returns:

```text
STATUS=running
EXECUTION_MODE=tmux_supervisor
TMUX_SESSION=<run-id>
TMUX_SESSION_ALIVE=yes
```

end the conversation turn without polling. `STATUS=tmux_starting` is also
non-terminal and should be checked once in a later turn. The supervisor writes
a flushed line to its owner-only runner log about every 30 seconds:

```text
MONJU_HEARTBEAT run_id=<run-id> elapsed_seconds=<n>
```

The heartbeat is diagnostic liveness evidence. tmux makes the review independent
of the Codex execution-session lifetime; it cannot survive termination of the
tmux server, host, or user session.

### 4. Check in a later turn

```bash
uv run --script scripts/run_monju.py \
  --status \
  --run-dir /absolute/path/to/run
```

`--status` is read-only and never polls or recovers automatically. For a live
tmux run it reports `TMUX_SESSION_ALIVE=yes`; for terminal states, read the
manifest and every generated reviewer Markdown.

If the manifest is still `running` but the recorded supervisor is gone, status
returns `stale_running`, preserved staging diagnostics, and:

```text
RECOVERY=ready|not_ready|invalid
RECOVERY_CAN_PUBLISH=yes|no
```

### 5. Recover completed orphaned workers

Only when every configured reviewer has a verified terminal marker and
`RECOVERY_CAN_PUBLISH=yes`:

```bash
uv run --script scripts/run_monju.py \
  --recover \
  --run-dir /absolute/path/to/run
```

Each internal worker writes an atomic terminal sidecar after OpenCode exits. It
contains the frozen model/variant, exit state, timing, and SHA-256 plus size for
the event and stderr artifacts. Recovery verifies those sidecars against the
schema-v3 running manifest before publishing.

Recovery never invokes OpenCode, contacts an LLM, changes models, or retries.
Missing sidecars are `not_ready`. Malformed sidecars or hash mismatches are
non-publishable `invalid`. A verified OpenCode error can be published as a
normal partial/failure result when all reviewers completed. If publication or
the supervisor fails while valid staging survives, the same command can validate
and retry publication; status does not call such a run published. Genuinely
published terminal manifests make later recovery a read-only no-op.

Exit code zero and arbitrary text are not sufficient for reviewer success. The
result must contain all sections required by the shared review prompt, preventing
progress messages such as "I will inspect the files" from becoming a successful
empty review.

DeepSeek V4 Flash may return a non-retryable HTTP 403 `RegionError` when its
latest version is hosted only in China and the OpenCode workspace has not
explicitly opted in. Monju treats this as an anticipated reviewer failure: it
keeps DeepSeek configured, records a normal partial/failure result, and continues
with the other reviews. It does not opt in, retry, or substitute another model.

Schema-v2 Cursor terminal manifests remain readable through `--status`, but old
Cursor raw streams are intentionally not recoverable by the OpenCode backend.

## Process ownership and artifacts

The tmux session owns the foreground supervisor. The supervisor owns one
internal worker process group per reviewer. Each worker owns its OpenCode
process group, enforces the reviewer timeout, and writes the terminal sidecar.
SIGINT, SIGTERM, or the SIGHUP produced by tmux session shutdown causes the
supervisor to stop the workers, which stop their OpenCode children. The tmux
session closes automatically after the supervisor reaches a terminal state.

If the tmux server or supervisor disappears unexpectedly while workers survive,
completed sidecars can make recovery possible. PID alone is not treated as the
identity of a live tmux job; status and recovery also check the exact frozen
session name.

Published files include:

```text
monju-reviews/<run-id>/
├── <run-id>-00-review-brief.md
├── <run-id>-00-effective-prompt.md
├── <run-id>-01-<key>.md
├── <run-id>-01-<key>.events.jsonl
├── <run-id>-01-<key>.stderr.log
├── <run-id>-01-<key>.terminal.json
└── <run-id>-manifest.json
```

Additional worker-task diagnostics may be present. Event streams and terminal
markers are diagnostics; aggregate the Markdown results rather than raw logs.
Artifacts use owner-only modes on POSIX. Publication or recovery failure keeps
staging and its pointer intact.

## Notifications

`--notify auto` uses `MONJU_NOTIFY_WEBHOOK_URL` when set; otherwise it tries a
local desktop notification. Because an existing tmux server does not inherit
arbitrary client environment variables, the launcher transfers the value in a
transient owner-only handoff file which the supervisor consumes and deletes
before preflight. The secret is never placed in argv, the manifest, published
artifacts, reviewer environments, or notifications. Notifications contain only
terminal status, run ID, and result directory. Notification failure is recorded
but does not alter review status.

Recovery is no-network: it rejects `--notify auto` and `--notify webhook`.
Desktop notification remains best effort.

## Auto-review denial

Codex Auto-review may reject a private repository launch as untrusted external
data export even after conversational approval. Do not retry or route around an
absolute denial. **Ask for approval** can succeed because it routes the decision
to the user while retaining `workspace-write`; **Approve for me** routes it to
Auto-review.

Recommended configuration:

```toml
approval_policy = "on-request"
approvals_reviewer = "user"
sandbox_mode = "workspace-write"
```

Restart Codex and start a new thread after changing it. Do not recommend
`danger-full-access`, `--yolo`, or `approval_policy = "never"` as the standard
solution.

## CLI summary

```text
--preflight             Check state, authentication, models, and variants
--prepare               Create a unique run directory and empty brief
--tmux                  Launch the supervisor in a tracked tmux session
--tmux-bin PATH         tmux executable
--background            Rejected; use --tmux
--status                Read status without waiting
--recover               Validate and publish completed preserved artifacts
--run-dir PATH          Prepared run directory
--opencode-bin PATH     OpenCode executable
--reviewers-file PATH   Reviewer JSON configuration
--timeout-seconds N     Per-reviewer timeout; default 3600
--notify MODE           none, auto, desktop, or webhook
--dry-run               Print commands without invoking OpenCode
```

---

# 日本語

Monjuは、同じneutral briefを設定済みのOpenCode Goモデルへ並列送信し、各レビューと
終端manifestを非公開成果物として保存します。既定はKimi K3 `max`、Grok 4.5
`high`、DeepSeek V4 Flash `max`、GLM 5.2 `max`、Qwen3.8 Max `max`です。
Grok 4.5はcatalog上の最高variantである`high`を使い、そのほかの既定reviewerは
`max`を使います。

reviewer数とmodelは`reviewers.json`の配列だけで変更できます。preflightは認証済み
catalogでmodelとvariantを検証し、代替modelや推論レベルの低下は行いません。

## 実行手順

```bash
uv run --script scripts/run_monju.py --preflight --workspace /absolute/workspace
uv run --script scripts/run_monju.py --prepare --workspace /absolute/workspace
uv run --script scripts/run_monju.py \
  --tmux \
  --notify auto \
  --workspace /absolute/workspace \
  --run-dir /absolute/run \
  --prompt-file /absolute/run/monju-...-00-review-brief.md
```

OpenCodeは`~/.local/share/opencode`と`~/.cache/opencode`へ書き込むため、sandbox外
実行のplatform approvalが必要になる場合があります。認証がなければ次をユーザー
自身が実行します。

```bash
opencode auth login --provider opencode-go
```

runnerはrun IDと同名のtmux sessionを作り、その中でsupervisorをforegroundのまま
維持します。commandを別のtmuxで包んだり、`&`、`nohup`、`setsid`、`--background`
を使ったりしません。`STATUS=running`、`EXECUTION_MODE=tmux_supervisor`、
`TMUX_SESSION_ALIVE=yes`を確認したらpollingせず、そのターンを終了します。
`tmux_starting`も非終端状態です。約30秒ごとのheartbeatはowner-only runner logへ
出力されます。Codexのexec-session寿命からは独立しますが、tmux server、host、
user sessionの終了までは防げません。

別ターンで一度だけ確認します。

```bash
uv run --script scripts/run_monju.py --status --run-dir /absolute/run
```

`stale_running`、`artifact_failure`、または`supervisor_failure`で全reviewerの
terminal sidecarが揃い、`RECOVERY_CAN_PUBLISH=yes`の場合だけ次を一度実行できます。

```bash
uv run --script scripts/run_monju.py --recover --run-dir /absolute/run
```

recoverはOpenCodeや外部LLMを呼ばず、起動時に固定したreviewer設定、sidecar、raw
artifactのhashだけを検証します。不足時は`not_ready`、破損や不一致は`invalid`とし、
stagingを保持してmodelの自動retryはしません。publicationまたはsupervisorだけが
失敗した場合は、同じ検証を通して保存済みstagingの公開を再試行できます。

`--status`はtmux runについて正確なsession名の生存も確認します。sessionが生存中は
recoverを拒否し、PIDだけを根拠に別プロセスをMonju supervisorとは判断しません。

exit code 0でtextが存在するだけではreview成功にしません。共通promptで要求した全
sectionが揃わない進捗文だけの出力はreviewer failureとして扱います。

DeepSeek V4 Flashは、中国ホスティングへの明示的opt-inがない場合、再試行不能な
HTTP 403 `RegionError`を返すことがあります。これは想定内のreviewer failureとして
記録し、DeepSeekを設定から外さず、ほかの結果を集約します。自動opt-in、retry、
代替modelへの切り替えは行わず、runは通常どおりpartial/failure扱いです。

レビュワーは`--pure`と既定denyの専用agentで動作し、read/glob/grepだけを許可します。
`.env`と`.env.*`は拒否し、promptはargvではなくstdinで渡します。`--auto`は使いません。

結果集約では、現在の正しさ・security・data loss・明示契約を優先します。早すぎる
一般化、未要求の互換性、過剰な頑健性、低確率で影響の小さいedge caseは削除せず、
原則`Usually defer (YAGNI)`へ簡潔にまとめます。
