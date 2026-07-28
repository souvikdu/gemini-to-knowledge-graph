"""
Tests for review_chats.py — manifest generation, regeneration, validation,
sensitive scanning, and mark-reviewed behavior.
"""

import csv
import json
import os
from datetime import datetime
from unittest.mock import patch

import pytest

import review_chats
from common import chat_fingerprint, get_db_connection, upsert_chat
from review_chats import (
    MANIFEST_HEADER,
    generate_or_refresh_manifest,
    luhn_valid,
    mark_all_reviewed,
    mask_chat_content,
    read_and_validate_manifest,
    run_mask_sensitive,
    scan_chat_file,
    scan_chat_file_verbose,
    write_manifest_atomically,
)


@pytest.fixture
def test_env(tmp_path):
    """Set up temporary database, chats_dir, and config for review tests."""
    chats_dir = tmp_path / "chats"
    chats_dir.mkdir()
    db_path = tmp_path / "chat_topics.db"
    manifest_dir = tmp_path / "review"
    manifest_dir.mkdir()
    manifest_path = manifest_dir / "chats_to_review.csv"
    sensitive_config_path = tmp_path / "sensitive_patterns.json"

    cfg = {
        "paths": {
            "chats_dir": str(chats_dir),
            "classifications_db": str(db_path),
        }
    }

    conn = get_db_connection(cfg)

    # Patch paths in review_chats
    with patch.object(review_chats, "MANIFEST_DIR", str(manifest_dir)), \
         patch.object(review_chats, "MANIFEST_PATH", str(manifest_path)), \
         patch.object(review_chats, "SENSITIVE_CONFIG_PATH", str(sensitive_config_path)):
        yield {
            "cfg": cfg,
            "conn": conn,
            "chats_dir": chats_dir,
            "manifest_path": manifest_path,
            "sensitive_config_path": sensitive_config_path,
            "tmp_path": tmp_path,
        }

    conn.close()


def create_sample_chat(chats_dir, conn, cid, title, turns_text, created_at, filename=None):
    if not filename:
        filename = f"{cid}.json"

    chat_data = {
        "conversation_id": cid,
        "title": title,
        "source": "gemini_web",
        "created_at": created_at,
        "updated_at": created_at,
        "turns": [{"role": "user", "text": t} for t in turns_text],
    }

    fpath = chats_dir / filename
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(chat_data, f, indent=2)

    fp = chat_fingerprint(chat_data)
    upsert_chat(
        conn,
        {
            "conversation_id": cid,
            "source": "gemini_web",
            "title": title,
            "content_hash": fp,
            "source_file": filename,
            "file_mtime": os.path.getmtime(fpath),
            "created_at": created_at,
            "updated_at": created_at,
        },
    )
    return chat_data, fp


