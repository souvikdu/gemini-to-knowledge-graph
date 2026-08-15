# Configuration Reference

Everything here lives in `config/config.json` unless noted otherwise. All
of it is validated at startup by the stage that needs it — a missing or
malformed key fails fast with a message telling you exactly what to fix.

---

## Local or cloud?

Both work — the classifier just needs an endpoint that speaks the
OpenAI-style `/v1/chat/completions` format. That covers local servers
([llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server` or
[Ollama](https://ollama.com)) as well as cloud providers (OpenAI, Groq,
Together, OpenRouter, Mistral, DeepSeek, Azure OpenAI) and Claude, via
Anthropic's [OpenAI-compatible endpoint](https://docs.claude.com/en/api/openai-sdk)
(documented by Anthropic as a testing/evaluation interface, not their
recommended production API).

**Privacy note:** this tool sends the full text of your conversations to
whichever endpoint you configure — potentially years of personal questions
and private details. If that matters more than raw quality/speed, local is
the safer default; nothing leaves your machine. Cloud is a fully supported
option if you don't have the hardware for a decent local model.

This project was built and tested against **Gemma 4** locally, but any
OpenAI-compatible model works — just make sure `api.context_window_tokens`
matches what you're actually serving (see below).

---

## The `api` block

```json
"api": {
  "url": "http://localhost:8080/v1/chat/completions",
  "model": "gemma4",
  "temperature": 0.05,
  "max_output_tokens": 350,
  "timeout": 300,
  "context_window_tokens": 24000,
  "safety_margin_tokens": 500
}
```

| Key | Meaning |
|---|---|
| `url` | Your LLM server's chat-completions endpoint. |
| `model` | Model name/tag as your server expects it. |
| `temperature` | Keep low — classification is a structured-output task, not creative writing. |
| `max_output_tokens` | Cap on reply length; the prompt asks for a fixed format so this can stay small. |
| `timeout` | Seconds to wait for a call before retrying. |
| `context_window_tokens` | **Set this to the context length your server is actually serving with right now** — the setting most likely to bite you. |
| `safety_margin_tokens` | Buffer subtracted from the input budget for token-counting estimation error. |

### Why `context_window_tokens` matters

The classifier reserves tokens for the system prompt, `max_output_tokens`,
and `safety_margin_tokens` — whatever's left is the per-chat input budget.
Long chats aren't split into multiple calls: if a chat doesn't fit, the
pipeline keeps the start and end and drops the middle.

**Set this to what your server actually serves, not the model's advertised
max** — a spec sheet saying "128K context" is irrelevant if your server
caps it lower to save VRAM/RAM:

- **llama.cpp:** check the `-c` / `--ctx-size` flag you launched with
- **Ollama:** check `num_ctx` — often defaults smaller than the model's max
- **Cloud APIs:** use the documented context window for the model in `api.model`

Too high → requests overflow and error out. Too low → more truncation than
necessary.

The classifier distills each conversation into a ~350-token summary with
category and topic labels — it doesn't need the full transcript to be
accurate, so chat-splitting (sending a long conversation across multiple
LLM calls and merging the results) is deliberately avoided in favor of
simplicity and speed. The full conversation text is preserved untouched in
the vault note regardless of any truncation during classification.

### Turn off reasoning / "thinking" mode for speed

If your local model has a reasoning or "thinking" mode (a `/think` toggle,
an `enable_thinking` flag, a reasoning-effort setting, etc.), turn it off
for classification. This is a fixed, structured extraction task — it gets
no benefit from chain-of-thought, and leaving reasoning on can turn a
~2–3 second classification into a ~10 second one, which adds up fast across
hundreds of conversations. Check your server's docs for the relevant
flag (llama.cpp, Ollama, and most reasoning-capable models expose one).

---

## Customizing the prompt (`config/prompts/classifier.md`)

You're free to edit the wording, tone, or guidance in `classifier.md` — the
instructions above `{categories_list}` and the framing around it are yours
to adjust. **Don't change the required response shape**, though:

```
Category: <category>[, <second category>]
Topic: <topic>, <topic>, <topic>, <topic>, <topic>
Summary: <1-2 sentence summary>
```

This three-line format is a deliberately compressed, low-token output —
`classify_chats.py`'s `parse_response()` parses it by label prefix (with a
positional fallback for unlabeled replies), and a JSON or free-form
response would either cost far more output tokens per chat or break the
parser outright. Add whatever extra context or rules you need in the
prompt body; just leave the `Category:`/`Topic:`/`Summary:` line structure
exactly as documented in the template.

---

## Sensitive-pattern scanning (`config/sensitive_patterns.json`)

Used by `review_chats.py` to automatically flag chats that might contain
personal or sensitive information. The review stage can be run at any time
(including mid-pipeline after classification), but it's most useful before
classification so you can filter out unwanted chats early. If you do
classify first and then decide to prune, `prune_chats.py` cleans up the
classifications and vault notes as part of its deletion cascade — no
manual cleanup needed. See [CLI.md](CLI.md#reviewing-extracted-chats) for how the scan
runs and how flags show up in the review manifest; this section covers only
the config file's shape.

### Setup

This file doesn't ship as `config/sensitive_patterns.json` directly —
copy the template first:

```bash
cp config/sensitive_patterns.example.json config/sensitive_patterns.json
```

Then edit your copy. Like `topics.json`, the template
(`sensitive_patterns.example.json`) is tracked in git; your actual
`sensitive_patterns.json` is gitignored, since — unlike the taxonomy — its
whole purpose is to hold things that are personal to you (your name, your
address, family members' names) and must never end up in a public repo or
commit history.

If `config/sensitive_patterns.json` doesn't exist, `review_chats.py` simply
runs with no scanning at all — this file is entirely optional.

### Schema

```json
{
  "patterns": {
    "email": "[\\w.+-]+@[\\w-]+\\.[\\w.-]+",
    "phone": "\\b\\d{3}[-.\\s]?\\d{3}[-.\\s]?\\d{4}\\b"
  },
  "keywords": {
    "family_name": ["Jane Doe", "Janie"],
    "home_address": ["123 Example Street"]
  }
}
```

| Key | Meaning |
|---|---|
| `patterns` | `{alias: regex string}`. Matched with `re.search` against each chat's title and every turn's text. Compiled once at startup — an invalid regex fails immediately with the offending alias named in the error. |
| `keywords` | `{alias: array of literal strings}`. Matched as a plain case-insensitive substring — not a regex — so you can safely include things like `+` or `.` in a name or address without escaping them. Each alias maps to a *list* so one alias can cover multiple spellings/variants (e.g. a nickname and a full name both under `family_name`). |

Both sections are optional — you can have only `patterns`, only `keywords`,
or both. `config/sensitive_patterns.example.json` in this repo has a fuller
example covering email, credit card, IPv4/IPv6 addresses, and a range of
personal-info keyword categories (name, phone, home address, birth date,
etc.) as a starting point.

### Luhn validation for `credit_card`

The `credit_card` alias has a **Luhn algorithm** validator applied automatically
at scan time. The pattern (`\b(?:\d[ -]?){12,18}\d\b`) is deliberately loose to
match 13–19 digit numbers with common separators; the Luhn check filters
false positives (e.g. random long numbers that happen to match the digit pattern).
A matched value only appears in `flags` if it passes the Luhn check. This means
editing the `credit_card` regex requires `--scan-sensitive` to retroactively
re-validate previously flagged chats.

### Masking behavior

When you run `review_chats.py --mask-sensitive --apply`, matched spans are
redacted in the chat JSON file as follows:

| Alias | Mask format | Rationale |
|---|---|---|
| `credit_card` | `[REDACTED:credit_card ...XXXX]` (last 4 digits visible) | Low sensitivity — last 4 digits are routinely shared |
| `email` | `[REDACTED:email domain=...]` (domain visible) | Low sensitivity — domain alone is rarely identifying |
| Everything else (ipv4, ipv6, and all keyword aliases) | `[REDACTED:{alias}]` (full redaction) | The exact match is either sensitive (IP) or literal PII (keywords) |

**Keyword aliases must never be listed in as partial-mask.** They represent
exact literal PII you configured, and leaving any part visible would defeat
the purpose of redaction.

### What appears where — aliases only, never values

Only the **alias name** (`email`, `family_name`, etc.) ever shows up in the
review manifest's `flags` column, in log output, or anywhere else this tool
writes to disk. The literal pattern/keyword values you configure, and the
actual matched text from your chats, are never written anywhere except back
to your own terminal — and only then if you explicitly ask for it via
`review_chats.py --show-sensitive <id>` or `--show-sensitive-all` (see
[CLI.md](CLI.md#reviewing-extracted-chats)).

### One exception: masking's partial reveal

`--mask-sensitive --apply` writes `[REDACTED:credit_card ...1234]` and
`[REDACTED:email domain=example.com]` — the one place a fragment of the
actual value is kept, not just the alias. Both are established
safe-to-show conventions (a card's last 4 digits, an email's domain), not
the sensitive value itself. Every other alias, including all
keyword-based matches, becomes a bare `[REDACTED:alias]`.

### Editing this file doesn't retroactively re-flag old chats

`review_chats.py`'s default run only scans chats that are brand new or whose
content has changed since the last scan — it does not re-open every chat
file on every ordinary run. This means editing `sensitive_patterns.json`
(adding a new keyword, tightening a regex) only affects chats scanned
*after* the edit; chats that were already scanned under the old rules keep
their old `flags` value until you explicitly force a full re-scan:

```bash
python review_chats.py --scan-sensitive
```

This is a deliberate trade-off — the alternative (re-scanning your entire
chat history on every run) would defeat the point of the incremental design
for what's usually an infrequent edit.

### Built-in match validators

A regex alone can't tell a real credit card from a random 16-digit
string. For one well-known alias, `review_chats.py` runs a second,
hardcoded check after the regex matches:

| Alias | Check |
|---|---|
| `credit_card` | Luhn checksum |

Not configurable from `sensitive_patterns.json` — tied to the alias name
in code. Renaming `credit_card` in your config loses the check.

---

## Configuring the vault graph

By default, the vault is generated at `Obsidian_Vault/` in the project
root. To rename or relocate it, change `paths.vault_dir` in
`config/config.json` — the pipeline creates the folder for you either way.

The `display_names`, `obsidian`, and `node_sizing` blocks control how the
vault's graph view looks.

### Display names (`display_names`)

Optionally label user and assistant roles in conversation notes instead of
the raw `user`/`model` text:

```json
"display_names": {
  "user": "You",
  "assistant": "Gemini"
}
```

Leave both empty to use the raw role strings.

### Node sizing (`node_sizing`)

Obsidian sizes notes by raw link count, which makes popular topics look
much larger than categories. This block overrides that with non-overlapping
size bands per tier, so categories are always larger than topics, topics
always larger than conversations:

```json
"node_sizing": {
  "conversation": 8,
  "topic": { "floor": 25, "ceiling": 60 },
  "category": { "floor": 72, "ceiling": 100 }
}
```

| Key | Meaning |
|---|---|
| `conversation` | Fixed size for every conversation note. |
| `topic.floor` / `topic.ceiling` | Sqrt-compressed range for topic nodes. |
| `category.floor` / `category.ceiling` | Sqrt-compressed range for category nodes. |

These values are written into each note's frontmatter as `node_size` and
recomputed every run. You'll need a community plugin such as **Custom Node
Size** for Obsidian to respect them.

> **Similarity Vault (`config/embedding.json`):** does **not** use
> `node_sizing`. It has a single tier of `type/conversation` notes, and
> relies on Obsidian's built-in graph behavior — *"the more nodes that
> reference a given node, the bigger it gets"* — so highly-connected
> (hub) conversations render larger automatically. No `node_size`
> frontmatter is written for Similarity Vault notes.

### Graph colors (`obsidian.colors`)

| Tag | Key | Default rgb |
|---|---|---|
| `#type/conversation` | `conversation` | `65280` |
| `#type/topic` | `topic` | `43947` |
| `#type/category` | `category` | `16711680` |
| `#status/unclassified` | `unclassified` | `8421504` |

These are the built-in fallbacks `obsidian_layout.py` uses when
`obsidian.colors` doesn't set a given key — `"rgb"` is a 24-bit packed
integer (e.g. `65280` = pure green), `"a"` is alpha (0–1). Override any of
them in `config.json` (the example `config.json` in this repo already does,
with its own color scheme). Other `obsidian` settings (`repelStrength`,
`linkDistance`, etc.) are passed through to Obsidian's graph config directly.

### Link placeholder resolution (`obsidian.search_url`)

Gemini often emits markdown links as `[Product Name](_link)` without real
URLs. The vault builder rewrites these to browser search links using the
configured `search_url`. Defaults to DuckDuckGo (`https://duckduckgo.com/?q=`)
— override it in `config.json` to use Google or another search engine.

### Sorting conversations

The vault includes a `sortspec.md` in `Conversations/` for the
[obsidian-custom-sort](https://github.com/SebastianMC/obsidian-custom-sort)
plugin, sorting by `updated` (newest first). Topic notes list their
conversations newest-first the same way.

> **Graph view:** `sortspec.md` is a config file, not a real note. Exclude
> it via **Settings → Files & Links → Excluded files**, or right-click it
> in the graph and choose "Exclude this file from graph."

> **Note:** the review manifest (`review/chats_to_review.csv`, produced by
> `review_chats.py`) sorts newest-first the same way, but by `updated_at`
> rather than this setting — it's a separate, fixed sort chosen to match
> Gemini's own conversation sidebar ordering, not something this config
> block controls. See [CLI.md](CLI.md#reviewing-extracted-chats).

---

## Embedding configuration (`config/embedding.json`)

Used by `embed_chats.py` and `embedding_layout.py` to build the optional
**Similarity Vault** (`Similarity_Vault/`), which links conversations directly
by semantic similarity instead of taxonomy categories.

### Setup

Copy the template:

```bash
cp config/embedding.example.json config/embedding.json
```

See `config/embedding.example.json` for the full structure. The key settings are:

- **`api.url` / `api.model`** — your OpenAI-compatible `/v1/embeddings` endpoint (e.g. Ollama with `qwen3-embedding:0.6b` or `nomic-embed-text`, or cloud providers).
- **`api.batch_size`** — number of summaries to embed per request (set to `1` if your endpoint doesn't support batching).
- **`top_k` / `min_similarity`** — maximum neighbors per chat and minimum cosine similarity score (defaults: `3` and `0.70`).
- **`paths.vault_dir`** — target vault directory (defaults to `Similarity_Vault`).
- **`obsidian`** — graph view physics and visual settings written directly to `.obsidian/graph.json`.

### Tuning `min_similarity` and `top_k`

- **`min_similarity`**: Start around `0.70`. If the graph feels too sparse, try lowering to `0.65`. If too many weakly related chats are linked, increase to `0.75`.
- **`--recompute-links`**: After adjusting `top_k` or `min_similarity` in `config/embedding.json`, run `python embed_chats.py --recompute-links` to immediately update links without re-calling the embedding API.