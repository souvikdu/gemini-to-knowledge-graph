"""
Similarity Vault builder — builds an independent Obsidian vault linking
conversations directly by embedding similarity (from embed_chats.py),
instead of the Category -> Topic hierarchy used by obsidian_layout.py.

Notes contain the full conversation transcript (turns) plus a summary and
a "Related Conversations" section linking similar chats. This vault is a
second lens on the same conversations, organized by similarity rather
than by Category -> Topic.

Usage:
    python embedding_layout.py           # incremental
    python embedding_layout.py --force   # wipe and regenerate everything
"""

import hashlib
import os
import shutil
import sys

from common import (
    get_db_connection,
    get_chat_rows,
    iter_chats,
    load_all_classifications,
    load_all_embeddings,
    load_all_similarity_links,
    load_config,
    load_embedding_config,
    load_existing_vault_state,
    log,
    make_safe_filename,
    stamp_note_mtime,
    sync_chats_to_db,
    yaml_str,
)
from obsidian_layout import (
    _resolve_link_placeholders,
    _strip_generated_image_tags,
    format_date,
)


# ── Staleness signature ─────────────────────────────────────────────────────


def similarity_note_signature(summary_hash: str, links: list, turns: list = None) -> str:
    """Combined fingerprint of the embedded text's own signature
    (embeddings.summary_hash — already a hash of title+summary+model,
    from Phase 1/2) AND the chat's current similarity-link set, so a
    note gets rewritten if either the embedded text or its neighbors
    changed.

    Also folds in the raw transcript turns: this vault now renders the
    full conversation, so a change to the underlying chat (new/edited
    turn) must trigger a rewrite even if the summary/embedding is
    unchanged.
    """
    h = hashlib.sha256(b"v1:")
    h.update((summary_hash or "").encode())
    for turn in (turns or []):
        h.update(((turn.get("role") or "") + "\x1f" + (turn.get("text") or "")).encode())
    for link in links:
        h.update(link["neighbor_id"].encode())
        h.update(f"{link['score']:.4f}".encode())
    return h.hexdigest()[:16]


# ── Note-action resolution ──────────────────────────────────────────────────


def resolve_note_action(cid, current_sig, existing_vault, used_filenames, title):
    """Decide whether to skip, rewrite, or create a note. Returns
    (action, notename) where action is 'skip' | 'rewrite' | 'new'."""
    if cid in existing_vault:
        existing_name, existing_sig = existing_vault[cid]
        if existing_sig and existing_sig == current_sig:
            return "skip", existing_name
        return "rewrite", existing_name
    base = make_safe_filename(title or "Untitled") or "Untitled"
    notename = base
    used_lower = {f.lower() for f in used_filenames}
    n = 2
    while notename.lower() in used_lower:
        notename = f"{base}-{n}"
        n += 1
    return "new", notename


# ── Note rendering ──────────────────────────────────────────────────────────


def render_conversation_block(turns, user_label, assistant_label, search_url):
    """Render the full turn-by-turn transcript, reusing the same
    placeholder/image-tag cleanup as obsidian_layout.py so the output
    matches the primary vault's formatting."""
    turn_blocks = []
    for turn in turns:
        role = turn.get("role", "unknown")
        text = turn.get("text") or ""
        text = _resolve_link_placeholders(text, search_url)
        text = _strip_generated_image_tags(text)
        if role == "user":
            label = user_label
            lines = text.split("\n")
            prefix = f"**{label}:** " if label else ""
            quoted = [f"> [!quote] {prefix}{lines[0]}"] + [f"> {line}" for line in lines[1:]]
            turn_blocks.append("\n".join(quoted))
        elif role in ("assistant", "model"):
            label = assistant_label
            lines = text.split("\n")
            prefix = f"**{label}:** " if label else ""
            quoted = [f"{prefix}{lines[0]}"] + [f"{line}" for line in lines[1:]]
            turn_blocks.append("\n".join(quoted))
        else:
            turn_blocks.append(text)
    if not turn_blocks:
        return ""
    return "\n\n## Conversation\n\n" + "\n\n".join(turn_blocks)


def render_note(cid, title, summary, categories, topics, chat_meta, turns,
                links, current_sig, embed_cfg, cid_to_notename,
                user_label, assistant_label, search_url):
    """Build the full markdown note for one conversation."""
    related_lines = []
    for link in links:
        neighbor_id = link["neighbor_id"]
        neighbor_name = cid_to_notename.get(neighbor_id)
        if not neighbor_name:
            # Defensive: neighbor may have been pruned since links were
            # computed. Skip rather than emit a broken wikilink.
            continue
        related_lines.append(
            f"* [[{neighbor_name}]] ({link['score']:.2f})"
        )
    if related_lines:
        related_block = "\n".join(related_lines)
    else:
        related_block = "_No similar conversations found._"

    conversation_block = render_conversation_block(
        turns, user_label, assistant_label, search_url
    )

    return f"""---
type: conversation
conversation_id: {yaml_str(cid)}
source: {yaml_str(chat_meta.get('source', 'unknown'))}
created: {format_date(chat_meta.get('created_at'))}
updated: {format_date(chat_meta.get('updated_at'))}
title: {yaml_str(title)}
summary: {yaml_str(summary or "_No summary available._")}
categories: [{', '.join(yaml_str(c) for c in categories)}]
topics: [{', '.join(yaml_str(t) for t in topics)}]
note_signature: {yaml_str(current_sig)}
tags:
  - "type/conversation"
---

## Related Conversations
{related_block}
{conversation_block}
"""


