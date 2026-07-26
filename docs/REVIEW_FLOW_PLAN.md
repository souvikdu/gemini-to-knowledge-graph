# Review Flow — Implementation Plan

## 1. Goal

Add an optional review step between extraction and classification so a user can
decide which chats to keep without opening every JSON file individually.

The review step generates a local CSV manifest containing chat titles and basic
metadata. The user marks unwanted rows explicitly as `DELETE`. The existing
`prune_chats.py` workflow then handles both those marked chats and chats whose
JSON files were deleted manually.

```text
Extract
  → Generate review manifest (auto-scans new/changed chats)
  → Mark rows DELETE and/or manually delete JSON files
  → Prune dry-run
  → Prune
  → Mark all reviewed
  → Classify
  → Vault
```

The intended commands are:

```bash
python review_chats.py
python review_chats.py --scan-sensitive
# Edit review/chats_to_review.csv

python prune_chats.py
python prune_chats.py --prune

python review_chats.py --mark-reviewed
```

Actual deletion remains exclusively owned by `prune_chats.py`.
`review_chats.py` only generates or refreshes review metadata.

## 2. Problems with the previous design

The previous draft should not be implemented as written for the following
reasons:

- A hash of the original ID set can prove that a file changed, but it cannot
  identify which IDs were removed. Diffing the edited manifest against the
  current database would also incorrectly treat chats extracted after manifest
  generation as deletion choices.
- Treating a missing row as a deletion instruction is unsafe. An editor error,
  truncated file, accidental filtering, or accidental row deletion could stage
  unintended removals.
- `ignored_conversations` is permanent extractor state, not pending review
  state. Currently only the extractor consults it; classification and vault
  generation still process JSON files for review-marked chats.
- Adding a review mark to `ignored_conversations` before pruning changes
  extractor behavior even though the user has not completed the deletion.
- Extending prune to delete database records for review-marked chats without
  deleting their JSON files is incomplete. A later `sync_chats_to_db()` would
  recreate those chat rows.
- Adding columns to the `chats` table is not migration-free. `CREATE TABLE IF
  NOT EXISTS` does not add columns to an existing SQLite table.
- Automatically scanning and then asking permission to write the manifest adds
  friction after the content has already been read.
- A flag such as `keyword:Priya` exposes and persists the sensitive keyword,
  contradicting the requirement to avoid storing matched sensitive values.
- A time-based stale-manifest restriction is unnecessary when each deletion is
  an explicit action tied to a stable conversation ID.
- Separate `--apply` and `--list-marked` commands duplicate responsibilities
  already covered by prune's dry-run and execution modes.

## 3. Scope and non-goals

### In scope

- Generate a local, human-editable review manifest.
- Show title, creation date, source, conversation ID, and content hash for each
  chat.
- Let the user explicitly change `KEEP` to `DELETE`.
- Preserve review choices and reviewed status when the manifest is regenerated.
- Detect re-downloaded chats with new content via `chat_fingerprint()` and mark
  previously reviewed chats as `RE-REVIEW`.
- Put newly discovered and re-review chats at the top of the manifest.
- Let normal prune discovery combine manual file deletions with manifest marks.
- Delete marked JSON files safely before running the existing database and
  vault cascade.
- Remove successfully pruned rows from the manifest after a prune run.
- Provide an explicit `--mark-reviewed` command with confirmation to collapse
  all review states (`NO`, `RE-REVIEW`) to `YES`.
- Automatically scan new and changed chats for sensitive content when the local
  scan configuration exists.
- Optionally perform a full re-scan of all chats using `--scan-sensitive`.

### Out of scope for the first implementation

- Opening-prompt previews or generated summaries.
- Interactive keep/remove prompts.
- A web UI.
- Full-text or fuzzy search.
- NER or LLM-based sensitive-data detection.
- Persistent review-status tables.
- Cached sensitive-scan results.
- Match excerpts or `--show-match` output.
- A flagged-only manifest mode.
- Automatic pruning from `review_chats.py`.

## 4. Review manifest

### 4.1 Command and location

Add a standalone `review_chats.py` command:

```bash
python review_chats.py
```

It calls `sync_chats_to_db()` and atomically generates or refreshes:

```text
review/chats_to_review.csv
```

Add `review/*.csv` to `.gitignore`. The review directory may later contain
tracked documentation, so ignore generated CSV files rather than the entire
directory.

### 4.2 CSV contract

