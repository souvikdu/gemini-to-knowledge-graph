# Architecture Reference

For *why* the pipeline is shaped this way, see [DESIGN_NOTES.md](DESIGN_NOTES.md).
This doc covers *what's actually in the codebase*.

```
Extract ──> Classify ──> Vault
```

---

## Shared utilities (`common.py`)

All three stages draw on a shared library, `common.py`, so config-loading,
hashing, and database logic never drift between scripts:

- **`load_config()`** — loads `config/config.json` with clear error messages
  for every missing or malformed key (`api.*`, `limits.*`, `node_sizing.*`,
  vault paths — each validated only by the stage that needs it)
- **`load_topics()`** — loads `config/topics.json` and returns lookup maps
  for normalizing/canonicalizing LLM output against the taxonomy
- **`chat_fingerprint(chat)`** — SHA-256 hash of a chat's title and turn
  text, used to detect when a chat's content has changed
- **`canonicalize_topic(name)`** — folds casing/whitespace variants of a
  known topic back to one canonical spelling
- **`dedup_case_insensitive(items)`** — deduplicates a list of strings
  case-insensitively, preserving the first occurrence's casing
- **`iter_chats(chats_dir)`** — generator that yields `(filepath, chat_dict)`
  for every `*.json` file in `chats/`, used by all downstream stages
- **`load_existing_vault_state(convos_dir)`** — scans an existing vault's
  `Conversations/` folder and returns a dict of `{cid: (notename, signature)}`
  for resume/rewrite-in-place detection
- **`yaml_str(value)`** — safely quotes a string for YAML frontmatter
- **`get_db_connection(cfg)` / `init_db(conn)`** — open (and create on first
  use) the SQLite database backing classifications, the `chats` metadata
  table, and `ignored_conversations`
- **`upsert_classification` / `load_all_classifications` / `delete_classifications`**
  — insert/replace, bulk-load, and delete rows in the `classifications` table
- **`add_ignored_conversations` / `load_ignored_conversations` / `remove_ignored_conversations`**
  — manage the `ignored_conversations` table: conversation IDs the extractor
  should skip on future runs (populated by `prune_chats.py`)
- **`upsert_chat` / `get_chat_row` / `get_chat_rows`** — insert/replace,
  single-lookup, and batched-lookup helpers for the `chats` table. The
  extractor writes here after every download; `prune_chats.py` deletes from
  here when pruning.
- **`sync_chats_to_db(conn, chats_dir)`** — scans `chats/*.json` using
  `os.scandir` + mtime pre-filtering, inserts or updates the `chats` table
  for any file whose `chat_fingerprint()` has changed. Called at the start
  of `classify_chats.py`, `obsidian_layout.py`, and `prune_chats.py`'s
  normal prune flow, so every stage's view of the DB is current before it acts.
- **`find_orphaned_cids` / `exceeds_prune_safety_threshold`**
  — shared helpers for `prune_chats.py`: find chat IDs with no corresponding
  file on disk, and safety-check the deletion ratio against a configurable
  threshold

---

## Extractor package (`extractors/`)

Each chat source has its own module under `extractors/`, sharing a common
write pattern from `extractors/base.py`:

- **`extractors/base.py`** — provides `upsert_chat_and_file()`, the shared
  "write JSON + set mtime + upsert DB" helper used by every extractor
- **`extractors/gemini.py`** — Gemini Web extraction logic, run via
  `python -m extractors.gemini`

---

## Vault structure

```
Category Hub (e.g. "Physics & Astronomy")
    └── Topic Note (e.g. "Gravitational Acceleration")
            └── Conversation Note (the full chat)
```

Conversations never link directly to categories — the graph is always
Category → Topic → Conversation.

Notable behaviors:

- **YAML frontmatter** on every conversation note: `created`, `updated`,
  source, `turn_count`, `word_count`, `categories`, `topics`,
  `note_signature`, `node_size`, etc.; tags include source tags
  (`source/gemini_web`); graph-view color groups; summary excerpts;
  `[!quote]` callouts with configurable user/assistant labels
- **Smart staleness detection:** `note_signature()` — a combined hash of
  chat content *and* classification outcome — so a chat whose text is
  unchanged but whose classification just went from `error` to `ok` still
  gets its note rewritten
