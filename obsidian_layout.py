"""
Build an Obsidian vault from classified chat records (SQLite DB, Stage 2 output).

Strict hierarchy (no direct shortcuts):
    Category Hub --> Topics --> Conversations
    (Chats never link directly to Categories; Categories only link to Topics)

Reads all settings from config/config.json.
"""

import json
import math
import os
import re
import urllib.parse
from datetime import datetime

import hashlib

from common import (
    load_config, load_topics, canonicalize_topic, log, yaml_str, chat_fingerprint,
    dedup_case_insensitive, iter_chats, load_existing_vault_state,
    get_db_connection, load_all_classifications, sync_chats_to_db,
    make_safe_filename, die,
)


# ── Hub-note filename collision resolution ─────────────────────────────


def build_topic_filename_map(chat_topics, topic_canonical_case, known_categories_lower):
    """Map of ``{lowercased_topic: winner_spelling}`` for topic names whose
    ``make_safe_filename()`` outputs collide across different chats.

    Groups all topic names by safe filename, then for each group with >1
    member picks the alphabetically-first spelling as the winner.  The
    returned dict remaps every colliding name to that winner (minus the
    winner itself).  Apply after ``dedup_case_insensitive`` in the
    conversation-processing loop, then re-dedup.
    """
    safe_groups = {}  # safe_lower -> {canonicalized names}

    for cid, rec in chat_topics.items():
        if not rec or rec.get("status") != "ok":
            continue
        for raw_name in rec.get("topic", []):
            if raw_name.strip().lower() in known_categories_lower:
                continue
            canonical = canonicalize_topic(raw_name, topic_canonical_case)
            safe = make_safe_filename(canonical).lower()
            safe_groups.setdefault(safe, set()).add(canonical)

    result = {}
    for members in safe_groups.values():
        if len(members) < 2:
            continue
        winner = min(members, key=lambda x: (x.lower(), x))
        for member in members:
            if member != winner:
                result[member.lower()] = winner

    return result


