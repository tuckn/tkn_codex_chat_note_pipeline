# tkn-codex-chat-note: Tkn Codex Chat Note Pipeline

ローカルのCodexチャットをRawとして保存し、**週次・月次・年次のReview Note**を生成するCLIです。人が読むMarkdownと、生成AIが再利用する出典付きJSONを保存します。

## 1. これは何か

```mermaid
flowchart LR
    CHAT[Codexログ] --> RAW[(Raw)]
    RAW --> EVENTS[(正規化イベント)]
    EVENTS --> WEEK[週次レビュー]
    WEEK --> MONTH[月次レビュー]
    MONTH --> YEAR[年次レビュー]
    RAW -. 必要な原文の照合 .-> WEEK
    RAW -. 必要な原文の照合 .-> MONTH
    RAW -. 必要な原文の照合 .-> YEAR
```

Raw取得・正規化は機械処理です。生成AIを使うのはレビュー作成です。月次は週次、年次は月次を主入力とし、不足する期間は下位レビューや正規化イベントで補います。各sourceを独立に処理し、source内の複数チャットを横断して振り返ります。

レビュー入力では会話を保持し、通常のツール本文を会話別の索引へ圧縮します。終了コードや診断文字列に問題の兆候がある出力は短い抜粋を残し、重要な結果・原因はRawで照合します。索引の件数はログ件数であり、独立した実行回数ではありません。正規化に失敗した会話は不足情報として明示します。

質問・説明・実施事項・判断理由・未解決事項に加え、Intelligence Layerへの候補と生成AIの挙動改善案を、出典・留保・次の確認事項とともに残します。推測や提案を事実として扱わず、Vault・Skill・AGENTS.mdへ自動採用しません。

### 1.1. 生成されるノートの例

以下は週次Review Noteの本文例です（説明用の架空データ）。

```markdown
保存資料を再利用する作業と、検証手順を見直す作業がつながった週だった。
既存情報を最初に確認できることが、両方の作業を支える可能性がある。[S001] [S002]

## テーマ別の実施・判断・残る点

| テーマ | 実施・結果・判断と理由 | 残る点・確認案 |
|---|---|---|
| 資料の再利用 | 既存ノートを見つけた後、再作成から追記へ方針を変更した。[S001] | 他の資料でも同じ索引が使えるかは未確認。 |

## 生成AIの挙動改善案

| 観察された問題 | 根拠と改善案 | 適用上の留保 | 確認方法 |
|---|---|---|---|
| 既存資料の見落とし | 配置を仮定して探索に失敗した。次回は資料の索引を開始時に渡す。[S001] | 反復する問題かは未確認。 | 次の作業で再探索が減るか確認する。 |
```

週次・月次・年次とも、最終統合でテーマ別に編集します。質問・活動は表、複数チャットの考察は文章、AI改善案は確認方法付きの表にします。詳細な抜粋はJSONに保持し、Markdownの出典は文書単位の参照にまとめます。単一バッチでも抽出後に最終統合を1回行い、複数バッチでは最後の統合をこの編集に使います。[Review Noteの仕様](docs/reference/review-note-format_ja.md)を参照してください。

保存前には自動校正を行います。抽出結果との照合で重要な話題の抜けを確認し、引用の重複だけで照合を省略せず、関連する抽出項目の原文も渡して後続訂正・完了報告を確認します。概要は本文の校正後に照合し、判断・完了状態を揃えます。無関係な話題の分離や、説明文の再利用目的・作業場所のルールの扱いも校正対象です。校正が完了しないノートは公開せず、上限到達時は次回に再開します。意味の正しさを保証する処理ではありません。

本文を膨らませないため、統合時は最大32項目・日本語7,000文字、校正後は最大36項目・8,400文字を検証します（タイトル・本文・留保・確認案の合計）。復元は重要事項6件まで、新しい話題はそのうち2件までです。各項目の文字数枠の合計も全体上限内に配分し、変更しない項目も検証します。照合した項目と理由はJSONに残します。週次・月次・年次とも、全作業の列挙ではなく主要テーマを選ぶレビューです。

校正は追加のモデル呼出しを伴います。空の期間は0回、それ以外は根拠校正が最低1回、完全一致しない抽出項目があれば欠落照合も1回以上、概要があれば本文との照合が概要1件につき1回増えます。実際の回数は本文と引用の量で増えます。共通の根拠を持つ項目は同じ校正リクエストへまとめ、原文を削らず重複送信を減らします。送信時のみ根拠IDを短縮し、検証・保存前に元のIDへ戻します。確認済みcacheも再利用しますが、呼出しごとのモデル側の入力や推論・出力トークンも増えるため、最初の数回は利用量レポートで確認してください。

