"""
Shared fixtures for the gemini-to-knowledge-graph test suite.
"""

import json
import sqlite3
import pytest

from common import init_db


@pytest.fixture
def make_chat():
    def _make(cid="gemini_test001", title="Test Chat", turns=None):
        if turns is None:
            turns = [
                {"role": "user", "text": "Hello"},
                {"role": "model", "text": "Hi there"},
            ]
        return {
            "conversation_id": cid,
            "source": "gemini_web",
            "title": title,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "turn_count": len(turns),
            "turns": turns,
        }

    return _make


@pytest.fixture
def write_chat(tmp_path):
    chats_dir = tmp_path / "chats"
    chats_dir.mkdir(exist_ok=True)

    def _write(chat):
        (chats_dir / f"{chat['conversation_id']}.json").write_text(
            json.dumps(chat), encoding="utf-8"
        )
        return chats_dir

    return _write


@pytest.fixture
def db_conn():
    """Return an in-memory SQLite connection with initialized tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn
