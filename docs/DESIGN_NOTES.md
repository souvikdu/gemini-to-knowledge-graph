# Design Notes — gemini-to-knowledge-graph

This document explains *why* the pipeline is shaped the way it is — the
reasoning behind decisions that aren't obvious just from reading the code,
including a few paths that were considered and deliberately not taken.
Nothing here is dated; it's grouped by what each decision is about, not when
it happened. For what shipped in a given release, see `CHANGELOG.md`.

---

## Why this exists

A review of comparable tools before continuing development found nothing
that combines this project's specific approach: bulk cookie-based
extraction, single-pass LLM classification against a fixed seed taxonomy,
SQLite-backed resumability, and a structured category → topic → conversation
hub graph. That combination — not any single piece of it — is what this
project is actually betting on.

---

## Core principles

Four things decide most of the trade-offs below, so they're worth naming up
front:

- **Resumability.** Every stage should be safely interruptible and
  re-runnable without redoing finished work or requiring a human to remember
  where it left off.
- **No plugin dependency for core features.** The vault must be fully usable
  in stock Obsidian. Community plugins (Custom Node Size, Custom Sort)
  enhance the experience but are never required to get value out of it.
- **No backfill or migration cost.** Evolving the pipeline shouldn't require
  a one-off script to fix up old data. New behavior should either apply
  cleanly to what already exists, or make its own case for reprocessing (see
  *Self-describing note versions* below).
- **Visual cleanliness in the graph output.** The Obsidian graph view is the
  primary way this data gets browsed, so structural decisions are weighed
  against whether they keep that view readable at hundreds or thousands of
  nodes — not just whether they're technically correct.

---

## Pipeline shape: three independent stages

Extract → Classify → Vault, as three separate scripts that don't share
in-memory state — only the filesystem (`chats/*.json`) and the SQLite DB.
Each stage can be re-run, debugged, or fail on its own without corrupting the
others' state. You can reclassify without re-extracting, or rebuild the
vault without reclassifying, which matters a lot when iterating on a prompt
or a taxonomy edit rather than the extraction logic.

---

## Extraction (Gemini Web)

### Cookie-based auth, not an official API
There's no official personal-export API for Gemini Web chat history —
cookie-based scraping via `gemini_webapi` is the only viable path. This is
deliberately isolated behind `extractors/gemini.py` and a source-agnostic
JSON contract, so it stays a source-specific implementation detail rather
than something the Classify/Vault stages need to know about.

### Bypassing the library's `read_chat()`
The library's own chat-reading method silently drops user turns with empty
text — which is exactly what an image-only, audio-only, or file-only upload
looks like. Parsing the raw batch-execute response directly, with an
explicit attachment scan, keeps those turns in the conversation instead of
losing them.

### Pinned chats are never checkpointed
Regular chats resume from a timestamp checkpoint; pinned chats are always
fetched in full. A pin/unpin event doesn't necessarily move a timestamp
forward, so a timestamp-based checkpoint alone would never notice a chat's
pin status changed. Fetching the (typically small) pinned set in full every
run is what makes reconciling pin status possible without a separate
tracking mechanism.

### Checkpoint only advances after a fully clean run
If any chat fails to download, the resume timestamp is not updated. A
partial run that did advance the checkpoint would permanently skip whatever
failed — the next run's resume point would already be past it.

### The `chats` table is a metadata index, not a copy of the content
It stores only what's needed to detect change cheaply and drive
resumability — `conversation_id`, `source`, `title`, `content_hash`,
`source_file`, `file_mtime`, first/last-seen timestamps, and pin status. It
never stores the actual turns or message text. JSON files remain the sole
store of full conversation content and the only thing a person actually
reviews or deletes by hand; the DB exists purely so downstream stages can
answer "has this file changed since I last looked at it?" without
re-reading and re-hashing every file in `chats_dir` on every run. The
extractor writes both the JSON file and this metadata row at download time
as the primary path; `sync_chats_to_db()` is a safety net that catches
anything that reached `chats_dir` without going through the extractor — a
file added or edited by hand during manual review, for instance.

---

## Classification

