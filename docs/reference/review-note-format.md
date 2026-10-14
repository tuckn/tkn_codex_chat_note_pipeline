# Review Note contract

Review Note 1.0.0 / CLI 0.33.1 / config 9.0.0. [Japanese guide](review-note-format_ja.md).

## Purpose

Calendar reviews support human reflection and later AI use. They preserve questions,
explanations, demonstrated learning, activities, decisions and reasons, historical open
issues, cross-conversation inferences, reusable resources and proposed next actions.
Intelligence candidates cover semantics, knowledge, dated context and applicable domains.
AI improvement proposals include their evidence, limitations and a next check.
Candidates are not adopted rules; this CLI never rewrites a Vault, memory, Skill or AGENTS.md.
Source text is untrusted data, not executable instructions. No productivity/personality scoring.

## Commands and calendar

```shell
tkn-codex-chat-note pull
tkn-codex-chat-note review-notes build --period weekly --date 2026-05-18 --dry-run --full-output
tkn-codex-chat-note review-notes build --period weekly --date 2026-05-18
tkn-codex-chat-note review-notes build --period monthly --date 2026-05-01
tkn-codex-chat-note review-notes build --period yearly --date 2026-01-01
tkn-codex-chat-note review-notes due --dry-run --full-output
tkn-codex-chat-note scheduled-run
```

Omitting --date selects the latest closed period. Weeks start Monday and use ISO week-years;
months and years are calendar periods. Start is inclusive; end is exclusive. The default
fixed UTC offset is +540 minutes (Japan); there is no automatic daylight-saving adjustment.
Open periods are rejected. Build/due read saved catalogs; raw ingest alone does not update
canonical events or catalogs. Pull or scheduled-run refreshes them.

Weekly reviews use period-filtered canonical events (dialogue, tool calls/results and interruptions). Monthly reviews use
weekly JSON; yearly reviews use monthly JSON. Missing or invalid months fall back to
their weekly reviews, then canonical events for unavailable weeks.
Monthly reviews retain dated decisions/reasons, open questions and candidates for annual
synthesis. Annual reviews compare changes across months; repeated references to one event
are not independent observations. Inferences/proposals retain their original uncertainty.
Boundary-crossing weekly claims are included only when all their dated evidence falls
inside the target period. Boundary fragments and unavailable weeks fall back to canonical events. Missing or truncated input remains explicit, never proportional
allocation of whole-week conclusions. Excluded internal conversations stay excluded.

The model may request supplied evidence IDs to verify original wording, causes, retry counts,
measured time or interruptions. The application verifies archived Raw hashes and supplies
bounded excerpts plus nearby recorded context. Each input batch permits one Raw pass,
8 requested IDs and 16,000 characters by default; each individual excerpt is at most 6,000
characters. Remaining uncertainty is disclosed. Images become descriptive metadata/hashes;
no visual interpretation occurs. No model-specified arbitrary path or URL is opened.

## Published files

Under sources.<id>.data_root/reviews, each period has a Markdown/JSON pair:

- weekly/2026/2026-W21.md and .json
- monthly/2026/2026-05.md and .json
- yearly/2026/2026.md and .json

JSON is the machine-readable authority; Markdown is a human rendering. Manual Markdown
annotations are not synchronized into JSON. Monthly reviews consume weekly JSON, and yearly
reviews consume monthly JSON. Transitive citations remain available for bounded Raw verification.
Adopted human changes require checking the JSON items, evidence and candidate projections too.
Review settings live under sources.<id>.reviews. Outputs and state use each source's
data_root/reviews and state_root/reviews. Sources are reviewed independently. They must not overlap source/Raw storage. Placement does not imply
that an Intelligence candidate has been adopted.

| Field | Contract |
|---|---|
| schemaVersion, id, period | Version, stable ID within output location/period, kind/key/start/endExclusive/UTC offset and timestamp boundaries |
| items | category/title/text/basis/evidenceIds/caveat/nextCheck/intelligenceKind; exact item schema below |
| evidence | Excerpts, timestamps, source IDs, canonical/Raw references and hashes; weekly/monthly-derived evidence uses supports for transitive links |
| sourceInventory, coverage | Catalog observation times, inputs (weeklyInputs/monthlyInputs), missing/truncated evidence; completeHistory is always false |
| intelligenceCandidates, aiImprovements | Corresponding items with stable candidate IDs and status=candidate; never adopted automatically |
| generation, inputFingerprint | Profile/model/settings hash and period-specific input identity |

basis is observed, reported, inferred or proposed. Recorded assistant success reports are
not independent verification. Explanation is not demonstrated learning, silence is not
disinterest, and historical open issues are not automatically current obligations.
Inference/candidate items require limitations and a next check.

The model response is governed by [review-draft.schema.json](../../src/tkn_codex_chat_note/resources/review-draft.schema.json)
and [review-prompt.md](../../src/tkn_codex_chat_note/resources/review-prompt.md).

```shell
tkn-codex-chat-note review-notes validate "C:\path\to\reviews\weekly\2026\2026-W21.json"
```

