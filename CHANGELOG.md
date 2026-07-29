# Changelog

All notable changes to this project are documented in this file. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.3.1] - 2026-07-29

### Added

- **`ASSISTANT_INSTRUCTIONS.md` — AI-guided walkthrough for the entire
  pipeline** ([#28](https://github.com/souvikdu/gemini-to-knowledge-graph/issues/28)):
  new file that users can attach as context to any AI coding assistant
  (Copilot, Claude, ChatGPT, etc.) for a step-by-step guided setup and
  usage walkthrough — no need to read through all the docs first. Covers
  every pipeline stage, environment setup, config, review flow, masking,
  common troubleshooting, and plugin recommendations.
- **README Quick Start callout linking to `ASSISTANT_INSTRUCTIONS.md`**:
  visible signpost in the Quick Start section so new users immediately
  see the guided-walkthrough option.

## [1.3.0] - 2026-07-29

### Added

- **`--mask-sensitive` / `--apply` flags for `review_chats.py`** ([#26](https://github.com/souvikdu/gemini-to-knowledge-graph/issues/26)):
  redacts matched sensitive spans in reviewed-and-kept chats instead of
  forcing an all-or-nothing keep/delete choice. Preview-only without `--apply`;
  with `--apply`, sensitive text in `title` + `turns[].text` is replaced by
  `[REDACTED:{alias}]` markers. `credit_card` and `email` use partial masks
  (last 4 digits / domain visible); all other aliases get full redaction.
  Includes automatic mtime preservation to avoid checkpoint corruption, and
  manifest `content_hash` / `flags` update to prevent spurious RE-REVIEW
  flags on the next manifest refresh.

- **Luhn validator for `credit_card` pattern**: the `credit_card` regex
  is now a deliberately loose `\b(?:\d[ -]?){12,18}\d\b` (13–19 digit
  candidates) paired with a Luhn check in both `scan_chat_file()` and
  `scan_chat_file_verbose()` to filter false positives. Existing manifests
  with stale `credit_card` flags require `--scan-sensitive` to re-validate.

### Fixed

- **Email partial-mask format changed to avoid regex re-match loop**:
  the previous `[REDACTED:email ...@domain]` format preserved the `@`
  prefix, which caused the email regex to re-trigger on already-masked
  text during re-scans. Changed to `[REDACTED:email domain=...]` so the
  preserved fragment no longer matches the email pattern.

### Changed

- The example `credit_card` regex in `config/sensitive_patterns.example.json`
  was replaced with the new wider pattern.

## [1.2.0] - 2026-07-26

### Added

- **Review stage** (`review_chats.py`): a new optional stage between extraction
  ([#24](https://github.com/souvikdu/gemini-to-knowledge-graph/issues/24))
  and classification. Generates an editable CSV manifest
  (`review/chats_to_review.csv`) so you can skim titles, flag sensitive content,
  and mark chats for deletion without opening a single JSON file. Includes:
  - **Sensitive-info scanning** against configurable regex patterns and keyword
    lists from `config/sensitive_patterns.json`. Only alias names (e.g.
    `email`, `family_name`) are ever persisted — never the matched text.
  - **Incremental scanning by default** — only new and content-changed chats
    are scanned on each run. `--scan-sensitive` forces a full re-scan when
    pattern rules are edited.
  - **Review lifecycle** — `reviewed` moves through `NO → YES → RE-REVIEW`.
    `--mark-reviewed` collapses all pending rows to `YES` in one batch action.
  - **Match inspection** — `--show-sensitive <id>` / `--show-sensitive-all`
    display the actual matched context on demand, without writing sensitive
    data to disk.

- **Sensitive-patterns example config** (`config/sensitive_patterns.example.json`):
  template with email, phone, credit-card, IPv4/IPv6, and personal-info keyword
  categories (name, address, birth date, etc.) as a starting point.

- **Prune integration with review manifest** (`prune_chats.py`): reads
  `DEL`-marked rows from the review manifest via
  `read_and_validate_manifest()` and folds them into its own
  candidate-discovery step alongside file orphans. The full cascade —
  classifications DB row, `chats` table entry, vault note, JSON file, ignore
  list — stays in one script, with a single safety gate for both mechanisms.

- **`truncate_title()`** (`common.py`): shared helper that truncates a chat
  title to 120 characters with `...` suffix. Used by review, classify, and
  vault stages for compact log and manifest output.

- **Tests** (`tests/test_review_chats.py`, `tests/test_prune_chats.py`):
  comprehensive coverage for the review manifest lifecycle, sensitive-info
  scanning, prune-discovery from both orphans and manifest rows, cascade
  safety, and manifest rewrite after pruning.

### Changed

- **`prune_chats.py` refactored** — imports `read_and_validate_manifest()` and
  `write_manifest_atomically()` from `review_chats.py`; discovers deletion
  candidates from both file orphans and manifest `DEL` rows; rewrites the
  manifest after pruning to drop successfully-pruned and stale rows.

### Documentation

- **`docs/ARCHITECTURE.md`** — added `truncate_title()` to shared utilities
  list; rewrote the Review stage section with clearer subsections (manifest,
  scanning, lifecycle, inspection, prune coupling); clarified that
  `--scan-sensitive` is only needed when pattern rules are edited.
- **`docs/CLI.md`** — full review-stage command reference with every flag;
  manifest column semantics (action, reviewed, flags); recommended workflow;
  troubleshooting entries for manifest issues, stuck RE-REVIEW, CSV
  permission errors, and stale flags after pattern edits.
- **`docs/CONFIGURATION.md`** — full sensitive-pattern scanning setup guide
  with schema reference and alias-only persistence guarantee; clarified that
  review can run at any time and prune handles downstream cleanup.
- **`docs/DESIGN_NOTES.md`** — review design decisions (explicit KEEP/DEL,
  manifest as staging not state, RE-REVIEW semantics, manifest sort order,
  incremental scanning); reframed "deliberately dumb" matcher rationale to
  emphasize false-negative avoidance.
- **`README.md`** — updated review-stage callout to reflect it can run at
  any time, recommended before classification.

## [1.1.0] - 2026-07-24

### Added

- **`api.model` field is now validated in config** (`common.py`). `load_config()`
  checks that `api.model` is present in `config/config.json` and exits with a
  clear error if missing. The field was already referenced in several places but
  never explicitly validated. ([#18](https://github.com/souvikdu/gemini-to-knowledge-graph/issues/18))

- **Strip inline image tags from vault notes** (`obsidian_layout.py`). Gemini's exported JSON sometimes includes `<Image .../>` placeholders
  that get rendered as raw XML in the vault note. A new
  `_strip_generated_image_tags()` helper replaces them with an Obsidian
  `[!info]-` callout preserving the original caption or alt text.
  Called alongside `_resolve_link_placeholders()` in the turn loop.

- **Contributing guide** (`docs/CONTRIBUTING.md`). Lightweight guide
  covering setup, PR workflow, testing expectations, and project
  philosophy for contributors.

### Fixed

- **Broken README links to docs files**. Renamed three docs files
  `docs/Architecture.md` → `docs/ARCHITECTURE.md`,
  `docs/Cli.md` → `docs/CLI.md`, and
  `docs/Configuration.md` → `docs/CONFIGURATION.md` to match the
  uppercase references already used in `README.md`.

### Changed

- **README restructured for clarity** (`README.md`). Reorganized the
  document into a logical flow that mirrors the actual pipeline stages
  (Extract → Classify → Vault → Prune) and grouped setup, configuration,
  and troubleshooting sections by audience. Consolidated duplicate
  flag-reference tables, moved the architecture diagram earlier, and
  added a quick-start section so new users can go from clone to first
  vault note in under a minute.

- **Ruff updated to 0.16.0 with project-level config** (`requirements-dev.txt`, `ruff.toml`).
  Upgraded ruff from 0.15.x to 0.16.0 and added `ruff.toml` that
  ignores three low-value rules for this codebase (BLE001, ASYNC230,
  S112) — catching broad `Exception` is intentional in resilient CLI
  scripts, blocking `open()` in async functions has negligible impact
  for a local extraction tool, and bare `except: continue` is handled
  by adjacent logging patterns. All other rules remain active.

## [1.0.2] - 2026-07-22

### Fixed

- **Hub-note filename collision across classified chats** (`obsidian_layout.py`, `common.py`). Two distinct topic names (e.g. "Data Science" vs "Data science") that produce the same `make_safe_filename()` output would silently overwrite each other's hub-note. Added a global pre-pass `build_topic_filename_map()` that scans all classified chats after DB load, groups topics by safe filename, and picks the alphabetically-first spelling for colliding groups. Non-destructive — no DB writes or schema changes. ([#12](https://github.com/souvikdu/gemini-to-knowledge-graph/issues/12))
- **`prune_chats.py` log message pointed at wrong path** (`prune_chats.py`). The `--unignore` log output referenced `last_timestamp` instead of the actual field `last_timestamp_regular`, and `config/extraction_state_gemini.json` instead of the actual path `checkpoint/extraction_state_gemini.json`.
- **`README.md` overclaimed on `--unignore` re-download guarantee** (`README.md`). The troubleshooting table and flag documentation stated re-download happens unconditionally on next extraction; now correctly reflects that it depends on the chat's timestamp passing the current checkpoint.

### Changed

- **`obsidian_layout.py` no longer requires classifications to run** (`obsidian_layout.py`). Previously the vault builder hard-exited with "No classifications found" when `classifications` table was empty. Now it proceeds and writes every conversation as an unclassified markdown note with full text, frontmatter, and source tags. Users can run Stage 1 + Stage 3 without an LLM, then add `classify_chats.py` later to build the graph hierarchy.
- **Truncation reframed as a deliberate trade-off** (`README.md`). The "known limitation" wording around chat-splitting is replaced with a description of why the ~350-token summary doesn't need the full transcript, and a note that the full conversation text is preserved untouched in the vault note regardless of truncation during classification.

### Added

- **Category seed collision check** (`obsidian_layout.py`). `_prepare_vault()` now dies early with a clear error if two categories in `config/topics.json` produce the same safe filename (not auto-resolvable since categories are static config).
- **`build_topic_filename_map()` tests** (`tests/test_obsidian_layout.py`). 7 tests covering no collisions, case-only collisions, safe-char stripping collisions, multiple groups, category-name exclusion, error-status skipping, and empty input.

## [1.0.0] - 2026-07-20

Initial release.

### Added

- **Extract** — `extractors/gemini.py`: cookie-based Gemini Web chat
  history extraction; raw batch-execute parsing that preserves empty-text
  (attachment-only) user turns the library's own `read_chat()` drops;
  timestamp-checkpointed resume for regular chats; always-full fetch with
  automatic pin-status reconciliation for pinned chats; source-agnostic
  JSON contract written to `chats/*.json`.
- **Classify** — `classify_chats.py`: single-pass LLM classification
  (1–2 categories, 2–5 topics, a 1–2 sentence summary) against a seed
  taxonomy (`config/topics.json`); works against any OpenAI-compatible
  `/v1/chat/completions` endpoint, local or cloud; two-pass response
  parser (strict label matching, then positional fallback for unlabeled
  replies); automatic remapping when the model answers with a topic in
  the category slot; differentiated retry strategy (backoff for
  transient failures, fast retry with no backoff for parse failures).
- **Vault** — `obsidian_layout.py`: strict Category → Topic → Conversation
  hierarchy with no direct category-to-chat shortcuts; sqrt-compressed
  node sizing per tier; `note_signature()`-based incremental resume that
  rewrites a note when either its content or its classification outcome
  changes; Gemini link-placeholder (`[label](_link)`) resolution to
  browser search links; `Uncategorized (<Category>)` fallback so a chat
  never silently loses its topic links; generated `.obsidian/graph.json`
  color groups and `sortspec.md`.
- **Prune** — `prune_chats.py`: standalone cascade-delete for orphaned
  conversations (classifications → `chats` row → ignore list → vault
  note), catching orphans regardless of whether they were ever
  classified; dry-run by default; `--confirm-large-delete` safety gate
  at a configurable orphan ratio; `--list-ignored` / `--unignore`
  management commands.
- **Shared state** — SQLite-backed `classifications`, `chats` (a
  metadata index, not a content copy), and `ignored_conversations`
  tables via `common.py`; `sync_chats_to_db()` safety-net sync called at
  the start of `classify_chats.py`, `obsidian_layout.py`, and
  `prune_chats.py`.
- **`common.py`** — shared config and taxonomy loading with actionable,
  fix-it-oriented error messages instead of raw tracebacks; chat
  fingerprinting; case-insensitive category/topic dedup; vault-state
  scanning for resume.
- **Docs** — `README.md` and `docs/DESIGN_NOTES.md` covering setup,
  configuration, flags, troubleshooting, and the reasoning behind key
  design decisions (including a few approaches considered and rejected).
- **Tooling** — `ruff` for linting (`ruff check`; CI runs
  `ruff check --output-format=github`); a pytest suite covering
  `common.py`, the classifier's parsing/truncation/resume logic, the
  vault builder's staleness detection and pure helpers, `prune_chats.py`'s
  orphan detection, and the extractor package.

### Known limitations

- No chat-splitting: conversations that don't fit
  `api.context_window_tokens` are truncated (start and end kept, middle
  dropped) rather than classified across multiple calls.
- Gemini Web is the only supported extraction source. The `extractors/`
  package is structured to add more, but none are implemented yet.