{%- set tables = [
    (questions, "質問・学び", ["テーマ", "質問・説明・確認できたこと", "残る点・確認案"], false),
    (activities, "テーマ別の実施・判断・残る点", ["テーマ", "実施・結果・判断と理由", "残る点・確認案"], false),
    (ai, "生成AIの挙動改善案", ["観察された問題", "根拠と改善案", "適用上の留保", "確認方法"], false),
    (candidates, "Intelligence Layerへの候補", ["候補", "再利用する内容・適用範囲", "留保", "採用前の確認"], true),
    (resources, "再利用できる資料・次に試すこと", ["対象", "資料・提案する実験", "留保・確認案"], false)
] -%}
{%- macro table(rows, heading, headers, candidate=false) -%}
{% if rows -%}
## {{ heading }}

| {{ headers|join(' | ') }} |
| {{ ('---|' * (headers|length)).rstrip('|')|replace('|', ' | ') }} |
{%- for item in rows %}
| {{ (item.title ~ (' (' ~ item.intelligenceKind ~ ')' if candidate else ''))|cell }} | {{ item.text|cell }} {{ item.refs }}{% if item.uncitableEvidenceIds %} (引用対象外の管理用イベントを除外。主張の根拠は要確認。){% endif %} | {% if headers|length == 4 %}{{ item.caveat|cell or '—' }} | {{ item.nextCheck|cell or '—' }}{% else %}{{ (item.caveat ~ ((' ' if item.caveat else '') ~ "確認案：" ~ item.nextCheck if item.nextCheck else ''))|cell or '—' }}{% endif %} |
{%- endfor %}
{% endif %}
{%- endmacro -%}
{%- macro overview_block(item) -%}
{{ item.text }} {{ item.refs }}
{%- endmacro -%}
{%- macro insight_block(item) -%}
### {{ item.title }}

{{ item.text }} {{ item.refs }}{% if item.uncitableEvidenceIds %} (引用対象外の管理用イベントを除外。主張の根拠は要確認。){% endif %}

{{ item.caveat }}

{{ item.nextCheck }}
{%- endmacro -%}
{%- macro coverage_block() -%}
## 対象範囲と出典

### 読み取り上の注意

保存済みの記録に基づくレビューです。生活・活動全体の網羅性を示すものではありません。当時の未解決事項を現在のタスクとはみなしません。Intelligence候補と改善案は採用前の内容です。
{% for item in overview %}
{% if item.uncitableEvidenceIds %}引用対象外の管理用イベントを除外。概要の主張の根拠は要確認。
{% endif %}
{% if item.caveat %}{{ item.caveat }}
{% endif %}
{% if item.nextCheck %}確認案：{{ item.nextCheck }}
{% endif %}
{% endfor %}
### 収録範囲

入力の根拠単位：{{ coverage.inputCount }}件、週次：{{ coverage.weeklyInputs|length }}件、月次：{{ coverage.get('monthlyInputs', [])|length }}件。対象範囲：{{ coverage.status }}。
{% if comparison %}
前期間の比較：{{ comparison.period.key }} · {{ presentation.comparisonStatus.get(comparison.status, comparison.status) }}。関連候補{{ comparison.selectedCount }}件。比較資料は今回の出来事・収録数に含めません。
{% endif %}
{% if coverage.gaps %}
収録範囲の制約は{{ coverage.gaps|length }}件の記録です。会話の欠落数ではありません。

| 制約の種類 | 記録数 | 読み取りへの影響 |
| --- | --- | --- |
{% for group in coverage_groups -%}
| {{ presentation.coverageKinds[group.kind][0] }} | {{ group.count }} | {{ presentation.coverageKinds[group.kind][1] }} |
{% endfor %}
{% endif %}
{% for gap in important_gaps -%}
- {{ gap|cell }}
{% endfor %}
出典番号は文書単位です。各項目に対応する正確な根拠ID・抜粋・日時・事実／推測／提案の区別はJSONに保持しています。考察は推測、改善案は提案として読んでください。

[構造化データ・全出典・不足情報](<{{ json_path }}>)
{%- endmacro -%}
{%- macro source_block(source) -%}
### {{ source.label }}

{{ source.name|cell }} · {{ source.span }}{% if source.historical %} · 前期間の比較資料{% endif %}{% if source.path %}

[出典文書](<{{ source.path }}>)
{% endif %}
<details>
<summary>根拠発言を確認 ({{ source.records|length }})</summary>

{% for citation in source.citations -%}
<a id="{{ citation.anchor }}"></a> **引用 {{ citation.number }}**: {% for record in citation.records %}[{{ record.label }}](#{{ record.anchor }}){% if not loop.last %}, {% endif %}{% endfor %}

{% endfor -%}
| 根拠ID | 日時 | 発言者 | 短い抜粋 | 原文位置 |
| --- | --- | --- | --- | --- |
{% for record in source.records -%}
| <a id="{{ record.anchor }}"></a>{{ record.label }} · {{ record.id }} | {{ record.times|join(' / ')|cell }} | {{ presentation.actors.get(record.actor, record.actor)|cell }} | {{ record.quote|cell }} | {{ record.locations|cell or '—' }} |
{% endfor %}
抜粋は最大160文字。根拠IDに対応する全内容・参照先はJSONに保持しています。

</details>
{%- endmacro -%}
{%- set ns = namespace(blocks=[frontmatter, '# ' ~ title, period.start ~ ' — ' ~ period_end ~ ' (UTC offset ' ~ offset ~ ' minutes)']) -%}
{%- if overview or not items -%}{%- do ns.blocks.append('## 概要') -%}{%- endif -%}
{%- for item in overview -%}{%- do ns.blocks.append(overview_block(item)) -%}{%- endfor -%}
{%- for rows, heading, headers, candidate in tables -%}
{%- if rows -%}{%- do ns.blocks.append(table(rows, heading, headers, candidate)|trim) -%}{%- endif -%}
{%- if loop.index == 2 and insights -%}
{%- do ns.blocks.append('## 考察・仮説') -%}
{%- for item in insights -%}{%- do ns.blocks.append(insight_block(item)) -%}{%- endfor -%}
{%- endif -%}
{%- endfor -%}
{%- if not items -%}{%- do ns.blocks.append("この期間について、保存済みの入力からレビューに使える根拠を確認できませんでした。") -%}{%- endif -%}
{%- do ns.blocks.append(coverage_block()) -%}
{%- for source in sources -%}{%- do ns.blocks.append(source_block(source)|trim) -%}{%- endfor -%}
{{ ns.blocks|join('\n\n') }}
