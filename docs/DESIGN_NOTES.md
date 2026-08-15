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
or a taxonomy edit rather than the extraction logic. Review sits as an
optional fourth stage between Extract and Classify, following the same
independence rule — it only touches the filesystem (`review/chats_to_review.csv`)
and the DB's `chats` table for reads, never the `classifications` table or
the vault. (Cleaning those up when pruning is handled by
`prune_chats.py` — see *Orphan cleanup was consolidated into one script*
below.)

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

## Review

### Explicit KEEP/DEL per row, not "missing row means delete"
An earlier design considered treating a chat's absence from a re-edited
manifest as an implicit deletion instruction — skip writing the file back
out, and whatever's missing gets pruned. This was rejected: a missing row
can't be distinguished from an editor mistake, a truncated save, an
accidental filter, or a chat that was simply extracted *after* the manifest
was last generated. Requiring an explicit `DEL` marker per row means intent
is always stated, never inferred from silence — the same reasoning already
behind keeping the ignore list a distinct state (see *Shared state & safety*
below) rather than overloading "not present" to mean something specific.

### Review state lives only in the CSV until prune actually runs
Marking a chat `DEL` does not write anywhere else — not to
`ignored_conversations`, not to any DB table. `ignored_conversations` is
live extractor state that the extractor consults on every run; writing a
pending, possibly-reversible review decision into it the moment someone
edits a CSV cell would mean a decision that hasn't been acted on yet is
already influencing what gets re-fetched. Nothing becomes permanent until
`prune_chats.py --prune` actually executes the cascade — the manifest is
staging, not state.

### RE-REVIEW instead of resetting straight back to NO
When a previously-reviewed (`YES`) chat's content or sensitive-info flags
change, it moves to `RE-REVIEW`, not back to `NO`. The distinction matters
in practice, not just semantically: `NO` and `RE-REVIEW` behave identically
for review purposes (both need a second look, both get swept up by
`--mark-reviewed`), but collapsing a drifted, already-vetted chat back to
"never reviewed" would ask the user to re-decide it from scratch on every
run that happens to touch it — for an actively-used chat history, that's
potentially dozens of re-decisions every single review pass. `RE-REVIEW`
preserves the fact that the chat was already looked at once, so the ask is
"confirm this is still fine" rather than "decide this from nothing," and
`action` (`KEEP`/`DEL`) is deliberately left untouched by this transition —
a chat someone already decided to keep doesn't silently become a deletion
candidate just because it drifted.

