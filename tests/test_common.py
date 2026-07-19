"""
Tests for common.py — chat_fingerprint, yaml_str, canonicalize_topic,
SQLite helpers, prune helpers.
"""

import json

from common import chat_fingerprint, yaml_str, canonicalize_topic


# ── chat_fingerprint ─────────────────────────────────────────────────────────


class TestChatFingerprint:
    def test_identical_chats_produce_identical_hashes(self, make_chat):
        chat_a = make_chat()
        chat_b = make_chat()
        assert chat_fingerprint(chat_a) == chat_fingerprint(chat_b)

    def test_different_turn_text_produces_different_hashes(self, make_chat):
        chat_a = make_chat(turns=[{"role": "user", "text": "Hello"}])
        chat_b = make_chat(turns=[{"role": "user", "text": "Goodbye"}])
        assert chat_fingerprint(chat_a) != chat_fingerprint(chat_b)

    def test_only_metadata_change_does_not_affect_hash(self, make_chat):
        """The whole point of hashing parsed fields, not raw bytes:
        a metadata-only touch (e.g. Gemini bumping updated_at) must
        NOT change the fingerprint."""
        chat_a = make_chat()
        chat_b = make_chat()
        chat_b["created_at"] = "2099-12-31T23:59:59+00:00"
        chat_b["updated_at"] = "2099-12-31T23:59:59+00:00"
        assert chat_fingerprint(chat_a) == chat_fingerprint(chat_b)

    def test_swapped_role_with_same_text_changes_hash(self, make_chat):
        """user<->model swap with identical text is semantically different
        and must produce a different fingerprint."""
        chat_a = make_chat(
            turns=[
                {"role": "user", "text": "Hello"},
                {"role": "model", "text": "Hi there"},
            ]
        )
        chat_b = make_chat(
            turns=[
                {"role": "model", "text": "Hello"},
                {"role": "user", "text": "Hi there"},
            ]
        )
        assert chat_fingerprint(chat_a) != chat_fingerprint(chat_b)

    def test_empty_turns_list(self, make_chat):
        chat = make_chat(turns=[])
        fp = chat_fingerprint(chat)
        assert isinstance(fp, str) and len(fp) == 16

    def test_missing_title_defaults_to_empty_string(self):
        chat = {"turns": [{"role": "user", "text": "Hi"}]}
        fp = chat_fingerprint(chat)
        assert isinstance(fp, str) and len(fp) == 16

    def test_none_turn_text_is_handled_safely(self, make_chat):
        chat = make_chat(turns=[{"role": "user", "text": None}])
        fp = chat_fingerprint(chat)
        assert isinstance(fp, str) and len(fp) == 16


# ── yaml_str ────────────────────────────────────────────────────────────────


class TestYamlStr:
    def test_plain_string_passes_through(self):
        assert yaml_str("hello") == '"hello"'

    def test_quotes_get_escaped(self):
        result = yaml_str('say "hello"')
        assert result == '"say \\"hello\\""'

    def test_backslashes_get_escaped(self):
        result = yaml_str("C:\\Users\\test")
        assert result == '"C:\\\\Users\\\\test"'

    def test_newlines_get_stripped(self):
        result = yaml_str("line1\nline2\r\nline3")
        assert "\n" not in result
        assert result.startswith('"') and result.endswith('"')

    def test_combined_special_characters(self):
        result = yaml_str('He said "hello"\nand then "goodbye"')
        assert result.count('\\"') >= 2
        assert "\n" not in result


# ── canonicalize_topic ──────────────────────────────────────────────────────


