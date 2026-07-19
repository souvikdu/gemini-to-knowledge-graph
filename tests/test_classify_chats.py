"""
Tests for classify_chats.py — parse_response, truncate_to_budget,
compute_todo.
"""

from unittest.mock import patch

import pytest

from classify_chats import (
    parse_response,
    truncate_to_budget,
    compute_todo,
)


SAMPLE_TOPIC_TO_CATEGORY = {
    "python": "Programming",
    "sql": "Data & Analytics",
    "pandas": "Data & Analytics",
}


# ── parse_response ──────────────────────────────────────────────────────────


class TestParseResponse:
    def test_basic_parsing(self):
        content = """Category: Programming, Mathematics
Topic: Python, Algorithms, Optimization
Summary: A discussion about algorithms and optimization techniques."""
        cats, topics, summary = parse_response(content, SAMPLE_TOPIC_TO_CATEGORY)
        assert cats == ["Programming", "Mathematics"]
        assert topics == ["Python", "Algorithms", "Optimization"]
        assert "algorithms and optimization" in summary

    def test_topic_name_mistaken_as_category_gets_normalized(self):
        """When the LLM outputs a topic name as a Category, it should be
        normalized to its parent category via the topic_to_category map."""
        content = """Category: Python
Topic: Python, Data Structures
Summary: Discussion about Python data structures."""
        cats, topics, summary = parse_response(content, SAMPLE_TOPIC_TO_CATEGORY)
        # "Python" is a topic belonging to "Programming" → should be normalized
        assert cats == ["Programming"]
        assert topics == ["Python", "Data Structures"]

    def test_missing_topic_line_raises_valueerror(self):
        content = """Category: Programming, Mathematics
Summary: Some summary."""
        with pytest.raises(ValueError, match="Could not parse"):
            parse_response(content, SAMPLE_TOPIC_TO_CATEGORY)

    def test_missing_category_line_raises_valueerror(self):
        content = """Topic: Python, Algorithms
Summary: Some summary."""
        with pytest.raises(ValueError, match="Could not parse"):
            parse_response(content, SAMPLE_TOPIC_TO_CATEGORY)

    def test_caps_at_two_categories_and_five_topics(self):
        content = """Category: A, B, C, D
Topic: 1, 2, 3, 4, 5, 6, 7
Summary: Many things."""
        cats, topics, summary = parse_response(content, SAMPLE_TOPIC_TO_CATEGORY)
        assert len(cats) == 2
        assert len(topics) == 5

    def test_duplicate_categories_are_deduplicated(self):
        content = """Category: Programming, Programming
Topic: Python
Summary: Duplicate check."""
        cats, topics, summary = parse_response(content, SAMPLE_TOPIC_TO_CATEGORY)
        assert cats == ["Programming"]

    def test_empty_content_raises_valueerror(self):
        with pytest.raises(ValueError, match="Could not parse"):
            parse_response("", SAMPLE_TOPIC_TO_CATEGORY)

    def test_summary_truncated_to_200_chars(self):
        long_summary = "Summary: " + "x" * 300
        content = f"Category: Programming\nTopic: Python\n{long_summary}"
        _, _, summary = parse_response(content, SAMPLE_TOPIC_TO_CATEGORY)
        assert len(summary) <= 200

    def test_case_insensitive_line_prefixes(self):
        content = """CATEGORY: Programming
TOPIC: Python
SUMMARY: Discussion."""
        cats, topics, summary = parse_response(content, SAMPLE_TOPIC_TO_CATEGORY)
        assert cats == ["Programming"]
        assert topics == ["Python"]
        assert summary == "Discussion."


# ── truncate_to_budget ──────────────────────────────────────────────────────


class TestTruncateToBudget:
    def test_short_text_passes_through_unchanged(self):
        text = "Short text"
        result = truncate_to_budget(text, 100, 0.3)
        assert result == text

    def test_boundary_equal_to_max_chars(self):
        text = "A" * 100
        result = truncate_to_budget(text, 100, 0.4)
        assert result == text

    def test_long_text_keeps_head_and_tail_with_marker(self):
        text = "A" * 500 + "B" * 500
        max_chars = 200
        head_fraction = 0.3
        result = truncate_to_budget(text, max_chars, head_fraction)
        assert result.startswith("A" * 60)  # head = 200 * 0.3 = 60
        assert result.endswith("B" * 60)  # tail = 200 - 60 - len(marker) = 200 - 60 - 52 = 88, but let's check
        assert "...[middle of conversation omitted for length]..." in result

    def test_marker_not_present_when_not_truncated(self):
        text = "Short text"
        result = truncate_to_budget(text, 100, 0.3)
        assert "...[middle of conversation omitted for length]..." not in result

    def test_zero_head_fraction(self):
        text = "A" * 100 + "B" * 100
        result = truncate_to_budget(text, 100, 0.0)
        # head = 0 → result is just marker + tail
        assert "A" not in result, "With head_fraction=0, no A's should remain"
        assert "...[middle of conversation omitted for length]..." in result
        assert result.endswith("B")

    def test_one_head_fraction(self):
        text = "A" * 100 + "B" * 100
        result = truncate_to_budget(text, 50, 0.9)
        # head = 45, marker = 52 chars → tail would be negative, so tail_len = 0
        assert "A" * 45 in result
        assert "...[middle of conversation omitted for length]..." in result



# ── compute_todo ────────────────────────────────────────────────────────────


class TestComputeTodo:
    def test_brand_new_chat_is_included(self, write_chat, make_chat):
        chat = make_chat(cid="new_chat")
        chats_dir = write_chat(chat)
        todo = compute_todo(str(chats_dir), {})
        assert len(todo) == 1
        assert todo[0][2] == "new_chat"

    def test_unchanged_chat_is_excluded(self, write_chat, make_chat):
        chat = make_chat(cid="stable")
        chats_dir = write_chat(chat)
        fp = "ae384d45bf71cc43"  # arbitrary hash — but must match
        processed = {"stable": {"content_hash": fp}}
        # Patch chat_fingerprint to return the expected hash
        with patch("classify_chats.chat_fingerprint", return_value=fp):
            todo = compute_todo(str(chats_dir), processed)
        assert len(todo) == 0

    def test_edited_chat_is_included(self, write_chat, make_chat):
        chat = make_chat(cid="editable", turns=[{"role": "user", "text": "old version"}])
        chats_dir = write_chat(chat)
        # Prior record has a DIFFERENT hash (simulating old content)
        processed = {"editable": {"content_hash": "aaaaaaaaaaaaaaaa"}}
        # The real fingerprint will be different from "aaaaaaaaaaaaaaaa"
        todo = compute_todo(str(chats_dir), processed)
        assert len(todo) == 1
        assert todo[0][2] == "editable"

    def test_multiple_chats_mixed_skip_and_include(self, write_chat, make_chat):
        chats = [
            make_chat(cid="new_one"),
            make_chat(cid="stable_one"),
            make_chat(cid="changed_one", turns=[{"role": "user", "text": "v2"}]),
        ]
        chats_dir = write_chat(chats[0])
        write_chat(chats[1])
        write_chat(chats[2])

        # Compute the real fingerprint for stable_one so it matches
        from common import chat_fingerprint as real_fp
        stable_hash = real_fp(chats[1])

        processed = {
            "stable_one": {"content_hash": stable_hash},
            "changed_one": {"content_hash": "bbbbbbbbbbbbbbbb"},
        }
        todo = compute_todo(str(chats_dir), processed)

        cids = {t[2] for t in todo}
        assert "new_one" in cids
        assert "stable_one" not in cids
        assert "changed_one" in cids
