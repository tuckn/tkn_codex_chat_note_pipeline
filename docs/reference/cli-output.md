# CLI result contract (0.46.0)

This contract applies only to terminal results. Python API returns, saved reports,
manifests, Review Note JSON/Markdown and communication protocols retain their schemas.
See the [README](../../README.md#41-cliの結果表示) for usage examples.

Every command emits one final result on stdout. The default is one `key=value` per
line, with dotted mapping keys and indexed arrays (`results[0].status`). Empty
containers are `[]`/`{}`, booleans `true`/`false`, and absent values `null`. Strings
are unquoted, Unicode and path separators are preserved, and paths are never
abbreviated or truncated. CR/LF/tab and other control/line-separator characters
are escaped. The list is for reading/copying; it is not a reversible serialization.

`--json` selects one valid JSON document instead. Logs, progress, diagnostics and
ANSI color are confined to stderr. `--quiet` and `--verbose` only affect stderr.
Neither TTY nor pipes/redirection select a format. `--json` and `--full-output`
can appear before, between or after command/subcommand names; child parsers do
not reset values selected by parent parsers. `--` retains its normal meaning.

## Fields and detail selection

| Field | Meaning |
| --- | --- |
| `command` | Full parsed command name; null when argument parsing cannot finish |
| `status` | `success`, `failed`, `incomplete`, `interrupted`; operations with their own status retain it (e.g. config `created`/`unchanged`, migration `planned`/`migrated`/`already-migrated`) |
| `exitCode` | Actual CLI exit code, independent of output format and verbosity |
| `ok`, `complete`, `dryRun` | Original operation fields, when applicable; a successful dry-run need not represent a completed persistent run |
| `threadCounts`, `generatedCount`, `pendingPeriods`, etc. | Original counters, when applicable |
| `failed`, `historyIssues`, `warnings`, `generationStop` | Original failures, undated history issues, warnings and stop details; never suppressed as routine records |
| `threads`, `results` | Actionable records and non-routine changes; full collections with `--full-output` |
| `threadsCount`/`resultsCount`, `threadsCounts`/`resultsCounts` | Total collection sizes and counts by status in ordinary output |
| `omittedDetailsCount`, `detailsHint` | Number omitted at that object level and how to inspect full details |
| `reportPath`, `reportPaths` | Saved reports, retained in either format; normally absent/null on dry-run |
| `error`, `errorType`, `targets` | Fatal diagnostic and separately copyable paths; config search paths, explicit config/artifact/source paths, or OSError filename/filename2 as available |
| `sourcePaths`, `jsonPath`, `notePath` | Diagnostic targets derived from structured source identities/periods, without parsing error prose |

Without `--full-output`, current/unchanged/excluded/skipped-empty records can be
summarized, except when they carry errors, warnings or coverage gaps. Planned,
deferred, blocked, preserved, failed and other non-routine records remain visible.
Migration file operations are summarized by count; `--full-output` includes every
source and destination. Configuration sources are never summarized. Raw capture
source records use the same routine-item rule. Nested multi-source capture/review
reports receive the same projection, without changing the saved report.

`--full-output` changes information volume in both text and JSON. It does not
change artifact generation, save a report by itself, or enable writes/inference.
Prefer reading an existing reportPath/reportPaths over rerunning a write command.
There is no saved report for a dry-run; rerun with `--dry-run --full-output` to
inspect its complete plan without persistent changes.

| Commands | Results |
| --- | --- |
| `config init`, `config list` | Creation/backup paths or resolved config, sources and schema metadata |
| `clone`, `pull`, `raw ingest` | Capture/normalization counters, failed and attention records, report paths |
| `review-notes build`, `review-notes due`, `scheduled-run` | Period results/counts, missing timestamps, stop reasons, pending work and capture/review reports |
| `review-notes validate`, `provenance validate` | Existing verification fields or selected-format failure diagnostics |
| `status` | Last saved coverage/status; no live scan added |
| `storage migrate` | Existing migration result and file count/details |
| `build-report` | Existing usage report counters, warnings and artifact paths |

## Exit codes and error handling

0 means command success, 1 operation/configuration/validation failure, 2 argument
error or incomplete work (e.g. generation budgets), and 130 interruption. Existing
command-specific success/completion classification is unchanged. A source can fail
while others complete; inspect `ok`, `complete`, failures and per-source results.
`--help` and `--version` keep ordinary successful guidance instead of a result.

Argument errors retain the chosen format even when parsing does not finish. Runtime
errors and KeyboardInterrupt produce a single final diagnostic whenever stdout is
still writable. Interruption does not imply rollback: previously completed writes
and checkpoints retain their existing semantics. Rerun to resume. An abrupt process
termination, closed output pipe or unavailable stdout cannot guarantee a result.

Machine consumers select `--json`; `--json --full-output` includes every report
item. CLI-only command/status/exit-code and diagnostic path fields belong to the
terminal result, separately from saved reports and Python return values.
