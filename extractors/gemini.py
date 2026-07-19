"""
Gemini Web extractor — downloads chat history from gemini.google.com
using cookie-based auth (gemini_webapi).

Run via ``python -m extractors.gemini``.
"""

import asyncio
import json
import os
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
from gemini_webapi import GeminiClient
from gemini_webapi.types import ChatInfo, RPCData
from gemini_webapi.constants import GRPC
from gemini_webapi.utils import extract_json_from_response, get_nested_value
import orjson

from common import (
    load_config, log, get_db_connection, load_ignored_conversations,
    get_chat_rows,
)
from extractors.base import upsert_chat_and_file

# Suppress the library's noisy DEBUG logs ("Gemini is still working...")
logging.getLogger("gemini_webapi").setLevel(logging.WARNING)

load_dotenv()


def to_iso(ts_float: float) -> str:
    """Convert Unix timestamp to ISO 8601."""
    try:
        return datetime.fromtimestamp(ts_float, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def safe_cid(raw: str) -> str:
    """Sanitize conversation ID for filenames."""
    return raw.replace("/", "_").replace(":", "_").replace(" ", "_")


def _has_attachments(attachments: list) -> bool:
    """Check if the raw attachment list contains any leaf entries."""
    def _scan(node):
        if not isinstance(node, list):
            return False
        if len(node) > 11 and isinstance(node[2], str) and "." in node[2]:
            return True
        return any(_scan(child) for child in node)
    return _scan(attachments)


async def _read_chat_turns(client, cid: str, limit: int = 200) -> list[dict] | None:
    """Parse the raw READ_CHAT response, preserving empty-text user turns.

    The ``gemini_webapi`` library's ``read_chat()`` drops user turns whose text is
    empty — e.g. image-only, audio-only, or file-only uploads.  This parser reads
    the raw ``_batch_execute`` response and uses a fixed placeholder when
    attachment metadata is found.

    Returns a list of ``{{"role": …, "text": …}}`` dicts, **newest-first** (matching
    the library's convention).  Returns ``None`` if parsing fails.
    """
    try:
        raw_payload = orjson.dumps([cid, limit, None, 1, [1], [4], None, 1]).decode("utf-8")
        raw_resp = await client._batch_execute(
            [RPCData(rpcid=GRPC.READ_CHAT, payload=raw_payload)]
        )
    except Exception:
        return None

    parts = extract_json_from_response(raw_resp.text)
    for part in parts:
        body_str = get_nested_value(part, [2])
        if not body_str:
            continue
        try:
            body = orjson.loads(body_str)
        except orjson.JSONDecodeError:
            continue

        turns_data = get_nested_value(body, [0])
        if not isinstance(turns_data, list):
            continue

        result: list[dict] = []
        for conv_turn in turns_data:
            # ── Model turn ──────────────────────────────────────────────
            candidates_list = get_nested_value(conv_turn, [3, 0])
            if candidates_list:
                for candidate_data in candidates_list:
                    rcid = get_nested_value(candidate_data, [0])
                    if not rcid:
                        continue
                    text = get_nested_value(candidate_data, [1, 0], "")
                    if text:
                        result.append({"role": "model", "text": text})

            # ── User turn ───────────────────────────────────────────────
            user_msg = get_nested_value(conv_turn, [2, 0])
            if isinstance(user_msg, list) and len(user_msg) > 0:
                user_text = user_msg[0] if isinstance(user_msg[0], str) else ""
                if user_text:
                    result.append({"role": "user", "text": user_text})
                else:
                    # Empty text → check for attachments, otherwise fallback
                    if len(user_msg) > 4 and isinstance(user_msg[4], list) and _has_attachments(user_msg[4]):
                        result.append({"role": "user", "text": "[User sent an attachment]"})
                    else:
                        result.append({"role": "user", "text": "[Unknown user input]"})

        return result

    return None


async def main():
    cfg = load_config()
    output_dir = cfg["paths"]["chats_dir"]
    state_file = cfg["paths"]["extraction_state_file"]
    conn = get_db_connection(cfg)

    os.makedirs(output_dir, exist_ok=True)

    # ── 1. Get cookies from .env ─────────────────────────────────────────
    print("Step 1: Loading Gemini session cookies from .env...")
    sid = os.getenv("GEMINI_1PSID")
    ts = os.getenv("GEMINI_1PSIDTS")

    if not sid or not ts:
        print("""
  GEMINI_1PSID and/or GEMINI_1PSIDTS not found in .env file.

  To get them:
    1. Open Chrome/Edge and go to gemini.google.com
    2. Open DevTools (F12) -> Application -> Cookies -> gemini.google.com
    3. Find and copy the values for:
       - __Secure-1PSID
       - __Secure-1PSIDTS
    4. Create a .env file in the project root with:
       GEMINI_1PSID="your-1psid-value"
       GEMINI_1PSIDTS="your-1psidts-value"
""")
        return

    # ── 2. Connect ──────────────────────────────────────────────────────
    print("\nStep 2: Connecting to Gemini Web API...")
    client = GeminiClient(secure_1psid=sid, secure_1psidts=ts)
    try:
        await client.init()
    except Exception as e:
        print(f"  Connection failed: {e}")
        print("  Your cookies may have expired. Re-login to gemini.google.com and update .env.")
        return

    from gemini_webapi.constants import AccountStatus
    if client.account_status != AccountStatus.AVAILABLE:
        print(f"\n  ✗ Account status: {client.account_status.name} — {client.account_status.description}")
        print("""
  Your Gemini session cookies have expired or are invalid.

  To get fresh cookies:
    1. Open Chrome/Edge and go to gemini.google.com
    2. Open DevTools (F12) -> Application -> Cookies -> gemini.google.com
    3. Find and copy the values for:
       - __Secure-1PSID
       - __Secure-1PSIDTS
    4. Update your .env file with the new values:
       GEMINI_1PSID="your-1psid-value"
       GEMINI_1PSIDTS="your-1psidts-value"
""")
        return

    print("  Connected successfully!")

    # ── 3. Load resume state (before pagination) ────────────────────────
    # Only track a checkpoint for regular chats. Pinned chats are always
    # fetched in full (<100) so we can reconcile pin status changes.
    # Ignore list lives in SQLite (populated by classify_chats.py --prune).
    last_ts_regular: float = 0.0
    ignore_ids: set[str] = set()
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
                last_ts_regular = state.get("last_timestamp_regular", 0.0)
        except Exception as e:
            print(f"  ⚠ Could not load state file {state_file}: {e}")
    ignore_ids = load_ignored_conversations(conn)
    if last_ts_regular:
        log(f"  Resume timestamp (regular): {last_ts_regular:.0f}")
    if ignore_ids:
        log(f"  Ignore list: {len(ignore_ids)} conversation IDs will be skipped")

    # ── 4. Fetch chats with pagination (stop early if already known) ────
    print("\nStep 4: Loading chat history (paginating)...")
    all_chats: list[ChatInfo] = []
    seen_cids: set[str] = set()

    async def fetch_page(filter_type: list, cursor: str | None = None):
        """Fetch one page and return (new_cursor, chat_list)."""
        payload = orjson.dumps([100, cursor, filter_type])
        resp = await client._batch_execute(
            [RPCData(rpcid=GRPC.LIST_CHATS, payload=payload.decode("utf-8"))]
        )
        parts = extract_json_from_response(resp.text)
        for part in parts:
            body_str = get_nested_value(part, [2])
            if not body_str:
                continue
            try:
                body = orjson.loads(body_str)
            except orjson.JSONDecodeError:
                continue
            if not isinstance(body, list):
                continue
            chat_list = get_nested_value(body, [2]) if len(body) > 2 else None
            if not isinstance(chat_list, list):
                continue  # this part isn't the chat-listing payload — keep scanning
            new_cursor = body[1] if len(body) > 1 else None
            if not isinstance(new_cursor, str):
                new_cursor = None
            return new_cursor, chat_list
        return None, []

    def parse_chats(chat_list: list) -> list[ChatInfo]:
        """Parse raw chat data into ChatInfo objects."""
        result = []
        for chat_data in chat_list:
            if not isinstance(chat_data, list) or len(chat_data) < 2:
                continue
            cid = get_nested_value(chat_data, [0], "")
            title = get_nested_value(chat_data, [1], "")
            if not cid or cid in seen_cids:
                continue
            seen_cids.add(cid)
            raw_pinned = get_nested_value(chat_data, [2])
            # chat_type = "pinned" if raw_pinned else "regular"
            ts_data = get_nested_value(chat_data, [5])
            ts = 0.0
            if isinstance(ts_data, list) and len(ts_data) >= 2:
                ts = float(ts_data[0]) + (float(ts_data[1]) / 1e9)
            result.append(ChatInfo(cid=cid, title=title, is_pinned=bool(raw_pinned), timestamp=ts))
        return result

    async def fetch_all_with_resume(filter_type: list, label: str, resume_ts: float):
        """Paginate, stopping early once the newest chat on a page is not
        newer than *resume_ts*. Returns the list of ChatInfo objects."""
        result: list[ChatInfo] = []
        cursor = None
        page = 0
        while True:
            page += 1
            cursor, raw_chats = await fetch_page(filter_type, cursor)
            parsed = parse_chats(raw_chats)
            result.extend(parsed)

            # Chats are newest-first — if the *last* (oldest) chat on this
            # page is not newer than our resume timestamp, there's nothing
            # new ahead.
            oldest_on_page = parsed[-1].timestamp if parsed else 0
            if resume_ts and oldest_on_page and oldest_on_page <= resume_ts:
                print(f"  Page {page} ({label}): {len(result)} chats (caught up, stopping)")
                break

            print(f"  Page {page} ({label}): {len(result)} chats...")
            if not cursor:
                break
        return result

    regular_chats = await fetch_all_with_resume([0, None, 1], "regular", last_ts_regular)
    pinned_chats = await fetch_all_with_resume([1, None, 1], "pinned", 0.0)  # always fetch all pinned

    # Inject into the client so list_chats() works
    all_chats = regular_chats + pinned_chats
    client._recent_chats = all_chats
    chats = all_chats
    if not chats:
        print("  No chats found.")
        return
    print(f"  Found {len(chats)} conversations total.")

    # ── 4b. Reconcile pin status ────────────────────────────────────────
    # Pinned chats are always fetched in full, so we can detect when a chat
    # was pinned or unpinned on the server and update the DB accordingly.
    pinned_in_metadata = {f"gemini_{safe_cid(info.cid)}" for info in pinned_chats}
    pinned_in_db = {
        row["conversation_id"]
        for row in conn.execute(
            "SELECT conversation_id FROM chats WHERE source = 'gemini_web' AND chat_type = 'pinned'"
        )
    }

    newly_pinned = (pinned_in_metadata - pinned_in_db) - ignore_ids
    newly_unpinned = pinned_in_db - pinned_in_metadata
    newly_pinned_count = len(newly_pinned)
    newly_unpinned_count = len(newly_unpinned)

    if newly_pinned_count or newly_unpinned_count:
        conn.executemany(
            "UPDATE chats SET chat_type = 'pinned' WHERE conversation_id = ?",
            [(cid,) for cid in newly_pinned],
        )
        conn.executemany(
            "UPDATE chats SET chat_type = 'regular' WHERE conversation_id = ?",
            [(cid,) for cid in newly_unpinned],
        )
        conn.commit()
        log(f"  Reconciled pin status: {newly_pinned_count} newly pinned, {newly_unpinned_count} newly unpinned")

    # ── 5. Download new chats ───────────────────────────────────────────
    # Merge regular + pinned into one flat list — order no longer matters.
    # The per-chat DB lookup (not list position) decides skip vs download.
    # Batch-query all fetched IDs upfront to avoid N individual SQL calls.
    print(f"\nStep 5: Downloading new conversations to '{output_dir}/'...")
    downloaded = 0
    errors = 0

    fetched_ids = [f"gemini_{safe_cid(info.cid)}" for info in chats]
    existing_rows = get_chat_rows(conn, fetched_ids)

    for info in chats:
        cid = info.cid
        cid_norm = safe_cid(cid)
        title = info.title or "Untitled"
        conv_label = f"gemini_{cid_norm}"

        # Permanent, intentional exclusion — not a gap
        if conv_label in ignore_ids:
            print(f"  {title[:80]} (skipped — in ignore list)")
            continue

        # Already current? Check the chats table directly.
        existing = existing_rows.get(conv_label)
        if existing and existing["updated_at"] == to_iso(info.timestamp):
            print(f"  {title[:80]} (already current)")
            continue

        print(f"  {title[:80]}")

        try:
            raw_turns = await _read_chat_turns(client, cid=cid, limit=200)
            if not raw_turns:
                print("    -> No turns (incomplete chat), skipping")
                continue

            # raw_turns are newest-first; reverse so turn 1 = oldest
            turns = []
            for i, turn in enumerate(reversed(raw_turns), 1):
                turns.append({
                    "turn_number": i,
                    "role": turn["role"],
                    "timestamp": to_iso(info.timestamp) if info.timestamp else "",
                    "text": turn["text"],
                })

            payload = {
                "conversation_id": conv_label,
                "source": "gemini_web",
                "title": title,
                "created_at": to_iso(info.timestamp) if info.timestamp else "",
                "updated_at": to_iso(info.timestamp) if info.timestamp else "",
                "turn_count": len(turns),
                "turns": turns,
            }

            out_name = f"gemini_{cid_norm}.json"
            upsert_chat_and_file(
                conn,
                payload=payload,
                out_name=out_name,
                output_dir=output_dir,
                timestamp=info.timestamp,
                chat_type="pinned" if info.is_pinned else "regular",
            )

            downloaded += 1

        except Exception as e:
            print(f"    ! Error: {e}")
            errors += 1

    # ── 6. Save checkpoint only after a fully successful run ────────────
    # Only track the regular checkpoint — pinned chats are always fetched.
    if errors == 0:
        row_r = conn.execute(
            "SELECT MAX(file_mtime) AS max_mtime FROM chats "
            "WHERE source = 'gemini_web' AND chat_type = 'regular'"
        ).fetchone()
        cp_regular = row_r["max_mtime"] if row_r and row_r["max_mtime"] is not None else last_ts_regular
        from datetime import datetime, timezone
        state = {
            "last_timestamp_regular": cp_regular,
            "last_checkpoint_at": datetime.now(timezone.utc).isoformat(),
        }
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        tmp = state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, state_file)  # atomic on POSIX and Windows
        log(f"Checkpoint saved (regular={cp_regular:.0f})")
    else:
        log(f"{errors} error(s) — checkpoint NOT updated")

    conn.close()

    # ── Summary ─────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print("Done!")
    print(f"  Downloaded:           {downloaded}")
    print(f"  Errors:               {errors}")
    print(f"  Pin status reconciled: {newly_pinned_count} pinned, {newly_unpinned_count} unpinned")
    print(f"  Output:               {output_dir}/")
    print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())
