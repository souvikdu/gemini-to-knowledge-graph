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
                "created_at": "2026-07-18T10:00:00Z",
                "source": "gemini_web",
                "conversation_id": "c1",
                "content_hash": fp1,
                "reviewed": "NO",
                "action": "KEEP",
                "title": "Keep Chat",
                "flags": "",
            },
            {
                "created_at": "2026-07-19T10:00:00Z",
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
                "created_at": "2026-07-18T10:00:00Z",
                "source": "gemini_web",
                "conversation_id": "c1",
                "content_hash": fp1,
                "reviewed": "YES",
                "action": "KEEP",
                "title": "Keep Chat",
                "flags": "",
            },
            {
                "created_at": "2026-07-19T10:00:00Z",
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
                "created_at": "2026-07-18T10:00:00Z",
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