Use Python's standard `csv` module so commas, quotes, Unicode, and other title
content are escaped correctly. Write UTF-8 with a header and the following
columns:

```csv
created_at,source,conversation_id,content_hash,reviewed,action,title,flags
2026-07-18T10:00:00+00:00,gemini_web,gemini_abc123,a1b2c3d4e5f67890,NO,KEEP,Refactoring SQLite,
2026-07-17T10:00:00+00:00,gemini_web,gemini_abc124,f0e1d2c3b4a59876,NO,DELETE,Resignation letter,email
```

Column behavior:

- `created_at`: the stored chat creation timestamp.
- `source`: the normalized extractor source.
- `conversation_id`: the stable identity used by review and prune.
- `content_hash`: the `chat_fingerprint()` value stored when the row was last
  written or refreshed. Used during regeneration to detect content changes.
- `reviewed`: `YES` after the user explicitly marks reviewed; `NO` for new
  chats; `RE-REVIEW` for previously reviewed chats whose content has changed.
- `action`: `KEEP` or `DELETE`; new chats default to `KEEP`. It is placed
  immediately before `title` so the decision and title can be reviewed side by
  side.
- `title`: the full stored title; CSV quoting handles special characters.
- `flags`: comma-separated safe sensitive-rule labels; populated automatically
  for new and changed chats when the local scan configuration exists, or for
  all chats when `--scan-sensitive` is used.

On the first generation, sort all rows newest first, with `conversation_id` as
the deterministic tie-breaker. On later generations, put new chats and
`RE-REVIEW` chats in a newest-first block at the top. Keep remaining existing
rows in their previous relative order so reviewed history remains stable.

### 4.3 Regeneration and resumability

When a manifest already exists, parse and validate it before generating the new
version. Preserve valid actions and reviewed flags by `conversation_id`, while
refreshing all other metadata from the current `chats` table.

Regeneration must:

- Add newly discovered chats at the top as `reviewed=NO, action=KEEP` with the
  current `content_hash` from the database.
- For existing chats whose database `content_hash` or sensitive scan `flags`
  differ from the values stored in the CSV manifest and whose `reviewed` value
  is `YES`: change `reviewed` to `RE-REVIEW` and move the row to the top block.
  This signals that the chat was previously reviewed but has since acquired new
  content or newly matched sensitive rules.
- For existing chats whose `content_hash` and `flags` have not changed: preserve
  their `KEEP`/`DELETE` action and `YES`/`NO`/`RE-REVIEW` reviewed state.
- Always refresh `content_hash`, dates, sources, titles, and scan flags from
  current data.
- Drop rows for chats no longer present in the database.
- Write to a temporary file in the same directory and replace the destination
  only after the complete CSV has been written successfully.

The existing manifest must remain untouched if parsing, synchronization,
scanning, or output generation fails.

### 4.4 Validation

Accept action and reviewed values case-insensitively after trimming whitespace,
then normalize them to uppercase on the next generation. Reject the entire
manifest instead of partially accepting it when it contains:

- Missing required columns.
- Empty conversation IDs.
- Duplicate conversation IDs.
- An action other than `KEEP` or `DELETE`.
- A reviewed value other than `YES`, `NO`, or `RE-REVIEW`.
- Structurally malformed CSV.

Metadata edited by the user is not authoritative. Prune must use the database
for current titles, source filenames, and other operational data; the manifest
provides conversation IDs, explicit actions, and review-progress state.

### 4.5 Marking all chats as reviewed

```bash
python review_chats.py --mark-reviewed
```

This command collapses all `reviewed` values (`NO`, `RE-REVIEW`) to `YES`,
signaling that the user has finished reviewing the current batch. It does not
change any `action` values.

Before writing, display a confirmation prompt showing:

- How many chats are currently `NO`.
- How many chats are currently `RE-REVIEW`.
- How many chats are currently `YES` (unchanged).
- The total number of chats that will be marked `YES`.

Require the user to confirm before proceeding. If the user declines, exit
without modifying the manifest.

`--mark-reviewed` is independent of prune. It can be run at any point during or
after the review pass. Running it before prune is useful when the user wants to
signal that all remaining chats have been reviewed, without necessarily pruning
anything.

## 5. Prune integration

### 5.1 Candidate discovery

Extend the normal `prune_chats.py` flow to calculate the deduplicated union of:

1. Orphans: chat rows whose corresponding JSON files are missing.
2. Review-marked chats: existing chat rows whose manifest action is `DELETE`.

