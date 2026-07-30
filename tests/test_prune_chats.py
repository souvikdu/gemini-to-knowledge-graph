"""
Tests for prune_chats.py — candidate discovery, review manifest integration,
file validation, safety threshold, and cascade deletion.
"""

import json
import os
from unittest.mock import patch

import pytest

import prune_chats
import review_chats
from common import chat_fingerprint, get_db_connection, upsert_chat
from prune_chats import main as prune_main
from prune_chats import validate_review_delete_file
from review_chats import write_manifest_atomically


@pytest.fixture
def prune_env(tmp_path):
    """Set up temporary database, chats_dir, vault_dir, and manifest path for prune tests."""
    chats_dir = tmp_path / "chats"
    chats_dir.mkdir()
    db_path = tmp_path / "chat_topics.db"
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    convos_dir = vault_dir / "Conversations"
    convos_dir.mkdir()
    manifest_dir = tmp_path / "review"
    manifest_dir.mkdir()
    manifest_path = manifest_dir / "chats_to_review.csv"

    cfg = {
        "paths": {
            "chats_dir": str(chats_dir),
            "classifications_db": str(db_path),
            "vault_dir": str(vault_dir),
        },
        "limits": {
            "max_prune_ratio": 0.5,
        },
    }

    conn = get_db_connection(cfg)

    with patch("prune_chats.load_config", return_value=cfg), \
         patch.object(prune_chats, "MANIFEST_PATH", str(manifest_path)), \
         patch.object(review_chats, "MANIFEST_PATH", str(manifest_path)):
        yield {
            "cfg": cfg,
            "conn": conn,
            "chats_dir": chats_dir,
            "vault_dir": vault_dir,
            "convos_dir": convos_dir,
            "manifest_path": manifest_path,
            "tmp_path": tmp_path,
        }

    conn.close()


def add_test_chat(chats_dir, conn, cid, title, turns_text, created_at, filename=None, write_file=True):
    if not filename:
        filename = f"{cid}.json"

    chat_data = {
        "conversation_id": cid,
        "title": title,
        "source": "gemini_web",
        "created_at": created_at,
        "turns": [{"role": "user", "text": t} for t in turns_text],
    }

    fpath = chats_dir / filename
    mtime = 0.0
    if write_file:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(chat_data, f, indent=2)
        mtime = os.path.getmtime(fpath)

    fp = chat_fingerprint(chat_data)
    upsert_chat(
        conn,
        {
            "conversation_id": cid,
            "source": "gemini_web",
            "title": title,
            "content_hash": fp,
            "source_file": filename,
            "file_mtime": mtime,
            "created_at": created_at,
        },
    )
    return chat_data, fp