# ── Graph config ────────────────────────────────────────────────────────────


def _write_graph_config(vault_dir, embed_cfg):
    """Write .obsidian/graph.json with color groups and visual settings,
    mirroring obsidian_layout.py but with a single color group
    (conversation only — this vault has no topic/category tiers)."""
    import json

    obsidian_dir = os.path.join(vault_dir, ".obsidian")
    os.makedirs(obsidian_dir, exist_ok=True)

    obs_cfg = embed_cfg.get("obsidian", {})

    graph_colors = obs_cfg.get("colors", {})
    color_groups = []
    tag_to_query = {
        "conversation": "tag:#type/conversation",
    }
    defaults = {
        "conversation": {"a": 1, "rgb": 65280},
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
    """Write sortspec.md for the obsidian-custom-sort plugin, mirroring
    obsidian_layout.py but targeting this vault's Conversations folder."""
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


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    cfg = load_config(require_vault=False)
    embed_cfg = load_embedding_config()

    conn = get_db_connection(cfg)
    sync_chats_to_db(conn, cfg["paths"]["chats_dir"])

    vault_dir = embed_cfg["paths"]["vault_dir"]
    convos_dir = os.path.join(vault_dir, "Conversations")

    # Display labels + search URL for rendering turns (mirrors obsidian_layout.py)
    display = cfg.get("display_names", {})
    user_label = display.get("user", "")
    assistant_label = display.get("assistant", "")
    search_url = cfg.get("obsidian", {}).get(
        "search_url", "https://duckduckgo.com/?q="
    )

    force = "--force" in sys.argv
    if force:
        if os.path.isdir(convos_dir):
            shutil.rmtree(convos_dir)
        os.makedirs(convos_dir, exist_ok=True)
        existing_vault = {}
    else:
        os.makedirs(convos_dir, exist_ok=True)
        existing_vault = load_existing_vault_state(convos_dir)

    classifications = load_all_classifications(conn)
    embeddings = load_all_embeddings(conn)
    all_links = load_all_similarity_links(conn)
    chat_rows = get_chat_rows(conn, list(embeddings.keys()))

    # Build cid -> turns map from the chat JSON files (needed for the
    # full conversation transcript in each note).
    cid_to_turns = {}
    for fpath, chat in iter_chats(cfg["paths"]["chats_dir"]):
        cid = chat.get("conversation_id")
        if cid:
            cid_to_turns[cid] = chat.get("turns", [])

    # ── Pass A: resolve every note's action + filename before rendering ──
    cid_to_notename = {}
    used_filenames = [name for name, _ in existing_vault.values()]
    actions = []  # (cid, action, notename, current_sig)
    counts = {"new": 0, "rewrite": 0, "skip": 0}
    zero_link_cids = []

    for cid in embeddings.keys():
        rec = classifications.get(cid, {})
        emb = embeddings[cid]
        links = all_links.get(cid, [])
        turns = cid_to_turns.get(cid, [])

        current_sig = similarity_note_signature(
            emb.get("summary_hash", ""), links, turns
        )
        action, notename = resolve_note_action(
            cid, current_sig, existing_vault, used_filenames, rec.get("title")
        )
        cid_to_notename[cid] = notename
        used_filenames.append(notename)
        counts[action] += 1
        if action != "skip":
            actions.append((cid, action, notename, current_sig))
        if not links:
            zero_link_cids.append(cid)

    # ── Pass B: render + write only new/rewritten notes ──────────────────
    for cid, action, notename, current_sig in actions:
        rec = classifications.get(cid, {})
        emb = embeddings[cid]
        links = all_links.get(cid, [])

        title = rec.get("title") or "Untitled"
        summary = rec.get("summary") or ""
        categories = rec.get("category", [])
        topics = rec.get("topic", [])
        chat_meta = chat_rows.get(cid, {})
        turns = cid_to_turns.get(cid, [])

        content = render_note(
            cid, title, summary, categories, topics, chat_meta, turns,
            links, current_sig, embed_cfg, cid_to_notename,
            user_label, assistant_label, search_url,
        )
        fpath = os.path.join(convos_dir, f"{notename}.md")
        with open(fpath, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)

        # Stamp mtime to match the chat's updated_at so filesystem ordering
        # reflects conversation chronology (mirrors obsidian_layout.py).
        stamp_note_mtime(fpath, chat_meta.get("updated_at", ""))

    # Write the graph view config + obsidian-custom-sort spec for this vault.
    _write_graph_config(vault_dir, embed_cfg)
    _write_sort_spec(vault_dir, cfg)

    # ── Summary ──────────────────────────────────────────────────────────
    log("=" * 60)
    log("Similarity Vault build complete")
    log(f"  Vault: {convos_dir}")
    log(f"  Total notes: {len(embeddings)}")
    log(f"    new:       {counts['new']}")
    log(f"    rewritten: {counts['rewrite']}")
    log(f"    skipped:   {counts['skip']}")
    log(f"  Notes with zero related links: {len(zero_link_cids)}")
    log("=" * 60)


if __name__ == "__main__":
    main()
