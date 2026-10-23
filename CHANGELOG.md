# Changelog

このプロジェクトの主な変更を、新しいバージョンから順に記録します。

既存履歴は Git のコミットと `pyproject.toml` のバージョン変更から整理しました。
日付はバージョン更新コミットの日付であり、配布日を保証するものではありません。
現在のパッケージ名になった 0.15.0 以降を対象とし、それ以前の前身プロジェクトの履歴は Git を参照してください。
Git で独立したバージョン更新を確認できない 0.19.x、0.26.x、0.27.x などは個別に掲載していません。

## 0.41.0 - 2026-10-24

- Add `review-notes due --all-history` to discover the first saved source event and build closed weekly, monthly and yearly periods without configuring dates.

## 0.40.2 - 2026-10-16

- Identify Codex chats in Japanese/English review titles for every period.
- Add an Overview heading and collect general notices plus overview qualifications/checks under Coverage and sources, separating them from the opening summary without inference.

## 0.40.1 - 2026-10-16

- Add flat Frontmatter v3.1 properties for previous-period availability, period/source, model-input reference status, selected-item count and distinct cited-item count.
- Derive metadata from saved comparison snapshots; keep legacy unknown states explicit and refresh existing Markdown without inference or content/date changes.

## 0.40.0 - 2026-10-15

- Compare current findings with up to six related items from the immediately preceding weekly/monthly/yearly JSON, enabled by reviews.previous_period.
- Keep historical snapshots and citations separate from current-period evidence; require current evidence for every item and preserve historical proposals, uncertainty and dates.
- Select context without inference and add it only to existing composition/audit stages; reuse extraction caches when the previous review changes.
- Avoid recursive historical carryover and re-aggregate current dated supports for lower-period rows containing comparisons. Retain the existing Markdown sections and prose limits.

## 0.39.0 - 2026-10-15

- Prefer actual chat names from the local session index, with meaningful request-text fallbacks; exclude standalone app-page metadata from normalization, review inputs and bounded Raw neighbors.
- Link each item to its exact evidence subset and expose bounded, deduplicated source excerpts, speakers, original timestamps and event positions in collapsed source details.
- Retain supported reusable explanation takeaways in question/learning text through extraction, synthesis and audits without adding a model stage or changing main-body layout.
- Group coverage limitations by kind and interpretation instead of conflating compaction, truncation, retrieval limits and normalization failures; retain all original diagnostics.

## 0.38.0 - 2026-10-15

- Classify Markdown as chatReview, identify sourceApplication: Codex independently of generation provider, and place created/updated/noteId last in Frontmatter v3.
- Record note creation/content-update dates for newly generated JSON; retain unknown legacy creation dates and preserve lifecycle dates for unchanged content or presentation-only changes.
- Package default-jp/default-en YAML and Markdown templates, schema, generation/audit/repair prompts and request framing; keep source/citation transformation in Python.
- Preserve existing inference request contents during relocation; track external request-template changes separately from presentation refreshes.

## 0.37.0 - 2026-10-14

- Add grouped, flat review Frontmatter v2 with description, JSON URI, stable noteId and stored generation provenance; preserve date strings and distinguish requested model from confirmed telemetry.
- Package a shared Frontmatter template and refresh presentation from saved JSON without inference, retaining edited/reviewed notes and backing up previous output.
- Document review resources versus the retired Session Note profiles; keep JSON v1 and existing review prose unchanged.

## 0.36.0 - 2026-10-14

- Pack source audits by shared evidence while preserving every selected excerpt, per-item citation scope, and final note order.
- Use lossless request-local evidence aliases and restore canonical IDs before validation, caching, and storage; include mappings in cache identity.
- Record repeated source transmission, retain correction/completion and overview checks for all review periods, and reuse unchanged extraction/coverage caches.

## 0.35.0 - 2026-10-14

- Reconcile distinct extracted findings even when citations overlap, and carry the sources of mapped findings into each theme's audit so later decisions and completion reports remain available.
- Check the overview after the body, using the source-audited statements and their citations, without rewriting the body.
- Allocate row budgets within one review-wide character ceiling and validate unchanged rows against their allocations too; keep bounded repairs and publication protection.
- Apply the same checks to weekly, monthly and yearly reviews, recording allocations and overview decisions in the audit trail.

## 0.34.0 - 2026-10-14

- Bound weekly/monthly/yearly composition and source corrections by item and prose limits.
- Limit restoration to six consequential findings and two new topics per review; keep considered
  findings and omission reasons in JSON instead of expanding the readable note into a full inventory.