- **Link placeholder resolution:** Gemini's `[Product Name](_link)`
  patterns are rewritten to real browser search links (configurable via
  `obsidian.search_url`)
- **File timestamps:** each conversation note's on-disk mtime is set to
  the conversation's own timestamp for correct filesystem ordering
- **Deduplication:** category and topic names are deduplicated
  case-insensitively at every stage
- **Uncategorized fallback:** if every topic a chat was assigned collides
  with a category name (and gets filtered to avoid an ambiguous wikilink),
  the chat falls back to a generic `Uncategorized (<Category>)` topic
  instead of losing its topic links entirely
- **Topic filename collision resolution:** two topic names that map to the
  same safe filename (e.g. "Data Science" vs "Data science") are resolved
  via a global pre-pass that scans all classifications, groups colliding
  topics by their safe filename, and deterministically picks one canonical
  spelling for each group. Category name collisions (which come from
  static config) are checked at startup and fail with a clear error since
  they cannot be auto-resolved.

---

## JSON contract

Stages 2 and 3 read a standard chat shape from `chats/*.json`. The
extractor writes this format; downstream stages don't care where it came
from — this is what makes the extractor package source-agnostic:

```json
{
  "conversation_id": "gemini_c_0b2b2434ededef14",
  "source": "gemini_web",
  "title": "Planning a Weekend Hiking Trip",
  "created_at": "2026-06-19T18:13:52+00:00",
  "updated_at": "2026-06-19T18:13:52+00:00",
  "turn_count": 2,
  "turns": [
    { "turn_number": 1, "role": "user", "timestamp": "...", "text": "..." },
    { "turn_number": 2, "role": "model", "timestamp": "...", "text": "..." }
  ]
}
```

`role` can be any string — the vault builder gives `user` a quote-callout
block and `assistant`/`model` a labeled response block; anything else is
rendered as plain text.

---

## Project Structure

```
gemini-to-knowledge-graph/
├── common.py                       # Shared helpers used by all stages
├── extractors/                     # Extraction modules
│   ├── __init__.py
│   ├── base.py                     # Shared upsert_chat_and_file() helper
│   └── gemini.py                   # Gemini Web extraction
├── classify_chats.py               # Stage 2 — LLM classification
├── obsidian_layout.py              # Stage 3 — Vault builder
├── prune_chats.py                  # Orphan cleanup from DB + vault
├── config/
│   ├── config.example.json         # Template — copy to config.json
│   ├── config.json                 # Your local config (gitignored)
│   ├── topics.example.json         # Template — copy to topics.json
│   ├── topics.json                 # Category/topic taxonomy (gitignored)
│   └── prompts/
│       └── classifier.md           # LLM prompt template
├── checkpoint/
│   └── extraction_state_gemini.json  # Extraction progress checkpoint
├── chats/                          # Extracted conversations (gitignored)
├── docs/
│   ├── CONFIGURATION.md            # Full config reference
│   ├── CLI.md                      # Flags, resuming, pruning
│   ├── ARCHITECTURE.md             # This file
│   ├── DESIGN_NOTES.md             # Why the pipeline is shaped this way
│   └── images/
│       └── graph-preview.png
├── Obsidian_Vault/                 # Generated Obsidian vault (gitignored)
│   ├── .obsidian/                  # Editor config, plugins (graph.json, custom-sort)
│   ├── Concepts/
│   │   ├── Categories/            # MOC notes per category
│   │   └── Topics/                # MOC notes per topic
│   ├── Conversations/             # Individual conversation notes
│   └── sortspec.md
├── tests/
│   ├── conftest.py
│   ├── test_common.py
│   ├── test_classify_chats.py
│   ├── test_obsidian_layout.py
│   ├── test_prune_chats.py
│   └── extractors/
│       ├── test_base.py
│       └── test_gemini.py
├── chat_topics.db                 # SQLite: classifications + ignore list (gitignored)
├── .env.example
├── .gitignore
├── pytest.ini
├── requirements.txt
├── requirements-dev.txt
├── CONTRIBUTING.md
└── README.md
```