Frontmatterは `type: chatReview` とし、概要、期間、取得元 `sourceApplication: Codex`、正本JSONへの参照、生成設定をまとめます。前週・前月・前年の参照状況は `previousReviewStatus`・`previousReviewReferenced`、モデルに渡した候補数と最終引用数は `previousReviewSelectedItemCount`・`previousReviewCitedItemCount` で確認できます。
末尾は `created`・`updated`・`noteId` です。日英のテンプレート・プロンプト・schemaは `profiles/default-jp`・`default-en` に同梱します。
表示テンプレートだけの変更は、次回実行時に保存済みJSONからモデル呼出しなしで反映します。
編集済み・確認済みのノートは保持します。[Frontmatterと日英プロファイル](docs/reference/review-note-format_ja.md#frontmatterと日英プロファイル)を参照してください。

出典はチャットの表示名を使い、本文の参照から、その項目を支える発言・日時・短い抜粋へ戻れます。
詳細は出典節に折りたたんで表示します。管理用イベントは引用対象から除外します。
「質問・学び」には再利用できる説明の要点を残し、収録範囲の制約は圧縮・切り詰め・取得上限・失敗などの種類別に表示します。

### 1.2. 対象範囲

| 項目 | 内容 |
|---|---|
| 取得元 | ローカルのCodexログ（保存済みアーカイブを含む） |
| 出力 | Raw、正規化イベント、catalog・provenance、週次・月次・年次Markdown＋JSON |
| 生成AI | Codex、Claude Code、GitHub Copilot CLI、Antigravity、Ollama、Azure OpenAI |
| source | PC・環境等の取得元。source間のレビューは統合しない |
| 対象外 | 会話ごとのSession Note生成、クラウドだけにある履歴の取得、候補の自動採用 |

## 2. セットアップ

Python 3.11以上とuvを使用します。生成プロバイダーは別途インストール・認証し、共有Bridge設定に実行プロファイルを用意してください。

```shell
cd <repository>
uv tool install .
tkn-codex-chat-note --help
tkn-codex-chat-note config init
```

ソースやリソースの更新後は、検証済みの依存関係を固定してインストール済みCLIへ反映します。

### 2.1. 設定

設定先は `~/.tkn/codex_chat_note_pipeline/config.yaml`。優先順位は既定値 → ユーザー設定 → 作業ディレクトリの `.tkn/config.yaml` → `--config` → CLIオプションです。`config init` は編集済みファイルを保護し、`--force` を明示した場合だけバックアップして置換します。

```yaml
schema_version: "9.0.0"
sources:
  my-pc:
    enabled: true
    source_root: ~/.codex
    include_archived: true
    raw_root: C:/path/to/codex/raw
    data_root: C:/path/to/codex/data
    state_root: C:/path/to/codex/state
    reviews:
      language: ja
      utc_offset_minutes: 540
      max_calls: 12
      input_characters: 50000
      raw_characters: 16000
      raw_requests: 8
generation:
  active_profile: codex
  profiles:
    codex:
      bridge_profile: codex-default
runtime_minutes: 230
model_timeout_seconds: 1800
```

`reviews` は各sourceの設定です。言語は `ja` / `en`、時差540分は日本時間です。レビューは `data_root/reviews`、実行状態は `state_root/reviews` に保存します。ルートを省略すると `~/.tkn/codex_chat_note_pipeline/<kind>/codex/<source_id>` を使用します。source IDと使用中のRaw/data/stateの保存先は維持してください。

生成プロファイルは取得元とは独立です。接続・モデル・認証は通常 `~/.tkn/genai_bridge/config.yaml` の参照先に置き、`--profile <name>` で選択します。[設定例](src/tkn_codex_chat_note/resources/config.example.yaml)にレビュー上限・利用量レポートの項目も掲載しています。

```shell
tkn-codex-chat-note config list
tkn-codex-chat-note config list --json
```

## 3. 実行する

共通オプション（`--source`、`--config`、`--profile` など）はコマンドの後ろに指定できます。従来の前置き形式も使用できます。同じオプションを複数回指定した場合は後の値が優先されます。`--profile` と `--provider`、`--quiet` と `--verbose` は、指定位置にかかわらず併用できません。

### 3.1. 定期実行

```shell
tkn-codex-chat-note scheduled-run --dry-run
tkn-codex-chat-note scheduled-run
```

毎回Rawを保存・正規化し、締まった期間のレビューを生成します。初回に管理領域を初期化するので、別途 `clone` は不要です。週次は翌月曜、月次は翌月1日、年次は翌年1月1日以降の実行で生成します。未実行・中断した分は次回に再開します。

保存済みの全履歴を起点に、終了済み期間のレビューを確認し、不足分を生成します。初回実行日による制限はありません。日次・週次・月次・年次に別々のタスクを登録する必要はありません。

| Task Schedulerの項目 | 設定例 |
|---|---|
| プログラム | インストール済み `tkn-codex-chat-note.exe` の絶対パス |
| 引数 | `scheduled-run --config "C:/path/to/config.yaml"` |
| 開始場所 | 設定で意図した作業ディレクトリ |
| トリガー | 毎日1回（例：03:00） |
| 実行ユーザー | 生成AIの認証を設定したユーザー |
| その他 | 未実行分をすぐ実行、多重実行しない |

[登録スクリプト例](scripts/register-review-task.ps1)は `-WhatIf` で確認できます。CLIのインストールはタスクを登録しません。実行時刻はWindows側の時計です。共有BridgeのCLI認証を使う場合は「ログオンしているときのみ実行」から確認してください。

### 3.2. 保存・正規化だけを実行する

```shell
tkn-codex-chat-note clone --dry-run
tkn-codex-chat-note clone
tkn-codex-chat-note pull
tkn-codex-chat-note raw ingest
```

`clone` は初期化・保存・正規化、`pull` は初期化済み領域の更新です。どちらも生成AIを呼びません。`raw ingest` はRaw保存だけでcatalogを更新しません。通常は `scheduled-run`、保存だけなら `clone` / `pull` を使用します。

### 3.3. レビューを個別に作る

```shell
tkn-codex-chat-note review-notes build --source my-pc --period weekly --dry-run
tkn-codex-chat-note review-notes build --source my-pc --period weekly
tkn-codex-chat-note review-notes build --source my-pc --period monthly
tkn-codex-chat-note review-notes build --source my-pc --period yearly
tkn-codex-chat-note review-notes due
# 設定に期間を指定せず、保存済みの全履歴から終了済み期間を生成
tkn-codex-chat-note review-notes due --source my-pc
```

`due` はsourceごとの保存済みcanonical eventsの最古日時を、設定されたタイムゾーンで開始日に変換します。既存設定の `start_date` と保存済みの初回実行日は対象範囲を制限しません。実行日（または `--as-of`）までの終了済み週・月・年が対象です。進行中の期間は対象外です。catalogがなければ先に `pull` が必要です。レビューに使える日付付きの記録がなければ対象は0件です。記録のない週・月・年は `skipped-empty` としてスキップし、空のノートは作成しません。既存の空ノートは保持します。`--dry-run` で書き込み・推論なしに確認でき、上限で中断した場合は同じコマンドの再実行で続行できます。

直前の同種ノート（週次なら前週、月次なら前月、年次なら前年）がある場合、今回の内容と関連する最大6項目を比較資料として参照します。今回の出典を必須とし、過去の未解決事項は自動で持ち越しません。`sources.<id>.reviews.previous_period: false` で無効化できます。既存の統合・校正に資料を加える方式で、専用のモデル呼出しはありませんが、入力量や校正の分割・修復回数は増える場合があります。

省略時は直近の締まった期間です。`--date YYYY-MM-DD` で対象期間内の日付を指定できます。未確定期間は生成しません。`--source` を省略すると有効なsourceをそれぞれ処理します。手動の `review-notes` は保存済みcatalogを使用するため、必要なら先に `pull` してください。

### 3.4. 確認する

```shell
tkn-codex-chat-note status
tkn-codex-chat-note provenance validate
tkn-codex-chat-note review-notes validate "C:/path/to/data/reviews/weekly/2026/2026-W21.json"
tkn-codex-chat-note build-report --no-open
```

`build-report` は保存済み利用量からHTMLとJSONを作ります。生成AIや価格検索は行いません。通常はHTMLを開き、`--no-open` は起動を抑制します。過去の利用量と各sourceのレビュー利用量を読み、取得できないトークン数・費用は不明として扱います。

## 4. 仕様

- `--dry-run` は推論・ネットワーク・出力・cache・state・ロックの作成を行いません。レビューの予定は保存済みcatalogに基づきます。
- レビューの `max_calls` と `runtime_minutes` はsourceごとの1回の実行に適用されます。複数sourceではその分の上限が加算されます。途中結果はcacheから再利用します。
- 編集・レビュー済みのReview Noteを保護します。`--force` は再生成、`--allow-edited` は未確認の手編集のみバックアップ後に置換します。確認済みノートの保護は解除しません。
- 成功終了は0、失敗・保護で処理できない項目ありは1、レビューの時間・回数上限による未完了は2です。不足情報付きの成功は履歴全体の完全性を保証しません。
- レビュー実行中はsource・対象期間の準備、モデル呼び出しの開始・応答受信と経過秒、cache再利用、期間ごとの結果を逐次表示します。回数上限と時間上限を区別し、上限停止時は未完了・残り期間数・再実行による再開をWARNINGで示します。残り期間数は資料があり生成・更新が必要な期間だけを数え、途中停止した期間を含みます。上限停止後も推論なしで残りを調べ、空の期間と変更のない生成済み期間は除外します。`--dry-run` の `pendingPeriods` は生成・更新の予定件数です。資料の読み取り失敗・破損・正規化失敗は記録なしと区別し、エラーとして通知します。日時のない会話記録は `undatedEventCount` と `historyIssues` で別途報告し、日時が分かる資料の処理は続けます。同じ日時不足のERRORはsourceごとに検出時の1回だけ表示し、終了時に汎用エラーとして重複表示しません。元ログの発言にも日時がなければ、再実行だけでは解消しません。旧形式のチャット開始日時は発言日時として自動補完しません。日時不明の記録が残る間は全完了とせず終了コード1にします。週・月・年を特定できないため、日時不明の記録を `pendingPeriods` には加算しません。`--quiet` でエラー以外の表示を抑制できます。
- 全コマンドの最終結果はstdoutへ既定で `key=value`、`--json` 指定時はJSONで出力します。進捗・警告・診断はstderrです。`--full-output` は形式を変えず、表示する詳細を増やします。
- Session Noteの生成・検証、専用言語プロファイル、`pull --limit`、`--idle-minutes` は廃止しました。旧生成物を読み込まず、削除もしません。

### 4.1. CLIの結果表示

処理・検証・設定操作を含む全コマンドが最終結果を標準出力へ返します。通常は1行1項目の `key=value` 一覧です。入れ子は `.`、配列は `[0]` などの添字、空の配列・マッピングは `[]`・`{}`、真偽値は `true`・`false`、未設定値は `null` で表示します。文字列にJSONの引用符を付けず、Windowsパスの `\` を重ねたり、長いパスを省略したりしません。改行などの制御文字はエスケープします。

```shell
tkn-codex-chat-note clone --dry-run
tkn-codex-chat-note review-notes due --dry-run --full-output
```

結果の `status`・`exitCode`、件数、`failed`・`historyIssues`、要確認の `threads`・`results` と理由、保存した `reportPath` / `reportPaths` を確認してください。変更なし・除外・空期間などの詳細は件数にまとめる場合があります。`omittedDetailsCount` と `detailsHint` があるときは、保存済みレポート、または同じコマンドの `--full-output` で詳細を確認できます。書き込みコマンドの再実行は再度処理するため、保存済みレポートがあればそちらを参照してください。

機械処理で読む場合は、必ず `--json` を付けます。通常の一覧を文字列解析しないでください。

```shell
tkn-codex-chat-note status --json
tkn-codex-chat-note review-notes due --dry-run --json --full-output
```

`--json` と `--full-output` はコマンドの前・途中・後ろに指定できます。`--json` は1回の結果を1つのJSON documentで出し、引数・設定・実行時エラーや中断も可能な限り同じ形式にします。対象パスは `targets`、`sourcePaths`、`jsonPath`・`notePath` などの独立した項目で確認できます。ログ・見出し・カラーはJSONに混ぜません。

`--quiet` / `--verbose` はstderrのログ量だけを変え、最終結果は抑制しません。TTY・パイプ・リダイレクトでも形式は自動変更しません。終了コードは0が成功、1が処理・設定・検証失敗、2が引数エラーまたは上限等による未完了、130が中断です。`--help` と `--version` は通常の案内を表示します。

表示オプションは成果物の種類や生成有無を変えません。CLIの表示形式と、Python APIの戻り値、保存するJSON・manifest・ノート・レポートの形式は別の契約です。

[CLI出力契約](docs/reference/cli-output.md)に項目とコマンドごとの詳細を記載しています。

### 4.2. config 8から9への更新

トップレベルの `reviews` を各 `sources.<id>.reviews` へ移し、`reviews.data_root`・`reviews.state_root`・`reviews.session_note_limit` を削除します。レビューは各sourceのdata/state配下になります。`generation.session_note_profile`・`idle_minutes`・`installed_at` も削除し、`schema_version` を `9.0.0` にします。

旧設定は自動上書きせず、更新方法を表示します。以前のsource横断レビューは新しいsource別レビューへ自動移行しません。旧レビュー・Session Noteは既存の場所に残ります。storage 5のRaw・正規化イベントはそのまま利用できます。保存先自体を移す場合は `storage migrate --from-config <old-config> --dry-run` で計画を確認してください。

- [Review Note仕様（日本語）](docs/reference/review-note-format_ja.md)
- [Review Note contract（English）](docs/reference/review-note-format.md)
- [Data contract](docs/reference/data-contract.md)
- [変更履歴](CHANGELOG.md)

## 5. 開発・検証

```shell
uv sync --group dev
uv run python -m pytest
uv run python -m ruff check .
uv run python -m mypy src/tkn_codex_chat_note
uv build
```

テストは一時ディレクトリと合成ログを使用します。実データや実モデルでの生成品質評価は別に行ってください。
