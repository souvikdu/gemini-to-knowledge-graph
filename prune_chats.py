"""
Standalone pruning script — removes orphaned chat records and review-marked
DELETE records from the database, regardless of classification status.

Orphans are conversation IDs that exist in the ``chats`` table but whose JSON
files no longer exist in ``chats_dir/``. This covers the pre-classification gap:
the old ``--prune`` flags on ``classify_chats.py`` and ``obsidian_layout.py``
could only detect orphans among already-classified chats; this script checks
everything the extractor has ever downloaded.

Candidates for pruning are:
1. Orphans: conversation IDs in the ``chats`` table whose JSON files no longer exist.
2. Review-marked chats: existing conversation IDs marked ``DEL`` in ``review/chats_to_review.csv``.

Usage:
    python prune_chats.py                           # dry-run (list candidates)
    python prune_chats.py --prune                    # confirm and execute
    python prune_chats.py --prune --confirm-large-delete  # force large prune
    python prune_chats.py --list-ignored             # show ignored conversations
    python prune_chats.py --unignore <cid>           # lift an ignore entry
"""

import json
import os
import sys

from common import (
    add_ignored_conversations,
    delete_classifications,
    delete_embeddings,
    delete_similarity_links,
    die,
    exceeds_prune_safety_threshold,
    find_orphaned_cids,
    get_db_connection,
    load_config,
    load_existing_vault_state,
    log,
    remove_ignored_conversations,
    sync_chats_to_db,
)
from review_chats import (
    MANIFEST_PATH,
    read_and_validate_manifest,
    write_manifest_atomically,
)


def validate_review_delete_file(cid: str, conn, chats_dir: str) -> str | None:
    """Validate candidate JSON file for a review-marked DELETE chat.

    1. Retrieves ``source_file`` from the database record.
    2. Verifies the candidate path is strictly inside ``chats_dir``.
    3. Verifies the file exists and is a ``.json`` file.
    4. Reads the JSON content and verifies its ``conversation_id`` matches ``cid``.

    Returns the absolute file path if valid, or None if validation fails.
    """
    row = conn.execute(
        "SELECT source_file FROM chats WHERE conversation_id = ?", (cid,)
    ).fetchone()
    if not row or not row["source_file"]:
        return None

    source_file = row["source_file"]
    abs_chats_dir = os.path.abspath(chats_dir)
    candidate_path = os.path.abspath(os.path.join(abs_chats_dir, source_file))

    # Path traversal check
    if not candidate_path.startswith(abs_chats_dir + os.sep) and candidate_path != abs_chats_dir:
        return None

    if not os.path.isfile(candidate_path) or not candidate_path.lower().endswith(".json"):
        return None

    try:
        with open(candidate_path, "r", encoding="utf-8") as f:
            chat_data = json.load(f)
        if chat_data.get("conversation_id") != cid:
            return None
    except Exception:
        return None

    return candidate_path


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


def _do_prune(candidates_to_prune, *, conn, cfg, **kwargs):
    """Cascade-delete records and record them as ignored.

    1. Delete from ``classifications`` (no-op if never classified)
    2. Delete from ``embeddings`` (no-op if never embedded)
    3. Delete from ``similarity_links`` (as owner or as someone else's neighbor)
    4. Delete from ``chats``
    5. Add to ``ignored_conversations``
    6. Delete stale vault notes

    Steps 1-5 are wrapped in a single transaction: if the process is
    killed mid-sequence the database is rolled back to its pre-prune
    state, preventing orphan records that would be undiscoverable on
    retry.
    """
    cids = list(candidates_to_prune)
    if not cids:
        return

    # Steps 1-5 in a single transaction
    with conn:
        # 1. Classifications
        deleted_cls = delete_classifications(conn, cids, commit=False)
        if deleted_cls:
            log(f"Deleted {deleted_cls} classification(s) from DB.")

        # 2. Embeddings
        deleted_emb = delete_embeddings(conn, cids, commit=False)
        if deleted_emb:
            log(f"Deleted {deleted_emb} embedding(s) from DB.")

        # 3. Similarity links (as owner or as someone else's neighbor)
        deleted_links = delete_similarity_links(conn, cids, commit=False)
        if deleted_links:
            log(f"Deleted {deleted_links} similarity link(s) from DB.")

        # 4. Chats table
        placeholders = ",".join("?" * len(cids))
        conn.execute(
            f"DELETE FROM chats WHERE conversation_id IN ({placeholders})",
            cids,
        )

        # 5. Ignore list (so the extractor never re-fetches these)
        add_ignored_conversations(conn, cids, reason="deleted-by-user", commit=False)

    # 4. Vault notes — build state ONCE, not per candidate
    vault_dir = cfg["paths"].get("vault_dir")
    removed_vault = 0
    if vault_dir:
        convos_dir = os.path.join(vault_dir, "Conversations")
        state = load_existing_vault_state(convos_dir)
        for cid in cids:
            if _delete_vault_note(cid, convos_dir, state):
                removed_vault += 1
    if removed_vault:
        log(f"Deleted {removed_vault} vault note(s).")

    log(f"Pruned {len(cids)} conversation(s) — IDs added to ignore list.")
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
        log("    (b) manually resetting last_timestamp_regular to 0 in")
        log("        checkpoint/extraction_state_gemini.json to force a full re-scan.")
    else:
        log("None of the specified IDs were in the ignore list — nothing to do.")