### Single-pass, not multi-turn
One request per conversation, asking for categories, topics, and a summary
all at once. Keeps cost and latency predictable and works within what a
small local model can reliably do in one shot — the trade-off is that a
single call has to be well-specified enough to not need a follow-up, which
is most of why the prompt and parser carry as much structure as they do.

### Seed taxonomy + coinage, not either extreme
A fully fixed taxonomy forces awkward mis-fits when a genuinely new subject
comes up repeatedly. A fully free-form "let the model invent categories
every time" approach fragments into near-duplicate one-off topics and
produces an unusable graph. `topics.json` is explicitly a *seed vocabulary,
not a hard constraint* — the classifier can coin a new topic under an
existing category when nothing fits, and a periodic manual dedupe/promotion
pass is the intended way to fold recurring coinages back into the seed list
(see *Designed but not yet implemented*).

### Category/topic confusion is treated as expected, not a rare error
The prompt explicitly warns the model that a topic can't double as a
category, and the parser has a dedicated remap step that catches a
topic-shaped category answer and maps it back to the real parent. This is
written as a first-class case, not a fallback, because a small local model
conflating the two turned out to be common enough to design around.

### Differentiated retry strategy
Transient failures (connection errors, non-200 responses) get exponential
backoff. Unparseable responses get an immediate fast-retry with no backoff —
at a low temperature, a malformed reply is a formatting issue, not a
flaky-server issue, so waiting several seconds before asking the same
question again doesn't change the odds of a better answer.

---

## Vault generation

### Strict three-tier hierarchy, no shortcuts
A conversation never links directly to a category — always
Category → Topic → Conversation. Every shortcut link is another kind of edge
the graph view has to render, and at scale that's what turns a browsable
graph into a hairball. This is the *visual cleanliness* principle applied
directly.

### Filenames start as titles, then get locked in by conversation ID
A new conversation note's filename is derived from the chat's title via
`make_safe_filename()` — a file called `Planning-a-Weekend-Hiking-Trip.md`
is far easier to scan in a file browser than an opaque
`gemini_c_0b2b2434ededef14.md`. But titles aren't stable: Gemini can
regenerate a chat's title asynchronously after the first few messages, so
the filename can't simply be re-derived from the title on every run — that
would either duplicate the note under the new title or require tracking
"this conversation's file used to be named X, now needs to become Y."

Instead, `load_existing_vault_state()` reads each note's own
`conversation_id` back out of its frontmatter and looks existing notes up by
that ID, never by filename. Once a note is created, its filename is fixed —
later runs find it again via the ID lookup and rewrite it in place, even if
the title has since changed, rather than renaming or duplicating it. The
extractor's own `chats/*.json` files use the opposite convention
(`gemini_<id>.json`, ID-based from the start), because that layer has no
reviewability requirement of its own — a person reviews `chats_dir` far
less often than they browse the vault.

### Self-describing note versions (`note_signature`)
This is what makes *no backfill or migration cost* actually true rather than
aspirational. `note_signature()` hashes both a chat's content and its
classification outcome, behind a hardcoded version prefix. Adding a new
frontmatter field means bumping that prefix — every existing note then
computes a signature that no longer matches what's stored in the vault, so
the next incremental run naturally rewrites it with the new field. No
separate migration script, no special-casing "notes written before version
X."

### Node sizing is sqrt-compressed into fixed bands per tier
Obsidian's default sizing by raw link count makes a popular topic look
bigger than a category. Fixed floor/ceiling bands per tier keep the tier
hierarchy visually obvious regardless of how lopsided the actual link counts
get.

### Link placeholders default to DuckDuckGo, kept fully overridable
Gemini's own `[label](_link)` placeholder has no real destination — clicking
it needs to go *somewhere* useful, so it's rewritten into a web search for
the link's label text. `obsidian.search_url` defaults to DuckDuckGo
(`https://duckduckgo.com/?q=`), chosen as a plain, no-account-required
search default. It stays a one-line config override for anyone who'd rather
default to Google or another engine instead — the example `config.json` in
this repo does exactly that.

### "Uncategorized" fallback instead of dropping links
If every topic assigned to a chat happens to collide with an actual category
name (and gets filtered to avoid an ambiguous wikilink), the chat falls back
to a synthetic `Uncategorized (<category>)` topic rather than silently
losing its topic links.