If the default review manifest does not exist, prune behaves exactly as it does
today. If it exists but is invalid, prune fails closed before deleting anything.

IDs marked `DELETE` but no longer present in the `chats` table are stale entries.
Report and skip them; do not recreate state or treat them as errors requiring
manual database repair. Remove those stale `DELETE` rows when the manifest is
successfully finalized, because they no longer reference a retained chat.

The prune dry-run should label why each candidate was selected:

```text
Found 2 conversation(s) eligible for pruning:
  - Refactoring SQLite (gemini_abc123) [file missing]
  - Resignation letter (gemini_abc124) [marked DELETE | flags: email]
```

A chat that is both orphaned and marked appears once. Its reason may show both
conditions, but it must be pruned only once.

### 5.2 Safety threshold

Apply the existing `max_prune_ratio` threshold to the complete deduplicated
candidate union. Keep the current `--confirm-large-delete` override and do not
introduce a separate review-specific threshold.

The denominator remains the number of currently tracked chats before deletion.
This preserves the existing safety model and protects against accidentally
marking a large portion of the manifest.

### 5.3 Deleting a marked JSON file

Unlike an orphan, a review-marked chat normally still has a JSON file. Before
deleting its database state:

1. Load `source_file` from the current `chats` row.
2. Resolve the configured `chats_dir` and candidate file to absolute paths.
3. Verify that the candidate path remains inside `chats_dir`.
4. Require the candidate to be a regular `.json` file.
5. Read the file and verify its `conversation_id` matches the marked ID.
6. Delete that JSON file.
7. Add the ID to the cascade set only after successful deletion.

Do not derive a filename from the conversation ID and do not trust a filename
from the CSV.

If validation or file deletion fails, report the affected chat and exclude it
from the database cascade. Continue processing other independently valid
candidates where safe, then return a non-zero exit status so partial failure is
visible. Keep its `DELETE` row in the manifest so the requested deletion remains
visible and can be retried.

Deleting the JSON file before the database transaction gives interruption-safe
recovery: if the process stops after file deletion, the remaining database row
is an ordinary orphan and will be found by the next prune run.

### 5.4 Existing cascade

Pass successfully prepared review-marked chats and ordinary orphans into the
existing prune cascade:

1. Delete matching classifications.
2. Delete matching rows from `chats`.
3. Add IDs to `ignored_conversations` with `reason="deleted-by-user"` so the
   extractor does not fetch them again.
4. Delete matching conversation notes from the vault.

Keep database steps 1–3 in one transaction. Continue to refresh category and
topic hub notes on the next `obsidian_layout.py` run rather than editing hubs in
the prune command.

### 5.5 Updating the review manifest after pruning

An executing `--prune` run atomically rewrites the manifest after candidate
processing:

- Remove every row whose chat was successfully pruned, whether it was selected
  by `DELETE` or by a manually missing JSON file.
- Remove stale `DELETE` rows whose IDs were already absent from `chats`.
- Keep failed `DELETE` rows unchanged so they remain available for retry.
- Preserve the `reviewed` and `action` values of all remaining rows unchanged.
- Preserve the relative order of all retained rows.

This rewrite occurs only for `--prune`; dry-run mode never changes the manifest.
Prune does not change review states; use `review_chats.py --mark-reviewed` to
collapse all review states to `YES`.

Write the updated CSV to a temporary file in the same directory and atomically
replace the original. If manifest rewriting fails after deletions have already
succeeded, report a non-zero exit status. A subsequent regeneration will remove
the deleted IDs using current database state while preserving the remaining
actions and reviewed flags.

### 5.6 CLI behavior

Keep the existing public interface:

```bash
python prune_chats.py
python prune_chats.py --prune
python prune_chats.py --prune --confirm-large-delete
python prune_chats.py --list-ignored
python prune_chats.py --unignore <cid>
```

The first two commands automatically include valid `DELETE` rows from the
default review manifest when it exists. No `review_chats.py --apply` command is
needed.

In dry-run mode, also report how many `KEEP` rows remain unreviewed (`NO` or
`RE-REVIEW`). `--prune` with no deletion candidates exits cleanly without
modifying the manifest.

Document that review and prune should be completed before classification. A
manifest mark alone is not a processing filter; it becomes permanent only when
prune removes the file and adds the ID to the ignore list.

## 6. Sensitive-information scan

### 6.1 Invocation modes

Sensitive scanning operates in two modes:

**Automatic scan (default):** When `config/sensitive_patterns.json` exists,
manifest generation automatically scans new and content-changed chats — those
whose `content_hash` differs from the value stored in the previous manifest or
that have no previous manifest row. This targets only the chats that need
scanning rather than the full corpus. If the configuration file is absent, no
scanning occurs and no error is raised.

**Full re-scan (`--scan-sensitive`):** Re-scans every chat against the current
rules, regardless of whether its content has changed:

```bash
python review_chats.py --scan-sensitive
```

Use this after updating `config/sensitive_patterns.json` to apply new or
modified rules retroactively. The flag authorizes opening and scanning every
chat file.

### 6.2 Configuration

Add a tracked template:

```text
config/sensitive_patterns.example.json
```

The user's local file remains untracked:

```text
config/sensitive_patterns.json
```

Add the local file to `.gitignore`. Use safe aliases for both regex rules and
literal keywords:

```json
{
  "patterns": {
    "email": "[\\w.+-]+@[\\w-]+\\.[\\w.-]+",
    "phone": "\\b\\d{3}[-.\\s]?\\d{3}[-.\\s]?\\d{4}\\b"
  },
  "keywords": {
    "family_name": "private literal value",
    "home_address": "private address value"
  }
}
```

Only aliases such as `email`, `family_name`, and `home_address` appear in the
manifest. Never persist or print the configured literal, matched value, full
line, or surrounding chat text.

### 6.3 Matching behavior

- Compile every regex before scanning any chats and fail clearly on invalid
  configuration.
- Match literal keyword values case-insensitively as substrings; do not interpret
  them as regexes.
- Scan the title and all turn text in the source-agnostic chat JSON shape.
- Record each matching alias at most once per chat.
- Sort flag aliases deterministically before joining them for CSV output.
- Do not cache results in SQLite in the first version. A requested scan always
  reflects the current chats and current rule file.
- If `--scan-sensitive` is supplied and the local configuration is missing or
  malformed, fail without replacing the existing manifest.

## 7. Module responsibilities

### `review_chats.py`

- Parse CLI flags (`--scan-sensitive`, `--mark-reviewed`).
- Load configuration and synchronize chat metadata.
- Parse and validate an existing review manifest.
- Detect content changes via `chat_fingerprint()` comparison against stored
  `content_hash` values and set `reviewed=RE-REVIEW` where applicable.
- Automatically scan new and changed chats when
  `config/sensitive_patterns.json` exists; scan all chats when
  `--scan-sensitive` is supplied.
- Generate the updated CSV atomically.
- On `--mark-reviewed`, display a confirmation summary and collapse all review
  states to `YES`.
- Never delete chat, database, classification, ignore-list, or vault state.

### `prune_chats.py`

- Parse and validate explicit deletion actions from the review manifest.
- Combine marked candidates with existing orphan discovery.
- Display the combined dry-run and enforce the existing safety threshold.
- Safely delete verified JSON files for marked candidates.
- Own the existing database, ignore-list, and vault cascade.
- Remove successfully pruned manifest rows after an executing prune run.
  Does not modify review states.

### `common.py`

- Keep generic CSV-independent review behavior out of shared utilities.
- Add shared helpers only where both commands need the same path validation or
  candidate behavior.
- Do not add review-state or sensitive-scan columns to `chats`.

## 8. Tests and acceptance criteria

### Review-manifest tests

- Generates the expected header with `content_hash` column and one correctly
  quoted row per chat.
- Places `action` immediately before `title`.
- On first generation, sorts by newest creation timestamp and conversation ID.
- Defaults new chats to `reviewed=NO, action=KEEP` with the current content
  hash.
- Adds later chats in a newest-first block above every existing row.
- Detects a previously `reviewed=YES` chat whose `content_hash` has changed
  and sets `reviewed=RE-REVIEW`, moving it to the top block.
- Does not change `reviewed` for chats whose `content_hash` is unchanged.
- Does not change `reviewed` for `NO` or `RE-REVIEW` chats even if content
  changes (they are already flagged for review).
- Preserves existing relative order, actions, and reviewed flags across
  regeneration when content is unchanged.
- Removes no-longer-tracked chats during regeneration.
- Refreshes changed titles, metadata, and `content_hash` without changing
  action or review state (unless triggering `RE-REVIEW`).
- Handles commas, quotes, newlines, and Unicode titles through CSV quoting.
- Rejects missing columns, duplicate IDs, empty IDs, unknown actions, invalid
  reviewed flags (including invalid `RE-REVIEW` spelling), and malformed CSV
  without overwriting the existing manifest.