def main():
    cfg = load_config()
    conn = get_db_connection(cfg)

    flags = {a for a in sys.argv[1:] if a.startswith("-")}
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
    chats_dir = cfg["paths"]["chats_dir"]
    log("Syncing chat files to DB before discovering candidates...")
    sync_chats_to_db(conn, chats_dir)

    known_cids = {
        row["conversation_id"]
        for row in conn.execute("SELECT conversation_id FROM chats")
    }

    if not known_cids:
        log("No conversations in DB — nothing to prune.")
        conn.close()
        return

    manifest_rows = None
    marked_del_cids = set()
    stale_del_cids = set()

    if os.path.exists(MANIFEST_PATH):
        try:
            manifest_rows = read_and_validate_manifest(MANIFEST_PATH)
        except Exception as e:
            die(f"Review manifest validation failed: {e}\nFix or remove '{MANIFEST_PATH}' to continue.")

        for r in manifest_rows:
            if r["action"] == "DEL":
                cid = r["conversation_id"]
                if cid in known_cids:
                    marked_del_cids.add(cid)
                else:
                    stale_del_cids.add(cid)

    orphans = find_orphaned_cids(known_cids, chats_dir)

    if stale_del_cids:
        log(f"Reported {len(stale_del_cids)} stale DEL row(s) in manifest (IDs no longer in DB).")

    # Build candidate lookup
    candidates = {}  # cid -> dict(reasons=set(), file_to_delete=path_or_None)

    for cid in orphans:
        candidates.setdefault(cid, {"reasons": set(), "file_to_delete": None})["reasons"].add("file missing")

    for cid in marked_del_cids:
        valid_file = validate_review_delete_file(cid, conn, chats_dir)
        if valid_file:
            cand = candidates.setdefault(cid, {"reasons": set(), "file_to_delete": None})
            cand["reasons"].add("marked DEL")
            cand["file_to_delete"] = valid_file
        else:
            if cid in orphans:
                log(f"  ⚠ Review-marked chat '{cid}' — file already missing (will still be pruned as orphan).")
            else:
                log(f"  ⚠ Review-marked chat '{cid}' JSON file validation failed — skipping.")

    if manifest_rows is not None:
        unreviewed_count = sum(1 for r in manifest_rows if r["reviewed"] in ("NO", "RE-REVIEW"))
        if unreviewed_count:
            log(f"Review manifest: {unreviewed_count} chat(s) remain unreviewed (NO or RE-REVIEW).")

    if not candidates:
        log("No eligible candidates found for pruning.")
        if stale_del_cids and "--prune" in flags and manifest_rows is not None:
            new_manifest = [r for r in manifest_rows if r["conversation_id"] not in stale_del_cids]
            write_manifest_atomically(new_manifest, MANIFEST_PATH)
            log(f"Cleaned up {len(stale_del_cids)} stale DEL row(s) from review manifest.")
        conn.close()
        return

    # Fetch DB titles for dry-run output
    db_titles = {}
    placeholders = ",".join("?" * len(candidates))
    for row in conn.execute(
        f"SELECT conversation_id, title FROM chats WHERE conversation_id IN ({placeholders})",
        list(candidates.keys()),
    ):
        db_titles[row["conversation_id"]] = row["title"] or "(no title)"

    # Build lookup of manifest flags if manifest exists
    manifest_flags = {}
    if manifest_rows:
        manifest_flags = {r["conversation_id"]: r["flags"].strip() for r in manifest_rows if r.get("flags")}

    log(f"Found {len(candidates)} conversation(s) eligible for pruning:")
    for cid in sorted(candidates.keys()):
        cand = candidates[cid]
        title = db_titles.get(cid, "(unknown)")
        reasons_str = " & ".join(sorted(cand["reasons"]))
        cand_flags = manifest_flags.get(cid)
        flags_suffix = f" | flags: {cand_flags}" if cand_flags else ""
        log(f"  - {title[:80]}  ({cid}) [{reasons_str}{flags_suffix}]")

    if "--prune" in flags:
        max_ratio = cfg.get("limits", {}).get("max_prune_ratio", 0.3)
        total_tracked = len(known_cids)
        if (
            exceeds_prune_safety_threshold(len(candidates), total_tracked, max_ratio)
            and "--confirm-large-delete" not in flags
        ):
            log(f"  ⚠ {len(candidates)} candidates out of {total_tracked} records"
                f" ({len(candidates)/total_tracked:.1%}) exceeds {max_ratio:.0%} safety threshold.")
            log("  Run with --prune --confirm-large-delete to force.")
            conn.close()
            return

        # 1. Delete JSON files on disk for review-marked candidates
        successfully_deleted_cids = []
        for cid, cand in candidates.items():
            fpath = cand["file_to_delete"]
            if fpath:
                try:
                    os.remove(fpath)
                    successfully_deleted_cids.append(cid)
                except Exception as e:
                    log(f"  ⚠ Failed to delete JSON file for '{cid}': {e}")
            else:
                successfully_deleted_cids.append(cid)

        # 2. Perform DB & vault cascade
        _do_prune(successfully_deleted_cids, conn=conn, cfg=cfg)

        # 3. Finalize manifest
        if manifest_rows is not None:
            pruned_set = set(successfully_deleted_cids)
            new_manifest = [
                r for r in manifest_rows
                if r["conversation_id"] not in pruned_set and r["conversation_id"] not in stale_del_cids
            ]
            write_manifest_atomically(new_manifest, MANIFEST_PATH)
            log(f"Updated review manifest '{MANIFEST_PATH}' — removed pruned and stale rows.")
    else:
        log("  Run with --prune to remove them from database and ignore list.")

    conn.close()


if __name__ == "__main__":
    main()
