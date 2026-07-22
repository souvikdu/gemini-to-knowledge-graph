# Code Review Punch List

Source: external review of the v1.0.0 worktree (commit `ecc5b58`), cross-checked
line-by-line against `common.py`, `classify_chats.py`, `obsidian_layout.py`,
`prune_chats.py`, `base.py`, `gemini.py`, and the published README.

Items are grouped by target version. Within each version they're ordered
highest severity first. Each item has a one-line fix direction — not a full
spec — consistent with `DESIGN_NOTES.md`'s style of recording *why*, not
just *what*.

---

## v1.0.1 — Patch (correctness fixes, no schema/contract changes)

Everything here is a real bug with a small, additive fix. No breaking
changes to config, DB schema, or the LLM response contract.

- [x] **`prune_chats.py` cascade isn't atomic** (`_do_prune`, ~L56-92)
  Each step (`delete_classifications`, `chats` DELETE, `add_ignored_conversations`)
  commits separately. A kill mid-sequence leaves an orphan undiscoverable on
  retry, and lets the extractor re-fetch something half-deleted.
  **Fix:** wrap the three steps in one `with conn:` transaction.

- [x] **`--force` deletes the vault before validating rebuild inputs** (`obsidian_layout.py`, `_prepare_vault`)
  `shutil.rmtree` runs before `load_topics()`, before the "no classifications
  found" check, and before the `chats_dir` existence check. Point `--force`
  at an empty/misconfigured setup and the vault is gone for nothing.
  **Fix:** move all validation checks before the `if force:` deletion block.

- [ ] **Unreadable JSON is treated as "deliberately deleted"** (`common.py: iter_chats`, `find_orphaned_cids`)
  `iter_chats()` silently skips files it can't parse. `find_orphaned_cids()`
  builds its "still exists" set from the same generator, so a corrupt file
  looks orphaned and can get pruned + permanently ignore-listed.
  **Fix:** have the prune scan collect parse failures separately and abort
  (with a clear message) if any known chat file is unreadable, instead of
  silently treating it as gone.

- [ ] **Incomplete/empty download doesn't count as an error** (`gemini.py`, main loop)
  `if not raw_turns: ... continue` skips the `errors` counter. Other
  successful downloads can still advance the checkpoint past a chat that
  never actually saved, so it's never retried.
  **Fix:** increment `errors` (or push to a small retry queue) whenever
  `_read_chat_turns()` returns `None`/empty.

- [ ] **Topic/category note filenames can collide** (`obsidian_layout.py`, `_write_topic_notes` / `_write_category_notes`)
  `make_safe_filename()` has no collision handling for hub notes (unlike
  conversation notes, which use `resolve_note_action`'s `used_filenames`
  allocator). Two differently-named topics sanitizing to the same string
  silently overwrite each other.
  **Fix:** build one safe-name map with a numeric suffix on collision,
  same pattern already used for conversations.

- [ ] **`api.model` isn't validated** (`common.py`, `API_KEYS`)
  Missing from the required-keys list, but read directly as `api_cfg["model"]`
  elsewhere — a missing key produces a raw `KeyError` instead of your usual
  friendly `die()` message.
  **Fix:** add `"model"` to `API_KEYS`.

- [x] **README overclaims on `--unignore`** (README troubleshooting table)
  States flatly "the next extraction will re-download it," but `unignore()`'s
  own runtime warning is much more conditional — it depends on a new message
  bumping the chat past the current pagination checkpoint, or manually
  resetting `last_timestamp` to 0.
  **Fix:** align the README wording with the caveat already in the code's
  own log output.

---

## v1.1.0 — Minor (data integrity & light architecture)

Slightly more involved, but still additive — no breaking changes to
existing vault notes, DB rows, or the classifier's response format.

- [ ] **`note_signature()` misses render-affecting inputs** (`obsidian_layout.py`)
  Only hashes `chat_fingerprint()` (title+turns) plus classification fields.
  Changing `display_names`, `obsidian.search_url`, or `node_sizing` in
  config doesn't change the signature, so existing notes silently stay
  stale after a config change.
  **Fix:** fold a canonical dict of render-context config values into the
  signature hash.

- [ ] **Parser fallback can silently accept malformed output** (`classify_chats.py`, `parse_response` / positional recovery)
  E.g. `"Sure:\nTopic: Python\nSummary: ..."` parses without error — `"Sure:"`
  becomes the category, gets stored as `status: "ok"`, and only falls back
  to General Knowledge later in `obsidian_layout.py` with a warning. No
  retry is triggered even though the response shape was ambiguous.
  **Fix:** validate positionally-recovered categories against the known
  taxonomy before accepting; treat an unrecognized recovery as a parse
  failure and retry instead of silently storing it.