class TestCanonicalizeTopic:
    def setup_method(self):
        self.canonical_map = {
            "prompt engineering": "Prompt Engineering",
            "machine learning": "Machine Learning",
        }

    def test_exact_match_returns_canonical(self):
        assert canonicalize_topic("Prompt Engineering", self.canonical_map) == "Prompt Engineering"

    def test_case_insensitive_match(self):
        assert canonicalize_topic("prompt engineering", self.canonical_map) == "Prompt Engineering"

    def test_mixed_case_match(self):
        assert canonicalize_topic("PROMPT Engineering", self.canonical_map) == "Prompt Engineering"

    def test_whitespace_tolerance(self):
        assert canonicalize_topic("  Prompt Engineering  ", self.canonical_map) == "Prompt Engineering"

    def test_unknown_topic_passes_through_unchanged(self):
        assert canonicalize_topic("Brand New Topic", self.canonical_map) == "Brand New Topic"


# ── SQLite DB helpers ───────────────────────────────────────────────────────


class TestInitDb:
    def test_creates_table_and_index(self, db_conn):
        """init_db creates the classifications table and status index."""
        tables = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='classifications'"
        ).fetchone()
        assert tables is not None

        indexes = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_status'"
        ).fetchone()
        assert indexes is not None

    def test_idempotent(self, db_conn):
        """Calling init_db twice does not raise."""
        from common import init_db
        init_db(db_conn)  # second call
        assert True


class TestUpsertAndLoadAllClassifications:
    def _insert(self, db_conn, rec):
        from common import upsert_classification
        upsert_classification(db_conn, rec)

    def _load(self, db_conn):
        from common import load_all_classifications
        return load_all_classifications(db_conn)

    def test_round_trip(self, db_conn):
        self._insert(db_conn, {
            "conversation_id": "cid1",
            "title": "Test",
            "category": ["Programming"],
            "topic": ["Python"],
            "summary": "A chat",
            "content_hash": "abcd1234",
            "status": "ok",
            "error": None,
            "classified_at": "2026-07-12T00:00:00+00:00",
        })
        out = self._load(db_conn)
        assert "cid1" in out
        rec = out["cid1"]
        assert rec["category"] == ["Programming"]
        assert rec["topic"] == ["Python"]
        assert rec["summary"] == "A chat"
        assert rec["status"] == "ok"
        assert rec["content_hash"] == "abcd1234"

    def test_empty_db_returns_empty_dict(self, db_conn):
        assert self._load(db_conn) == {}

    def test_upsert_replaces_by_conversation_id(self, db_conn):
        self._insert(db_conn, {
            "conversation_id": "cid1",
            "title": "Old",
            "category": ["Old"],
            "topic": [],
            "summary": "",
            "content_hash": "old",
            "status": "ok",
            "error": None,
            "classified_at": "",
        })
        self._insert(db_conn, {
            "conversation_id": "cid1",
            "title": "New",
            "category": ["Programming"],
            "topic": ["Python"],
            "summary": "Updated",
            "content_hash": "new",
            "status": "ok",
            "error": None,
            "classified_at": "",
        })
        out = self._load(db_conn)
        assert out["cid1"]["title"] == "New"
        assert out["cid1"]["category"] == ["Programming"]
        assert out["cid1"]["content_hash"] == "new"

    def test_multiple_cids_all_returned(self, db_conn):
        for i in range(3):
            self._insert(db_conn, {
                "conversation_id": f"cid{i}",
                "title": f"Chat{i}",
                "category": [],
                "topic": [],
                "summary": "",
                "content_hash": f"hash{i}",
                "status": "ok",
                "error": None,
                "classified_at": "",
            })
        out = self._load(db_conn)
        assert set(out.keys()) == {"cid0", "cid1", "cid2"}

    def test_error_record_loads_safely(self, db_conn):
        self._insert(db_conn, {
            "conversation_id": "err",
            "title": "Err",
            "category": [],
            "topic": [],
            "summary": "",
            "content_hash": "",
            "status": "error",
            "error": "API timeout",
            "classified_at": "",
        })
        out = self._load(db_conn)
        rec = out["err"]
        assert rec["category"] == []
        assert rec["topic"] == []
        assert rec["status"] == "error"
        assert rec["error"] == "API timeout"


