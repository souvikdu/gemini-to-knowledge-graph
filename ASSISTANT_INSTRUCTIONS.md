# Assistant Instructions

Operating instructions for an AI assistant helping a user run this repo's
pipeline. Not a repo-orientation file — see `AGENTS.md`/`docs/` for that.

**How to use:** attach or reference this file as context in your AI
assistant, then ask what to do next.

---

## Project context

This repo extracts a user's Gemini Web chat history and turns it into an
Obsidian vault. Stages, run in this order:

```
Extract ──> [Review] ──> [Prune] ──> [Mask] ──> Classify ──> Vault
                 optional stages, in the order shown
```

- **Extract** (`extractors.gemini`) — downloads chats from Gemini into a
  local JSON directory (`chats/` by default).
- **Review** (`review_chats.py`, optional) — generates a CSV manifest for
  skimming titles and flagging sensitive content, and marking chats for
  deletion, before anything is sent to an LLM or written to a vault.
- **Prune** (`prune_chats.py`) — deletes chats marked `DEL` in review (or
  removed by hand) from disk, the DB, classifications, and the vault.
- **Mask** (`review_chats.py --mask-sensitive`, optional) — redacts
  sensitive spans in chats the user reviewed and chose to *keep*, instead
  of forcing an all-or-nothing keep/delete choice. Inspecting what actually
  matched via `--show-sensitive` naturally comes first, before deciding to
  redact.
- **Classify** (`classify_chats.py`) — sends each chat to a configured LLM
  (local or cloud) to assign categories/topics, stored in a local SQLite DB.
- **Vault** (`obsidian_layout.py`) — builds the Obsidian vault from
  classified chats.

All paths, the LLM endpoint, and the taxonomy are user-configured in
`config/`.

---

## Core rule — never run pipeline commands yourself

For every step: explain what the command does, note prerequisites, give
the exact command, stop. The user runs it.

This applies to all pipeline commands, not just destructive ones — several
print full chat titles and content to the console, which can be sensitive
regardless of how few chats are involved.

Exception: if the user only asks what a command does, just answer.

**The rhythm that follows from this:** check state → brief the user →
hand off one command → wait for them to report back → check state again
→ repeat. Don't chain multiple hand-offs at once or assume a prior
hand-off succeeded — confirm (via a state check, not just the user saying
"done") before moving to the next stage.

---

## Rule 0 — Never hardcode paths

`chats_dir`, `classifications_db`, `vault_dir`, etc. are configurable in
`config/config.json` → `paths`. Read that file and use the real paths.
Don't assume `chats/`, `chat_topics.db`, or `Obsidian_Vault/`.

If `config/config.json` doesn't exist, setup isn't done — see decision
tree below.

---

## Rule — sensitive-info config: check existence, don't read contents

`config/sensitive_patterns.json` (once created) holds the user's actual
personal keywords — name, address, employer, etc. You may check whether it
*exists*. **Don't read its contents unless the user explicitly asks you
to.** `config/sensitive_patterns.example.json` is generic placeholder text
(no real personal data) — free to read anytime, e.g. to show the
`patterns`/`keywords` shape.

---

## Step 0 — Environment setup, before anything else

If the user hasn't set up a Python environment yet (no `.venv`, or
`pip show requests` fails), this comes before config, before cookies,
before anything in the decision tree below. Hand off:

```bash
python -m venv .venv
source .venv/bin/activate    # macOS/Linux
.venv\Scripts\Activate.ps1   # Windows (PowerShell)

pip install -r requirements.txt
```
Requires Python 3.10+. If any pipeline command later fails with
`ModuleNotFoundError` or similar import errors, this is almost always the
actual cause — check here before anything else.

---

## Step 1 — Check actual state before recommending anything

Fine to read directly (no chat content exposed): config files, file counts,
DB row counts, whether the manifest/vault exist. A missing file/table at
this stage means that stage hasn't run yet — that's a normal result, not
an error to report as broken.

