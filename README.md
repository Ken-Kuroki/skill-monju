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
| Qwen3.8 Max | `opencode-go/qwen3.8-max` | `max` |

Grok 4.5 uses `high`, its highest catalogued variant; the other shipped reviewers
use `max`. Reviewer count, models, and variants are controlled by
[`reviewers.json`](reviewers.json) rather than fixed in the runner.

> **Usage warning:** A run with the four default reviewers at these highest
> reasoning variants can consume roughly an entire five-hour OpenCode Go usage
> allowance in one launch. This refers to service allowance, not five hours of
> elapsed wall-clock time. Reduce the entries in `reviewers.json` when conserving
> allowance matters.

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
終端manifestを非公開成果物として保存し、別の会話ターンでCodexが結果を集約できる
ようにします。

既定のレビュアーは次の4件です。

| レビュアー | OpenCode Goモデル | 推論variant |
|---|---|---|
| Kimi K3 | `opencode-go/kimi-k3` | `max` |
| Grok 4.5 | `opencode-go/grok-4.5` | `high` |
| DeepSeek V4 Flash | `opencode-go/deepseek-v4-flash` | `max` |
| Qwen3.8 Max | `opencode-go/qwen3.8-max` | `max` |

Grok 4.5はcatalog上の最高variantである`high`を使い、そのほかの既定レビュアーは
`max`を使います。レビュアー数、モデル、variantはrunnerには固定せず、
[`reviewers.json`](reviewers.json)で管理します。

> **利用量の注意:** 既定の4レビュアーを上記の最高推論variantで動かすと、OpenCode
> Goの5時間利用枠を1回の実行でほぼ使い切る可能性があります。レビューの実時間が
> 5時間という意味ではありません。利用枠を節約したい場合は、実行前に
> `reviewers.json`のレビュアー数を減らしてください。

## 安全性

- 全レビュアーにstdin経由で同じ実効promptを渡します。
- OpenCodeは`--pure`で実行し、`--auto`は使いません。Monju専用agentは既定ですべての
  toolを拒否し、read、glob、grepだけを許可します。
- 通常ファイルのreadを許可した後で`.env`と`.env.*`を明示的に拒否し、
  `.env.example`だけを再許可します。
- レビュアーはファイル編集、shell command、web tool、subagent委譲を実行できません。
  promptでも同じ制約を伝えます。
- raw streamは公開までowner-onlyの一時stagingへ保持します。
- レビュアー環境から`MONJU_NOTIFY_WEBHOOK_URL`を除去します。
- モデル、variant、自動routingの代替は行いません。

Monjuはbriefと、レビュアーが読む対象ファイルの内容をOpenCode Goや外部LLM
サービスへ送信することがあります。明示的なMonju呼び出しは、この範囲のread-only
処理を承認しますが、Codex platform側のapproval policyを上書きしません。

## 動作要件

- Python 3.11以降。Pythonの実行には`uv`を使います。
- tmux。run IDと同名のsessionがforeground supervisorを永続的に所有します。
- OpenCode CLI。まず`PATH`を確認し、見つからなければ公式installerの
  `~/.opencode/bin/opencode`を使います。
- 認証済みのOpenCode Goアカウント：

  ```bash
  opencode auth login --provider opencode-go
  ```

- `reviewers.json`に記載した各モデルとvariantの利用権。
- process group制御を利用できるPOSIX環境またはWSL2。native Windowsはbest effortです。

OpenCodeはversion確認やmodel一覧取得だけでも`~/.local/share/opencode`と
`~/.cache/opencode`へ書き込みます。sandbox内のcoding agentから実行する場合、
preflightとreview起動にsandbox外実行のapprovalが必要になることがあります。

## インストール

Codexからskillとして使う場合は、シンボリックリンクでインストールします。

```bash
./install.sh --agent codex
```

installerはskill hostとして`cursor`と`claude`にも対応しています。Cursor向けに
インストールしてもreview backendは変わらず、レビュアーはOpenCode Go経由で動きます。

```bash
./install.sh --list
./install.sh --agent all
./install.sh --agent cursor --scope project --project /path/to/repo
./install.sh --agent codex --uninstall
```

## レビュアー設定

同梱する設定は次の形式です。

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

配列要素の追加と削除でレビュアー数を変更できます。`key`には重複しない安全なslugを
指定してください。同じモデルとvariantの組み合わせは拒否します。providerは
`opencode-go/...`だけを受け付け、選択可能なvariantがないモデルに限り`variant`を
省略できます。