- Reject overlong replacements, repair bounded responses once, and preserve existing notes on failure.
  Include measured counts in length errors and resend only affected rows and sources for their repair.
- Include turn-ending and terminal replies plus the next user message in audit context to avoid
  mistaking progress-only excerpts for absent completion or correction reports.

## 0.33.1 - 2026-10-14

- Label the insights section neutrally so source-backed hypotheses from a single conversation
  are not presented as conclusions supported by multiple conversations.

## 0.33.0 - 2026-10-13

- Automatically reconcile missing findings and verify composed weekly/monthly/yearly items against
  bounded source excerpts before publication, including request/completion and temporal scope.
- Split unrelated topics and retain material reuse purposes; validate audit coverage and per-item
  citation permissions, and retain the quality decisions in JSON.
- Share repeated sources within audit batches, checkpoint completed audits, and resume under existing
  budgets. Failed audits preserve existing notes; added model calls appear in usage records.

## 0.32.0 - 2026-10-13

- Edit weekly, monthly and yearly findings in a dedicated final synthesis pass; connect themes,
  decisions, outcomes and remaining questions instead of repeating classified event lists.
- Render thematic tables, narrative cross-chat insights and verifiable AI improvement proposals.
  Group source links by document and retain full excerpts and exact claim citations in JSON.
- Reuse extraction checkpoints when editorial policy changes; budget and cache the final pass,
  including single-batch reviews. Empty periods still require no model calls.

## [Unreleased]

## [0.31.0] - 2026-10-13

- 出力検証エラー時は応答だけを修復し、呼出し上限を超えた場合も元資料を再送せず再開できるようにする。

- 週次レビューの通常ツール本文を出典付きの索引へ圧縮し、診断抜粋と会話本文を優先。原文は保存したまま必要時に照合する。
- 会話ごとのまとまりを保って分割し、正規化失敗を対象範囲の不足として明示する。
- Raw照合でUnicode区切り文字を物理行の改行と誤認しないよう修正。

## [0.30.0] - 2026-10-12

### 変更

- Session Note生成・検証コマンド、専用プロファイルとテンプレートを削除。週次はRawから機械的に正規化したイベントを直接使用する。既存の生成済みファイルは削除しない。
- config 9.0：レビュー設定を `sources.<id>.reviews` へ統合。出力と実行状態は各sourceのdata/state配下のreviewsへ保存する。sourceごとに独立した週次→月次→年次を生成。
- `clone` / `pull` は取得・正規化のみ。`scheduled-run` は取得・正規化後に期限到来レビューを生成。Session Noteの生成上限・idle設定・言語プロファイルを廃止。
- 旧8.x設定は明示更新が必要。旧source横断レビューは自動移行しない。Raw・正規化イベントのstorage 5は継続利用できる。

## [0.29.1] - 2026-10-12

### 変更

- 年次レビューの主入力を週次から月次JSONへ変更。未生成・不正な月次はその月の週次へ戻り、不足する元資料を補完する。
- 月次・週次からRawまでの出典を保持し、年次でも限定的な原文照合を可能にする。入力一覧に `monthlyInputs` を追加。
- 月次の判断理由・未解決事項・候補・留保を年次へ引き継ぐ生成指針と、日英ドキュメントを更新。

## [0.29.0] - 2026-10-12

### 追加

- 週次・月次・年次の `review-notes build` と、未処理期間に追いつく `review-notes due`。月次と年次はそれぞれ週次を主な入力にし、期間境界や不足情報は元資料で補完。
- 人向けMarkdownと、出典・留保・Intelligence候補・生成AIの改善案を持つJSON。限定的なRaw照合、入力ごとの再利用、途中結果の再開、編集済み・確認済み出力の保護。
- 日次のRaw保全と期限到来時の生成を行う `scheduled-run`。Windows Task Schedulerの登録例（`-WhatIf`対応、登録は利用者が実行）。
- config 8.4の `reviews` 設定。既存8.x設定は読み込み時に互換変換し、設定ファイルは書き換えない。レビューの利用量記録も `build-report` の対象に追加。
- `CHANGELOG.md` を追加。

### 変更

- `config show` を `config list` に変更。既定はコピー可能な `key=value` 一覧とし、構造化出力には `--json` を追加。旧コマンドは削除したため、スクリプトや手順を更新し、JSON を使う処理は `config list --json` に変更してください。インストール済みCLIには `uv tool install . --reinstall` で反映します。
- ドキュメントのコマンド例のコードブロックを `console` から `shell` に変更（`782b4d7`）。