class TestDeleteClassifications:
    def _insert(self, db_conn, cid):
        from common import upsert_classification
        upsert_classification(db_conn, {
            "conversation_id": cid,
            "title": "",
            "category": [],
            "topic": [],
            "summary": "",
            "content_hash": "",
            "status": "ok",
            "error": None,
            "classified_at": "",
        })

    def test_deletes_specific_cids(self, db_conn):
        from common import delete_classifications
        self._insert(db_conn, "a")
        self._insert(db_conn, "b")
        self._insert(db_conn, "c")
        n = delete_classifications(db_conn, ["a", "c"])
        assert n == 2
        remaining = {r["conversation_id"] for r in
                     db_conn.execute("SELECT conversation_id FROM classifications")}
        assert remaining == {"b"}

    def test_empty_cid_list_returns_zero(self, db_conn):
        from common import delete_classifications
        assert delete_classifications(db_conn, []) == 0

    def test_nonexistent_cid_does_not_raise(self, db_conn):
        from common import delete_classifications
        n = delete_classifications(db_conn, ["ghost"])
        assert n == 0


# ── Prune helpers ───────────────────────────────────────────────────────────


class TestFindOrphanedCids:
    def test_no_orphans_when_all_chats_exist(self, tmp_path, write_chat, make_chat):
        from common import find_orphaned_cids
        make_chat(cid="a")
        make_chat(cid="b")
        chats_dir = write_chat(make_chat(cid="a"))
        write_chat(make_chat(cid="b"))
        orphans = find_orphaned_cids({"a", "b"}, str(chats_dir))
        assert orphans == set()

    def test_returns_orphans_when_chat_file_missing(self, tmp_path, write_chat, make_chat):
        from common import find_orphaned_cids
        chats_dir = write_chat(make_chat(cid="a"))
        orphans = find_orphaned_cids({"a", "b"}, str(chats_dir))
        assert orphans == {"b"}

    def test_matches_by_conversation_id_not_filename(self, tmp_path, write_chat, make_chat):
        """A file named differently than the cid should still be matched."""
        from common import find_orphaned_cids
        chat = make_chat(cid="real_id")
        chat_dir = tmp_path / "chats"
        chat_dir.mkdir(exist_ok=True)
        (chat_dir / "weird_name.json").write_text(json.dumps(chat), encoding="utf-8")
        orphans = find_orphaned_cids({"real_id"}, str(chat_dir))
        assert orphans == set()

    def test_missing_chats_dir_returns_empty_set(self, tmp_path):
        from common import find_orphaned_cids
        orphans = find_orphaned_cids({"a"}, str(tmp_path / "nonexistent"))
        assert orphans == set()

    def test_only_json_files_are_considered(self, tmp_path, make_chat):
        from common import find_orphaned_cids
        chat_dir = tmp_path / "chats"
        chat_dir.mkdir()
        (chat_dir / "not_json.txt").write_text("garbage", encoding="utf-8")
        # No .json files → existing_cids is empty → everything is orphaned
        orphans = find_orphaned_cids({"a"}, str(chat_dir))
        assert orphans == {"a"}


class TestExceedsPruneSafetyThreshold:
    def test_below_threshold(self):
        from common import exceeds_prune_safety_threshold
        assert exceeds_prune_safety_threshold(2, 10, max_ratio=0.3) is False

    def test_at_threshold(self):
        from common import exceeds_prune_safety_threshold
        assert exceeds_prune_safety_threshold(3, 10, max_ratio=0.3) is False

    def test_above_threshold(self):
        from common import exceeds_prune_safety_threshold
        assert exceeds_prune_safety_threshold(4, 10, max_ratio=0.3) is True

    def test_zero_known_count_returns_false(self):
        from common import exceeds_prune_safety_threshold
        assert exceeds_prune_safety_threshold(5, 0) is False

    def test_negative_values(self):
        from common import exceeds_prune_safety_threshold
        # Both negative → behaves like 0/0 which is False
        assert exceeds_prune_safety_threshold(-1, 10) is False

    def test_default_ratio_is_0_3(self):
        from common import exceeds_prune_safety_threshold
        # 4/10 = 0.4 > 0.3 → True
        assert exceeds_prune_safety_threshold(4, 10) is True
        # 3/10 = 0.3 == 0.3 → False (not strictly greater)
        assert exceeds_prune_safety_threshold(3, 10) is False


