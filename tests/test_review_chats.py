"""
Tests for review_chats.py — manifest generation, regeneration, validation,
sensitive scanning, and mark-reviewed behavior.
"""

import csv
import json
import os
from unittest.mock import patch

import pytest

import review_chats
from common import chat_fingerprint, get_db_connection, upsert_chat
from review_chats import (
    MANIFEST_HEADER,
    generate_or_refresh_manifest,
    mark_all_reviewed,
    read_and_validate_manifest,
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
