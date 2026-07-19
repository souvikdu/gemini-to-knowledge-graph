"""
Single-pass chat classifier using an LLM via OpenAI-compatible API.

Reads whole conversations from gemini_chats/ and assigns 1-2 Categories
and 2-5 Topics per chat, using config/topics.json as the seed vocabulary.
Results are written to the SQLite `classifications` table as they complete,
so the run is safely interruptible and resumable.

Usage:
    python classify_chats.py           # process everything unprocessed
    python classify_chats.py 20        # process only the next 20 (testing)
    python classify_chats.py --retry   # retry previously failed chats
"""

import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from common import (
    load_config,
    load_topics,
    die,
    log,
    chat_fingerprint,
    dedup_case_insensitive,
    iter_chats,
    get_db_connection,
    load_all_classifications,
    upsert_classification,
    sync_chats_to_db,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def load_env():
    """Load .env and return LLM_API_KEY (may be empty for local)."""
    load_dotenv()
    return os.getenv("LLM_API_KEY", "")


def build_system_prompt(categories, topics_by_category, cfg):
    """Load prompt template and substitute the category/topic listing."""
    prompt_file = cfg["paths"]["prompt_file"]
    with open(prompt_file, "r", encoding="utf-8") as f:
        template = f.read()

    lines = []
    for c in categories:
        topics = ", ".join(sorted(topics_by_category.get(c, [])))
        lines.append(f"  {c}: {topics}")
    categories_list = "\n".join(lines)

    return template.replace("{categories_list}", categories_list)


# ── Chat text assembly / truncation ─────────────────────────────────────────


def estimate_tokens(text):
    return len(text) // 4


def build_chat_text(chat, max_turn_chars):
    """Flatten a chat's title + turns into one text blob."""
    parts = [f"Title: {chat.get('title') or 'Untitled'}"]
    for turn in chat.get("turns", []):
        role = turn.get("role", "unknown")
        text = (turn.get("text") or "").strip()
        if len(text) > max_turn_chars:
            text = text[:max_turn_chars] + " …[turn truncated]"
        if text:
            parts.append(f"[{role}] {text}")
    return "\n\n".join(parts)


def truncate_to_budget(text, max_chars, head_fraction):
    """Keep the start and the end, drop the middle for oversized chats."""
    marker = "\n\n...[middle of conversation omitted for length]...\n\n"
    if len(text) <= max_chars:
        return text
    head_len = int(max_chars * head_fraction)
    tail_len = max_chars - head_len - len(marker)
    tail_len = max(tail_len, 0)
    return text[:head_len] + marker + (text[-tail_len:] if tail_len else "")


# ── API call ─────────────────────────────────────────────────────────────────


def _split_csv_items(raw: str) -> list[str]:
    """Split comma-separated values, strip brackets, and clean whitespace."""
    return [t.strip().rstrip("]").lstrip("[").strip()
            for t in raw.split(",") if t.strip()]


def _normalize_categories(raw_cats: list[str], topic_to_category: dict) -> list[str]:
    """Map topic-looking items back to their parent category, dedup."""
    result = []
    for c in raw_cats:
        c = c.strip()

        c_lower = c.lower().strip()
        if c_lower in topic_to_category:
            mapped = topic_to_category[c_lower]
            log(f"    ↳ Normalized '{c}' -> category '{mapped}' (was a topic in seed)")
            result.append(mapped)
        else:
            result.append(c)
    return dedup_case_insensitive(result)


def parse_response(content, topic_to_category):
    """Parse the model's Category/Topic/Summary reply.

    Pass 1 is strict prefix matching — handles correctly labeled responses,
    and partially-labeled ones (e.g. ``Topic:``/``Summary:`` present but
    ``Category:`` dropped).

    Pass 2 is a positional fallback for whatever pass 1 left empty: gemma4
    sometimes drops every label but reliably keeps the same line order
    (category, then topic, then summary), so an unconsumed line is assigned
    to whichever field is still missing, in that fixed order. This is what
    lets a completely unlabeled reply like::

        General Knowledge
        Miscellaneous
        The user asked to set a reminder.

    still parse correctly instead of erroring and burning a retry.
    """
    lines = [line.strip() for line in content.strip().splitlines() if line.strip()]
    categories, topics, summary = [], [], ""
    consumed = [False] * len(lines)

    # ── Pass 1: strict prefix matching ────────────────────────────────
    for i, line in enumerate(lines):
        low = line.lower()
        if low.startswith("category:"):
            categories = _normalize_categories(
                _split_csv_items(line.split(":", 1)[1]), topic_to_category
            )
            consumed[i] = True
        elif low.startswith("topic:"):
            topics = dedup_case_insensitive(_split_csv_items(line.split(":", 1)[1]))
            consumed[i] = True
        elif low.startswith("summary:"):
            summary = line.split(":", 1)[1].strip()
            consumed[i] = True

    # ── Pass 2: positional fallback for any still-missing fields ──────
    if not categories or not topics or not summary:
        recovered = []
        for i, line in enumerate(lines):
            if consumed[i]:
                continue
            if not categories:
                categories = _normalize_categories(_split_csv_items(line), topic_to_category)
                recovered.append("category")
            elif not topics:
                topics = dedup_case_insensitive(_split_csv_items(line))
                recovered.append("topic")
            elif not summary:
                summary = line
                recovered.append("summary")
            else:
                break
            consumed[i] = True
        if recovered:
            log(f"  ⚠ Response missing label(s) for: {', '.join(recovered)} — recovered positionally")

    if not categories or not topics:
        raise ValueError(
            f"Could not parse Category/Topic lines.\n"
            f"--- LLM response start ---\n{content}\n--- LLM response end ---"
        )
    return categories[:2], topics[:5], summary


def classify_chat(chat_text, system_prompt, topic_to_category, cfg, api_key):
    api_cfg = cfg["api"]
    limits = cfg["limits"]
    user_msg = (
        f"Classify this conversation:\n\n{chat_text}\n\nNow output your classification:"
    )

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_err = None
    for attempt in range(limits["max_retries"]):
        try:
            r = requests.post(
                api_cfg["url"],
                headers=headers,
                json={
                    "model": api_cfg["model"],
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": api_cfg["temperature"],
                    "max_tokens": api_cfg["max_output_tokens"],
                },
                timeout=api_cfg["timeout"],
            )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                log(f"    ⚠ API returned {r.status_code}, retry {attempt+1}/{limits['max_retries']}")
                time.sleep(5 * (attempt + 1))
                continue

            data = r.json()
            choices = data.get("choices")
            if not choices or not isinstance(choices, list) or len(choices) == 0:
                last_err = f"No choices in response: {str(data)[:300]}"
                log(f"    ⚠ Empty choices, retry {attempt+1}/{limits['max_retries']}")
                time.sleep(5 * (attempt + 1))
                continue

            msg = choices[0].get("message", {})
            content = msg.get("content") if isinstance(msg, dict) else ""
            if not content or not content.strip():
                finish_reason = choices[0].get("finish_reason", "?")
                last_err = f"Empty content (finish_reason={finish_reason}): {str(data)[:300]}"
                log(f"    ⚠ Empty response content, retry {attempt+1}/{limits['max_retries']}")
                time.sleep(5 * (attempt + 1))
                continue

            try:
                cats, topics, summary = parse_response(content, topic_to_category)
            except ValueError:
                log(f"    ⚠ RAW LLM RESPONSE:\n{content}")
                last_err = (
                    f"Could not parse Category/Topic lines.\n"
                    f"--- LLM response start ---\n{content}\n--- LLM response end ---"
                )
                if attempt + 1 < limits["max_retries"]:
                    log(f"    ⚠ Parse failed, fast retry {attempt+1}/{limits['max_retries']}")
                continue  # Fast retry — no backoff sleep for parse failures

            return cats, topics, summary
        except requests.exceptions.ConnectionError as e:
            last_err = f"Cannot reach API ({e}) — is your LLM server running?"
            log(f"    ⚠ Connection lost, retry {attempt+1}/{limits['max_retries']}")
            time.sleep(10 * (attempt + 1))
        except Exception as e:
            last_err = str(e)
            if attempt + 1 < limits["max_retries"]:
                log(f"    ⚠ Attempt {attempt + 1} failed: {str(e)[:120]}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(last_err)


# ── Resume / IO ──────────────────────────────────────────────────────────────


def compute_todo(chats_dir, processed_records):
    """Decide which chat files need (re)classification. Pure-ish:
    reads chat files but no network/config dependency, so it's directly
    unit testable. Returns [(fpath, chat_dict, cid), ...]."""
    todo = []
    for fpath, chat in iter_chats(chats_dir):
        cid = chat.get("conversation_id") or os.path.basename(fpath)
        if cid in processed_records:
            prior_hash = processed_records[cid].get("content_hash", "")
            if prior_hash and prior_hash == chat_fingerprint(chat):
                continue  # unchanged — skip
        todo.append((fpath, chat, cid))
    return todo


def main():
    cfg = load_config(require_api=True, require_limits=True)
    api_key = load_env()
    paths = cfg["paths"]
    api_cfg = cfg["api"]
    limits = cfg["limits"]

    # CLI
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = set(a for a in sys.argv[1:] if a.startswith("-"))
    retry = "--retry" in flags
    try:
        limit = int(args[0]) if args else None
    except ValueError:
        log(f"  ✗ Invalid limit argument: {args[0]!r} — must be a number")
        return

    log("=" * 60)
    model_name = api_cfg.get("model", "unknown")
    log(f"Chat Classifier ({model_name}, Category/Topic)")
    if retry:
        log("  Mode: RETRY (re-process only previously failed chats)")
    log("=" * 60)

    categories, topics_by_category, topic_to_category, _topic_canonical = load_topics(cfg)
    system_prompt = build_system_prompt(categories, topics_by_category, cfg)

    system_tokens = estimate_tokens(system_prompt)
    input_budget_tokens = (
        api_cfg["context_window_tokens"]
        - system_tokens
        - api_cfg["max_output_tokens"]
        - api_cfg["safety_margin_tokens"]
    )
    if input_budget_tokens < 500:
        die(f"Input budget too small ({input_budget_tokens} tokens). "
            f"Increase context_window_tokens in config/config.json "
            f"(current: {api_cfg['context_window_tokens']}, "
            f"system prompt alone ~{system_tokens} tok).")
    input_budget_chars = input_budget_tokens * 4
    log(f"System prompt ~{system_tokens} tok; per-chat input budget ~{input_budget_tokens} tok "
        f"(~{input_budget_chars} chars)")

    # API connectivity test
    log("Testing API connectivity...")
    test_headers = {"Content-Type": "application/json"}
    if api_key:
        test_headers["Authorization"] = f"Bearer {api_key}"
    try:
        dummy_payload = {
            "model": api_cfg["model"],
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1
        }
        resp = requests.post(
            api_cfg["url"],
            headers=test_headers,
            json=dummy_payload,
            timeout=10
        )
        if resp.status_code != 200:
            log(f"  ✗ API returned status {resp.status_code} — check api.url in config.json")
            return
        log("  ✓ API reachable")
    except Exception as e:
        log(f"  ✗ API not reachable: {e}")
        return

    if not os.path.isdir(paths["chats_dir"]):
        log(f"  ✗ Missing {paths['chats_dir']}/")
        return

    # ── Open DB connection ──────────────────────────────────────────────────
    conn = get_db_connection(cfg)
    sync_chats_to_db(conn, paths["chats_dir"])
    processed_records = load_all_classifications(conn)
    log(f"Resume: {len(processed_records)} records in DB")

    # ── Build todo list ─────────────────────────────────────────────────────
    if retry:
        # Fast path: only error IDs — skip the full compute_todo() scan
        error_ids = {cid for cid, rec in processed_records.items()
                     if rec.get("status") == "error"}
        todo = []
        for fpath, chat in iter_chats(paths["chats_dir"]):
            cid = chat.get("conversation_id") or os.path.basename(fpath)
            if cid in error_ids:
                todo.append((fpath, chat, cid))
        log(f"Retry mode: {len(error_ids)} error IDs, {len(todo)} to retry")
    else:
        todo = compute_todo(paths["chats_dir"], processed_records)

    if limit:
        todo = todo[:limit]

    log(f"{len(todo)} conversations to classify\n")
    if not todo:
        conn.close()
        return

    start = time.time()
    ok_count = 0
    err_count = 0

    for i, (fpath, chat, cid) in enumerate(todo, 1):
        title = chat.get("title") or "Untitled"
        chat_text = build_chat_text(chat, limits["max_turn_chars"])
        was_truncated = len(chat_text) > input_budget_chars
        chat_text = truncate_to_budget(chat_text, input_budget_chars,
                                       limits["truncate_head_fraction"])

        log(f"[{i}/{len(todo)}] {title[:70]}{' (truncated)' if was_truncated else ''}")

        fp = chat_fingerprint(chat)
        record = {
            "conversation_id": cid,
            "title": title,
            "content_hash": fp,
            "classified_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            categories_list, topics_list, summary = classify_chat(
                chat_text, system_prompt, topic_to_category, cfg, api_key
            )
            record.update({
                "category": categories_list,
                "topic": topics_list,
                "summary": summary,
                "status": "ok",
            })
            ok_count += 1
            log(f"    -> Category: {', '.join(categories_list)} | "
                f"Topic: {', '.join(topics_list)}")
            log(f"    -> Summary: {summary[:120]}")
        except Exception as e:
            record.update({"status": "error", "error": str(e)})
            err_count += 1
            log(f"    ✗ FAILED: {e}")

        upsert_classification(conn, record)

        if i % 25 == 0:
            elapsed = time.time() - start
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(todo) - i) / rate if rate > 0 else 0
            log(f"  ... {i}/{len(todo)} done, {rate:.2f}/s, ETA {eta/60:.1f} min")

    conn.close()

    elapsed = time.time() - start
    log(f"\n{'='*60}")
    log(f"DONE! {ok_count} classified, {err_count} failed in {elapsed/60:.1f} min")
    if err_count:
        log(f"⚠ {err_count} errors logged — error status stored in DB.\n"
            f"  Fix the issue, then re-run with: python classify_chats.py --retry")


if __name__ == "__main__":
    main()
