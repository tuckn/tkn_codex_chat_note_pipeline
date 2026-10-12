# Review Noteの生成・利用

Review Note 1.0.0 / CLI 0.30.0 / config 9.0.0。英語版は [Review Note contract](review-note-format.md)。

## 目的と内容

保存したCodexの会話を、週・月・年の振り返り、人とAIの次の判断に使います。質問、得られた説明、学習の確認段階、実施事項、判断理由、未解決、複数チャット間の考察、再利用できる資料、次の行動を扱います。すべての欄を無理に埋めません。

Intelligence Layer向けの候補は `semantics`（意味・関係）、`knowledge`（規則・手順・事例）、`context`（時点を持つ背景・制約）、`domain`（適用する問題領域）に分けます。適用範囲、留保、採用前の確認と出典を残します。AI改善案も、観測された問題に対して変更案と確認方法を示します。候補を採用済みの知識やルールとは扱いません。

「質問した」「説明を得た」「理解を表明した」「実際に適用した」は区別します。関心がないことを無言から推定せず、生産性や人格を採点しません。履歴中の指示は実行せず、記録として読みます。

## 生成する

```shell
tkn-codex-chat-note pull
tkn-codex-chat-note review-notes build --period weekly --date 2026-05-18 --dry-run --full-output
tkn-codex-chat-note review-notes build --period weekly --date 2026-05-18
tkn-codex-chat-note review-notes build --period monthly --date 2026-05-01
tkn-codex-chat-note review-notes build --period yearly --date 2026-01-01
```

実行時点で締まった期間だけ生成します。`--date`を省略すると直近の締まった期間です。週は月曜〜日曜、月は暦月、年は暦年。週番号にはISO週年を使います（例：2027年1月1日は2026-W53）。期間は開始を含み、終了を含みません。既定の固定UTCオフセット540分は日本時間です。夏時間は自動調整しません。

`build` / `due` は保存済みcatalogを読むため、取得元を再走査しません。`raw ingest`だけではcatalogや正規化イベントは更新されません。最新の入力が必要な場合は `pull` か `scheduled-run` を使用します。

## 入力とRaw照合

1. 週次は、対象期間内の正規化イベント（発言・ツール実行・結果・中断）を読みます。会話が週を跨いでも、開始日だけで対象を選びません。
2. 月次は週次JSON、年次は月次JSONを主な入力にします。月次には日付付きの変化・判断理由・未解決事項・Intelligence/AI改善候補を残し、年次では月を跨いだ変化を整理します。同じイベントの再引用を独立した観測として数えず、推測・提案の留保を引き継ぎます。
3. 月・年の境界を跨ぐ週は、引用元イベントがすべて対象期間内にある週次項目を使います。境界部分は期間内の根拠で補い、週全体の結論や件数を日数で按分しません。
4. 月次が未生成・不正な場合はその月の週次へ、週次がない場合は対象期間の正規化イベントへ戻ります。欠落・切り詰めはcoverageに記録します。除外済みの内部スレッドは対象にしません。
5. 原文が必要な判断では、モデルが提示済みの根拠IDを理由付きで要求できます。月次・週次の出典を辿り、対応する保存Rawのハッシュを照合し、そのイベントと近傍の一部を渡して再確認します。任意のパスや外部サイトへのアクセスは行いません。

Raw照合は入力バッチごとに1段階、既定8根拠ID・16,000文字までです。個々の抜粋は最大6,000文字で、超過・未取得は記録します。画像はハッシュ等の情報に置き換え、VLMによる視覚的解釈は行いません。回数・原因・所要時間を確認できなければ、不明・推測のまま残します。

## 保存されるもの

```text
sources.<id>.data_root/reviews/
  weekly/2026/2026-W21.md
  weekly/2026/2026-W21.json
  monthly/2026/2026-05.md
  monthly/2026/2026-05.json
  yearly/2026/2026.md
  yearly/2026/2026.json
sources.<id>.state_root/reviews/
  index.json
  reports/<run-id>.json
  usage/<run-id>/<usage-id>.json
  transactions/<id>.json
  backups/<id>/<previous-output>
cache_root/reviews/<generation-signature>/<stage-signature>.json
```