# ── Chats table helpers ─────────────────────────────────────────────────────


class TestUpsertChat:
    def _insert(self, db_conn, cid="cid1", **overrides):
        from common import upsert_chat
        record = {
            "conversation_id": cid,
            "source": "gemini_web",
            "title": "Test Chat",
            "content_hash": "abcd1234",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "first_seen_at": "2026-07-01T00:00:00+00:00",
            "last_seen_at": "2026-07-01T00:00:00+00:00",
        }
        record.update(overrides)
        upsert_chat(db_conn, record)

    def test_round_trip(self, db_conn):
        self._insert(db_conn)
        from common import get_chat_row
        row = get_chat_row(db_conn, "cid1")
        assert row is not None
        assert row["conversation_id"] == "cid1"
        assert row["source"] == "gemini_web"
        assert row["title"] == "Test Chat"
        assert row["content_hash"] == "abcd1234"

    def test_upsert_replaces_by_pk(self, db_conn):
        self._insert(db_conn, title="Old Title", content_hash="old_hash")
        self._insert(db_conn, title="New Title", content_hash="new_hash")
        from common import get_chat_row
        row = get_chat_row(db_conn, "cid1")
        assert row["title"] == "New Title"
        assert row["content_hash"] == "new_hash"

    def test_first_seen_at_preserved_on_update(self, db_conn):
        self._insert(db_conn, first_seen_at="2026-01-01T00:00:00+00:00")
        self._insert(db_conn, title="Updated",
                     first_seen_at="2099-01-01T00:00:00+00:00")
        from common import get_chat_row
        row = get_chat_row(db_conn, "cid1")
        assert row["title"] == "Updated"
        # first_seen_at must NOT have been overwritten
        assert row["first_seen_at"] == "2026-01-01T00:00:00+00:00"

    def test_last_seen_at_updated_on_conflict(self, db_conn):
        self._insert(db_conn, last_seen_at="2026-01-01T00:00:00+00:00")
        self._insert(db_conn, last_seen_at="2026-07-01T00:00:00+00:00")
        from common import get_chat_row
        row = get_chat_row(db_conn, "cid1")
        assert row["last_seen_at"] == "2026-07-01T00:00:00+00:00"


class TestGetChatRow:
    def test_returns_row_for_known_cid(self, db_conn):
        from common import upsert_chat
        upsert_chat(db_conn, {
            "conversation_id": "known",
            "source": "test",
            "content_hash": "h1",
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-01-01T00:00:00+00:00",
        })
        from common import get_chat_row
        row = get_chat_row(db_conn, "known")
        assert row is not None
        assert row["conversation_id"] == "known"

    def test_returns_none_for_unknown_cid(self, db_conn):
        from common import get_chat_row
        row = get_chat_row(db_conn, "nonexistent")
        assert row is None


