"""
Standalone pruning script — removes orphaned chat records from the database,
regardless of classification status.

Orphans are conversation IDs that exist in the ``chats`` table but whose JSON
files no longer exist in ``chats_dir/``. This covers the pre-classification gap:
the old ``--prune`` flags on ``classify_chats.py`` and ``obsidian_layout.py``
could only detect orphans among already-classified chats; this script checks
everything the extractor has ever downloaded.

Usage:
    python prune_chats.py                           # dry-run (list orphans)
    python prune_chats.py --prune                    # confirm and execute
    python prune_chats.py --prune --confirm-large-delete  # force large prune
    python prune_chats.py --list-ignored             # show ignored conversations
    python prune_chats.py --unignore <cid>           # lift an ignore entry
"""

import os
import sys

from common import (
    load_config,
    log,
    get_db_connection,
    sync_chats_to_db,
    find_orphaned_cids,
    exceeds_prune_safety_threshold,
    delete_classifications,
    add_ignored_conversations,
    remove_ignored_conversations,
    load_existing_vault_state,
)


def _delete_vault_note(cid, convos_dir, state):
    """Delete a conversation note from the vault if it exists, matching by
    ``conversation_id`` in YAML frontmatter.

    *state* is a pre-built ``{cid: (notename, signature)}`` dict from
    ``load_existing_vault_state()`` — pass it in rather than rebuilding
    per orphan (O(N) per call vs O(N²)).
    """
    entry = state.get(cid)
    if entry is None:
        return False
    notename = entry[0]
    fpath = os.path.join(convos_dir, f"{notename}.md")
    try:
        os.remove(fpath)
        return True
    except Exception:
        return False


def _do_prune(orphans, *, conn, cfg, **kwargs):
    """Cascade-delete orphaned records and record them as ignored.

    1. Delete from ``classifications`` (no-op if never classified)
    2. Delete from ``chats``
    3. Add to ``ignored_conversations``
    4. Delete stale vault notes

    Steps 1-3 are wrapped in a single transaction: if the process is
    killed mid-sequence the database is rolled back to its pre-prune
    state, preventing orphan records that would be undiscoverable on
    retry.
    """
    # Steps 1-3 in a single transaction
    with conn:
        # 1. Classifications
        deleted_cls = delete_classifications(conn, list(orphans), commit=False)
        if deleted_cls:
            log(f"Deleted {deleted_cls} orphaned classification(s) from DB.")

        # 2. Chats table
        placeholders = ",".join("?" * len(orphans))
        conn.execute(
            f"DELETE FROM chats WHERE conversation_id IN ({placeholders})",
            list(orphans),
        )

        # 3. Ignore list (so the extractor never re-fetches these)
        add_ignored_conversations(conn, list(orphans), reason="deleted-by-user", commit=False)

    # 4. Vault notes — build state ONCE, not per orphan
    vault_dir = cfg["paths"].get("vault_dir")
    removed_vault = 0
    if vault_dir:
        convos_dir = os.path.join(vault_dir, "Conversations")
        state = load_existing_vault_state(convos_dir)
        for cid in orphans:
            if _delete_vault_note(cid, convos_dir, state):
                removed_vault += 1
    if removed_vault:
        log(f"Deleted {removed_vault} orphaned vault note(s).")

    log(f"Pruned {len(orphans)} conversation(s) — IDs added to ignore list.")
    log("Hub notes (Topic/Category) are not touched by this script —")
    log("they refresh automatically on the next 'python obsidian_layout.py' run.")


def list_ignored(conn):
    """Print every row from the ``ignored_conversations`` table."""
    rows = conn.execute(
        "SELECT conversation_id, reason, ignored_at"
        " FROM ignored_conversations ORDER BY ignored_at"
    ).fetchall()
    if not rows:
        log("No ignored conversations found.")
        return
    log(f"Ignored conversations ({len(rows)}):")
    for row in rows:
        log(f"  {row['conversation_id']}  (reason: {row['reason']},"
            f" ignored at: {row['ignored_at']})")


def unignore(conn, cids):
    """Remove conversations from the ignore list and warn the user about
    what this does (and doesn't) restore."""
    removed = remove_ignored_conversations(conn, list(cids))
    if removed:
        log(f"Removed {removed} conversation(s) from ignored_conversations.")
        log("\u2696 This only lifts the re-fetch block. It does NOT restore the")
        log("  deleted chat file, classification, or vault note — those are")
        log("  permanently gone.")
        log("  The conversation will reappear only via:")
        log("    (a) a new message on Gemini's side (bumps its timestamp past")
        log("        the current checkpoint), or")
        log("    (b) manually resetting last_timestamp to 0 in")
        log("        config/extraction_state_gemini.json to force a full re-scan.")
    else:
        log("None of the specified IDs were in the ignore list — nothing to do.")


def main():
    cfg = load_config()
    conn = get_db_connection(cfg)

    flags = set(a for a in sys.argv[1:] if a.startswith("-"))
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    # ── List-ignored / unignore — fast-path, no sync needed ─────────────
    if "--list-ignored" in flags:
        list_ignored(conn)
        conn.close()
        return

    if "--unignore" in flags and args:
        unignore(conn, args)
        conn.close()
        return

    # ── Normal prune flow ───────────────────────────────────────────────
    log("Syncing chat files to DB before computing orphans...")
    sync_chats_to_db(conn, cfg["paths"]["chats_dir"])

    known_cids = {
        row["conversation_id"]
        for row in conn.execute("SELECT conversation_id FROM chats")
    }

    if not known_cids:
        log("No conversations in DB — nothing to prune.")
        conn.close()
        return

    orphans = find_orphaned_cids(known_cids, cfg["paths"]["chats_dir"])

    if not orphans:
        log("No orphans found — all conversations have matching files.")
        conn.close()
        return

    # Fetch titles from chats table for readable dry-run output
    orphan_titles = {}
    for row in conn.execute(
        "SELECT conversation_id, title FROM chats WHERE conversation_id IN ({})"
        .format(",".join("?" * len(orphans))),
        list(orphans),
    ):
        orphan_titles[row["conversation_id"]] = row["title"] or "(no title)"

    log(f"Found {len(orphans)} orphaned conversation(s)"
        f" (file missing from chats/):")
    for cid in sorted(orphans):
        title = orphan_titles.get(cid, "(unknown)")
        log(f"  - {title[:80]}  ({cid})")

    if "--prune" in flags:
        max_ratio = cfg.get("limits", {}).get("max_prune_ratio", 0.3)
        if exceeds_prune_safety_threshold(
            len(orphans), len(known_cids), max_ratio
        ):
            if "--confirm-large-delete" in flags:
                _do_prune(orphans, conn=conn, cfg=cfg)
            else:
                log(f"  ⚠ {len(orphans)} orphans out of {len(known_cids)}"
                    f" records ({len(orphans)/len(known_cids):.1%})"
                    f" exceeds {max_ratio:.0%} safety threshold.")
                log("  Run with --prune --confirm-large-delete to force.")
        else:
            _do_prune(orphans, conn=conn, cfg=cfg)
    else:
        log("  Run with --prune to remove them from the database"
            " and ignore list.")

    conn.close()


if __name__ == "__main__":
    main()