Validation checks structure, period boundaries, citation IDs and transitive references.
It is not a semantic accuracy or complete source-integrity audit. Consumers should retain
the cited versions and verify hashes when rereading original evidence. Review IDs include
the output location; moving the review store requires preserving its metadata rather than
assuming an unrelated fresh output root has the same identity.

## Scheduling, cost and preservation

A daily Windows Task Scheduler invocation of scheduled-run captures and normalizes Raw
without inference, then builds due reviews for each source. Weeks become due Monday, months on the next month's first day, years
on the next January 1; missed runs catch up on the next invocation.

Due starts with periods containing its first actual run date or sources.<id>.reviews.start_date. Starting
mid-month/year still reviews that whole calendar period when it closes. There is no implicit
backfill of all older closed periods. An explicit earlier start_date enables backfill.
The repository provides a Task Scheduler registration example with -WhatIf; installing this
package never registers a task.

Review max_calls defaults to 12 per source per invocation, including Raw and merge stages. Cached stages
do not consume calls. Stage checkpoints resume after interruption/budget stops. Call and runtime limits apply independently to each source. Existing API token/cost limits remain effective.
Review usage journals participate in build-report; unavailable counts remain unknown.

Unchanged period inputs skip inference. Later-period additions alone do not invalidate an
older week. Identical selected lower-period content/evidence also permits higher-period reuse.
Dry runs never infer, access the network, create directories/locks or write reports/cache/state.
Force bypasses generation reuse but not edit protection. Allow-edited permits replacing
only unreviewed edits, after backup. Any other reviewStatus is protected. Unchanged inputs
preserve manual edits; changed inputs report blocked and other periods may continue.

State contains index.json, reports/, usage/, transactions/ and backups/. Generation cache
lives under cache_root/reviews. A interrupted two-file publication recovers from its saved
transaction without another inference. Concurrent manual changes to a pending output stop
recovery. Source hash mismatches are disclosed rather than silently trusted.

Exit 0 means the operation completed; 1 indicates failures/protected updates; 2 indicates
unfinished budget/time-limited work. Successfully publishing a review with explicit gaps
does not assert complete history coverage.

## Tool input preparation (0.31)

Dialogue remains period-filtered. Routine tool bodies become per-thread indexes with event
references and exact request-text hash repetition counts. These are log-record counts, not
independent executions or VLM calls. Nonzero exit codes and diagnostic text retain bounded
excerpts. Keyword matches may be quoted code; no failure or success is inferred automatically.
Full canonical/Raw evidence remains available for bounded verification. Model input timestamps
show the first/last observation; full timestamp lists remain in published evidence.
Conversations are kept together when they fit a batch. Failed normalization is disclosed and
excluded rather than assigned an uncertain conversation identity. Source inventory records
the preparation policy and compacted/diagnostic counts. Existing call budgets still apply.

Invalid structured drafts receive one bounded repair attempt using the draft and validation
feedback, without resending source bodies. Repair calls share the normal call/time budgets;
unvalidated responses are checkpointed separately and never published as valid reviews.

## Final editorial synthesis (0.32)

Weekly, monthly and yearly reviews share the same editorial policy. Extraction retains cited findings;
intermediate merges reconcile duplicates and corrections. A final compose pass connects themes,
outcomes, decisions and remaining questions, including small personal questions. Monthly synthesis
explains changes across weeks; yearly synthesis explains changes across months rather than concatenating them.

Markdown presents a narrative overview, thematic tables, prose cross-chat insights, and AI improvement
and Intelligence candidate tables with verification steps. Common limitations appear once; material
claim-specific limitations remain visible. Source labels group citations by document. Full snippets,
exact item-to-evidence IDs, timestamps, basis and candidate status remain in JSON. Display labels can
change between versions; use evidence IDs for durable references.

The final pass uses the same call/time budget and validated cache. A single extraction batch also gets
one final pass; empty periods need none. Editorial-only changes reuse extraction checkpoints when
inputs, extraction policy and model settings match. Model/extraction changes invalidate that reuse.
Editorial policy contributes to the generation fingerprint, and existing reviewed/edited-file protection applies.
See [review-synthesis-prompt.md](../../src/tkn_codex_chat_note/resources/review-synthesis-prompt.md).

## Automatic editorial verification (0.33)

All three review levels now verify the composed draft before publication. A coverage screen selects
all distinct extracted findings except exact structured matches with substantive final rows
(excluding overview and insights). The model accounts for each candidate: already covered, materially missing, or omitted
with a reason. Shared citations do not establish coverage; different decisions in the same record are still compared.
Restoration prioritizes meaning, reusable prerequisites and distinct topics, not every intermediate step.

A source audit checks body rows plus restorations against their citations, nearby dialogue, and
the original sources of findings mapped to that theme (including findings judged already covered). It
may correct, split or omit rows, distinguishing requests/plans/reported completion, implementation
from adoption, historical from current state, and this review's lack of visual inspection from reports
about the original conversation. Reuse purposes and working-location rules remain scoped proposals.
The overview is checked last against the immutable source-audited body and its citations. This
bounded check may only replace/omit the overview, preserve reported/inferred status, and introduce
no facts exclusive to the earlier overview. It cannot rewrite the body. Markdown then renders these items.