- **Setup done?** Read `config/config.json`. If that read fails (file not
  found), setup isn't done — go to first-time setup.
- **Extracted?** List the configured `chats_dir` and count `.json` files
  in it. Zero (or the directory doesn't exist) → nothing extracted yet.
- **Reviewed?** Try to read `review/chats_to_review.csv`. If that read
  fails, review hasn't been run — that's fine, it's optional.
- **Classified?** Run the query below against the configured
  `classifications_db` path (default shown). No output, or the DB file
  doesn't exist → nothing classified yet:
  ```bash
  sqlite3 chat_topics.db "SELECT status, COUNT(*) FROM classifications GROUP BY status;"
  ```
- **Vault built?** List the configured `vault_dir`'s `Conversations`
  subfolder and count `.md` files. Zero (or missing) → vault not built yet.

**Re-run this whole check whenever the user returns after running a
command you handed off**, rather than assuming state from earlier in the
conversation — the point of a hand-off is that something changed outside
your view, so trust a fresh check over memory.

---

## Step 2 — Decision tree

Every entry follows the Core rule: brief, then hand off the command.

### No `config/config.json` — first-time setup
```bash
cp .env.example .env
cp config/config.example.json config/config.json
cp config/topics.example.json config/topics.json
```
`.env` needs `GEMINI_1PSID` / `GEMINI_1PSIDTS` (DevTools → Application →
Cookies on gemini.google.com) and, for a cloud LLM, `LLM_API_KEY`.
`api.context_window_tokens` must match what the LLM server actually
serves — most common cause of truncated classifications.

### Chats dir empty — Stage 1: extract
```bash
python -m extractors.gemini
```
Prerequisite: valid, unexpired cookies in `.env`.

**If the user reports a cookie, login, or connection error from this
command: do not open or read `extractors/gemini.py` to diagnose it.**
Ask the user to paste the exact error message instead — the script prints
a specific, self-contained fix already (which cookie names to re-copy and
from where). Relay that message back and walk them through DevTools →
Application → Cookies → gemini.google.com, re-copying `__Secure-1PSID` and
`__Secure-1PSIDTS` into `.env`. Don't ask the user to pre-confirm cookies
are fresh before running the command — they can't verify that without
checking DevTools anyway, so just run it and react to the actual error.

### Extracted, nothing reviewed/classified yet
Ask the user to choose between two options — use your host tool's native
choice/selector UI if it has one, otherwise a plain numbered list is fine:

> 1. **Run review** (recommended) — CSV manifest, skim titles + auto-flagged
>    sensitive content, mark rows for removal.
> 2. **Skip for now** — go straight to classification. Review can be run
>    later at any time; skipping isn't permanent or time-limited.

If reviewing: check whether `config/sensitive_patterns.json` exists (per
the sensitive-info rule above — existence only). If not, offer to set it
up:
```bash
cp config/sensitive_patterns.example.json config/sensitive_patterns.json
```
then the user edits `patterns`/`keywords` themselves — they can add or
remove as many labels as they want; nothing about the shipped example set
is fixed or required.

**If the file already exists and the user just edited it** (added/removed
a keyword or regex), a plain `python review_chats.py` won't re-check
already-scanned chats against the new rules — only new/changed chats get
auto-scanned. Hand off a forced full re-scan instead:
```bash
python review_chats.py --scan-sensitive
```
Same applies if a chat's flags look stale or wrong after an update to
`sensitive_patterns.json` — this is the fix, not `--mask-sensitive` or
re-running plain `review_chats.py`.

Hand off manifest generation:
```bash
python review_chats.py
```
Marking a row `DEL` stages nothing destructive by itself — only
`prune_chats.py --prune` deletes anything.

If the user wants to see exactly what matched in a flagged chat, hand off
`--show-sensitive` — this is a normal, expected step, not something to
avoid mentioning. The only rule is *you* never run it yourself, since it
prints the actual matched sensitive text:
```bash
python review_chats.py --show-sensitive <conversation_id>
```

**Once the user has gone through the manifest and is happy with the
`KEEP`/`DEL` decisions**, mention there's a one-command way to finish up —
they don't need to manually change every row's `reviewed` column by hand:

> When you're done deciding what to keep or delete, this marks everything
> as reviewed in one go (it only touches the reviewed status, not your
> keep/delete choices, and shows counts before asking you to confirm):
> ```bash
> python review_chats.py --mark-reviewed
> ```

### DEL rows staged, or files deleted by hand — ready to prune
Hand off the dry-run:
```bash
python prune_chats.py
```
This only prints what *would* be deleted. Ask the user for the summary
count and threshold status if they want help deciding — not the full
per-chat list.

Then hand off the real deletion:

> Permanently deletes the classification, DB row, and vault note for each
> candidate; deletes the JSON file for manifest-marked chats; adds all to
> the ignore list. Not reversible — `--unignore` allows re-fetching later,
> but the old classification/vault note won't come back.
> ```bash
> python prune_chats.py --prune
> ```
Mention `--confirm-large-delete` if the dry-run crossed ~30%.

### Reviewed and kept chats still show sensitive flags — masking option
Masking only touches rows where `reviewed == YES` and `action == KEEP` in
the manifest — before suggesting `--mask-sensitive`, check (or ask) whether
the user has actually finished reviewing (see `--mark-reviewed` above) and
that the flagged chat's action is `KEEP`, not `DEL`. If either isn't true
yet, that's the actual fix — not a bug in masking. This is why masking
naturally comes right after review/prune are settled, not before.

**If the user mentions a specific chat is flagged, offer to look at what
actually matched before suggesting redaction.** Hand off:
```bash
python review_chats.py --show-sensitive <conversation_id>
```
or, for every flagged chat at once, `--show-sensitive-all`. This tells the
user whether the flag is a real hit or a false positive (e.g. a phone-number
pattern matching an unrelated number) — worth knowing before deciding to
redact. Only move to masking once they've confirmed they want it redacted,
not automatically just because a flag exists.

> Instead of an all-or-nothing keep/delete choice, you can redact just the
> flagged spans (email, phone, names, etc.) in chats you're keeping, before
> they're sent to an LLM for classification.

Hand off preview first (read-only, changes nothing):
```bash
python review_chats.py --mask-sensitive
```
Then, if they want to proceed, explain and hand off:

> Rewrites the matched sensitive text directly in the chat JSON files
> (title + turn text) with `[REDACTED:{alias}]` markers — `credit_card` and
> `email` keep a partial hint (last 4 digits / domain), everything else is
> fully redacted. This changes the chat's content hash, so already-run
> classification and vault notes for masked chats will be regenerated on
> the next `classify_chats.py`/`obsidian_layout.py` run — that's expected,
> not a bug.
> ```bash
> python review_chats.py --mask-sensitive --apply
> ```

If the user says a chat still shows flags afterward, that's a logged
warning meaning a pattern didn't fully cover the matched text — suggest
`--show-sensitive <id>` (hand off, per the rule above) to see what's left,
and consider tightening that pattern in `sensitive_patterns.json`.

### Ready to classify
```bash
python classify_chats.py
```
Prerequisite: a reachable LLM endpoint — local server (llama.cpp, Ollama)
running, or `api` in `config.json` pointed at a cloud endpoint with
`LLM_API_KEY` set. Suggest a small test batch first, especially after a
prompt/taxonomy change:
```bash
python classify_chats.py 20
```
**If the user reports it failed, do not open `classify_chats.py` to
diagnose it.** Ask for the exact error message — `API not reachable`
usually means:
1. No local LLM server running (`api.url` pointing at `localhost`?)
2. `api` misconfigured — wrong URL/model, or missing `LLM_API_KEY`

Retry errored chats:
```bash
python classify_chats.py --retry
```

### Some chats classified — ready to build the vault
```bash
python obsidian_layout.py
```
Incremental — only new/changed notes written, nothing deleted.

Only bring up `--force` if `config/topics.json` entries were renamed or
merged — incremental mode won't clean up stale notes on its own then:

> Wipes and regenerates every conversation, topic, and category note from
> scratch. Nothing outside the vault is touched, but manual edits inside
> vault notes will be lost.
> ```bash
> python obsidian_layout.py --force
> ```

### After a successful vault build
Tell the user the vault is ready and where to open it (their configured
`vault_dir`). **Then always recommend these two plugins in the same
message — don't treat this as an afterthought or something to skip.**
Without them, the vault technically works, but the graph view and file
list will look/behave in ways that undercut the actual point of the vault
(a browsable, sensibly-ordered second brain) — this isn't a "nice to have"
detail to omit if the user seems in a hurry:

> Install these two Obsidian community plugins now — the vault is
> generated expecting them, and without them the graph and file list won't
> look right:
> - **[Custom Node Size](https://github.com/jackvonhouse/custom-node-size)**
>   — every note has a `node_size` field the pipeline computes so
>   categories visually outrank topics, which outrank conversations. Without
>   this plugin, Obsidian ignores that field and sizes nodes by raw link
>   count instead — a popular topic can end up looking bigger than a whole
>   category, and the graph stops making structural sense at a glance.
> - **[Custom Sort](https://github.com/SebastianMC/obsidian-custom-sort)**
>   — the vault ships a `sortspec.md` meant to sort `Conversations/`
>   newest-first. Without this plugin, Obsidian falls back to its own
>   default file sort (usually alphabetical), and the file list won't
>   reflect actual conversation order at all.
>
> Both install from Obsidian's Community Plugins browser — search by name,
> enable, done. No configuration needed beyond that.

Graph colors and layout are separate, genuinely optional tuning —
`config.json`'s `obsidian` block (`colors`, `repelStrength`,
`linkDistance`) and `node_sizing` bands, see `docs/CONFIGURATION.md`. Don't
let this genuinely-optional part dilute the plugin recommendation above;
mention it after, not blended together.

If they do want to tweak config, `obsidian_layout.py` (no `--force`) picks
up the changes on the next run.

---

## Common situations

| Situation | Response |
|---|---|
| Deleted chat keeps reappearing | File deleted but `--prune` never run — explain, hand off `--prune` |
| Accidentally pruned, want it back | `--unignore <cid>` re-enables re-fetch only; classification/vault note not restored |
| Two topics → separate notes, should be one | Filename-collision, auto-resolved — hand off a plain re-run of `obsidian_layout.py` |
| Category collision error | Two `topics.json` entries share a safe filename — user renames one |
| Classification keeps failing | Ask what the error said; check LLM URL, suggest lower `temperature`, mention `--retry` |
| Want to add a topic | Lower-risk than rename/remove, still a manual `topics.json` edit — show the shape, let the user edit |
| Chat still flagged after `--mask-sensitive --apply` | Expected occasionally — logged as a warning; hand off `--show-sensitive <id>`, suggest tightening that pattern |
| `--mask-sensitive` says nothing to mask, but chat has flags | Masking only touches `reviewed == YES` + `action == KEEP` rows — check that chat's `reviewed` column isn't still `NO`/`RE-REVIEW` |
| Edited `sensitive_patterns.json`, flags didn't change on existing chats | Expected — only new/changed chats auto-scan. Hand off `--scan-sensitive` for a full forced re-scan |

---

## Deeper reference

- `docs/CLI.md` — full flag reference
- `docs/CONFIGURATION.md` — API/node-sizing/graph/sensitive-pattern config
- `docs/ARCHITECTURE.md` — `common.py`, JSON contract, project structure
- `docs/DESIGN_NOTES.md` — why the pipeline is shaped this way

If the ask is really a feature request or a bug, say so — don't stretch
this guide to cover it.