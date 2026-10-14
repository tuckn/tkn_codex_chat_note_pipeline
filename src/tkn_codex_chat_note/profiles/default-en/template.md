{%- set tables = [
    (questions, "Questions and learning", ["Theme", "Question / demonstrated state", "Limits / check"], false),
    (activities, "Activities, decisions and open questions", ["Theme", "Outcome / decision and reason", "Remaining limits / check"], false),
    (ai, "Proposed AI behavior improvements", ["Observed problem", "Evidence and proposed change", "Limits", "How to verify"], false),
    (candidates, "Intelligence Layer candidates", ["Candidate", "Reusable content and scope", "Limits", "Before adoption"], true),
    (resources, "Resources and next experiments", ["Subject", "Resource / proposed experiment", "Limits / check"], false)
] -%}
{%- macro table(rows, heading, headers, candidate=false) -%}
{% if rows -%}
## {{ heading }}

| {{ headers|join(' | ') }} |
| {{ ('---|' * (headers|length)).rstrip('|')|replace('|', ' | ') }} |
{%- for item in rows %}
| {{ (item.title ~ (' (' ~ item.intelligenceKind ~ ')' if candidate else ''))|cell }} | {{ item.text|cell }} {{ item.refs }} | {% if headers|length == 4 %}{{ item.caveat|cell or '—' }} | {{ item.nextCheck|cell or '—' }}{% else %}{{ (item.caveat ~ ((' ' if item.caveat else '') ~ "Check: " ~ item.nextCheck if item.nextCheck else ''))|cell or '—' }}{% endif %} |
{%- endfor %}
{% endif %}
{%- endmacro -%}
{%- macro overview_block(item) -%}
{{ item.text }} {{ item.refs }}{% if item.caveat %}

{{ item.caveat }}{% endif %}{% if item.nextCheck %}

Check: {{ item.nextCheck }}{% endif %}
{%- endmacro -%}
{%- macro insight_block(item) -%}
### {{ item.title }}

{{ item.text }} {{ item.refs }}

{{ item.caveat }}

{{ item.nextCheck }}
{%- endmacro -%}
{%- macro coverage_block() -%}
## Coverage and sources

Input evidence units: {{ coverage.inputCount }}; weekly reviews: {{ coverage.weeklyInputs|length }}; monthly reviews: {{ coverage.get('monthlyInputs', [])|length }}; status: {{ coverage.status }}.
{% if coverage.gaps %}
There are {{ coverage.gaps|length }} recorded coverage limitations; see JSON for all details.
{% endif %}
{% for gap in important_gaps -%}
- {{ gap|cell }}
{% endfor %}
Source labels group citations by document; exact evidence IDs, snippets, timestamps and claim status are preserved for every item in JSON.

[Structured data and full provenance](<{{ json_path }}>)
{%- endmacro -%}
{%- macro source_block(source) -%}
### {{ source.label }}

{{ source.name|cell }} · {{ source.span }}{% if source.path %}

[Source document](<{{ source.path }}>)
{% endif %}
{%- endmacro -%}
{%- set ns = namespace(blocks=[frontmatter, '# ' ~ title, period.start ~ ' — ' ~ period_end ~ ' (UTC offset ' ~ offset ~ ' minutes)', "This review covers saved records, not a complete measure of life or work. Historical open issues are not necessarily current tasks. Intelligence candidates and improvements are unadopted proposals."]) -%}
{%- for item in overview -%}{%- do ns.blocks.append(overview_block(item)) -%}{%- endfor -%}
{%- for rows, heading, headers, candidate in tables -%}
{%- if rows -%}{%- do ns.blocks.append(table(rows, heading, headers, candidate)|trim) -%}{%- endif -%}
{%- if loop.index == 2 and insights -%}
{%- do ns.blocks.append('## Insights and hypotheses') -%}
{%- for item in insights -%}{%- do ns.blocks.append(insight_block(item)) -%}{%- endfor -%}
{%- endif -%}
{%- endfor -%}
{%- if not items -%}{%- do ns.blocks.append("No reviewable evidence was found in the saved inputs for this period.") -%}{%- endif -%}
{%- do ns.blocks.append(coverage_block()) -%}
{%- for source in sources -%}{%- do ns.blocks.append(source_block(source)|trim) -%}{%- endfor -%}
{{ ns.blocks|join('\n\n') }}