- [ ] **Extractor writes aren't atomic** (`base.py: upsert_chat_and_file`)
  Plain `open(path, "w")` — an interruption mid-write can leave a truncated
  JSON file while the DB still marks the conversation current, so the next
  extraction run may skip re-downloading it.
  **Fix:** write to a sibling temp file, flush, set mtime, `os.replace()`
  into place.

- [ ] **Topic-to-category ownership isn't recorded** (`common.py` classifications table, `classify_chats.py` parsing, `obsidian_layout.py` topic→category assignment)
  Categories and topics are stored as independent JSON arrays. A coined
  topic has no recorded parent category — the vault assigns it to the
  first category of whichever chat happens to reference it first
  (filename-sort order), which can produce a stable but wrong hierarchy.
  **Fix:** add a `classification_topics(conversation_id, topic, category, ordinal)`
  table alongside the existing columns (additive, no prompt/parser contract
  change needed — keep the 3-line LLM response format as-is).

- [ ] **Path-key validation is coupled across all stages** (`common.py`, `PATH_KEYS` / `load_config`)
  Every stage's config must include `vault_dir`, `prompt_file`, and
  `extraction_state_file` even if that stage never reads them, despite
  `api`/`limits`/`node_sizing` already being validated conditionally via
  `require_*` flags.
  **Fix:** make the same three path keys conditionally required, gated by
  which stage is loading config — no need for full per-stage schema classes.

---

## Backlog — deferred (larger effort or genuinely optional)

Real observations, but bigger lifts or lower urgency for a solo,
pre-widescale, git-clone-distributed tool. Revisit when they'd actually
block something.

- [ ] **200-turn cap on Gemini extraction** (`gemini.py`, `_read_chat_turns(limit=200)`)
  No pagination and no warning when a conversation hits exactly 200 turns —
  older content in long chats is silently omitted. Needs either read-API
  pagination (if the underlying library supports it) or at minimum a flag
  marking a maxed-out response as incomplete.

- [ ] **Truncation fragility on edge-case config** (`classify_chats.py`, `truncate_to_budget`)
  `truncate_head_fraction >= 1` (or a very coarse `len(text)//4` token
  estimate on CJK/emoji-heavy chats) can produce output that exceeds the
  input budget. Not a problem at current default (0.65), but unguarded.
  Add a validation check + consider a real tokenizer where available.

- [ ] **Failed vault-note deletion during prune is swallowed** (`prune_chats.py`, `_delete_vault_note`)
  Exception is caught and just lowers the `removed_vault` count — no
  explicit warning or retry path for that specific note. Low-frequency
  failure mode; add a warning log when it happens.

- [ ] **Test coverage gaps** (flagged by the review, all additive)
  - Parser test for preamble + missing labels (exposes the positional
    misparse directly)
  - Malformed-file prune coverage (unreadable known chat should block
    pruning, not become an orphan)
  - `note_signature()` dependency tests (source/date, display names,
    search URL, node size, sanitized hub collisions)
  - Extractor tests that exercise `upsert_chat_and_file`, `_read_chat_turns`,
    pagination, and checkpoint writing directly instead of re-simulating
    the logic

- [ ] **Minor refactors** (no behavior change, pure cleanup)
  - Extract a single `select_todo(chats_dir, records, statuses=None)`
    helper shared by normal and `--retry` selection in `classify_chats.py`
  - Split `_process_conversations` in `obsidian_layout.py` into a pure
    normalize/accumulate layer separate from note rendering
  - Move `gemini.py`'s pagination/parsing helpers to module scope so tests
    can call production code directly
  - Validate the classify-limit CLI arg as strictly positive (currently
    `0` means "no limit" and negatives silently slice from the end)

---

## Reviewed, no action needed

The review flagged these as "assessment" items and I agree — they're
reasonable as-is for the current scale and distribution model:

- `base.py`'s extractor pattern doesn't need an abstract base class; a
  new source just needs to emit the JSON contract and call the shared
  write helper.
- The SQLite schema is appropriately denormalized apart from topic
  ownership (above). `idx_status` is currently unused since retry mode
  loads everything and filters in Python — fine at current data volumes,
  worth revisiting only if `classifications` grows to the point where a
  full table scan is noticeably slow.
- `common.py`'s breadth (config, fingerprints, scanning, SQLite) is
  legitimate cross-stage infrastructure, not a module that's grown beyond
  its purpose. Splitting out a `storage.py` would be worth it if more
  sources/tables arrive — premature otherwise.