- Atomically replaces the manifest after successful generation.

### Mark-reviewed tests

- Displays a confirmation summary with counts of `NO`, `RE-REVIEW`, and `YES`
  chats.
- On confirmation, collapses all `NO` and `RE-REVIEW` rows to `YES`.
- Does not change `action` values.
- Does not modify the manifest if the user declines the confirmation.
- Atomically replaces the manifest.

### Prune tests

- Existing orphan-only behavior remains unchanged when no manifest exists.
- Dry-run reports marked chats without mutating files, database rows, ignore
  entries, classifications, or vault notes.
- Candidate discovery returns the union of orphans and `DELETE` rows.
- A candidate that is both orphaned and marked is processed once.
- New database chats absent from an old manifest are never deletion candidates.
- Stale manifest IDs are reported and skipped.
- Invalid manifest content aborts pruning before mutation.
- Pruning a marked chat deletes its verified JSON file, classification, chat
  row, and vault note, and then adds it to the ignore list.
- A successful prune removes the corresponding row from the manifest.
- A successfully pruned manual-file orphan is also removed from the manifest,
  even when its action was `KEEP`.
- A failed marked deletion remains `DELETE` in the manifest for retry.
- Stale `DELETE` rows are removed when the manifest is finalized.
- Prune does not change `reviewed` values for any retained rows.
- Running `--prune` with no deletion candidates exits cleanly without modifying
  the manifest.
- Dry-run mode never changes actions, reviewed flags, or row order.
- Finalization preserves retained-row order, review states, and replaces the
  CSV atomically.
- Path traversal, a non-JSON source file, conversation-ID mismatch, malformed
  JSON, and file-deletion failure leave that chat's database state intact.
- An interruption after JSON removal is recoverable as an ordinary orphan.
- The combined candidate count uses the existing large-delete threshold.
- `--confirm-large-delete` permits an intentional large combined prune.

### Sensitive-scan tests

- When `config/sensitive_patterns.json` exists, new and content-changed chats
  are automatically scanned during manifest generation.
- `--scan-sensitive` scans all chats regardless of content changes.
- When the configuration file is absent and `--scan-sensitive` is not supplied,
  no scan runs and no error is raised.
- Configured regex and literal keyword rules flag matching chats.
- Matching is case-insensitive for literal keywords.
- Only safe aliases appear in CSV and logs.
- Duplicate matches produce one flag per alias.
- Missing configuration with `--scan-sensitive`, invalid JSON, invalid schema,
  empty aliases or values, and invalid regexes fail without replacing the
  manifest.
- A later scan reflects chat or rule changes without stale cached results.

### Repository verification

Before considering implementation complete:

```bash
pytest -v
ruff check .
```

Update `docs/CLI.md`, `docs/ARCHITECTURE.md`, and relevant configuration
documentation to describe the new optional stage and the fact that prune now
combines manual file deletions with explicit manifest marks.

## 9. Assumptions and selected defaults

- The manifest uses explicit `KEEP` and `DELETE` actions; missing rows do not
  carry deletion meaning.
- `action` appears immediately before `title` for side-by-side review.
- New chats enter at the top as `reviewed=NO, action=KEEP` with the current
  `content_hash`; existing rows keep their relative order unless promoted to
  `RE-REVIEW`.
- `content_hash` in the CSV tracks the `chat_fingerprint()` value at the time
  the row was last written. A mismatch against the current database value
  triggers `RE-REVIEW` for previously reviewed chats.
- `review_chats.py --mark-reviewed` is the explicit review-pass completion
  point: it collapses all review states to `YES` after user confirmation.
- Prune removes successfully deleted rows from the manifest but does not change
  review states for retained rows.
- Failed `DELETE` rows remain visible for retry rather than being discarded from
  the manifest.
- Title-only review is sufficient for the first version. Opening prompts and
  summaries are deferred.
- CSV is preferred over fixed-width text because it provides unambiguous parsing
  while remaining editable in a text editor or spreadsheet.
- Sensitive scanning is automatic for new and changed chats when the local
  configuration exists; `--scan-sensitive` forces a full re-scan.
- Sensitive keyword aliases are safe to display; configured values are private.
- The default review CSV and local scan configuration are generated/private
  state and must never be committed.
- Review marks are not stored in SQLite and do not affect extraction until
  prune succeeds.
- `prune_chats.py` remains the sole deletion authority and retains its existing
  dry-run, safety-threshold, ignore-list, and vault-cleanup semantics.
