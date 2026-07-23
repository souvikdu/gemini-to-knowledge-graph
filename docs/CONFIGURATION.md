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