class TestSyncChatsToDb:
    def test_new_file_gets_inserted(self, tmp_path, make_chat, db_conn):
        from common import sync_chats_to_db, get_chat_row
        chats_dir = tmp_path / "chats"
        chats_dir.mkdir()
        chat = make_chat(cid="gemini_new")
        (chats_dir / "gemini_new.json").write_text(
            json.dumps(chat), encoding="utf-8"
        )
        sync_chats_to_db(db_conn, str(chats_dir))
        row = get_chat_row(db_conn, "gemini_new")
        assert row is not None
        assert row["content_hash"] == chat_fingerprint(chat)

    def test_unchanged_file_does_not_rewrite(self, tmp_path, make_chat, db_conn):
        from common import sync_chats_to_db, get_chat_row, upsert_chat
        chats_dir = tmp_path / "chats"
        chats_dir.mkdir()
        chat = make_chat(cid="gemini_unchanged")
        fp = chat_fingerprint(chat)
        (chats_dir / "gemini_unchanged.json").write_text(
            json.dumps(chat), encoding="utf-8"
        )
        # Pre-insert with the same hash
        upsert_chat(db_conn, {
            "conversation_id": "gemini_unchanged",
            "source": "gemini_web",
            "content_hash": fp,
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-01-01T00:00:00+00:00",
        })
        before = get_chat_row(db_conn, "gemini_unchanged")["last_seen_at"]
        # Running sync should skip it (hash unchanged), not touching last_seen_at
        sync_chats_to_db(db_conn, str(chats_dir))
        after = get_chat_row(db_conn, "gemini_unchanged")["last_seen_at"]
        assert before == after

    def test_changed_file_updates_content_hash(self, tmp_path, make_chat, db_conn):
        from common import sync_chats_to_db, get_chat_row, upsert_chat
        chats_dir = tmp_path / "chats"
        chats_dir.mkdir()
        chat = make_chat(cid="gemini_changed")
        (chats_dir / "gemini_changed.json").write_text(
            json.dumps(chat), encoding="utf-8"
        )
        # Pre-insert with a stale/different hash
        upsert_chat(db_conn, {
            "conversation_id": "gemini_changed",
            "source": "gemini_web",
            "content_hash": "stale_hash",
            "first_seen_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-01-01T00:00:00+00:00",
        })
        sync_chats_to_db(db_conn, str(chats_dir))
        row = get_chat_row(db_conn, "gemini_changed")
        assert row["content_hash"] == chat_fingerprint(chat)


class TestGetChatRows:
    def test_returns_matching_rows(self, db_conn):
        from common import upsert_chat, get_chat_rows
        for cid in ["a", "b", "c"]:
            upsert_chat(db_conn, {
                "conversation_id": cid,
                "source": "test",
                "content_hash": f"h{cid}",
                "first_seen_at": "2026-01-01T00:00:00+00:00",
                "last_seen_at": "2026-01-01T00:00:00+00:00",
            })
        result = get_chat_rows(db_conn, ["a", "c", "missing"])
        assert set(result.keys()) == {"a", "c"}
        assert result["a"]["content_hash"] == "ha"

    def test_empty_list_returns_empty_dict(self, db_conn):
        from common import get_chat_rows
        assert get_chat_rows(db_conn, []) == {}


# ── Ignored conversation helpers ───────────────────────────────────────────


class TestListIgnored:
    def test_list_ignored_shows_rows(self, db_conn):
        from common import add_ignored_conversations
        add_ignored_conversations(db_conn, ["cid1", "cid2"], reason="test")
        rows = db_conn.execute(
            "SELECT conversation_id, reason"
            " FROM ignored_conversations ORDER BY conversation_id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["conversation_id"] == "cid1"

    def test_unignore_removes_row(self, db_conn):
        from common import add_ignored_conversations, remove_ignored_conversations
        add_ignored_conversations(db_conn, ["cid1"], reason="test")
        n = remove_ignored_conversations(db_conn, ["cid1"])
        assert n == 1
        remaining = db_conn.execute(
            "SELECT conversation_id FROM ignored_conversations"
        ).fetchall()
        assert len(remaining) == 0

    def test_unignore_nonexistent_cid_is_noop(self, db_conn):
        from common import remove_ignored_conversations
        n = remove_ignored_conversations(db_conn, ["ghost"])
        assert n == 0