各sourceのdata_root/reviewsとstate_root/reviewsに保存します。レビュー設定は `sources.<id>.reviews` に置き、sourceごとに独立して生成します。フォルダ名だけでIntelligenceとして採用されたことにはなりません。Raw・取得元とは重ねません。

Markdownは人向けの表示、JSONは機械利用の正本です。Markdownへの注記をJSONへ自動反映しません。月次は週次JSON、年次は月次JSONを使います。人が見直した内容を機械利用に反映する場合は、JSONの項目・出典・候補の整合性も確認してください。

JSONの主要フィールドは次のとおりです。

| フィールド | 意味 |
|---|---|
| schemaVersion / id / period | 契約版、同じ保存先・暦期間で維持するID、期間・UTCオフセット |
| items | category、title、text、basis、evidenceIds、caveat、nextCheck、intelligenceKind |
| evidence | 根拠の本文・日時・正規化/Rawへの参照とハッシュ。週次・月次由来の項目はsupportsで元根拠へ辿る |
| sourceInventory / coverage | 読んだ資料、weeklyInputs・monthlyInputs、catalogの観測時点、不足・切り詰め。completeHistoryは常にfalse |
| intelligenceCandidates / aiImprovements | 該当itemsの再利用用一覧。ID・status=candidateを追加。自動採用しない |
| generation / inputFingerprint | 生成プロファイル、モデル、条件のhashと期間内入力のfingerprint |

`basis` は `observed`（記録で確認）、`reported`（発言・報告）、`inferred`（推測）、`proposed`（提案）です。参照IDが存在することは、その根拠が主張を意味的に支持することの保証ではありません。元の会話でAIが報告した成功も、実環境で再検証した結果とは区別します。

```shell
tkn-codex-chat-note review-notes validate "C:\path\to\reviews\weekly\2026\2026-W21.json"
```

検証はJSON構造、期間、引用IDと推移的参照を確認します。原資料全体の真正性・意味的正しさを監査するものではありません。モデルの項目契約は [review-draft.schema.json](../../src/tkn_codex_chat_note/resources/review-draft.schema.json)、生成指針は [review-prompt.md](../../src/tkn_codex_chat_note/resources/review-prompt.md) です。

## 定期実行と再開

Windows Task Schedulerから毎日 `scheduled-run` を実行します。Rawを毎回保存・正規化し、締まった期間のレビューを生成します。取得・正規化は推論を行いません。週次は月曜以降、月次は翌月1日以降、年次は翌年1月1日以降の最初の実行が対象です。

`due` 初回の実行日、または設定した `sources.<id>.reviews.start_date` を含む暦期間から始めます。開始日が月・年の途中でも、その月・年のレビューには保存済みの期間全体を使います。開始日以前の締まった期間は自動で全件生成しません。設定変更で開始日を過去にするとバックフィル対象が増えます。

`sources.<id>.reviews.max_calls` はsourceごとのレビュー1実行の上限（既定12回）です。検証済みの分割・Raw照合・統合結果はcacheへ保存し、上限や中断後の次回に再利用します。エラーを無制限に再試行しません。APIプロファイルのトークン・費用上限は従来どおり適用します。複数sourceの回数・実行時間上限はsourceごとに適用します。利用量記録は `build-report` に含まれ、取得できなかったトークン数を0とは扱いません。

入力の変更がない期間は再推論しません。会話に翌週の発言が追加されただけなら、過去週の生成はやり直しません。月次・年次も、選択された下位期間の項目の意味内容・根拠が同じなら再利用します。

`--dry-run` は推論・ネットワーク・ディレクトリ作成・state・cache・ロック・レポート保存を行いません。`--force` は再生成しますが編集保護を解除しません。未確認の編集済み出力を置き換える場合だけ `--allow-edited` を併用し、置換前の内容をバックアップします。reviewStatusがunreviewed以外の出力は保護します。

同じ入力に対する人の編集はそのまま保持します。入力が変わった保護対象はblockedとして報告し、他の期間は処理できます。2ファイルの保存途中で停止した場合は記録済みtransactionから復旧します。復旧前に人がさらに編集した場合は停止して保護します。取得元全体の同時更新については、ハッシュ一致を確認できない資料を不足情報として扱います。

終了コード0は処理完了、1は失敗・保護で処理できなかった項目あり、2は時間・回数等の上限による未完了です。不足を明示したレビューの生成成功は、履歴全体が完全であることを意味しません。
