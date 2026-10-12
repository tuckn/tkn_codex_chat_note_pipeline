# Data contract: chat evidence and calendar reviews

CLI 0.30.0 · config 9.0.0 · storage 5 · catalog/provenance 1.0.0 · Review Note 1.0.0

This is the file interface for consumers of captured Codex conversations and calendar
reviews. For operation, see the [README](../../README.md); for review content, see the
[Review Note contract](review-note-format.md).

## Ownership and processing

The application captures local chat files, exports deterministic Canonical Events,
records observed Project membership and generates weekly, monthly and yearly reviews.
Weekly reviews consume Canonical Events; monthly reviews consume weekly JSON; yearly
reviews consume monthly JSON. Missing lower-period evidence falls back to Canonical
Events and bounded, hash-verified Raw excerpts. Session Note generation is removed.
Existing Session Notes are neither read as review inputs nor deleted.

Intelligence candidates and AI behavior improvements remain proposals. Adopted knowledge,
semantics, rules and Working Context belong to downstream processes. This CLI never
rewrites a Vault, Skill, memory or AGENTS.md automatically.

`raw ingest` only captures source files. `clone` initializes storage, captures and
normalizes; `pull` refreshes an initialized store. These commands do not call a model.
`scheduled-run` captures and normalizes, then generates closed, due review periods.
A successful normalization does not imply that reviews have been generated.

## Source identity and configuration

`sources` is an ID-keyed map. Each source has source_root, optional raw_root/data_root/
state_root, enabled/include_archived, and reviews settings. Source IDs identify acquisition
environments, not inference providers. Preserve their spelling across configuration updates.
The source provider is `codex`. Generation profiles are configured separately.

Source IDs allow ASCII letters/digits/._-, starting with a letter or digit. Windows device
names, trailing dots, duplicate YAML keys and case-only duplicate IDs are rejected.
Map layers merge by ID; an explicit map suppresses implicit source defaults. Disabled
sources are not scanned. `--source <id>` selects one enabled source.

Each source owns its catalog, evidence, reviews and checkpoints. Reviews are generated
independently; sources are not merged into one calendar note. Review settings belong to
`sources.<id>.reviews`. Review outputs are `<data_root>/reviews`; review state is
`<state_root>/reviews`. Review-specific output roots are not configurable.

Configuration 9 rejects earlier schemas during ordinary commands with migration guidance.
Move top-level reviews into each source, remove its data_root/state_root/session_note_limit,
and remove generation.session_note_profile, idle_minutes and installed_at. Preserve source
IDs and final Raw/data/state paths. Existing storage 5 needs no rewrite. Historical combined
review stores are not automatically converted to source-local stores.

## Stable identities and references

- threadId is the original conversation ID. threadKey is UUIDv5 in the URL namespace
  for `codex-thread:<threadId>`, independent of Project membership and location.
- Catalog identity is `(sourceProvider, sourceId, threadKey)`. Identical threadKeys in
  different environments are allowed.
- `data:/` resolves under this source's data_root. `raw:/<provider>/<source_id>/` is
  a virtual prefix: remove it and resolve the remaining path under this source's raw_root.
- Canonical event IDs include threadKey, capture SHA-256 and local event/line identity.
  They identify a particular version, not a stable event across arbitrary rewrites.
- Entity versions are `sha256:<hex>` of exact bytes. Join evidence by `(id, version)`.
  Filenames and locators are not identities.
- Review IDs bind output location and period. Candidate IDs bind their review and content.
  Retain original IDs and metadata when relocating or adopting an artifact.

Raw schema-3 paths are latest source copies and may change. Historical citations use
retained provenance snapshotRef and hash, not a mutable latest Raw path. No cloud-only
history is fetched, and no complete-history claim is made.

## Published artifacts

| Artifact | Contract |
| --- | --- |
| Raw manifest | Reads 1/2/3; writes 3, with refs, hashes, byte counts and capture metadata |
| store.json | 1.0.0; storageVersion 5, source identity, Raw prefix and legacy aliases |
| catalog/threads.json | 1.0.0; asOf and threads |
| Canonical export | 1.0.0; parser 2, event locators, source metadata and diagnostics |
| Provenance entities, activities, index | 1.0.0; versioned artifacts and normalization lineage |
| Capture/normalization report | Integer 4, for source-local and multi-source responses |
| Review Markdown and JSON | 1.0.0; periods, claims, evidence, candidates and coverage |

Catalog rows include source identity, threadId/threadKey, capture and canonical refs/hashes,
status/reason, dates and observed membership/history. New rows do not publish Session Note
noteId/noteRef/noteSha256. A current status means normalized evidence is available, not that
an AI summary exists. Failed/excluded rows and diagnostics remain visible. Internal or
approval-only conversations are excluded from review inputs.

Membership preserves explicit, projectless, root-inferred, ambiguous, unmatched or
unavailable observations. Nullable IDs and candidate IDs retain uncertainty. Membership
history dates when the pipeline observed a change, not when the user made it.