def format_date(date_val):
    if not date_val:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(str(date_val).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(date_val)[:16]


def _resolve_link_placeholders(text, search_url):
    """Replace Gemini placeholder and googleusercontent links with real
    browser-search links, and remove bare googleusercontent URLs.

    Gemini emits two kinds of invalid links that need handling:
    1. ``[label](_link)`` -- old-style placeholder links
    2. ``[label](http://googleusercontent.com/...)`` -- newer placeholder links
       that look real but aren't accessible outside Gemini's UI

    Both are rewritten to ``[label](<search_url><url_encoded_label>)`` so
    clicking the link in Obsidian opens the user's default browser with a
    search for the label text.

    Bare ``http://googleusercontent.com/...`` URLs (not inside markdown link
    syntax) are simply removed -- they are unclickable reference IDs.
    """
    def _replacer(m):
        label = m.group(1)
        encoded = urllib.parse.quote(label, safe="")
        return f"[{label}]({search_url}{encoded})"

    # Rewrite placeholder markdown links -- both _link and googleusercontent
    text = re.sub(
        r"\[([^\]]+)\]\((?:\_link|https?://googleusercontent\.com/[^)]*)\)",
        _replacer,
        text,
    )

    # Remove any remaining bare googleusercontent URLs (plain text, no label)
    text = re.sub(r"https?://googleusercontent\.com/\S*", "", text)

    return text


_IMAGE_TAG_RE = re.compile(r"<Image\b([^/]*)/>", re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def _strip_generated_image_tags(text):
    """Replace Gemini's inline <Image .../> placeholders with the
    caption/alt text Gemini gave us, so the note renders something
    readable instead of raw XML markup."""
    def _replacer(m):
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        label = attrs.get("caption") or attrs.get("alt") or "image"
        return f"\n> [!info]- Image : {label}\n"
    return _IMAGE_TAG_RE.sub(_replacer, text)


# ── Load taxonomy + classifications ─────────────────────────────────────────


def normalize_category(name, known_categories_lower):
    """Case/whitespace-insensitive match against the known category list.
    Anything the classifier invented that doesn't match falls back to
    General Knowledge rather than silently creating a stray hub."""
    key = name.strip().lower()
    if key in known_categories_lower:
        return known_categories_lower[key]
    log(f"  ⚠ Unrecognized category '{name}' -> General Knowledge")
    return "General Knowledge"


def opening_prompt(turns, max_chars=400):
    for turn in turns:
        if turn.get("role") == "user" and (turn.get("text") or "").strip():
            text = turn["text"].strip()
            if len(text) > max_chars:
                text = text[:max_chars].rstrip() + "…"
            return text
    return ""


def yaml_tag_block(tags):
    lines = "\n".join(f'  - "{t}"' for t in tags)
    return f"tags:\n{lines}"


def scaled_node_size(count, max_count, floor, ceiling):
    """sqrt-compress a count into [floor, ceiling]."""
    if max_count <= 0 or count <= 0:
        return floor
    scaled = math.sqrt(count) / math.sqrt(max_count)
    return round(floor + scaled * (ceiling - floor))


def note_signature(chat, rec):
    """Combined fingerprint of the chat's own text AND its current
    classification outcome. chat_fingerprint() alone misses a chat
    that stays textually identical but transitions from
    unclassified/error to a successful classification (e.g. after
    --retry) — this catches that case too."""
    h = hashlib.sha256(b"v1:")  # v1: initial schema
    h.update(chat_fingerprint(chat).encode())
    if rec:
        h.update((rec.get("status") or "").encode())
        h.update(json.dumps(rec.get("category", []), sort_keys=True).encode())
        h.update(json.dumps(rec.get("topic", []), sort_keys=True).encode())
        h.update((rec.get("summary") or "").encode())
    return h.hexdigest()[:16]


def resolve_note_action(cid, chat, rec, existing_vault, used_filenames):
    """Decide whether to skip, rewrite, or create a conversation note.
    Pure — no file I/O — so it's directly unit testable.
    Returns (action, notename) where action is 'skip' | 'rewrite' | 'new'."""
    current_sig = note_signature(chat, rec)
    if cid in existing_vault:
        existing_name, existing_sig = existing_vault[cid]
        if existing_sig and existing_sig == current_sig:
            return "skip", existing_name
        return "rewrite", existing_name
    base = make_safe_filename(chat.get("title") or "Untitled") or "Untitled"
    notename = base
    used_lower = {f.lower() for f in used_filenames}
    n = 2
    while notename.lower() in used_lower:
        notename = f"{base}-{n}"
        n += 1
    return "new", notename


# ── Vault preparation ────────────────────────────────────────────────────────


def _prepare_vault(cfg, force):
    """Load config, create directory structure, load classifications.

    Returns a dict with all the context needed by downstream steps.
    """
    paths = cfg["paths"]
    ns = cfg["node_sizing"]

    vault_dir = paths["vault_dir"]
    chats_dir = paths["chats_dir"]
    convos_dir = os.path.join(vault_dir, "Conversations")
    category_dir = os.path.join(vault_dir, "Concepts", "Categories")
    topic_dir = os.path.join(vault_dir, "Concepts", "Topics")

    # Validate all inputs BEFORE touching the filesystem,
    # so --force never destroys a vault for nothing.
    categories, _, topic_to_category, topic_canonical_case = load_topics(cfg)
    known_categories_lower = {c.lower(): c for c in categories}

    # Die early if two categories produce the same safe filename — this
    # cannot be resolved automatically because category hubs are defined
    # by config/topics.json, not auto-generated from LLM output.
    cat_safe_names = {}
    for cat in categories:
        safe = make_safe_filename(cat).lower()
        if safe in cat_safe_names:
            die(
                f"✗ Category collision: '{cat}' and '{cat_safe_names[safe]}' both "
                f"produce the safe filename '{make_safe_filename(cat)}'.\n\n"
                f"  Rename one of them in config/topics.json."
            )
        cat_safe_names[safe] = cat

    conn = get_db_connection(cfg)
    sync_chats_to_db(conn, chats_dir)
    chat_topics = load_all_classifications(conn)
    topic_filename_map = build_topic_filename_map(
        chat_topics, topic_canonical_case, known_categories_lower,
    )
    if topic_filename_map:
        log(f"Filename collision map: {len(topic_filename_map)} topic names remapped")
    if not chat_topics:
        log("No classifications yet — conversations will be written without category/topic links.")
        log("  Run classify_chats.py later to add the graph hierarchy.")

    if not os.path.isdir(chats_dir):
        log(f"Missing {chats_dir}/")
        conn.close()
        return None

    if force:
        import shutil
        for d in (convos_dir, category_dir, topic_dir):
            if os.path.isdir(d):
                shutil.rmtree(d)
                log(f"  Removed {os.path.relpath(d, vault_dir)}/")

    os.makedirs(convos_dir, exist_ok=True)
    os.makedirs(category_dir, exist_ok=True)
    os.makedirs(topic_dir, exist_ok=True)

    display = cfg.get("display_names", {})
    user_label = display.get("user", "")
    assistant_label = display.get("assistant", "")

    obs_cfg = cfg.get("obsidian", {})
    search_url = obs_cfg.get(
        "search_url",
        "https://duckduckgo.com/?q=",
    )

    existing_vault = {} if force else load_existing_vault_state(convos_dir)
    if existing_vault:
        log(f"Resume: {len(existing_vault)} notes already in vault (will skip unchanged, rewrite stale)")
    elif force:
        log("Force mode: regenerating all notes from scratch")

    return {
        "vault_dir": vault_dir,
        "chats_dir": chats_dir,
        "convos_dir": convos_dir,
        "category_dir": category_dir,
        "topic_dir": topic_dir,
        "conv_size": ns["conversation"],
        "topic_range": (ns["topic"]["floor"], ns["topic"]["ceiling"]),
        "cat_range": (ns["category"]["floor"], ns["category"]["ceiling"]),
        "user_label": user_label,
        "assistant_label": assistant_label,
        "categories": categories,
        "known_categories_lower": known_categories_lower,
        "topic_to_category": topic_to_category,
        "topic_canonical_case": topic_canonical_case,
        "conn": conn,
        "chat_topics": chat_topics,
        "existing_vault": existing_vault,
        "search_url": search_url,
        "topic_filename_map": topic_filename_map,
    }


# ── Conversation processing ──────────────────────────────────────────────────


def _process_conversations(ctx):
    """Iterate chat files, resolve note actions, write conversation notes.

    Returns a dict of accumulators needed for topic/category note generation.
    """
    convos_dir = ctx["convos_dir"]
    chats_dir = ctx["chats_dir"]
    conv_size = ctx["conv_size"]
    user_label = ctx["user_label"]
    assistant_label = ctx["assistant_label"]
    known_categories_lower = ctx["known_categories_lower"]
    topic_to_category = ctx["topic_to_category"]
    topic_canonical_case = ctx["topic_canonical_case"]
    topic_filename_map = ctx["topic_filename_map"]
    chat_topics = ctx["chat_topics"]
    existing_vault = ctx["existing_vault"]
    search_url = ctx["search_url"]

    cid_to_notename = {cid: name for cid, (name, _) in existing_vault.items()}
    cid_to_date = {}
    topic_to_chats = {}
    topic_canonical_parent = {}
    category_to_topics = {}
    topic_to_sources = {}
    category_to_sources = {}
    used_filenames = set(cid_to_notename.values())

    unclassified = 0
    error_count = 0
    new_count = 0
    rewritten_count = 0
    skipped_count = 0

    files = list(iter_chats(chats_dir))
    log(f"Processing {len(files)} chat files...")

    for idx, (fpath, chat) in enumerate(files, 1):
        fname = os.path.basename(fpath)
        cid = chat.get("conversation_id") or fname.replace(".json", "")
        title = chat.get("title") or "Untitled"
        if len(title) > 120:
            title = title[:117].rstrip() + "..."
        turns = chat.get("turns", [])

        rec = chat_topics.get(cid)
        categories_for_chat, topics_for_chat = [], []
        status_tag = "type/conversation"

        if rec is None:
            unclassified += 1
            status_tag = "status/unclassified"
        elif rec.get("status") == "error":
            error_count += 1
            status_tag = "status/classification-error"
        else:
            categories_for_chat = [
                normalize_category(m, known_categories_lower) for m in rec.get("category", [])
            ]
            categories_for_chat = dedup_case_insensitive(categories_for_chat)
            topics_for_chat = [
                canonicalize_topic(t, topic_canonical_case)
                for t in rec.get("topic", [])
            ]
            topics_for_chat = dedup_case_insensitive(topics_for_chat)

            # Remap colliding topic names to a deterministic spelling
            topics_for_chat = [
                topic_filename_map.get(t.lower(), t)
                for t in topics_for_chat
            ]
            topics_for_chat = dedup_case_insensitive(topics_for_chat)

            filtered = []
            for t in topics_for_chat:
                if t.lower() in known_categories_lower:
                    log(f"  ⚠ Topic '{t}' matches a category name — skipping")
                else:
                    filtered.append(t)
            topics_for_chat = filtered

            if not topics_for_chat:
                fallback_cat = categories_for_chat[0] if categories_for_chat else "General Knowledge"
                log(f"  ⚠ All topics for '{title}' matched category names —"
                    f" using fallback topic 'Uncategorized ({fallback_cat})'")
                topics_for_chat = [f"Uncategorized ({fallback_cat})"]

            source = chat.get('source', 'unknown')
            for topic in topics_for_chat:
                topic_to_chats.setdefault(topic, set()).add(cid)
                topic_to_sources.setdefault(topic, set()).add(source)
                if topic not in topic_canonical_parent:
                    parent = topic_to_category.get(topic.lower()) or (
                        categories_for_chat[0] if categories_for_chat else "General Knowledge"
                    )
                    topic_canonical_parent[topic] = parent
                parent = topic_canonical_parent[topic]
                category_to_topics.setdefault(parent, set()).add(topic)
                category_to_sources.setdefault(parent, set()).add(source)

        cid_to_date[cid] = chat.get("updated_at", "")

        action, notename = resolve_note_action(cid, chat, rec, existing_vault, used_filenames)
        if action == "skip":
            skipped_count += 1
            continue
        if action == "new":
            used_filenames.add(notename)
            new_count += 1
        else:
            rewritten_count += 1
        cid_to_notename[cid] = notename

        tags = [status_tag, f"source/{chat.get('source', 'unknown')}"]

        turn_blocks = []
        for turn in turns:
            role = turn.get("role", "unknown")
            text = turn.get("text", "")
            # Replace Gemini's [text](_link) placeholders with browser-search links
            text = _resolve_link_placeholders(text, search_url)
            # Strip Gemini's inline AI-generated image tags, preserving caption/alt
            text = _strip_generated_image_tags(text)
            if role == "user":
                label = user_label
                lines = text.split("\n")
                prefix = f"**{label}:** " if label else ""
                quoted = [f"> [!quote] {prefix}{lines[0]}"] + [f"> {line}" for line in lines[1:]]
                turn_blocks.append("\n".join(quoted))
            elif role == "assistant" or role == "model":
                label = assistant_label
                lines = text.split("\n")
                prefix = f"**{label}:** " if label else ""
                quoted = [f"{prefix}{lines[0]}"] + [f"{line}" for line in lines[1:]]
                turn_blocks.append("\n".join(quoted))
            else:
                turn_blocks.append(text)
        turns_block = (
            "\n\n## Conversation\n\n" + "\n\n".join(turn_blocks) if turn_blocks else ""
        )

        topic_links = (
            "\n".join(f"* [[{make_safe_filename(t)}]]" for t in topics_for_chat) if topics_for_chat else ""
        )

        word_count = sum(len(t.get("text", "").split()) for t in turns)

        summary_text = (
            rec.get("summary", "").strip()
            if rec and rec.get("status") == "ok"
            else ""
        )
        if not summary_text:
            summary_text = opening_prompt(turns) or "_No summary available._"

        content = f"""---
type: conversation
conversation_id: {yaml_str(cid)}
source: {yaml_str(chat.get('source', 'unknown'))}
created: {format_date(chat.get('created_at'))}
updated: {format_date(chat.get('updated_at'))}
title: {yaml_str(title)}
turn_count: {len(turns)}
word_count: {word_count}
categories: [{', '.join(yaml_str(c) for c in categories_for_chat)}]
summary: {yaml_str(summary_text)}
note_signature: {yaml_str(note_signature(chat, rec))}
node_size: {conv_size}
{yaml_tag_block(tags)}
---

## Topics
{topic_links}
{turns_block}
"""
        with open(
            os.path.join(convos_dir, f"{notename}.md"), "w", encoding="utf-8", newline="\n"
        ) as cf:
            cf.write(content)

        # Stamp .md mtime to match the chat's updated_at timestamp from the
        # JSON data so filesystem ordering reflects conversation chronology.
        ts_str = chat.get("updated_at", "")
        if ts_str:
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                chat_ts = dt.timestamp()
                os.utime(os.path.join(convos_dir, f"{notename}.md"), (chat_ts, chat_ts))
            except (ValueError, TypeError):
                pass

        if idx % 100 == 0:
            log(f"  ... {idx}/{len(files)}")

    return {
        "cid_to_notename": cid_to_notename,
        "cid_to_date": cid_to_date,
        "topic_to_chats": topic_to_chats,
        "topic_canonical_parent": topic_canonical_parent,
        "category_to_topics": category_to_topics,
        "topic_to_sources": topic_to_sources,
        "category_to_sources": category_to_sources,
        "unclassified": unclassified,
        "error_count": error_count,
        "new_count": new_count,
        "rewritten_count": rewritten_count,
        "skipped_count": skipped_count,
    }


# ── Topic notes ──────────────────────────────────────────────────────────────


def _write_topic_notes(ctx, acc):
    """Write one .md note per topic with backlinks to conversations."""
    topic_dir = ctx["topic_dir"]
    topic_range = ctx["topic_range"]
    topic_to_chats = acc["topic_to_chats"]
    topic_canonical_parent = acc["topic_canonical_parent"]
    topic_to_sources = acc["topic_to_sources"]
    cid_to_notename = acc["cid_to_notename"]
    cid_to_date = acc["cid_to_date"]

    max_topic_total = max((len(cids) for cids in topic_to_chats.values()), default=1)

    for topic, cids in topic_to_chats.items():
        parent = topic_canonical_parent.get(topic, "General Knowledge")
        safe = make_safe_filename(topic)
        links = "\n".join(
            f"* [[{cid_to_notename[c]}]]"
            for c in sorted(cids, key=lambda c: cid_to_date.get(c, ""), reverse=True)
        )
        sources = topic_to_sources.get(topic, {"unknown"})
        source_tags = "\n".join(f'  - "source/{s}"' for s in sorted(sources))
        content = f"""---
type: topic
category: {yaml_str(parent)}
conversations: {len(cids)}
node_size: {scaled_node_size(len(cids), max_topic_total, *topic_range)}
tags:
  - "type/topic"
{source_tags}
---

# {topic}

**Category:** [[{make_safe_filename(parent)}]]

## Conversations ({len(cids)})
{links}
"""
        with open(
            os.path.join(topic_dir, f"{safe}.md"), "w", encoding="utf-8", newline="\n"
        ) as f:
            f.write(content)


# ── Category hub notes ───────────────────────────────────────────────────────


def _write_category_notes(ctx, acc):
    """Write one .md hub note per category with backlinks to topics."""
    category_dir = ctx["category_dir"]
    cat_range = ctx["cat_range"]
    categories = ctx["categories"]
    category_to_topics = acc["category_to_topics"]
    category_to_sources = acc["category_to_sources"]
    topic_to_chats = acc["topic_to_chats"]

    category_totals = {
        cat: len(set.union(set(), *(topic_to_chats.get(t, set()) for t in category_to_topics.get(cat, set()))))
        for cat in categories
    }
    max_category_total = max(category_totals.values(), default=1)

    for cat in categories:
        topics = sorted(category_to_topics.get(cat, set()), key=str.lower)
        if not topics:
            continue
        safe = make_safe_filename(cat)
        links = "\n".join(f"* [[{make_safe_filename(t)}]]" for t in topics)
        total_convos = category_totals[cat]
        sources = category_to_sources.get(cat, {"unknown"})
        source_tags = "\n".join(f'  - "source/{s}"' for s in sorted(sources))
        content = f"""---
type: category
subtopics: {len(topics)}
conversations: {total_convos}
node_size: {scaled_node_size(total_convos, max_category_total, *cat_range)}
tags:
  - "type/category"
{source_tags}
---

# {cat}

## Topics ({len(topics)})
{links}
"""
        with open(
            os.path.join(category_dir, f"{safe}.md"), "w", encoding="utf-8", newline="\n"
        ) as f:
            f.write(content)


# ── Graph config ─────────────────────────────────────────────────────────────


def _write_graph_config(vault_dir, cfg):
    """Write .obsidian/graph.json with color groups and visual settings
    from config. Every field in the output dict is either read from
    ``cfg["obsidian"]`` or falls back to a sensible default — no dead
    config values."""
    obsidian_dir = os.path.join(vault_dir, ".obsidian")
    os.makedirs(obsidian_dir, exist_ok=True)

    obs_cfg = cfg.get("obsidian", {})

    graph_colors = obs_cfg.get("colors", {})
    color_groups = []
    tag_to_query = {
        "conversation": "tag:#type/conversation",
        "topic": "tag:#type/topic",
        "category": "tag:#type/category",
        "unclassified": "tag:#status/unclassified",
    }
    defaults = {
        "conversation": {"a": 1, "rgb": 65280},
        "topic": {"a": 1, "rgb": 43947},
        "category": {"a": 1, "rgb": 16711680},
        "unclassified": {"a": 1, "rgb": 8421504},
    }
    for key, query in tag_to_query.items():
        color = graph_colors.get(key, defaults[key])
        color_groups.append({
            "query": query,
            "color": color,
        })

    graph_config = {
        "collapse-filter": False,
        "search": "",
        "showTags": False,
        "showAttachments": False,
        "hideUnresolved": False,
        "showOrphans": True,
        "collapse-color-groups": False,
        "colorGroups": color_groups,
        "collapse-display": False,
        "showArrow": False,
        "textFadeMultiplier": obs_cfg.get("textFadeMultiplier", 0),
        "nodeSizeMultiplier": obs_cfg.get("nodeSizeMultiplier", 1),
        "lineSizeMultiplier": obs_cfg.get("lineSizeMultiplier", 0.3),
        "collapse-forces": False,
        "centerStrength": obs_cfg.get("centerStrength", 0.518),
        "repelStrength": obs_cfg.get("repelStrength", 10),
        "linkStrength": obs_cfg.get("linkStrength", 1),
        "linkDistance": obs_cfg.get("linkDistance", 250),
        "scale": obs_cfg.get("scale", 1),
        "close": False,
    }
    with open(os.path.join(obsidian_dir, "graph.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(graph_config, f, indent=2)


# ── Sort spec ────────────────────────────────────────────────────────────────


def _write_sort_spec(vault_dir, cfg):
    """Write sortspec.md for obsidian-custom-sort plugin."""
    sorting = cfg.get("obsidian", {}).get("sorting", {})
    sort_prop = sorting.get("property", "updated")
    sort_dir = sorting.get("direction", "desc")
    order = "< a-z" if sort_dir == "asc" else "> a-z"
    sortspec_path = os.path.join(vault_dir, "sortspec.md")
    with open(sortspec_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"""---
sorting-spec: |
  target-folder: Conversations
  {order} by-metadata: {sort_prop}
---
""")


# ── Summary ──────────────────────────────────────────────────────────────────


def _print_summary(ctx, acc):
    """Print the final summary block."""
    vault_dir = ctx["vault_dir"]
    categories = ctx["categories"]
    category_to_topics = acc["category_to_topics"]
    has_hierarchy = bool(acc["topic_to_chats"])
    total_in_vault = len(acc["cid_to_notename"])
    print(f"""
{'='*55}
Obsidian Vault Updated
{'='*55}
  Vault:          {vault_dir}/
  Total notes:    {total_in_vault} conversations
  New this run:   {acc['new_count']}  (+ {acc['rewritten_count']} rewritten, {acc['skipped_count']} unchanged)
  Unclassified:   {acc['unclassified']}  (run classify_chats.py)
  Errored:        {acc['error_count']}  (run classify_chats.py --retry)
  Topics:         {len(acc['topic_to_chats'])} notes
  Categories:     {sum(1 for c in categories if category_to_topics.get(c))} notes
{'='*55}
Open '{vault_dir}' as a vault in Obsidian.
""")
    if has_hierarchy:
        print("Graph hierarchy: Category → Topic → Conversation (chats never link directly to categories)")
    else:
        print("Run classify_chats.py to add the category/topic hierarchy.")


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    import sys
    force = "--force" in sys.argv

    cfg = load_config(require_vault=True)
    ctx = _prepare_vault(cfg, force)
    if ctx is None:
        return

    acc = _process_conversations(ctx)
    _write_topic_notes(ctx, acc)
    _write_category_notes(ctx, acc)
    _write_graph_config(ctx["vault_dir"], cfg)
    _write_sort_spec(ctx["vault_dir"], cfg)

    ctx["conn"].close()
    _print_summary(ctx, acc)


if __name__ == "__main__":
    main()
