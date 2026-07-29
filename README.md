# Monju

Human-facing documentation for the Monju Codex skill.

- [English](#english)
- [日本語](#日本語)

> This README is for human operators. It is intentionally separate from the
> agent instructions in `SKILL.md` and is not referenced from them.

## English

### What Monju does

Monju asks three Cursor CLI models to review the same material independently and
in parallel:

| Reviewer | Fixed model specification |
| --- | --- |
| Kimi K3 | `kimi-k3-max` |
| Grok 4.5 | `cursor-grok-4.5-high` |
| Claude Fable 5 | `claude-fable-5-thinking-max` |

The concrete review brief is generated for each task by Codex. The runner keeps
the model selection, highest non-Fast model IDs, read-only mode, prompt envelope,
parallel execution, and output filenames deterministic.

Reviews normally take several minutes or longer. Codex keeps the supervisor in
the foreground of a managed background terminal, ends the launch turn without
polling, and checks and aggregates results in a later conversation turn.

Monju is suitable for:

- implementation-plan reviews;
- post-implementation and code reviews;
- documentation and design reviews;
- risk analysis and independent second opinions.

### Platform support

| Host environment | Support level |
| --- | --- |
| macOS | Supported |
| Linux | Supported |
| Windows with WSL2 | Supported and recommended; uses Linux/POSIX behavior |
| Native Windows (PowerShell or CMD) | Best effort only; not supported end to end |

Cursor CLI's supported Windows path is WSL. For WSL2, prefer a workspace and
output root in the WSL Linux filesystem rather than under `/mnt/c`, and use a
webhook when completion must be visible outside the WSL session. Native-Windows
branches remain as compatibility aids, but they are not exercised by real
Windows CI and do not constitute a support promise.

### Safety and review independence

- All three reviewers receive the same effective prompt.
- Reviewers run in Cursor Ask mode and are instructed not to modify files,
  execute mutating commands, or delegate to subagents.
- Fast variants, Auto routing, lower-effort fallback, replacement models, and
  additional reviewer agents are not used.
- Reported `system/init` model names must match a normalized allowlist built from
  the fixed IDs and observed maximum-quality display names; unknown names fail
  visibly.
- Review briefs exclude `monju-reviews/`, and intermediate model streams remain
  outside the reviewed workspace until all reviewers finish. This reduces
  accidental cross-review contamination but is not a hard same-user security
  boundary; a process that deliberately follows internal paths may still reach
  temporary artifacts.
- Monju itself writes review artifacts and diagnostic logs to the configured
  output directory.
- On POSIX, including WSL2, interruptions terminate reviewer process groups. In
  the best-effort native-Windows path, only direct reviewer processes are
  terminated; descendant cleanup depends on Cursor CLI behavior. Publication
  failures preserve staging instead of deleting completed work.
- Owner-only `0600`/`0700` modes protect artifacts on POSIX. The best-effort
  native-Windows path requires an output root protected by an owner-only DACL.

### Pragmatic prioritization

Monju keeps marginal observations visible without turning all of them into work.
Each reviewer separates `Act now` findings from `Usually defer (YAGNI)` findings.
Premature abstraction, compatibility outside the stated support matrix,
disproportionate defensive machinery, and unlikely low-impact edge cases normally
go in the deferred section.

The final Codex aggregation reassesses that disposition and recommends only
act-now work by default. Reviewer consensus does not by itself justify extra
complexity. Likely correctness or security failures, irreversible data loss,
and explicit contract violations are not downgraded as YAGNI. A small localized
change that clearly simplifies current code is also act-now; stylistic preferences
and high-churn rewrites are not. Every non-duplicate finding is accounted for in
an act-now, deferred, uncertain, or rejected-after-verification category, without
letting the compact deferred section dominate the result.

### Proposed experiments

Reviewers do not perform experiments in the current mode. They are encouraged
to propose an experiment when it would materially improve confidence or resolve
an important uncertainty.

Every proposed experiment must include:

- the question or uncertainty it addresses;
- the exact procedure and commands, where applicable;
- preconditions and required inputs;
- expected writes, side effects, or external interactions;
- at least two materially different possible outcomes;
- an inconclusive or failed outcome when plausible;
- an interpretation for every possible outcome;
- risks and approval requirements.

Predicted outcomes are explicitly hypothetical and must not be presented as
observed evidence. Any later execution requires separate user authorization.

### Installation

Clone this repository, then install the skill with a symbolic link so agent
updates in the checkout are picked up immediately:

```bash
git clone <repository-url> monju
cd monju
./install.sh --agent codex
```

Run `./install.sh` without options for an interactive prompt. Supported targets:

| Agent | Personal scope | Project scope |
| --- | --- | --- |
| Codex | `~/.codex/skills/monju` | not supported |
| Cursor | `~/.cursor/skills/monju` | `<project>/.cursor/skills/monju` |
| Claude Code | `~/.claude/skills/monju` | `<project>/.claude/skills/monju` |

Common examples:

```bash
./install.sh --list
./install.sh --agent all
./install.sh --agent cursor --scope project --project /path/to/repo
./install.sh --agent codex --uninstall
```

`--force` can replace or remove a symlink that points elsewhere. It never
removes or replaces a regular file or directory. A custom `--dest` must end in
`/monju` or name a parent directory ending in `/skills`.

`install.sh` lives at the repository root because it is a human-facing setup
script. `scripts/run_monju.py` stays under `scripts/` because that matches the
usual agent-skill layout for bundled tools referenced from `SKILL.md`.

Do not install into `~/.cursor/skills-cursor/`; that directory is reserved for
Cursor's built-in skills.

### Requirements

- Codex with the `monju` skill installed.
- Cursor CLI with the `agent` command available.
- A logged-in Cursor account with access to all three configured models.
- `uv` for running the bundled Python runner.
- On Windows, WSL2; native PowerShell and CMD execution is not supported end to
  end.

Useful checks:

```bash
agent --version
AGENT_CLI_CREDENTIAL_STORE=file agent status
AGENT_CLI_CREDENTIAL_STORE=file agent models
```

If needed, authenticate with:

```bash
AGENT_CLI_CREDENTIAL_STORE=file agent login
```

The file credential store is intentional. A macOS login Keychain can be
unavailable to a remotely controlled Codex process even when an interactive
terminal is logged in. Complete login yourself in the browser; never send a
password through Codex.

### Using Monju from Codex

Normally, ask Codex to use the skill and describe the review target:

```text
Use $monju to review this implementation plan for correctness, missing work,
operational risk, and test coverage.
```

```text
Use $monju to review the changes in this repository after implementation.
Focus on regressions, security, and compatibility.
```

```text
Use $monju to review this design document. Identify ambiguous requirements and
propose any experiments that would resolve important uncertainties.
```

Codex creates a task-specific review brief in a unique run directory and runs
Monju as the foreground command of a Codex background terminal. It does not wait
or poll in that turn. In a later turn, ask Codex to check the saved run; it reads
the three results, verifies material findings against the source, and reports
consensus, unique findings, disagreements, and unexecuted experiment proposals.

Invoking Monju is itself authorization for the scoped read-only review,
preflight, artifact creation, and exact-workspace trust registration when
needed. Codex must not ask again in chat before proceeding. If sandbox escalation
is required, Codex submits the exact tool call directly and the platform may show
its approval popup. Authentication or SSO that genuinely requires user action
remains a blocking exception.

### Direct runner invocation

Codex normally invokes the runner automatically. Its asynchronous workflow
starts with a preflight:

```bash
AGENT_CLI_CREDENTIAL_STORE=file \
uv run --script "/absolute/path/to/monju/scripts/run_monju.py" \
  --preflight \
  --workspace "/absolute/path/to/workspace"
```

The preflight performs an actual create-and-delete write probe in the relevant
`~/.cursor/projects` directory, verifies the workspace-specific trust marker,
and checks file-store authentication. If it reports
`PREFLIGHT=cursor_state_unwritable`, rerun both preflight and launch outside the
filesystem sandbox. If it reports `PREFLIGHT=workspace_trust_required`, Codex
directly submits a narrowly scoped platform approval request and runs the
following command outside the sandbox with a PTY:

```bash
AGENT_CLI_CREDENTIAL_STORE=file \
agent --workspace "/absolute/path/to/workspace" --trust
```

The user does not need to open a terminal. Once Cursor reaches its initial
prompt, Codex interrupts it without submitting a prompt and reruns preflight.
Codex does not ask for a separate conversational confirmation before submitting
the platform approval request. Invoking Monju already authorizes trust
registration for the exact workspace.
The approval must name the exact workspace being persistently trusted. Do not
use `--yolo` or `--force`, request a broad reusable approval, or edit Cursor's
trust marker directly.

After `PREFLIGHT=ok`, allocate the run directory:

```bash
uv run --script "/absolute/path/to/monju/scripts/run_monju.py" \
  --prepare \
  --workspace "/absolute/path/to/workspace" \
  --output-root "/absolute/path/to/output"
```

Write the review brief to the printed `PROMPT_FILE`, then use one Codex unified
exec session to start the following command with a short initial yield:

```bash
AGENT_CLI_CREDENTIAL_STORE=file \
uv run --script "/absolute/path/to/monju/scripts/run_monju.py" \
  --notify auto \
  --workspace "/absolute/path/to/workspace" \
  --run-dir "/absolute/path/from/RUN_DIR" \
  --prompt-file "/absolute/path/from/PROMPT_FILE"
```

Keep this runner in the exec session's foreground. When the tool yields a live
session ID after about three seconds, Codex can end its turn without polling;
the managed background terminal continues owning the supervisor. Do not add
`&`, `nohup`, `setsid`, or `--background`. A human invoking the command directly
must leave that terminal command running until review completion.

Check it later without waiting:

```bash
uv run --script "/absolute/path/to/monju/scripts/run_monju.py" \
  --status \
  --run-dir "/absolute/path/from/RUN_DIR"
```

`--status` reports `stale_running` when a run still says `running` but its
recorded supervisor process no longer exists. It also reports `STALE_REASON`,
reviewer progress counts, and the preserved staging path. Initialized or
completed reviewers indicate external termination after real progress; meaningful
stderr without any initialization indicates startup failure. The known
`LC_ALL=C.UTF-8` locale warning is ignored for this classification.

Runner options:

```text
--preflight             Check Cursor state access, workspace trust, and authentication
--prepare               Create a unique run directory and empty brief
--background            Rejected; use a foreground Codex background terminal
--status                Read current status without waiting
--run-dir PATH          Prepared run directory
--agent-bin PATH        Cursor CLI executable; default: agent
--timeout-seconds N     Per-reviewer timeout; default: 3600
--notify MODE           none, auto, desktop, or webhook; default: none
--dry-run               Print exact commands without invoking Cursor
```

The prompt file must be non-empty UTF-8 Markdown. It should identify the
objective, exact scope, requirements, constraints, review criteria, exclusions,
and any questions the reviewers must answer.

### Completion notifications

Notifications run inside the foreground supervisor after a terminal manifest
has been published. Do not append a separate notification command to the runner
invocation; the long-lived supervisor sends the completion notification itself.

| Mode | Behavior |
| --- | --- |
| `none` | Disable notifications. This is the runner default. |
| `auto` | Use the configured webhook, otherwise the local desktop. |
| `desktop` | Use the current OS desktop mechanism. |
| `webhook` | POST JSON to the URL in `MONJU_NOTIFY_WEBHOOK_URL`. |

The skill uses `--notify auto`. Set the webhook in the environment before
launching when the review runs on a remote machine:

```bash
export MONJU_NOTIFY_WEBHOOK_URL="https://notification-service.example/hook"
```

Keep that value out of command arguments and review briefs. The JSON body
contains only the event name, terminal status, run ID, result directory, and
short message; it does not include source code or review text. The runner removes
the webhook variable from each Cursor reviewer subprocess environment.

Desktop backends are:

- macOS: `osascript`; a logged-in GUI session and allowed notifications are
  required.
- Linux: `notify-send`; a graphical session, notification daemon, and D-Bus are
  required.
- WSL2: the Linux backend applies; prefer a webhook because `notify-send` may not
  surface in the Windows desktop session.
- Native Windows, best effort only: `msg.exe`, addressed to the current
  `USERNAME` rather than broadcast to every session; that user or RDP session
  must allow session messages.

Desktop delivery may be invisible on a headless or remotely controlled host.
Use a webhook to reach another device. Notification delivery is best effort:
its `sent`, `failed`, or `disabled` result is recorded under `notification` in
the manifest, but it never changes the review status or exit code.

### Output layout

Each run receives a unique UTC timestamp, process ID, and random suffix:

```text
<output-root>/
└── monju-<UTC timestamp>-p<PID>-r<nonce>/
    ├── <run-id>-00-review-brief.md
    ├── <run-id>-00-effective-prompt.md
    ├── <run-id>-01-kimi-k3.md
    ├── <run-id>-02-grok-4-5.md
    ├── <run-id>-03-fable-5.md
    ├── <run-id>-01-kimi-k3.events.jsonl
    ├── <run-id>-02-grok-4-5.events.jsonl
    ├── <run-id>-03-fable-5.events.jsonl
    ├── <run-id>-01-kimi-k3.stderr.log
    ├── <run-id>-02-grok-4-5.stderr.log
    ├── <run-id>-03-fable-5.stderr.log
    ├── <run-id>-runner.pid
    └── <run-id>-manifest.json
```

The runner determines every filename; reviewer models cannot choose or alter
them. Existing run directories are never overwritten.

The `*.md` files are the human-readable reviews. The event streams, stderr
logs, and manifest are diagnostic records. Treat event streams as potentially
sensitive because they may contain excerpts from reviewed files.

### Failure behavior

Terminal manifest states are `success`, `partial_failure`, `failure`,
`interrupted`, `artifact_failure`, and `supervisor_failure`. A reviewer fails
visibly if it:

- times out or exits with an error;
- produces no successful final result;
- reports a model name outside the known-good normalized allowlist;
- reports a forbidden Fast variant.

Successful reviews from a partially failed run are preserved. Monju never
silently retries with a lower effort or substitute model. On a POSIX
interruption, reviewer process groups are terminated and available diagnostics
are published; this includes WSL2. In the best-effort native-Windows path,
direct Cursor processes are terminated, while helper descendants depend on
Cursor CLI cleanup. If publishing fails, the manifest records
`staging_preserved` when possible.

For troubleshooting:

1. Run `--preflight`. If Cursor state is unwritable, obtain sandbox approval
   before launch. If workspace trust is missing, have Codex run
   `agent --workspace <path> --trust` with a PTY under a narrowly scoped
   outside-sandbox approval, then rerun preflight.
2. Check `AGENT_CLI_CREDENTIAL_STORE=file agent status` and run the matching
   `agent login` command if necessary.
3. Check account-visible models with
   `AGENT_CLI_CREDENTIAL_STORE=file agent models`.
4. Open `<run-id>-manifest.json` for the overall status and requested/reported
   model names.
5. Inspect the affected reviewer's `*.stderr.log` and `*.events.jsonl`.

## 日本語

### Monjuの概要

Monjuは、同じ対象を3つのCursor CLIモデルへ渡し、相互に独立したレビューを
並列実行するCodex Skillです。

| レビュワー | 固定モデル指定 |
| --- | --- |
| Kimi K3 | `kimi-k3-max` |
| Grok 4.5 | `cursor-grok-4.5-high` |
| Claude Fable 5 | `claude-fable-5-thinking-max` |

具体的なレビュー指示は、タスクごとにCodexが生成します。モデル選択、最高非Fast
モデルID、読み取り専用モード、共通プロンプト、3並列実行、保存ファイル名はランナー
側で固定されます。

レビューには通常数分以上かかります。Codexは管理対象background terminalの
前景で監督プロセスを動かし、ポーリングせずに起動ターンを終了します。別の
会話ターンで結果を確認・集約します。

主な用途は次のとおりです。

- 実装計画のレビュー
- 実装後のコードレビュー
- ドキュメントや設計のレビュー
- リスク分析や独立したセカンドオピニオン

### 対応プラットフォーム

| ホスト環境 | 対応レベル |
| --- | --- |
| macOS | 対応 |
| Linux | 対応 |
| Windows + WSL2 | 対応・推奨。Linux/POSIXとして動作 |
| Windowsネイティブ（PowerShell、CMD） | best effortのみ。エンドツーエンドでは非対応 |

Cursor CLIのWindows向け正式経路はWSLです。WSL2では、`/mnt/c`配下よりWSLの
Linuxファイルシステム内にワークスペースと出力先を置き、WSL外でも完了を確認
したい場合はWebhookを使うことを推奨します。Windowsネイティブ向けの分岐は
互換性の補助として残しますが、Windows実機CIでは検証しておらず、対応保証には
含めません。

### 安全性とレビューの独立性

- 3モデルには同一の有効プロンプトが渡されます。
- レビュワーはCursorのAskモードで動作し、ファイル変更、変更を伴うコマンド
  実行、サブエージェントへの委譲を禁止されています。
- Fast版、Autoルーティング、低effortへのフォールバック、代替モデル、追加の
  レビュワーエージェントは使用しません。
- `system/init`が報告するモデル名は、固定IDと実際に観測した最高品質の表示名
  からなる正規化allowlistに一致する必要があります。未知の名前は明示的に失敗
  させます。
- レビュー指示では`monju-reviews/`を対象外とし、全レビューが完了するまで中間
  ストリームを対象ワークスペースの外に置きます。これは意図しない結果混入を
  減らすための運用上の分離であり、同一ユーザー内の厳密なセキュリティ境界では
  ありません。内部パスを意図的に追えば、一時成果物へ到達できる場合があります。
- Monju自体は、指定された出力先へレビュー結果と診断ログを書き込みます。
- WSL2を含むPOSIXでは、中断時にレビュワーのプロセスグループを終了します。
  best effortのWindowsネイティブ経路では直接のレビュワープロセスだけを終了
  し、子孫プロセスの後始末はCursor CLIの動作に依存します。成果物の公開に
  失敗した場合は完了済みデータを消さず、一時ディレクトリへ保存します。
- POSIXでは`0600`/`0700`で成果物を所有者限定にします。best effortのWindows
  ネイティブ経路では、出力先へあらかじめ所有者限定DACLを設定してください。

### 実用的な優先順位

Monjuは細かな指摘も消しませんが、そのすべてを実装タスクにはしません。各
レビューワーは、指摘を`Act now`と`Usually defer (YAGNI)`に分けます。早すぎる
抽象化、明示されていない環境への互換性、釣り合わない防御コード、影響が小さく
発生しにくいエッジケースは、通常後者へ置きます。

Codexによる最終集約でも分類を見直し、既定では`Act now`だけを推奨します。
複数レビューワーの合意だけを理由に複雑さを増やしません。一方、現実的に起こる
正しさ・セキュリティの問題、不可逆なデータ損失、明示契約違反はYAGNI扱い
しません。現在のコードを明確に単純化する小さな局所修正も`Act now`ですが、
好みだけのスタイル変更や変更量の大きい書き換えは対象外です。重複を除く各指摘
は、今やる、見送る、不確実、検証後に棄却、のいずれかへ置き、簡潔な見送り項目
が集約結果の中心にならないようにします。

### 追加実験の提案

現在のモードでは、レビュワー自身は実験を行いません。ただし、結論の確度を
大きく高める場合や重要な不確実性を解消できる場合は、追加実験を積極的に提案
します。

各実験案には次の項目が必要です。

- 解消したい疑問または不確実性
- 該当する場合は正確な手順とコマンド
- 前提条件と必要な入力
- 想定される書き込み、副作用、外部アクセス
- 少なくとも2通りの実質的に異なる想定結果
- 妥当な場合は、失敗または判定不能となる結果
- 各想定結果が何を支持、否定、または未解決とするか
- リスクと必要な承認

想定結果はあくまで仮説として記載され、観測済みの証拠として扱われません。
後から実験を実行する場合は、ユーザーによる別途の承認が必要です。

### インストール

リポジトリをクローンし、シンボリックリンクでSkillを登録します。リンク先の
チェックアウトを更新すれば、Skillも追従します。

```bash
git clone <repository-url> monju
cd monju
./install.sh --agent codex
```

オプションを省略すると対話式でエージェントを選べます。対応先は次のとおりです。

| エージェント | personal | project |
| --- | --- | --- |
| Codex | `~/.codex/skills/monju` | 非対応 |
| Cursor | `~/.cursor/skills/monju` | `<project>/.cursor/skills/monju` |
| Claude Code | `~/.claude/skills/monju` | `<project>/.claude/skills/monju` |

よく使う例:

```bash
./install.sh --list
./install.sh --agent all
./install.sh --agent cursor --scope project --project /path/to/repo
./install.sh --agent codex --uninstall
```

`--force`で置換・削除できるのは、別の場所を指すシンボリックリンクだけです。
通常ファイルや実ディレクトリは削除・置換しません。独自の`--dest`は
`/monju`で終わるか、`/skills`で終わる親ディレクトリを指定してください。

`install.sh` は人間向けのセットアップ用なのでリポジトリ直下に置いています。
`scripts/run_monju.py` は `SKILL.md` から参照される実行時ツールなので、一般的な
Skill構成に合わせて `scripts/` 配下に置いています。

`~/.cursor/skills-cursor/` にはインストールしないでください。Cursor組み込み
Skill専用のディレクトリです。

### 必要条件

- `monju` SkillがインストールされたCodex
- `agent`コマンドを利用できるCursor CLI
- 設定された3モデルすべてを利用できる、ログイン済みのCursorアカウント
- 同梱Pythonランナーを実行するための`uv`
- WindowsではWSL2。ネイティブPowerShell/CMDでの実行はエンドツーエンドでは
  サポートしません。

確認用コマンド:

```bash
agent --version
AGENT_CLI_CREDENTIAL_STORE=file agent status
AGENT_CLI_CREDENTIAL_STORE=file agent models
```

未ログインの場合:

```bash
AGENT_CLI_CREDENTIAL_STORE=file agent login
```

ファイル認証ストアの利用は意図的です。リモート操作中のCodexプロセスからは、
対話ターミナルがログイン済みでもmacOSログインキーチェーンを参照できない場合が
あります。ブラウザでのログインはユーザー自身が完了し、パスワードをCodexへ
送らないでください。

### Codexからの使い方

通常は、CodexへSkillの使用とレビュー対象を指示します。

```text
$monjuを使って、この実装計画をレビューしてください。
正しさ、作業漏れ、運用リスク、テスト範囲を重視してください。
```

```text
$monjuを使って、このリポジトリの実装後レビューをしてください。
リグレッション、セキュリティ、互換性を重視してください。
```

```text
$monjuを使って、この設計文書をレビューしてください。
曖昧な要件を特定し、重要な不確実性を解消できる実験も提案してください。
```

Codexは一意な実行フォルダ内にタスク専用のレビュー指示を作成し、Codex
background terminalの前景コマンドとしてMonjuを実行します。そのターンでは
待機もポーリングも行いません。別のターンで保存済み実行の確認を依頼すると、
3件の結果を読み、重要な指摘を元資料で検証したうえで、合意点、単独の有用な
指摘、意見の相違、未実行の実験案をまとめます。

Monjuの呼び出し自体を、対象範囲の読み取り専用レビュー、preflight、成果物の
作成、必要な場合の対象workspace限定のtrust登録に対する許可として扱います。
Codexは実行前に会話であらためて確認しません。sandbox外実行が必要な場合は、
対象コマンドを直接ツールへ送信し、実行基盤が必要に応じて承認ポップアップを
表示します。認証やSSOなど、ユーザー本人の操作が本当に必要な場合だけ停止します。

### ランナーの直接実行

通常はCodexが自動的に実行します。Codexの非同期ワークフローでは、最初に
preflightを実行します。

```bash
AGENT_CLI_CREDENTIAL_STORE=file \
uv run --script "/absolute/path/to/monju/scripts/run_monju.py" \
  --preflight \
  --workspace "/absolute/path/to/workspace"
```

preflightは、該当する`~/.cursor/projects`内で一時ファイルを作成・削除して実際の
書き込み可否を確認し、workspace固有のtrust markerとファイル認証を検証します。
`PREFLIGHT=cursor_state_unwritable`の場合は、preflightと起動の両方をfilesystem
sandbox外で再実行してください。`PREFLIGHT=workspace_trust_required`の場合は、
Codexが対象workspaceを明記した限定的な実行承認をツールから直接要求し、次の
コマンドをPTY付きでsandbox外実行します。

```bash
AGENT_CLI_CREDENTIAL_STORE=file \
agent --workspace "/absolute/path/to/workspace" --trust
```

ユーザーがターミナルを開く必要はありません。Cursor Agentが最初のプロンプトまで
到達したら、Codexはプロンプトを送信せずにプロセスを終了し、preflightを再実行
します。プラットフォームの承認要求を送る前に、会話上の確認は挟みません。Monjuの
呼び出し時点で、そのworkspaceに限定したtrust登録は許可済みとして扱います。
承認理由には、永続的にtrust登録するworkspaceの絶対パスを明記します。
`--yolo`や`--force`、広範な再利用可能承認、trust markerの直接編集は使いません。

`PREFLIGHT=ok`の後に実行フォルダを確保します。

```bash
uv run --script "/absolute/path/to/monju/scripts/run_monju.py" \
  --prepare \
  --workspace "/absolute/path/to/workspace" \
  --output-root "/absolute/path/to/output"
```

表示された`PROMPT_FILE`へレビュー指示を書き、Codex unified exec sessionを
1つ使って、短い初回yield付きで次のコマンドを起動します。

```bash
AGENT_CLI_CREDENTIAL_STORE=file \
uv run --script "/absolute/path/to/monju/scripts/run_monju.py" \
  --notify auto \
  --workspace "/absolute/path/to/workspace" \
  --run-dir "/RUN_DIRで表示された絶対パス" \
  --prompt-file "/PROMPT_FILEで表示された絶対パス"
```

runner自体をexec sessionの前景に置きます。約3秒後にツールが生存中のsession
IDを返したら、Codexはポーリングせずターンを終了できます。管理対象background
terminalが引き続き監督プロセスを所有します。`&`、`nohup`、`setsid`、
`--background`は使いません。人が直接実行する場合は、レビュー完了までその
ターミナルコマンドを動かしておく必要があります。

後から、待機せず状態を確認します。

```bash
uv run --script "/absolute/path/to/monju/scripts/run_monju.py" \
  --status \
  --run-dir "/RUN_DIRで表示された絶対パス"
```

マニフェストが`running`のまま監督プロセスだけ終了している場合、`--status`は
`stale_running`に加え、`STALE_REASON`、レビュワーごとの進捗数、保存済みstaging
パスを表示します。initまたはfinalイベントがあれば実処理後の外部終了、
初期化がなく意味のあるstderrだけがあれば起動失敗と分類します。既知の
`LC_ALL=C.UTF-8` locale警告はこの分類では無視します。

ランナーのオプション:

```text
--preflight             Cursor状態領域、workspace trust、認証を確認
--prepare               一意な実行フォルダと空のbriefを作成
--background            拒否。Codex background terminalの前景で実行
--status                待機せず現在の状態を表示
--run-dir PATH          準備済み実行フォルダ
--agent-bin PATH        Cursor CLI実行ファイル。既定値: agent
--timeout-seconds N     レビュワーごとの制限時間。既定値: 3600
--notify MODE           none、auto、desktop、webhook。既定値: none
--dry-run               Cursorを実行せず、正確なコマンドを表示
```

プロンプトファイルは、空ではないUTF-8 Markdownである必要があります。レビュー
目的、正確な対象範囲、要件、制約、評価基準、除外事項、回答必須の質問を記載
してください。

### 完了通知

通知は、前景の監督プロセスが終端マニフェストを公開した後に実行します。
runnerコマンドの後ろへ別の通知コマンドをつなげず、長寿命の監督プロセス自身に
完了通知を送らせます。

| モード | 動作 |
| --- | --- |
| `none` | 通知しません。ランナーの既定値です。 |
| `auto` | Webhookが設定済みならWebhook、なければローカル通知を使います。 |
| `desktop` | 実行OSのデスクトップ通知を使います。 |
| `webhook` | `MONJU_NOTIFY_WEBHOOK_URL`へJSONをPOSTします。 |

Skillからは`--notify auto`を指定します。リモートマシンでレビューを実行し、
手元の端末へ通知したい場合は、起動前の環境へWebhookを設定します。

```bash
export MONJU_NOTIFY_WEBHOOK_URL="https://notification-service.example/hook"
```

この値はコマンド引数やレビュー指示へ書かないでください。JSON本文に含めるのは
イベント名、終端状態、実行ID、結果フォルダ、短いメッセージだけです。ソース
コードやレビュー本文は送信しません。ランナーは各Cursorレビューワーの
サブプロセス環境からWebhook変数を削除します。

デスクトップ通知の実装は次のとおりです。

- macOS: `osascript`。GUIログイン、通知許可が必要です。
- Linux: `notify-send`。GUIセッション、通知デーモン、D-Busが必要です。
- WSL2: Linux向けバックエンドを使います。`notify-send`がWindowsデスクトップ
  へ表示されない場合があるため、Webhookを推奨します。
- Windowsネイティブ（best effortのみ）: `msg.exe`。全セッションへ一斉送信
  せず、現在の`USERNAME`だけを宛先にします。対象ユーザーまたはRDPセッション
  でメッセージ受信が許可されている必要があります。

ヘッドレス環境やリモート操作中のマシンでは、デスクトップ通知が見えない場合が
あります。別端末で受け取る場合はWebhookを使用してください。通知はベスト
エフォートです。`sent`、`failed`、`disabled`の結果をマニフェストの
`notification`へ記録しますが、レビュー状態や終了コードは変更しません。

### 出力ファイル

各実行には、UTC時刻、プロセスID、ランダム接尾辞を含む一意な実行IDが
割り当てられます。

```text
<output-root>/
└── monju-<UTC時刻>-p<PID>-r<nonce>/
    ├── <run-id>-00-review-brief.md
    ├── <run-id>-00-effective-prompt.md
    ├── <run-id>-01-kimi-k3.md
    ├── <run-id>-02-grok-4-5.md
    ├── <run-id>-03-fable-5.md
    ├── <run-id>-01-kimi-k3.events.jsonl
    ├── <run-id>-02-grok-4-5.events.jsonl
    ├── <run-id>-03-fable-5.events.jsonl
    ├── <run-id>-01-kimi-k3.stderr.log
    ├── <run-id>-02-grok-4-5.stderr.log
    ├── <run-id>-03-fable-5.stderr.log
    ├── <run-id>-runner.pid
    └── <run-id>-manifest.json
```

すべてのファイル名はランナーが決定し、レビューモデルは選択・変更できません。
既存の実行ディレクトリを上書きすることもありません。

`*.md`が人間向けのレビュー結果です。イベントストリーム、標準エラーログ、
マニフェストは診断用記録です。イベントストリームにはレビュー対象ファイルの
抜粋が含まれる可能性があるため、機密情報として扱ってください。

### 失敗時の動作

マニフェストの終端状態は`success`、`partial_failure`、`failure`、
`interrupted`、`artifact_failure`、`supervisor_failure`です。次の場合、
該当レビュワーは明示的に失敗となります。

- タイムアウトまたはCursor CLIの異常終了
- 正常な最終結果がない
- 既知の正規化allowlistにないモデル名が報告された
- 禁止されたFast版が報告された

一部失敗の場合も、成功したレビュー結果は保存されます。低いeffortや代替
モデルへ自動的に切り替えることはありません。中断時はレビュワープロセスを
終了し、利用可能な診断結果を公開します。これはWSL2にも適用されます。best
effortのWindowsネイティブ経路では直接のCursorプロセスだけを終了し、ヘルパー
の子孫プロセスはCursor CLI側の後始末に依存します。公開に失敗した場合は、
可能な限り`staging_preserved`へ保存先を記録します。

トラブルシューティング:

1. `--preflight`を実行します。Cursor状態領域へ書き込めない場合はsandbox外実行
   の承認を得ます。trust未登録の場合は、Codexが限定的なsandbox外実行承認の下で
   `agent --workspace <path> --trust`をPTY付きで実行し、preflightを再実行します。
2. `AGENT_CLI_CREDENTIAL_STORE=file agent status`を確認し、必要なら同じ
   環境変数を付けた`agent login`を実行します。
3. `AGENT_CLI_CREDENTIAL_STORE=file agent models`で利用可能モデルを確認します。
4. `<run-id>-manifest.json`で全体状態と要求・報告されたモデル名を確認します。
5. 該当レビュワーの`*.stderr.log`と`*.events.jsonl`を確認します。