preflightは認証済みcatalogを次のcommandで取得します。

```bash
opencode models opencode-go --verbose
```

存在しない、無効、または未知のモデルとvariantは拒否し、品質を自動的に下げません。

## 実行手順

### 1. Preflight

```bash
uv run --script scripts/run_monju.py \
  --preflight \
  --workspace /absolute/path/to/workspace
```

preflightでは次を確認します。

- tmux executableとuser-owned tmux server socketへのaccess
- OpenCode data/cache directoryへの実際の書き込み可否
- OpenCode Go credential
- 現在のverbose catalogにある全設定モデルとvariant

主な結果は次のとおりです。

- `PREFLIGHT=ok`
- `PREFLIGHT=tmux_state_unwritable`: preflightとlaunchを同じsandbox外approvalで再実行
- `PREFLIGHT=opencode_state_unwritable`: 対象を限定したsandbox外approvalで再実行
- `PREFLIGHT=opencode_auth_required`: OpenCode Goへloginしてから再実行
- `PREFLIGHT=reviewer_model_invalid`: `reviewers.json`を修正し、代替モデルは使わない

`TMUX_SERVER=existing`は既存のuser tmux serverがrunを所有することを示します。
`TMUX_SERVER=absent`も正常であり、`--tmux`がreview sessionとともにserverを起動します。

OpenCodeはCursor Workspace Trustを使いません。MonjuはOpenCodeのcredentialや設定を
自動編集しません。

### 2. Run directoryとbriefの準備

```bash
uv run --script scripts/run_monju.py \
  --prepare \
  --workspace /absolute/path/to/workspace \
  --output-root /absolute/path/to/workspace/monju-reviews
```

commandは衝突しない`RUN_DIR`と`PROMPT_FILE`を出力します。そのファイルへscope、要件、
制約、除外事項、risk tolerance、具体的な質問を含むneutral briefを1つ書きます。
review対象から`monju-reviews/`を除外してください。

### 3. 起動

```bash
uv run --script scripts/run_monju.py \
  --tmux \
  --notify auto \
  --workspace /absolute/path/to/workspace \
  --run-dir /absolute/path/to/run \
  --prompt-file /absolute/path/to/run/monju-...-00-review-brief.md
```

runnerはrun IDと同名のtmux sessionを作り、shellを介さずにsupervisorをforeground
commandとして起動します。commandを別のtmuxで包んだり、`&`、`nohup`、`setsid`、
`--background`を使ったりしません。launcherが次を返したらpollingせず、その会話
ターンを終了します。

```text
STATUS=running
EXECUTION_MODE=tmux_supervisor
TMUX_SESSION=<run-id>
TMUX_SESSION_ALIVE=yes
```

`STATUS=tmux_starting`も非終端状態なので、別ターンで一度だけ確認します。supervisorは
約30秒ごとに次のheartbeatをowner-only runner logへflushします。

```text
MONJU_HEARTBEAT run_id=<run-id> elapsed_seconds=<n>
```

heartbeatはrunner内部の生存診断です。tmuxによりCodex exec-sessionの寿命からは
独立しますが、tmux server、host、user session自体の終了までは防げません。

### 4. 別ターンで状態確認

```bash
uv run --script scripts/run_monju.py \
  --status \
  --run-dir /absolute/path/to/run
```

`--status`はread-onlyであり、自動pollingや自動recoverを行いません。生存中のtmux run
では`TMUX_SESSION_ALIVE=yes`を返します。終端状態ならmanifestと生成済みの全review
Markdownを読みます。

manifestが`running`のままsupervisorが消失した場合は、`stale_running`とstagingの
診断情報に加えて次を返します。

```text
RECOVERY=ready|not_ready|invalid
RECOVERY_CAN_PUBLISH=yes|no
```

### 5. 完了済み孤立workerのrecover

全レビュアーの検証済みterminal sidecarが揃い、`RECOVERY_CAN_PUBLISH=yes`の場合だけ
実行します。

```bash
uv run --script scripts/run_monju.py \
  --recover \
  --run-dir /absolute/path/to/run
```

各workerはOpenCode終了後、モデルとvariant、終了状態、時刻、event/stderr artifactの
sizeとSHA-256を含むterminal sidecarをatomicに書き込みます。recoverはそれらを起動時の
schema-v3 running manifestと照合します。

