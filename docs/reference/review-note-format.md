# Review Note contract

Review Note 1.0.0 / CLI 0.29.1 / config 8.4.0. [Japanese guide](review-note-format_ja.md).

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
tkn-codex-chat-note pull --limit 10
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

Weekly reviews use period-filtered Session Note timeline evidence. Monthly reviews use
weekly JSON; yearly reviews use monthly JSON. Missing or invalid months fall back to
their weekly reviews, then source notes/canonical events for unavailable weeks.
Monthly reviews retain dated decisions/reasons, open questions and candidates for annual
synthesis. Annual reviews compare changes across months; repeated references to one event
are not independent observations. Inferences/proposals retain their original uncertainty.
Boundary-crossing weekly claims are included only when all their dated evidence falls
inside the target period. Boundary fragments and unavailable weeks fall back to source
notes/canonical events. Missing or truncated input remains explicit, never proportional
allocation of whole-week conclusions. Excluded internal conversations stay excluded.

The model may request supplied evidence IDs to verify original wording, causes, retry counts,
measured time or interruptions. The application verifies archived Raw hashes and supplies
bounded excerpts plus nearby recorded context. Each input batch permits one Raw pass,
8 requested IDs and 16,000 characters by default; each individual excerpt is at most 6,000
characters. Remaining uncertainty is disclosed. Images become descriptive metadata/hashes;
no visual interpretation occurs. No model-specified arbitrary path or URL is opened.

## Published files

Under reviews.data_root, each period has a Markdown/JSON pair:

- weekly/2026/2026-W21.md and .json
- monthly/2026/2026-05.md and .json
- yearly/2026/2026.md and .json

JSON is the machine-readable authority; Markdown is a human rendering. Manual Markdown
annotations are not synchronized into JSON. Monthly reviews consume weekly JSON, and yearly
reviews consume monthly JSON. Transitive citations remain available for bounded Raw verification.
Adopted human changes require checking the JSON items, evidence and candidate projections too.
Default data/state roots are separate directories under ~/.tkn/codex_chat_note_pipeline;
both are configurable. They must not overlap source/Raw storage. Placement does not imply
that an Intelligence candidate has been adopted.

| Field | Contract |
|---|---|
| schemaVersion, id, period | Version, stable ID within output location/period, kind/key/start/endExclusive/UTC offset and timestamp boundaries |
| items | category/title/text/basis/evidenceIds/caveat/nextCheck/intelligenceKind; exact item schema below |
| evidence | Excerpts, timestamps, source IDs, note/canonical/Raw references and hashes; weekly/monthly-derived evidence uses supports for transitive links |
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

A daily Windows Task Scheduler invocation of scheduled-run captures Raw each time. When
periods become due it updates at most reviews.session_note_limit Session Notes (default 10),
then builds reviews. Weeks become due Monday, months on the next month's first day, years
on the next January 1; missed runs catch up on the next invocation.

Due starts with periods containing its first actual run date or reviews.start_date. Starting
mid-month/year still reviews that whole calendar period when it closes. There is no implicit
backfill of all older closed periods. An explicit earlier start_date enables backfill.
The repository provides a Task Scheduler registration example with -WhatIf; installing this
package never registers a task.

Review max_calls defaults to 12 per invocation, including Raw and merge stages. Cached stages
do not consume calls. Stage checkpoints resume after interruption/budget stops. Session Note
and review limits/deadlines are separate. Existing API token/cost limits remain effective.
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
