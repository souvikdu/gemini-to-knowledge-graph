# Changelog

All notable changes to this project are documented in this file. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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