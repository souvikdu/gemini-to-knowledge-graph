"""
Shared utilities for extractor modules.

``upsert_chat_and_file`` is the common "write JSON + set mtime + upsert DB"
pattern used by every extractor.
"""

import json
import os
from datetime import datetime, timezone

from common import chat_fingerprint, upsert_chat


def upsert_chat_and_file(
    conn,
    *,
    payload: dict,
    out_name: str,
    output_dir: str,
    timestamp: float | None = None,
    chat_type: str = "regular",
) -> None:
    """Write *payload* to ``output_dir/out_name``, set its mtime to
    *timestamp* (if given), and upsert the corresponding row into the
    ``chats`` table.

    This is the shared "write + touch + upsert" pattern used by every
    extractor module.
    """
    out_path = os.path.join(output_dir, out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    # Set on-disk mtime to match the conversation's actual timestamp
    if timestamp:
        os.utime(out_path, (timestamp, timestamp))

    # Mirror into the chats table
    now_iso = datetime.now(timezone.utc).isoformat()
    fp = chat_fingerprint(payload)
    upsert_chat(conn, {
        "conversation_id": payload["conversation_id"],
        "source": payload.get("source", ""),
        "title": payload.get("title"),
        "content_hash": fp,
        "source_file": out_name,
        "file_mtime": timestamp or 0.0,
        "chat_type": chat_type,
        "created_at": payload.get("created_at", ""),
        "updated_at": payload.get("updated_at", ""),
        "first_seen_at": now_iso,
        "last_seen_at": now_iso,
    })