---

## Shared state & safety

### The ignore list is a distinct state, not a side effect of "unclassified"
A conversation the user deliberately deleted and a conversation that simply
hasn't been processed yet need to be distinguishable — otherwise a
deliberate deletion could look identical to a processing gap, and either get
silently re-fetched or get treated as more urgent to classify than it is.

### Orphan cleanup was consolidated into one script
`prune_chats.py` exists so cleanup has a single place responsible for the
full cascade — classification record, `chats` row, ignore list, vault note —
instead of each stage script owning a partial `--prune` flag that only
cleaned up its own slice of state. Because it works directly off the `chats`
metadata table rather than the `classifications` table, it can catch
orphans that were deleted before ever being classified — the gap the old
per-stage `--prune` flags couldn't see.

### A safety threshold gates large deletions
Bulk-deleting a large fraction of tracked conversations in one run is far
more likely to mean "pointed at the wrong `chats_dir`" than "intentional
mass cleanup." The threshold makes that distinction the user's to confirm
explicitly (`--confirm-large-delete`) rather than something that happens
silently.

---

## Decisions considered and rejected

Kept here specifically because the reasoning is worth preserving even though
nothing shipped from it.

**Renaming vault notes when a chat's title changes.** Considered and
rejected. New notes are named from the title for reviewability (see
*Filenames start as titles...* above), but re-deriving and renaming the file
every time the underlying title changes would risk producing a duplicate
note under the new name, or require tracking a rename history. Instead the
filename is fixed at creation and located again by `conversation_id` from
frontmatter, never re-derived from the current title.

**Mid-point relationship notes**, proposed for a possible future
similarity-graph mode. Rejected because force-directed layout quality
degrades as the corpus grows, and a synthetic in-between node adds a layer
of graph complexity without a real informational gain over a direct link.

**Cluster hub notes**, proposed for the same feature. Rejected because
clustering loses pairwise precision (which two specific chats are actually
similar) and introduces hub lifecycle questions — when a cluster splits,
merges, or gets renamed — that are disproportionate to what a direct top-K
nearest-neighbor link already achieves.

**Dual-mode single vault with tag-based filtering**, also for the same
feature. Rejected in favor of a plain config toggle between hierarchy mode
and similarity mode. Running both in the same vault via tags caused
coexistence problems; a clean either/or toggle doesn't ask Obsidian's graph
view to represent two different definitions of "what a link means" at the
same time.

---

## Designed but not yet implemented

Kept separate on purpose — being explicit about what's designed versus what
actually exists is one of the working principles behind this project, and
folding roadmap ideas in as if they were shipped would work against that.

- **Similarity graph mode.** The config toggle described above: direct
  top-K wikilinks between conversations based on embedding similarity, no
  hub notes, mutually exclusive with the current hierarchy mode (switching
  requires `obsidian_layout.py --force`).
- **`embed_chats.py`.** A standalone embedding pipeline reading from
  `classifications.summary` rather than raw chat text — summaries are
  model-curated and already fit within the token limits of small local
  embedding models (typically 256–512 tokens), where raw conversation text
  often wouldn't. Embeddings would live in a dedicated table keyed by a hash
  of the summary text, resumable the same way every other stage is.
- **A chat review manifest.** The reviewability idea above: a read-only CSV
  export (filename, title, turn count, opening prompt) sorted to surface
  likely throwaway chats first, so reviewing hundreds of chats before
  classification doesn't require opening each one.
- **Taxonomy promotion workflow.** A periodic pass to surface topics the
  classifier has coined that aren't yet in the seed taxonomy, so a
  genuinely recurring new topic gets promoted into `topics.json` as a
  canonical entry instead of being re-coined ad hoc indefinitely.
- **Other ideas noted but not committed to:** message-level incremental sync
  (diffing only new turns in an updated conversation, rather than
  re-treating the whole chat as changed), a vault lint pass for
  orphaned topics and near-duplicate taxonomy nodes, an MCP server layer over
  the vault, and browser-extension-based extraction as a fallback to cookie
  scraping.