### Manifest sort order matches Gemini's own sidebar, not the vault's convention
Rows are sorted newest-first by `updated_at`. This is a different key than
the vault's own conversation sort (`created` vs. `updated`, see
[CONFIGURATION.md](CONFIGURATION.md#sorting-conversations)) — the review
manifest's sort was chosen specifically so a chat that just received a new
message rises back toward the top of the manifest the same way it would in
Gemini's own conversation sidebar, since "what's been active recently" is
the more useful ordering for a pre-classification skim than "when did this
conversation start."

### Sensitive-info matching favors simplicity over false negatives
Plain regex + literal case-insensitive substring matching, nothing smarter.
No NER, no LLM-based classification of what counts as sensitive. This is a
deliberate choice: the goal is to **never miss a match**, not to be
surgically precise. False positives are acceptable — you can investigate
them on demand with `--show-sensitive`. But a false negative — sensitive
content the scanner silently ignores — is invisible and irrecoverable, which
would defeat the purpose of having a scanner in the first place.

This follows the same logic as the *Decisions considered and rejected*
section below: a smarter matcher would need to be right often enough to be
trusted unsupervised, and getting that right is its own hard problem
disproportionate to what this feature needs to accomplish. A straightforward
matcher with a cheap, on-demand inspection path is more auditable than an
opaque one that's occasionally wrong in ways that are hard to notice.

### Only alias names are ever persisted or displayed
The manifest's `flags` column, log output, and every other place scan
results surface show only the configured alias (`email`, `family_name`)
never the literal pattern/keyword value or the matched text. The one
deliberate exception is `--show-sensitive` / `--show-sensitive-all`, which
print full matched context — but only on-demand, per-chat, straight to the
terminal, never written back into the manifest or any other file. This
keeps the manifest itself no more sensitive than the alias vocabulary a
user already chose to configure.

### Incremental scanning by default, explicit full re-scan on demand
A default `review_chats.py` run only scans chats that are new or whose
content has changed since the manifest was last generated — not the entire
history, every time. Editing `config/sensitive_patterns.json` (a new
keyword, a tightened regex) doesn't retroactively re-flag chats that were
already scanned under the old rules; applying an edited rule set across
existing history requires the explicit `--scan-sensitive` flag. This is the
same trade-off underpinning `sync_chats_to_db()`'s mtime pre-filtering
elsewhere in the pipeline: re-reading every file on every ordinary run to
catch an infrequent edit isn't worth paying for by default, so the cost is
made opt-in instead.

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
per-stage `--prune` flags couldn't see. It's also the single place that acts
on `review_chats.py`'s `DEL`-marked rows, for the same reason: one script
owns the entire deletion cascade regardless of which mechanism (manual file
deletion or manifest mark) decided a chat should go.

### A safety threshold gates large deletions
Bulk-deleting a large fraction of tracked conversations in one run is far
more likely to mean "pointed at the wrong `chats_dir`" than "intentional
mass cleanup." The threshold makes that distinction the user's to confirm
explicitly (`--confirm-large-delete`) rather than something that happens
silently. This applies identically whether the candidates came from file
orphans, `DEL`-marked manifest rows, or a combination of both — the
denominator is always the full set of currently-tracked chats, not a
per-mechanism count, so marking a large batch `DEL` is held to the same bar
as an equally large batch of orphans would be.

---

## Similarity Graph & Embeddings (Similarity Vault)

### Two separate vaults, not a mode toggle in a single vault
An early proposal explored a config toggle within `obsidian_layout.py` to switch
the primary vault between hierarchy mode and similarity mode. This was rejected in
favor of two independent vaults: `Obsidian_Vault/` (hierarchical) and
`Similarity_Vault/` (similarity-based).
- **Zero coexistence conflict:** A single vault cannot cleanly serve two different
  meanings of "what a wikilink represents" (parent taxonomy vs semantic neighbor)
  without muddying graph navigation.
- **Side-by-side exploration:** Users can keep both vaults open simultaneously in
  Obsidian to explore their history through two distinct lenses without having to
  wipe or regenerate either.

### Embedding `title + summary`, not raw transcripts
`embed_chats.py` embeds `truncate_title(title) + summary` instead of full turn-by-turn
transcripts. Summaries generated during classification are already distilled and concise,
comfortably fitting within the 256–512 token context limits of fast local embedding
models (e.g. `qwen3-embedding:0.6b`). Embedding raw multi-turn transcripts would
routinely overflow token budgets, add significant latency, and require complex chunking
and pooling strategies for marginal link quality gain.

### Brute-force pairwise NumPy similarity over vector databases
Storing embeddings in SQLite as compact float32 BLOBs and computing pairwise cosine
similarity using NumPy (`normalized @ normalized.T`) is deliberate:
- At personal chat history scale (hundreds to low thousands of conversations),
  computing pairwise similarities takes less than 5 milliseconds.
- Introducing a dedicated vector database (Chroma, Qdrant, Milvus) or ANN indexing
  library (FAISS, HNSW) would add heavyweight dependencies and external service
  management for zero practical performance gain at this corpus size.

### Directional Top-K with strict score threshold
Similarity links are directional: note A listing note B in its top-K does not require
note B to list note A. Requiring symmetric links would force weak matches into
unrelated conversations. Applying a hard `min_similarity` cutoff ensures that chats
with no genuinely close semantic neighbors have zero links rather than noisy connections.

### Dynamic graph physics instead of artificial node size tiers
Unlike the hierarchical vault—which requires `node_sizing` floor and ceiling bands to
prevent popular topics from visually dwarfing categories—`Similarity_Vault/` lets
Obsidian's natural force-directed graph physics size nodes dynamically: the more notes
that reference a given conversation, the larger that hub conversation note renders.

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

**Mid-point relationship notes**, proposed for the similarity graph. Rejected
because force-directed layout quality degrades as the corpus grows, and a
synthetic in-between node adds a layer of graph complexity without a real
informational gain over a direct link.

**Cluster hub notes**, proposed for the similarity graph. Rejected because
clustering loses pairwise precision (which two specific chats are actually
similar) and introduces hub lifecycle questions — when a cluster splits,
merges, or gets renamed — that are disproportionate to what a direct top-K
nearest-neighbor link already achieves.

**Dual-mode single vault with tag-based filtering.** Rejected in favor of two
separate vaults (`Obsidian_Vault/` and `Similarity_Vault/`). Running both link
styles in the same vault caused coexistence problems; separate vaults allow both
lenses to exist concurrently without interference.

**Dedicated Vector Database / ANN libraries (Chroma, LanceDB, FAISS).** Rejected
in favor of SQLite BLOB storage + vectorized NumPy matrix multiplication. Keeps
dependencies minimal (`numpy` only) and avoids external service maintenance.

---

## Designed but not yet implemented

Kept separate on purpose — being explicit about what's designed versus what
actually exists is one of the working principles behind this project, and
folding roadmap ideas in as if they were shipped would work against that.

- **Semantic search CLI.** A `search_chats.py` utility that embeds a user query
  and prints the top matching conversations by cosine similarity against stored
  vectors in `chat_topics.db`.
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