Exact duplicate captures and provable byte-prefix versions are coalesced for normalization.
Divergent histories are retained, ordered by history start while preserving source order.
No active branch is inferred and history_base does not discard records. historyBranches,
sourceSetSha256 and per-file sourceDiagnostics describe the retained set. Branch events
have history-qualified localId/branchId and physical Raw line locators. Singular capture
fields identify only the first retained history; use the branch records for full coverage.

## Provenance and reads

Entities contain schemaVersion, id, version, sha256, ref, kind, mediaType, byteCount and
snapshotRef. Exact snapshots live at `data:/provenance/blobs/<hash-prefix>/<hash>`.
Entity filenames hash id + NUL + sha256. A relocated entity can retain its first locator;
use snapshotRef for historical bytes.

Activities record run/subject/stage, timestamps, agent, used/generated entity versions and
derivedFrom edges. The current upstream generation stage is deterministic normalization.
Review JSON carries its own claim-level evidence and transitive citations. A dependency
edge alone does not establish semantic truth.

The provenance index records runId/asOf/pipelineComplete/artifacts/activityRefs. Current
artifacts identify Canonical Events and thread/source identity. Older derived artifacts
in a reused store remain inactive; they are not refreshed or deleted.

Consumers should:

1. Preserve the catalog/index bytes and run metadata used for selection.
2. Verify supported schemas, source identity and hashes of selected canonical/review files.
3. Resolve historical snapshotRef safely and verify hashes and byte counts.
4. Preserve evidence IDs/versions and inferred/proposed status in downstream outputs.
5. Recheck inputs before publishing; retry if concurrent updates changed them.

Files are atomically replaced individually, not as one database transaction. Readers can
observe different generations and must reject inconsistent combinations. `raw ingest`
does not republish the derived index. `provenance validate` checks structure, identities,
snapshot hashes/lengths and current artifacts; it does not prove semantic accuracy.

## Storage and migration

Omitted source roots resolve to `~/.tkn/codex_chat_note_pipeline/<kind>/codex/<source_id>`.
Configured roots are final paths. Roots may share a parent but must not overlap one another
or input directories. Ownership markers bind source identity; locks protect actual roots.

- Raw: sessions/, archived_sessions/, manifest.jsonl and metadata/.
- Data: source-aligned/, catalog/, provenance/, store.json and reviews/.
- State: pipeline.json, normalization/, reports/, last-run.json and reviews/.
- Review state: index.json, reports/, usage/, transactions/ and backups/.

Historical Session Note files/checkpoints may coexist without being updated. Back up
Raw/data/state together. Review state is needed for restart and edit protection. Cache is
regenerable. Review stage cache lives at cache_root/reviews, keyed by generation signature
and exact prompt. Hash/schema/citation validation precedes reuse.

store.json rawRefAliases/dataRefAliases translate legacy locators longest-prefix first,
once, rejecting traversal before and after translation. Immutable snapshots retain bytes.
The storage-5 ownership filename and applicationId remain legacy identifiers deliberately;
a package rename does not change ownership of existing stores.

`storage migrate --from-config <old-config> --dry-run` plans a copy into fresh destination
roots. The source configuration is standalone. Historical config schemas 2 through 8 and
current 9 are supported by the explicit migration reader, subject to supported minor
versions; ordinary runtime configuration only accepts 9. Source stores 2/3/4 and completed
5 are supported. Preserve source identity and stop concurrent source writers.

Migration copies Raw, canonical data, provenance, reviews, historical notes and state.
Mutable locators are translated, while immutable snapshots and old note bytes remain
unchanged. Source data/configuration are not modified. Conflicts fail closed; interrupted
copies resume from their plan and write errors roll back destination changes. Review IDs
bind location: migration is not a promise of automatic review identity regeneration.

## Review generation and usage

The [Review Note contract](review-note-format.md) specifies period boundaries, fallback,
bounded Raw verification, categories, candidate projections, publication and protection.
JSON is authoritative for higher-period generation; Markdown is a human rendering.

Generation profiles select shared tkn_genai_bridge profiles. Effective generation settings
and Bridge version bind cache identity; profile-label and price-only changes do not.
The shared Bridge configuration is resolved explicitly, with no implicit application-CWD
Bridge config. Authentication is lazy and never runs during dry-run.

Review call/time and API limits apply per source invocation. Cached validated stages do
not consume calls. Budget exhaustion defers unfinished work for a later invocation.
Transport errors are not automatically retried. Missing usage/cost stays unknown, not zero.
Configured reservations are conservative estimates, not invoice guarantees.

Review usage records live under each source's state_root/reviews/usage. build-report reads
these plus historical source journals/reports, deduplicating recorded attempts. It keeps
known subtotals distinct from missing totals, and cached/reasoning tokens as subsets.
Price scenarios are reference estimates, not actual invoices. Cost calculation uses Bridge
pricing. Reports contain index.html, usage.json and usage.csv, without prompt/answer bodies.

Dry-run is read-only: no inference, authentication, network, directories, locks, reports,
cache or state writes. It describes saved evidence and planned work, not a persisted update.
