# Contributing to gemini-to-knowledge-graph

Solo-maintained, git-clone-distributed personal project — keep changes small and focused.

## Before you start

- For anything beyond a small fix, open an issue first. Scope is intentionally
  narrow — check [docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md) for what's
  already been considered and rejected.
- For structural changes, read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first.

## Setup

```bash
git clone <url>
cd gemini-to-knowledge-graph
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
cp config/config.example.json config/config.json
cp config/topics.example.json config/topics.json
```

## Adding a new chat source

`classify_chats.py` and `obsidian_layout.py` only read the standard JSON
contract from `chats/*.json` — no changes needed there.

1. Add `extractors/<source>.py`
2. Write output via `extractors/base.py`'s `upsert_chat_and_file()`
   (contract: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#json-contract))
3. Prefix `conversation_id` with the source name (`gemini_`, `chatgpt_`, etc.)
4. Add tests under `tests/extractors/`

## Making changes

- Keep diffs targeted; shared logic goes in `common.py`, not duplicated across scripts
- Config/DB schema changes should be additive (no renames/removals)
- Never mutate original input files (`chats/*.json`) in place

## Before opening a PR

```bash
pytest -v
ruff check .
```

PR titles must follow [Conventional Commits](https://www.conventionalcommits.org/) (CI-enforced):

```
feat(extractors): add ChatGPT extractor
fix(prune): correct cascade delete ordering
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`.

## Bug reports

Include: Python/OS version, your LLM server + model, relevant `config.json`
values (redact keys), steps to reproduce, and the timestamped log output
around the failure.

---

By contributing, you agree your contributions are licensed under this
project's MIT License. Questions → open an issue.