## [0.28.2] - 2026-09-27

### 変更

- `tkn-genai-bridge` を 0.7.0 から 0.10.0 に更新し、公開コミット `9cf534e` の ZIP URL に固定。
- 本CLIの入力は引き続きテキストのみ。Bridgeの画像入力機能は使用しません。

## [0.28.1] - 2026-09-25

### 変更

- `tkn-genai-bridge` の依存先を Git URL からコミット固定の ZIP URL に変更（`55d55d7`）。

## [0.28.0] - 2026-09-25

### 変更

- 推論処理を `tkn-genai-bridge` に統合し、共有 Bridge プロファイルを参照する設定へ移行。
- 利用量・費用の集計を Bridge の形式に対応させ、キャッシュ利用量や部分的に取得できた利用量を扱う処理を追加。
- README を日本語の `README.md` に集約し、セットアップ・実行・参照情報を再構成。

根拠: `dfd2f92`。

## [0.25.0] - 2026-09-20

### 追加

- 利用状況レポートに診断情報と根拠の追跡を追加。
- HTTP ステータス、エラー、根拠の保存先、検証失敗・通信再試行の診断をレポートと CSV 出力に追加（`f06407e`）。

## [0.24.0] - 2026-09-19

### 追加

- プロバイダーに依存しない利用量記録と、保存済みデータからの利用状況レポート生成を追加。
- 推論の処理段階ごとの利用量追跡と、レポート設定を追加（`da3a64d`）。

## [0.23.1] - 2026-09-17

### 変更

- Session Note 生成時の進捗表示を改善（`feec6c0`）。

## [0.23.0] - 2026-09-17

### 追加

- 途中結果の未解決項目を扱う `pendingStateItems` のスキーマ、構造・引用の検証、最終状態との整合処理を追加。

### 変更

- `pendingStateItems` を公開用ノートのデータから除外し、生成プロンプトと検証処理を更新（`2c49553`）。

## [0.22.1] - 2026-09-16

### 変更

- 入力検証と再生成に関するドキュメント・ログを改善（`e62b436`）。

## [0.22.0] - 2026-09-15

### 変更

- 推論入力の重複排除・圧縮を改善し、HTML や大量の抽出テキストの処理を拡充。
- 圧縮・重複排除した文字数の計測を追加し、日本語・英語プロンプトを更新（`21ac544`）。

## [0.21.2] - 2026-09-15

### 変更

- 出力動作とメタデータの扱いに関するドキュメントを更新（`51c7489`）。
- SDK の通信詳細をコンソールに出さず、警告・エラーを残すようログ表示を改善（`6b634ed`）。

## [0.21.1] - 2026-09-15

### 変更

- 予算の扱いに関するドキュメントを更新（`8006967`）。

## [0.21.0] - 2026-09-14

### 変更

- 生成設定を名前付きプロファイルで選択する構成に変更し、設定スキーマを 8.0.0 に更新。
- 生成時のプロファイル・プロバイダー情報を記録し、設定移行を対応させた（`0351f76`）。

## [0.20.0] - 2026-09-14

### 追加

- 分割生成で残る未解決項目と最終状態の整合処理を追加。
- 生成前のトークン・費用見積もりと Azure 認証関連の処理を拡充（`d7b5f33`）。

## [0.18.0] - 2026-09-14

### 追加

- Azure OpenAI・Ollama の API 推論、利用量追跡、API 上限・接続設定の検証を追加。
- ハッシュで固定した Canonical Events を用いて Session Note を評価するスクリプトを追加（`59d9977`）。

## [0.17.0] - 2026-09-14

### 追加

- 生成途中の検証済み結果を再利用するキャッシュと、再利用件数の表示を追加。

### 変更

- 推論向け入力の圧縮・重複排除を導入し、生成効率を改善（`7e93a0f`）。

## [0.16.1] - 2026-09-13

### 変更

- ノート内の画像の扱いを改善（`6d16445`）。

## [0.16.0] - 2026-09-13

### 追加

- 同一スレッドの異なる履歴を、単一の履歴に決めずに保全する機能を追加。
- 履歴分岐と取得元集合の SHA-256 を Session Note の来歴に追加。

### 変更

- 不正な JSONL レコードの診断と旧形式ログの読み取りを改善（`564febb`）。

## [0.15.0] - 2026-09-13

### 変更

- パッケージを `tkn-genai-chat-note-pipeline` から `tkn-codex-chat-note-pipeline` に改名。
- モジュール・設定・テストを `genai_chat_note` から `codex_chat_note` の名称に移行（`0322a68`）。
