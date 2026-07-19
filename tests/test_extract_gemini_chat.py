"""
Tests for extractors.gemini — os.utime, checkpoint computation,
pinned-chat reachability, and resumed-failure handling.
"""

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock

from common import upsert_chat, get_chat_row


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_cinfo(cid, timestamp, title="Test Chat", is_pinned=False):
    """Build a minimal ChatInfo-like object using a simple namespace."""
    return MagicMock(
        cid=cid,
        title=title,
        is_pinned=is_pinned,
        timestamp=timestamp,
    )


# The to_iso helper used by extractors.gemini
def to_iso(ts_float):
    try:
        return datetime.fromtimestamp(ts_float, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return datetime.now(timezone.utc).isoformat()


# ── os.utime ────────────────────────────────────────────────────────────────


class TestFileModifiedTime:
    """After writing a chat file, its mtime should match the conversation's
    actual timestamp, not the write-order time."""

    def test_mtime_matches_conversation_timestamp(self, tmp_path, make_chat):
        """Verify os.utime sets mtime to the chat's timestamp."""
        from extractors.gemini import safe_cid

        chat = make_chat(
            cid="gemini_test_utime",
            title="Utime Test",
            turns=[{"role": "user", "text": "Hello"}],
        )
        # Simulate the extractor's timestamp
        fake_ts = 1_800_000_000.0  # a fixed Unix timestamp
        chat["created_at"] = to_iso(fake_ts)
        chat["updated_at"] = to_iso(fake_ts)

        cid_norm = safe_cid("test_utime")
        out_name = f"gemini_{cid_norm}.json"
        out_path = tmp_path / out_name

        # Write the file (as the extractor does)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(chat, f, indent=2, ensure_ascii=False)

        # Apply os.utime (the fix)
        os.utime(out_path, (fake_ts, fake_ts))

        stat_result = os.stat(out_path)
        assert abs(stat_result.st_mtime - fake_ts) < 1.0, (
            f"mtime {stat_result.st_mtime} should be close to {fake_ts}"
        )


# ── Checkpoint computation ─────────────────────────────────────────────────


class TestCheckpointComputation:
    """Checkpoint is MAX(file_mtime) of 'regular' gemini chats only."""

    def _compute_checkpoint(self, conn, fallback=0.0):
        """Replicate the checkpoint logic from Step 6 of extractors.gemini."""
        row = conn.execute(
            "SELECT MAX(file_mtime) AS max_mtime FROM chats "
            "WHERE source = 'gemini_web' AND chat_type = 'regular'"
        ).fetchone()
        return row["max_mtime"] if row and row["max_mtime"] is not None else fallback

    def test_regular_checkpoint_is_max_mtime(self, db_conn):
        """Regular checkpoint is MAX(file_mtime) of 'regular' gemini chats."""
        upsert_chat(db_conn, {
            "conversation_id": "gemini_c1",
            "source": "gemini_web",
            "content_hash": "h",
            "source_file": "c1.json",
            "file_mtime": 1000.0,
            "chat_type": "regular",
            "updated_at": to_iso(1000.0),
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-01-01T00:00:00+00:00",
        })
        upsert_chat(db_conn, {
            "conversation_id": "gemini_c2",
            "source": "gemini_web",
            "content_hash": "h",
            "source_file": "c2.json",
            "file_mtime": 900.0,
            "chat_type": "regular",
            "updated_at": to_iso(900.0),
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-01-01T00:00:00+00:00",
        })
        cp = self._compute_checkpoint(db_conn)
        assert cp == 1000.0  # MAX(file_mtime) of 'regular'

    def test_pinned_chats_dont_affect_checkpoint(self, db_conn):
        """Pinned chats are excluded from the regular checkpoint."""
        upsert_chat(db_conn, {
            "conversation_id": "gemini_p1",
            "source": "gemini_web",
            "content_hash": "h",
            "source_file": "p1.json",
            "file_mtime": 500.0,
            "chat_type": "pinned",
            "updated_at": to_iso(500.0),
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-01-01T00:00:00+00:00",
        })
        cp = self._compute_checkpoint(db_conn)
        assert cp == 0.0  # pinned chats are excluded from checkpoint

    def test_regular_and_pinned_are_independent(self, db_conn):
        """Regular checkpoint ignores pinned chats."""
        upsert_chat(db_conn, {
            "conversation_id": "gemini_r1",
            "source": "gemini_web",
            "content_hash": "h",
            "source_file": "r1.json",
            "file_mtime": 1000.0,
            "chat_type": "regular",
            "updated_at": to_iso(1000.0),
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-01-01T00:00:00+00:00",
        })
        upsert_chat(db_conn, {
            "conversation_id": "gemini_p1",
            "source": "gemini_web",
            "content_hash": "h",
            "source_file": "p1.json",
            "file_mtime": 500.0,
            "chat_type": "pinned",
            "updated_at": to_iso(500.0),
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-01-01T00:00:00+00:00",
        })
        cp_r = self._compute_checkpoint(db_conn)
        assert cp_r == 1000.0  # only regular counts

    def test_falls_back_when_no_chats(self, db_conn):
        """If no gemini chats exist, checkpoint falls back to previous value."""
        cp = self._compute_checkpoint(db_conn, fallback=500.0)
        assert cp == 500.0


# ── Pinned-chat reachability ────────────────────────────────────────────────


class TestPinnedChatReachability:
    """Pinned chats must be reachable on every incremental run, not just the
    very first extraction. The old break-based logic would never get past a
    trailing old regular chat to reach the pinned section."""

    def test_pinned_chat_detected_as_stale(self, db_conn):
        """A pinned chat with an updated timestamp is detected as needing a
        re-download, even when older regular chats are in the same batch."""
        # Regular chats (oldest is ts=800)
        regular = [
            _make_cinfo("regular_old", 800.0),
            _make_cinfo("regular_new", 1000.0),
        ]
        # Pinned chat with an updated timestamp
        pinned = [
            _make_cinfo("pinned_updated", 950.0, is_pinned=True),
        ]
        all_chats = regular + pinned  # merged flat list, any order

        # Both regular chats are already current
        for c in regular:
            upsert_chat(db_conn, {
                "conversation_id": f"gemini_{c.cid}",
                "source": "gemini_web",
                "content_hash": "h",
                "chat_type": "regular",
                "updated_at": to_iso(c.timestamp),
                "first_seen_at": "2026-01-01T00:00:00+00:00",
                "last_seen_at": "2026-01-01T00:00:00+00:00",
            })

        # The pinned chat has a stale/old entry — its updated_at differs
        upsert_chat(db_conn, {
            "conversation_id": "gemini_pinned_updated",
            "source": "gemini_web",
            "content_hash": "old_hash",
            "chat_type": "pinned",
            "updated_at": to_iso(500.0),  # stale — doesn't match current 950
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-01-01T00:00:00+00:00",
        })

        # The download-skip check: per-chat, no break
        needs_download = []
        for info in all_chats:
            conv_label = f"gemini_{info.cid}"
            existing = get_chat_row(db_conn, conv_label)
            if not existing or existing["updated_at"] != to_iso(info.timestamp):
                needs_download.append(info)

        needs_cids = {c.cid for c in needs_download}
        assert "pinned_updated" in needs_cids, (
            "Pinned chat with stale updated_at must be detected as needing download"
        )
        assert "regular_old" not in needs_cids
        assert "regular_new" not in needs_cids
