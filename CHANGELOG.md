# Changelog

All notable changes to this project are documented in this file. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.1] - 2026-07-21

### Fixed

- **`--force` no longer destroys vault before validation fails** (`obsidian_layout.py`). Running `obsidian_layout.py --force` with a broken config (missing `topics.json`, missing classifications, wrong `chats_dir`) previously deleted the vault first and then failed. Now validation runs first — the vault is untouched if the rebuild would fail.
- **Prune cascade is now atomic** (`prune_chats.py`). The three-step deletion (classifications → chats table → ignore list) was split across three separate commits. If the process was killed mid-sequence, remaining orphan records could become undiscoverable on retry. The steps are now wrapped in a single transaction — all three succeed together or none do.

### Added

- **`commit=False` parameter** on `delete_classifications()` and `add_ignored_conversations()` in `common.py`, enabling callers to manage their own transactions.

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