recoverはOpenCodeや外部LLMを呼ばず、モデル変更やretryも行いません。sidecar不足は
`not_ready`、破損やhash不一致は公開不能な`invalid`です。全レビュアーが完了していれば、
検証済みOpenCode errorを通常のpartial/failureとして公開できます。publicationまたは
supervisorだけが失敗し、有効なstagingが残っている場合は、同じ検証を通して公開を
再試行できます。すでに終端manifestが公開済みなら、再recoverはread-onlyのno-opです。

exit code 0でtextが存在するだけではreview成功にしません。共通promptで要求した全
sectionが揃わない進捗文だけの出力はreviewer failureとして扱います。

DeepSeek V4 Flashは、中国ホスティングへの明示的opt-inがない場合、再試行不能な
HTTP 403 `RegionError`を返すことがあります。これは想定内のreviewer failureとして
記録し、DeepSeekを設定から外さず、ほかの結果を集約します。自動opt-in、retry、
代替モデルへの切り替えは行わず、runは通常どおりpartial/failure扱いです。

schema-v2のCursor終端manifestは`--status`で引き続き読めますが、旧Cursor raw streamは
OpenCode backendからrecoverできません。

## プロセスの所有関係と成果物

tmux sessionがforeground supervisorを所有し、supervisorがレビュアーごとの内部worker
process groupを所有します。各workerはOpenCode process groupを所有し、timeoutを適用して
terminal sidecarを書きます。SIGINT、SIGTERM、またはtmux session終了時のSIGHUPを受けると、
supervisor、worker、OpenCode childの順に停止します。終端状態に達するとtmux sessionは
自動的に閉じます。

tmux serverまたはsupervisorが予期せず消失しても、完了済みsidecarがあればrecoverできる
場合があります。PIDだけをlive tmux jobの識別根拠にせず、statusとrecoverは起動時に固定した
正確なsession名も確認します。

公開成果物は次の構成です。

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

追加のworker-task診断ファイルが存在する場合があります。event streamとterminal markerは
診断用であり、集約にはMarkdownを使います。POSIXでは成果物をowner-onlyにし、公開または
recover失敗時はstagingとpointerを保持します。

## 通知

`--notify auto`は`MONJU_NOTIFY_WEBHOOK_URL`があればwebhookを使い、なければlocal desktop
通知を試します。既存tmux serverはclientの任意の環境変数を継承しないため、launcherは
owner-onlyの一時handoff fileで値を渡し、supervisorがpreflight前に読み取って削除します。
secretをargv、manifest、公開成果物、レビュアー環境、通知本文へ入れません。通知本文は終端status、
run ID、結果directoryだけです。通知失敗は記録しますがreview statusを変更しません。

recoverはno-networkなので`--notify auto`と`--notify webhook`を拒否します。desktop通知は
best effortです。

## Auto-reviewによる拒否

Codex Auto-reviewはprivate repositoryから未確認の外部送信先へのdata exportとして、
会話上で承認済みでもlaunchを拒否することがあります。absolute denyを受けたら再試行や
迂回実行をしません。**Ask for approval**は`workspace-write`を維持したまま判断をユーザーへ
送るため成功し得ますが、**Approve for me**はAuto-reviewへ判断を送ります。

推奨設定は次のとおりです。

```toml
approval_policy = "on-request"
approvals_reviewer = "user"
sandbox_mode = "workspace-write"
```

変更後はCodexを再起動し、新しいthreadを開始します。標準解決策として
`danger-full-access`、`--yolo`、`approval_policy = "never"`を勧めません。

## 結果集約

結果集約では、現在の正しさ、セキュリティ、データ損失、明示された契約を優先します。早すぎる
一般化、未要求の互換性、過剰な頑健性、低確率で影響の小さいedge caseは削除せず、
原則`Usually defer (YAGNI)`へ簡潔にまとめます。

## CLI一覧

```text
--preflight             状態領域、認証、model、variantを確認
--prepare               一意なrun directoryと空のbriefを作成
--tmux                  追跡可能なtmux sessionでsupervisorを起動
--tmux-bin PATH         tmux executable
--background            拒否される旧option。--tmuxを使用
--status                待機せず現在状態を読み取り
--recover               完了済みの保存artifactを検証・公開
--run-dir PATH          prepare済みrun directory
--opencode-bin PATH     OpenCode executable
--reviewers-file PATH   レビュアーJSON設定
--timeout-seconds N     レビュアーごとのtimeout。既定3600秒
--notify MODE           none、auto、desktop、webhook
--dry-run               OpenCodeを起動せずcommandを表示
```