class TestManifestGeneration:
    def test_initial_manifest_generation(self, test_env):
        conn = test_env["conn"]
        cfg = test_env["cfg"]
        chats_dir = test_env["chats_dir"]
        manifest_path = test_env["manifest_path"]

        create_sample_chat(chats_dir, conn, "c1", "Chat One", ["Hello"], "2026-07-18T10:00:00Z")
        create_sample_chat(chats_dir, conn, "c2", "Chat Two", ["World"], "2026-07-19T10:00:00Z")

        generate_or_refresh_manifest(conn, cfg)

        assert manifest_path.exists()
        rows = read_and_validate_manifest(str(manifest_path))
        assert len(rows) == 2

        # Sort order: newest updated_at first (c2 before c1)
        assert rows[0]["conversation_id"] == "c2"
        assert rows[0]["reviewed"] == "NO"
        assert rows[0]["action"] == "KEEP"

        assert rows[1]["conversation_id"] == "c1"
        assert rows[1]["reviewed"] == "NO"
        assert rows[1]["action"] == "KEEP"

    def test_csv_header_and_quoting(self, test_env):
        conn = test_env["conn"]
        cfg = test_env["cfg"]
        chats_dir = test_env["chats_dir"]
        manifest_path = test_env["manifest_path"]

        create_sample_chat(chats_dir, conn, "c1", 'Title with "quotes" and , comma', ["Hi"], "2026-07-18T10:00:00Z")
        generate_or_refresh_manifest(conn, cfg)

        with open(manifest_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
            assert header == MANIFEST_HEADER
            row = next(reader)
            assert row[6] == 'Title with "quotes" and , comma'


class TestManifestRegeneration:
    def test_preserves_user_actions_and_ordering(self, test_env):
        conn = test_env["conn"]
        cfg = test_env["cfg"]
        chats_dir = test_env["chats_dir"]
        manifest_path = test_env["manifest_path"]

        create_sample_chat(chats_dir, conn, "c1", "Chat One", ["Hello"], "2026-07-18T10:00:00Z")
        create_sample_chat(chats_dir, conn, "c2", "Chat Two", ["World"], "2026-07-19T10:00:00Z")

        generate_or_refresh_manifest(conn, cfg)

        # User edits manifest: marks c1 as DEL and reviewed=YES
        rows = read_and_validate_manifest(str(manifest_path))
        for r in rows:
            if r["conversation_id"] == "c1":
                r["action"] = "DEL"
                r["reviewed"] = "YES"

        write_manifest_atomically(rows, str(manifest_path))

        # Regenerate manifest
        generate_or_refresh_manifest(conn, cfg)

        refreshed = read_and_validate_manifest(str(manifest_path))
        c1_row = next(r for r in refreshed if r["conversation_id"] == "c1")
        assert c1_row["action"] == "DEL"
        assert c1_row["reviewed"] == "YES"

    def test_content_change_triggers_rereview(self, test_env):
        conn = test_env["conn"]
        cfg = test_env["cfg"]
        chats_dir = test_env["chats_dir"]
        manifest_path = test_env["manifest_path"]

        chat_data, fp1 = create_sample_chat(chats_dir, conn, "c1", "Old Title", ["Hello"], "2026-07-18T10:00:00Z")
        create_sample_chat(chats_dir, conn, "c2", "Chat Two", ["World"], "2026-07-19T10:00:00Z")

        generate_or_refresh_manifest(conn, cfg)

        # Mark both as YES
        rows = read_and_validate_manifest(str(manifest_path))
        for r in rows:
            r["reviewed"] = "YES"
        write_manifest_atomically(rows, str(manifest_path))

        # Now update chat c1 with new turn (new content_hash)
        chat_data["turns"].append({"role": "model", "text": "New response"})
        fpath = chats_dir / "c1.json"
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(chat_data, f, indent=2)

        # Upsert new hash to DB (with new updated_at so it sorts to top)
        fp2 = chat_fingerprint(chat_data)
        assert fp1 != fp2
        upsert_chat(conn, {
            "conversation_id": "c1",
            "source": "gemini_web",
            "title": "Old Title",
            "content_hash": fp2,
            "source_file": "c1.json",
            "file_mtime": os.path.getmtime(fpath),
            "created_at": "2026-07-18T10:00:00Z",
            "updated_at": "2026-07-20T10:00:00Z",
        })

        generate_or_refresh_manifest(conn, cfg)

        refreshed = read_and_validate_manifest(str(manifest_path))

        # c1 should be at top (sorted by updated_at DESC) and marked RE-REVIEW
        assert refreshed[0]["conversation_id"] == "c1"
        assert refreshed[0]["reviewed"] == "RE-REVIEW"
        assert refreshed[0]["content_hash"] == fp2

        # c2 stays YES at position 1
        assert refreshed[1]["conversation_id"] == "c2"
        assert refreshed[1]["reviewed"] == "YES"

    def test_flag_change_triggers_rereview(self, test_env):
        conn = test_env["conn"]
        cfg = test_env["cfg"]
        chats_dir = test_env["chats_dir"]
        manifest_path = test_env["manifest_path"]
        sensitive_config_path = test_env["sensitive_config_path"]

        create_sample_chat(chats_dir, conn, "c1", "Secret Chat", ["Contact test@example.com"], "2026-07-18T10:00:00Z")
        generate_or_refresh_manifest(conn, cfg)

        # Mark c1 as YES with empty flags (no config file existed yet)
        rows = read_and_validate_manifest(str(manifest_path))
        rows[0]["reviewed"] = "YES"
        rows[0]["flags"] = ""
        write_manifest_atomically(rows, str(manifest_path))

        # Add sensitive config
        config_data = {"patterns": {"email": r"[\w.+-]+@[\w-]+\.[\w.-]+"}}
        with open(sensitive_config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)

        # Force scan
        generate_or_refresh_manifest(conn, cfg, scan_sensitive=True)

        refreshed = read_and_validate_manifest(str(manifest_path))
        c1_row = refreshed[0]
        assert c1_row["flags"] == "email"
        assert c1_row["reviewed"] == "RE-REVIEW"


class TestManifestValidation:
    def test_validation_errors(self, test_env):
        manifest_path = str(test_env["manifest_path"])

        # Invalid header
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write("col1,col2\n1,2\n")

        with pytest.raises(RuntimeError, match="header mismatch"):
            read_and_validate_manifest(manifest_path)

        # Invalid action
        rows = [{
            "updated_at": "2026-01-01",
            "source": "src",
            "conversation_id": "c1",
            "content_hash": "hash",
            "reviewed": "YES",
            "action": "INVALID_ACTION",
            "title": "t",
            "flags": "",
        }]
        write_manifest_atomically(rows, manifest_path)

        with pytest.raises(RuntimeError, match="Invalid action"):
            read_and_validate_manifest(manifest_path)

        # Invalid reviewed state
        rows[0]["action"] = "KEEP"
        rows[0]["reviewed"] = "MAYBE"
        write_manifest_atomically(rows, manifest_path)

        with pytest.raises(RuntimeError, match="Invalid reviewed state"):
            read_and_validate_manifest(manifest_path)


class TestSensitiveScan:
    def test_sensitive_scanning(self, test_env):
        conn = test_env["conn"]
        cfg = test_env["cfg"]
        chats_dir = test_env["chats_dir"]
        sensitive_config_path = test_env["sensitive_config_path"]
        manifest_path = test_env["manifest_path"]

        config_data = {
            "patterns": {
                "email": r"[\w.+-]+@[\w-]+\.[\w.-]+",
            },
            "keywords": {
                "secret_key": ["confidential_token"],
            },
        }

        with open(sensitive_config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)

        create_sample_chat(
            chats_dir, conn, "c1", "Secret Chat",
            ["Contact me at test@example.com with confidential_token"],
            "2026-07-18T10:00:00Z"
        )

        generate_or_refresh_manifest(conn, cfg)

        rows = read_and_validate_manifest(str(manifest_path))
        c1_flags = rows[0]["flags"].split(",")
        assert "email" in c1_flags
        assert "secret_key" in c1_flags

    def test_array_keywords(self, test_env):
        conn = test_env["conn"]
        cfg = test_env["cfg"]
        chats_dir = test_env["chats_dir"]
        sensitive_config_path = test_env["sensitive_config_path"]
        manifest_path = test_env["manifest_path"]

        config_data = {
            "keywords": {
                "name_variations": ["John", "Jonathan"],
                "greeting": ["hello"],
            },
        }

        with open(sensitive_config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)

        # Chat contains one match from the array and one from the string
        create_sample_chat(
            chats_dir, conn, "c1", "Chat with Jonathan",
            ["Jonathan said hello"],
            "2026-07-18T10:00:00Z"
        )

        generate_or_refresh_manifest(conn, cfg)

        rows = read_and_validate_manifest(str(manifest_path))
        c1_flags = rows[0]["flags"].split(",")
        assert "name_variations" in c1_flags
        assert "greeting" in c1_flags

    def test_array_keywords_multiple_match_single_alias(self, test_env):
        conn = test_env["conn"]
        cfg = test_env["cfg"]
        chats_dir = test_env["chats_dir"]
        sensitive_config_path = test_env["sensitive_config_path"]
        manifest_path = test_env["manifest_path"]

        config_data = {
            "keywords": {
                "birth_date": ["23-01-2000", "23/01/2000"],
            },
        }

        with open(sensitive_config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f)

        create_sample_chat(
            chats_dir, conn, "c1", "Birthday chat",
            ["My birth date is 23-01-2000"],
            "2026-07-18T10:00:00Z"
        )

        generate_or_refresh_manifest(conn, cfg)

        rows = read_and_validate_manifest(str(manifest_path))
        c1_flags = rows[0]["flags"].split(",")
        assert "birth_date" in c1_flags
        assert len(c1_flags) == 1


class TestMarkReviewed:
    def test_mark_all_reviewed(self, test_env):
        manifest_path = str(test_env["manifest_path"])

        rows = [
            {
                "updated_at": "2026-01-01",
                "source": "src",
                "conversation_id": "c1",
                "content_hash": "h1",
                "reviewed": "NO",
                "action": "KEEP",
                "title": "t1",
                "flags": "",
            },
            {
                "updated_at": "2026-01-02",
                "source": "src",
                "conversation_id": "c2",
                "content_hash": "h2",
                "reviewed": "RE-REVIEW",
                "action": "DEL",
                "title": "t2",
                "flags": "",
            },
        ]
        write_manifest_atomically(rows, manifest_path)

        with patch("builtins.input", return_value="y"):
            mark_all_reviewed()

        refreshed = read_and_validate_manifest(manifest_path)
        assert refreshed[0]["reviewed"] == "YES"
        assert refreshed[1]["reviewed"] == "YES"
        assert refreshed[1]["action"] == "DEL"  # Action is preserved


# ── Helpers for mask-sensitive tests ────────────────────────────────────────


def _write_sensitive_config(sensitive_config_path, **overrides):
    """Write a sensitive_patterns.json with reasonable defaults."""
    config = {
        "patterns": {
            "credit_card": r"\b(?:\d[ -]?){12,18}\d\b",
            "email": r"[\w.+-]+@[\w-]+\.[\w.-]+",
        },
        "keywords": {
            "name": ["John"],
            "secret": ["confidential"],
        },
    }
    config.update(overrides)
    with open(sensitive_config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)


class TestLuhnValidator:
    """Tests for ``luhn_valid()`` — known-valid vs random-digit rejection."""

    def test_luhn_valid_known_good(self):
        # Standard test PANs that pass Luhn
        assert luhn_valid("4111111111111111") is True   # Visa test
        assert luhn_valid("5500000000000004") is True   # MasterCard test
        assert luhn_valid("378282246310005") is True    # Amex test
        assert luhn_valid("30569309025904") is True     # Diners Club test

    def test_luhn_invalid_random_digits(self):
        assert luhn_valid("1234567890123456") is False
        assert luhn_valid("0000000000000000") is True   # All zeros *is* Luhn-valid
        assert luhn_valid("1111111111111111") is False

    def test_luhn_different_lengths(self):
        assert luhn_valid("49927398716") is True        # 11-digit known
        assert luhn_valid("49927398717") is False       # Same with wrong check


class TestCreditCardRegex:
    """The new ``credit_card`` pattern must match multi-length candidates and
    not match IPv4-looking strings or very short digit runs."""

    def test_matches_16_digit_no_separator(self):
        p = __import__("re").compile(r"\b(?:\d[ -]?){12,18}\d\b")
        assert p.search("4111111111111111")

    def test_matches_15_digit_amex(self):
        p = __import__("re").compile(r"\b(?:\d[ -]?){12,18}\d\b")
        assert p.search("378282246310005")

    def test_matches_14_digit_diners(self):
        p = __import__("re").compile(r"\b(?:\d[ -]?){12,18}\d\b")
        assert p.search("30569309025904")

    def test_matches_with_separators(self):
        p = __import__("re").compile(r"\b(?:\d[ -]?){12,18}\d\b")
        assert p.search("4111 1111 1111 1111")      # spaces
        assert p.search("4111-1111-1111-1111")      # dashes

    def test_does_not_match_ipv4(self):
        p = __import__("re").compile(r"\b(?:\d[ -]?){12,18}\d\b")
        # 192.168.1.1 has dots (not a separator) so should not match
        assert p.search("192.168.1.1") is None

    def test_does_not_match_short_runs(self):
        p = __import__("re").compile(r"\b(?:\d[ -]?){12,18}\d\b")
        assert p.search("12345") is None
        assert p.search("123456789012") is None      # 12 digits, too short


class TestScanWithLuhnFilter:
    """Verify that Luhn-invalid credit_card matches are excluded from flags."""

    def test_luhn_valid_credit_card_appears_in_flags(self, test_env):
        scp = test_env["sensitive_config_path"]
        _write_sensitive_config(scp, patterns={
            "credit_card": r"\b(?:\d[ -]?){12,18}\d\b",
        }, keywords={})

        patterns, keywords = review_chats.load_sensitive_rules(str(scp))
        chat = {
            "title": "My card is 4111111111111111",
            "turns": [],
        }
        fpath = test_env["chats_dir"] / "test.json"
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(chat, f)

        flags = scan_chat_file(str(fpath), patterns, keywords)
        assert "credit_card" in flags

    def test_luhn_invalid_credit_card_excluded(self, test_env):
        scp = test_env["sensitive_config_path"]
        _write_sensitive_config(scp, patterns={
            "credit_card": r"\b(?:\d[ -]?){12,18}\d\b",
        }, keywords={})

        patterns, keywords = review_chats.load_sensitive_rules(str(scp))
        chat = {
            "title": "My card is 1234567890123456",  # Luhn-invalid
            "turns": [],
        }
        fpath = test_env["chats_dir"] / "test.json"
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(chat, f)

        flags = scan_chat_file(str(fpath), patterns, keywords)
        assert "credit_card" not in flags

    def test_non_credit_card_unaffected(self, test_env):
        scp = test_env["sensitive_config_path"]
        _write_sensitive_config(scp, patterns={
            "email": r"[\w.+-]+@[\w-]+\.[\w.-]+",
            "credit_card": r"\b(?:\d[ -]?){12,18}\d\b",
        }, keywords={})

        patterns, keywords = review_chats.load_sensitive_rules(str(scp))
        chat = {
            "title": "My email is test@example.com and my invalid card is 1234567890123456",
            "turns": [],
        }
        fpath = test_env["chats_dir"] / "test.json"
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(chat, f)

        flags = scan_chat_file(str(fpath), patterns, keywords)
        assert "email" in flags
        assert "credit_card" not in flags

    def test_luhn_filter_in_verbose_scan(self, test_env):
        scp = test_env["sensitive_config_path"]
        _write_sensitive_config(scp, patterns={
            "credit_card": r"\b(?:\d[ -]?){12,18}\d\b",
        }, keywords={})

        patterns, keywords = review_chats.load_sensitive_rules(str(scp))
        chat = {
            "title": "Valid: 4111111111111111, Invalid: 1234567890123456",
            "turns": [],
        }
        fpath = test_env["chats_dir"] / "test.json"
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(chat, f)

        matches = scan_chat_file_verbose(str(fpath), patterns, keywords)
        # Should only have the valid card in matches
        assert "credit_card" in matches
        assert len(matches["credit_card"]) == 1
        assert "4111111111111111" in matches["credit_card"][0][1]


class TestMaskChatContent:
    """Unit tests for the pure function ``mask_chat_content()``."""

    def test_credit_card_partial_mask(self):
        patterns = {"credit_card": __import__("re").compile(r"\b(?:\d[ -]?){12,18}\d\b")}
        chat = {"title": "Card: 4111111111111111", "turns": []}
        masked, changed = mask_chat_content(chat, patterns, None)
        assert changed is True
        assert "[REDACTED:credit_card ...1111]" in masked["title"]

    def test_email_partial_mask(self):
        patterns = {"email": __import__("re").compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")}
        chat = {"title": "Email: user@example.com", "turns": []}
        masked, changed = mask_chat_content(chat, patterns, None)
        assert changed is True
        assert "[REDACTED:email domain=example.com]" in masked["title"]

    def test_ipv4_full_mask(self):
        patterns = {"ipv4": __import__("re").compile(r"\d+\.\d+\.\d+\.\d+")}
        chat = {"title": "IP: 10.0.0.1", "turns": []}
        masked, changed = mask_chat_content(chat, patterns, None)
        assert changed is True
        assert "[REDACTED:ipv4]" in masked["title"]
        assert "10.0.0.1" not in masked["title"]

    def test_keyword_full_mask(self):
        chat = {"title": "My name is John", "turns": []}
        keywords = {"name": ("John",)}
        masked, changed = mask_chat_content(chat, None, keywords)
        assert changed is True
        assert "[REDACTED:name]" in masked["title"]
        assert "John" not in masked["title"]

    def test_fields_other_than_title_turn_untouched(self):
        patterns = {"email": __import__("re").compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")}
        chat = {
            "conversation_id": "abc-123",
            "source": "gemini_web",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "title": "Email: user@test.com",
            "turns": [
                {"role": "user", "turn_number": 1, "text": "Contact: user@test.com"},
            ],
        }
        masked, changed = mask_chat_content(chat, patterns, None)
        assert changed is True
        assert masked["conversation_id"] == "abc-123"
        assert masked["source"] == "gemini_web"
        assert masked["created_at"] == "2026-01-01T00:00:00Z"
        assert masked["updated_at"] == "2026-01-01T00:00:00Z"
        assert masked["turns"][0]["role"] == "user"
        assert masked["turns"][0]["turn_number"] == 1

    def test_no_change_when_nothing_matches(self):
        patterns = {"email": __import__("re").compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")}
        chat = {"title": "Just a regular chat", "turns": []}
        masked, changed = mask_chat_content(chat, patterns, None)
        assert changed is False
        assert masked["title"] == "Just a regular chat"

    def test_mask_in_turn_text(self):
        patterns = {"email": __import__("re").compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")}
        chat = {
            "title": "Hello",
            "turns": [
                {"role": "user", "text": "My email is test@example.com"},
            ],
        }
        masked, changed = mask_chat_content(chat, patterns, None)
        assert changed is True
        assert "[REDACTED:email domain=example.com]" in masked["turns"][0]["text"]


class TestMaskSensitiveIntegration:
    """Integration tests for the full ``--mask-sensitive`` / ``--apply`` flow."""

    def test_dry_run_no_writes(self, test_env):
        """Preview mode should not modify files, DB, or manifest."""
        conn = test_env["conn"]
        cfg = test_env["cfg"]
        chats_dir = test_env["chats_dir"]
        manifest_path = test_env["manifest_path"]
        scp = test_env["sensitive_config_path"]

        _write_sensitive_config(scp, patterns={
            "email": r"[\w.+-]+@[\w-]+\.[\w.-]+",
        }, keywords={})

        create_sample_chat(
            chats_dir, conn, "c1", "Hello",
            ["Contact test@example.com"],
            "2026-07-18T10:00:00Z",
        )
        generate_or_refresh_manifest(conn, cfg)

        # Mark c1 as reviewed YES, KEEP, with email flag
        rows = read_and_validate_manifest(str(manifest_path))
        rows[0]["reviewed"] = "YES"
        rows[0]["action"] = "KEEP"
        write_manifest_atomically(rows, str(manifest_path))

        # Capture original file content
        orig_content = (chats_dir / "c1.json").read_text()
        orig_mtime = os.path.getmtime(chats_dir / "c1.json")

        # Run preview
        run_mask_sensitive(conn, cfg, apply_changes=False)

        # Assert no changes
        assert (chats_dir / "c1.json").read_text() == orig_content
        assert os.path.getmtime(chats_dir / "c1.json") == orig_mtime
        # Re-read manifest — should be unchanged
        rows_after = read_and_validate_manifest(str(manifest_path))
        assert rows_after[0]["reviewed"] == "YES"
        assert rows_after[0]["flags"] == "email"
        assert rows_after[0]["content_hash"] == rows[0]["content_hash"]

    def test_apply_masks_and_updates_manifest(self, test_env):
        conn = test_env["conn"]
        cfg = test_env["cfg"]
        chats_dir = test_env["chats_dir"]
        manifest_path = test_env["manifest_path"]
        scp = test_env["sensitive_config_path"]

        _write_sensitive_config(scp, patterns={
            "email": r"[\w.+-]+@[\w-]+\.[\w.-]+",
        }, keywords={})

        _, orig_fp = create_sample_chat(
            chats_dir, conn, "c1", "Hello",
            ["Contact test@example.com"],
            "2026-07-18T10:00:00Z",
        )
        generate_or_refresh_manifest(conn, cfg)

        rows = read_and_validate_manifest(str(manifest_path))
        rows[0]["reviewed"] = "YES"
        rows[0]["action"] = "KEEP"
        write_manifest_atomically(rows, str(manifest_path))

        # Apply
        run_mask_sensitive(conn, cfg, apply_changes=True)

        # File should be masked on disk
        masked_content = (chats_dir / "c1.json").read_text()
        assert "[REDACTED:email domain=example.com]" in masked_content
        assert "test@example.com" not in masked_content

        # Manifest: flags cleared, content_hash changed
        refreshed = read_and_validate_manifest(str(manifest_path))
        c1_row = next(r for r in refreshed if r["conversation_id"] == "c1")
        assert c1_row["flags"] == ""
        assert c1_row["content_hash"] != orig_fp
        assert c1_row["reviewed"] == "YES"   # unchanged
        assert c1_row["action"] == "KEEP"    # unchanged

        # Mtime should match chat's updated_at, not wall-clock
        new_mtime = os.path.getmtime(chats_dir / "c1.json")
        chat_ts = datetime.fromisoformat("2026-07-18T10:00:00Z").timestamp()
        assert abs(new_mtime - chat_ts) < 1.0

    def test_only_yes_keep_flagged_touched(self, test_env):
        """Rows that are NO, DEL, or have no flags should be untouched."""
        conn = test_env["conn"]
        cfg = test_env["cfg"]
        chats_dir = test_env["chats_dir"]
        manifest_path = test_env["manifest_path"]
        scp = test_env["sensitive_config_path"]

        _write_sensitive_config(scp, patterns={
            "email": r"[\w.+-]+@[\w-]+\.[\w.-]+",
        }, keywords={})

        # c1: YES, KEEP, flagged → should be masked
        create_sample_chat(chats_dir, conn, "c1", "Chat one", ["test@example.com"], "2026-07-18T10:00:00Z")
        # c2: NO (unreviewed) → not touched
        create_sample_chat(chats_dir, conn, "c2", "Chat two", ["test2@example.com"], "2026-07-19T10:00:00Z")
        # c3: YES, DEL → not touched
        create_sample_chat(chats_dir, conn, "c3", "Chat three", ["test3@example.com"], "2026-07-20T10:00:00Z")

        generate_or_refresh_manifest(conn, cfg)
        rows = read_and_validate_manifest(str(manifest_path))
        for r in rows:
            if r["conversation_id"] == "c1":
                r["reviewed"] = "YES"
                r["action"] = "KEEP"
            elif r["conversation_id"] == "c3":
                r["reviewed"] = "YES"
                r["action"] = "DEL"
            # c2 stays NO
        write_manifest_atomically(rows, str(manifest_path))

        c2_content_before = (chats_dir / "c2.json").read_text()
        c3_content_before = (chats_dir / "c3.json").read_text()

        run_mask_sensitive(conn, cfg, apply_changes=True)

        # c2 and c3 unchanged on disk
        assert (chats_dir / "c2.json").read_text() == c2_content_before
        assert (chats_dir / "c3.json").read_text() == c3_content_before

        # c1 masked
        assert "[REDACTED:email" in (chats_dir / "c1.json").read_text()

    def test_idempotent_second_run(self, test_env):
        """Running --apply twice is a no-op the second time (no candidates)."""
        conn = test_env["conn"]
        cfg = test_env["cfg"]
        chats_dir = test_env["chats_dir"]
        manifest_path = test_env["manifest_path"]
        scp = test_env["sensitive_config_path"]

        _write_sensitive_config(scp, patterns={
            "email": r"[\w.+-]+@[\w-]+\.[\w.-]+",
        }, keywords={})

        create_sample_chat(chats_dir, conn, "c1", "Hello", ["test@example.com"], "2026-07-18T10:00:00Z")
        generate_or_refresh_manifest(conn, cfg)
        rows = read_and_validate_manifest(str(manifest_path))
        rows[0]["reviewed"] = "YES"
        rows[0]["action"] = "KEEP"
        write_manifest_atomically(rows, str(manifest_path))

        # First apply
        run_mask_sensitive(conn, cfg, apply_changes=True)

        # Second apply — should find no candidates (flags now empty)
        refreshed = read_and_validate_manifest(str(manifest_path))
        c1_row = next(r for r in refreshed if r["conversation_id"] == "c1")
        assert c1_row["flags"] == ""

        # Masked content still there (not double-masked)
        masked_content = (chats_dir / "c1.json").read_text()
        assert masked_content.count("[REDACTED:email") == 1