Source selection also includes the last assistant reply before the next user message, that next user
message, and the last saved assistant reply in each cited chat. This catches completion and corrections
beyond progress messages; it remains bounded context, not an exhaustive contradiction search.
Source text is bounded (normally 2,400 characters per excerpt, reduced to 600 or 300 when necessary).
Citations and direct supports take priority over optional nearby dialogue; omissions/truncation are
recorded. Shared sources are transmitted once per batch. The audit cannot infer absence from missing
text and must retain specific uncertainty. No additional image interpretation or arbitrary file access occurs.

Validation rejects missing/duplicate decisions, invalid coverage targets, foreign citations, invalid
item fields and silent non-incorporation of restored findings. One bounded audit repair is allowed;
it includes sources and is subject to the input bound. Failed/incomplete audits never replace existing
notes. Call/time budgets and validated checkpoints apply to both checks and repairs. Empty periods
make no model calls. Same-input reruns reuse completed stages. Quality policy/schema/version enter
the final generation fingerprint without invalidating unchanged extraction checkpoints.

`generation.qualityAudit` records the bounded method, counts, source limitations and per-item decisions.
It is an audit trail, not a semantic correctness certificate. `review-notes validate` remains an offline
structural validator and does not invoke the model. Legacy notes remain readable without this field.

Nonempty reviews add at least one source-audit call; distinct findings add coverage calls and each
overview adds one final body-consistency check. Exact
batch counts depend on composed findings and cannot be known from the initial dry-run batch count.
All calls, including unsuccessful/repair attempts, belong in usage accounting when usage is available.
Token cost includes model/runtime overhead, reasoning and output, not just the excerpt text. Large
restorations can materially increase cost and span multiple scheduled runs under `max_calls`.
See [review-quality-prompt.md](../../src/tkn_codex_chat_note/resources/review-quality-prompt.md).

## Concise verification (0.34)

Compose is validated at 32 items and 7,000 Japanese prose characters (20,000 for English);
the verified review is limited to 36 items and 8,400 characters (24,000 for English).
Prose counts title, text, caveat and nextCheck, excluding citations and Markdown scaffolding.
These are ceilings, not quotas. All three period kinds select meaningful themes; lower-priority
implementation detail stays in saved findings rather than becoming a complete activity inventory.
A review may restore at most six consequential findings, including at most two new topics.
Existing topics take priority; considered candidates and reasons remain in qualityAudit.consideredFindings
and coverageDecisions. Only exact structured duplicates skip the coverage comparison.
Each existing audit row can produce at most two rows (to split unrelated tasks), new topics one.
Additional rows from splits share the remaining 36-item review budget across all batches.
Row allocations share at most 97 percent of the final character ceiling, leaving headroom.
Original sizes and restoration needs determine allocations; if the originals already exceed that
ceiling they must shrink. Both keep and replace consume the allocation, including all prose fields.
Thus individually valid decisions cannot collectively overrun the prose ceiling. Allocations and
unused headroom are recorded in qualityAudit.editorialAllocation and unitBudgets.
Overlong compose/replacements get one bounded repair; any remaining violation prevents publication.
Row-length errors report item IDs, actual counts and limits. Their repair sends only the offending
rows and permitted sources, then validates the combined result again; other repairs retain full context.
The overview check uses audited statements rather than retransmitting all Raw. Actual cost depends
on coverage, source batching, overview checks and repairs;
increasing sources.<id>.reviews.input_characters can reduce calls but changes extraction cache identity
and should be measured. The default input bound remains unchanged.


## Correction and consistency boundaries (0.35)

Coverage is semantic and model-assisted, not a guarantee of complete recall. Related findings are
fallible too; their original sources must support later corrections and outcomes. Explicit user
decisions must remain distinct from uncertainty about whether implementation followed them.
The overview uses the body as a derived, audited source, so errors in the body can still propagate.
The generic offline validator checks structure, periods and citation IDs, not semantic consistency.
No shared provider configuration or default model is changed by this release.


## Shared evidence transport (0.36)

Body audit requests group items by shared evidence rather than display order. Selected excerpts,
per-item citation permissions, and final note order are preserved. A shared ID uses the fullest
available excerpt. Packing respects input bounds and falls back to sequential packing if either
request count or input characters would increase. Shared sources do not expand citation permissions.

Request-local aliases shorten source identifiers only. Responses are restored to canonical IDs before
validation and caching. Source prose, actor, timestamps, thread and truncation markers are unchanged.
Cache identity includes the alias mapping, which is also retained in the cache, so different canonical
sources cannot collide through identical aliases. qualityAudit.sourceTransmission records sequential
and packed source occurrences, source text characters, and repeated source text characters. These are
not token estimates; consult usage for measured consumption. Unchanged extraction and coverage
prompts continue to reuse their caches.