class TestPruneIntegration:
    def test_orphan_only_prune_without_manifest(self, prune_env):
        conn = prune_env["conn"]
        chats_dir = prune_env["chats_dir"]

        add_test_chat(chats_dir, conn, "c1", "Keep Chat", ["Hi"], "2026-07-18T10:00:00Z", write_file=True)
        add_test_chat(chats_dir, conn, "c2", "Orphan Chat", ["Bye"], "2026-07-19T10:00:00Z", write_file=False)

        with patch("sys.argv", ["prune_chats.py", "--prune"]):
            prune_main()

        # c2 should be removed from DB and added to ignored_conversations
        rows = conn.execute("SELECT conversation_id FROM chats").fetchall()
        cids = [r["conversation_id"] for r in rows]
        assert cids == ["c1"]

        ignored = conn.execute("SELECT conversation_id FROM ignored_conversations").fetchall()
        assert [r["conversation_id"] for r in ignored] == ["c2"]

    def test_prune_review_del_candidate(self, prune_env):
        conn = prune_env["conn"]
        chats_dir = prune_env["chats_dir"]
        manifest_path = prune_env["manifest_path"]

        _, fp1 = add_test_chat(chats_dir, conn, "c1", "Keep Chat", ["Hi"], "2026-07-18T10:00:00Z")
        _, fp2 = add_test_chat(chats_dir, conn, "c2", "Delete Chat", ["Bye"], "2026-07-19T10:00:00Z")

        # Create manifest marking c2 as DEL
        rows = [
            {
                "updated_at": "2026-07-18T10:00:00Z",
                "source": "gemini_web",
                "conversation_id": "c1",
                "content_hash": fp1,
                "reviewed": "NO",
                "action": "KEEP",
                "title": "Keep Chat",
                "flags": "",
            },
            {
                "updated_at": "2026-07-19T10:00:00Z",
                "source": "gemini_web",
                "conversation_id": "c2",
                "content_hash": fp2,
                "reviewed": "NO",
                "action": "DEL",
                "title": "Delete Chat",
                "flags": "",
            },
        ]
        write_manifest_atomically(rows, str(manifest_path))

        with patch("sys.argv", ["prune_chats.py", "--prune"]):
            prune_main()

        # c2 JSON file should be deleted on disk
        assert not (chats_dir / "c2.json").exists()
        assert (chats_dir / "c1.json").exists()

        # DB row for c2 deleted
        db_cids = [r["conversation_id"] for r in conn.execute("SELECT conversation_id FROM chats")]
        assert db_cids == ["c1"]

        # Manifest rewritten: c2 row removed, c1 remains with reviewed=NO (unmodified)
        with open(manifest_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            assert len(lines) == 2  # header + c1
            assert "c1" in lines[1]
            assert "NO" in lines[1]
            assert "c2" not in lines[1]

    def test_stale_del_row_cleanup(self, prune_env):
        conn = prune_env["conn"]
        chats_dir = prune_env["chats_dir"]
        manifest_path = prune_env["manifest_path"]

        _, fp1 = add_test_chat(chats_dir, conn, "c1", "Keep Chat", ["Hi"], "2026-07-18T10:00:00Z")

        # Manifest contains c1 (KEEP) and c_stale (DEL) which isn't in DB
        rows = [
            {
                "updated_at": "2026-07-18T10:00:00Z",
                "source": "gemini_web",
                "conversation_id": "c1",
                "content_hash": fp1,
                "reviewed": "YES",
                "action": "KEEP",
                "title": "Keep Chat",
                "flags": "",
            },
            {
                "updated_at": "2026-07-19T10:00:00Z",
                "source": "gemini_web",
                "conversation_id": "c_stale",
                "content_hash": "stalehash",
                "reviewed": "YES",
                "action": "DEL",
                "title": "Stale Chat",
                "flags": "",
            },
        ]
        write_manifest_atomically(rows, str(manifest_path))

        with patch("sys.argv", ["prune_chats.py", "--prune"]):
            prune_main()

        # Stale row removed from manifest
        with open(manifest_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "c_stale" not in content
            assert "c1" in content

    def test_dry_run_candidate_flags_display(self, prune_env, capsys):
        conn = prune_env["conn"]
        chats_dir = prune_env["chats_dir"]
        manifest_path = prune_env["manifest_path"]

        _, fp1 = add_test_chat(chats_dir, conn, "c1", "Flagged Chat", ["Secret"], "2026-07-18T10:00:00Z")

        rows = [
            {
                "updated_at": "2026-07-18T10:00:00Z",
                "source": "gemini_web",
                "conversation_id": "c1",
                "content_hash": fp1,
                "reviewed": "NO",
                "action": "DEL",
                "title": "Flagged Chat",
                "flags": "email,phone",
            },
        ]
        write_manifest_atomically(rows, str(manifest_path))

        with patch("sys.argv", ["prune_chats.py"]):
            prune_main()

        captured = capsys.readouterr()
        assert "flags: email,phone" in captured.out


# ── validate_review_delete_file ──────────────────────────────────────────


class TestValidateReviewDeleteFile:
    """Direct unit tests for prune_chats.validate_review_delete_file().

    This function does security-relevant validation (path traversal prevention,
    file extension check, conversation_id cross-match) before any deletion
    happens — so it needs direct coverage rather than just integration coverage.
    """

    def test_happy_path(self, prune_env):
        """Valid file with matching conversation_id returns the file path."""
        conn = prune_env["conn"]
        chats_dir = prune_env["chats_dir"]

        add_test_chat(
            chats_dir, conn, "c1", "Test",
            ["Hello"], "2026-07-18T10:00:00Z",
            filename="valid.json", write_file=True,
        )

        result = validate_review_delete_file("c1", conn, str(chats_dir))
        expected = os.path.abspath(os.path.join(str(chats_dir), "valid.json"))
        assert result == expected

    def test_path_traversal_attempt(self, prune_env):
        """source_file with ../ that would escape chats_dir returns None."""
        conn = prune_env["conn"]
        chats_dir = prune_env["chats_dir"]

        # Create a file outside chats_dir to simulate where traversal would point
        outside_file = prune_env["tmp_path"] / "outside.json"
        with open(outside_file, "w") as f:
            json.dump({"conversation_id": "c_traversal"}, f)

        # But the DB says it's inside chats_dir/../outside.json
        fp = chat_fingerprint({"dummy": True})
        upsert_chat(conn, {
            "conversation_id": "c_traversal",
            "source": "gemini_web",
            "title": "Traversal Attempt",
            "content_hash": fp,
            "source_file": "../outside.json",
            "file_mtime": 0.0,
            "created_at": "2026-07-18T10:00:00Z",
        })

        result = validate_review_delete_file("c_traversal", conn, str(chats_dir))
        assert result is None

    def test_non_json_source_file(self, prune_env):
        """source_file ending in .txt (not .json) returns None."""
        conn = prune_env["conn"]
        chats_dir = prune_env["chats_dir"]

        fp = chat_fingerprint({"dummy": True})
        upsert_chat(conn, {
            "conversation_id": "c_txt",
            "source": "gemini_web",
            "title": "Not JSON",
            "content_hash": fp,
            "source_file": "note.txt",
            "file_mtime": 0.0,
            "created_at": "2026-07-18T10:00:00Z",
        })

        (chats_dir / "note.txt").write_text("not json")
        result = validate_review_delete_file("c_txt", conn, str(chats_dir))
        assert result is None

    def test_conversation_id_mismatch(self, prune_env):
        """File's embedded conversation_id differs from the cid arg → None."""
        conn = prune_env["conn"]
        chats_dir = prune_env["chats_dir"]

        chat_data = {"conversation_id": "c_other", "title": "Wrong ID", "turns": []}
        with open(chats_dir / "mismatch.json", "w") as f:
            json.dump(chat_data, f)

        fp = chat_fingerprint(chat_data)
        upsert_chat(conn, {
            "conversation_id": "c_mismatch",
            "source": "gemini_web",
            "title": "Mismatch",
            "content_hash": fp,
            "source_file": "mismatch.json",
            "file_mtime": os.path.getmtime(chats_dir / "mismatch.json"),
            "created_at": "2026-07-18T10:00:00Z",
        })

        result = validate_review_delete_file("c_mismatch", conn, str(chats_dir))
        assert result is None

    def test_malformed_json(self, prune_env):
        """Unparseable JSON content returns None without raising."""
        conn = prune_env["conn"]
        chats_dir = prune_env["chats_dir"]

        fp = chat_fingerprint({"dummy": True})
        upsert_chat(conn, {
            "conversation_id": "c_bad_json",
            "source": "gemini_web",
            "title": "Bad JSON",
            "content_hash": fp,
            "source_file": "bad.json",
            "file_mtime": 0.0,
            "created_at": "2026-07-18T10:00:00Z",
        })

        (chats_dir / "bad.json").write_text("{invalid json!!!}")
        result = validate_review_delete_file("c_bad_json", conn, str(chats_dir))
        assert result is None

    def test_missing_db_row(self, prune_env):
        """No DB row for the given cid returns None."""
        conn = prune_env["conn"]
        chats_dir = prune_env["chats_dir"]
        result = validate_review_delete_file("nonexistent", conn, str(chats_dir))
        assert result is None

    def test_file_does_not_exist_on_disk(self, prune_env):
        """DB row exists but file is missing from disk returns None."""
        conn = prune_env["conn"]
        chats_dir = prune_env["chats_dir"]

        fp = chat_fingerprint({"dummy": True})
        upsert_chat(conn, {
            "conversation_id": "c_missing_file",
            "source": "gemini_web",
            "title": "Missing File",
            "content_hash": fp,
            "source_file": "ghost.json",
            "file_mtime": 0.0,
            "created_at": "2026-07-18T10:00:00Z",
        })

        result = validate_review_delete_file("c_missing_file", conn, str(chats_dir))
        assert result is None
