"""
Tests for obsidian_layout.py — note_signature, resolve_note_action.
"""


from obsidian_layout import (
    note_signature, resolve_note_action, make_safe_filename,
    scaled_node_size, normalize_category, opening_prompt,
    yaml_tag_block, format_date, _resolve_link_placeholders,
)


# ── note_signature ──────────────────────────────────────────────────────────


class TestNoteSignature:
    """note_signature(chat, rec) is the core fix for the bug:
    chat_fingerprint alone doesn't detect a change in classification
    state when the chat text itself is unchanged."""

    def test_same_chat_and_same_rec_produce_same_signature(self, make_chat):
        chat = make_chat()
        rec = {"status": "ok", "category": ["Programming"], "topic": ["Python"], "summary": "Good chat"}
        sig1 = note_signature(chat, rec)
        sig2 = note_signature(chat, rec)
        assert sig1 == sig2

    def test_same_chat_rec_none_vs_classified_produces_different_signature(self, make_chat):
        """REGRESSION TEST FOR THE BUG: Chat text is IDENTICAL, but rec
        goes from None (unclassified) to a successful classification.
        chat_fingerprint alone would say 'unchanged' — but note_signature
        must differ."""
        chat = make_chat()
        sig_unclassified = note_signature(chat, None)
        sig_classified = note_signature(
            chat, {"status": "ok", "category": ["Programming"], "topic": ["Python"], "summary": "Great"}
        )
        assert sig_unclassified != sig_classified

    def test_same_chat_rec_error_vs_ok_produces_different_signature(self, make_chat):
        """Chat text is unchanged, but the classification status went from
        error to ok (e.g. after --retry). Must produce a different signature."""
        chat = make_chat()
        sig_error = note_signature(
            chat, {"status": "error", "error": "API timeout"}
        )
        sig_ok = note_signature(
            chat, {"status": "ok", "category": ["Programming"], "topic": ["Python"], "summary": "Fixed!"}
        )
        assert sig_error != sig_ok

    def test_same_chat_different_categories_produces_different_signature(self, make_chat):
        """Same chat, same status, but different category/topic lists
        (e.g. after taxonomy change or reclassification with different
        result). Must produce a different signature."""
        chat = make_chat()
        rec_a = {"status": "ok", "category": ["Programming"], "topic": ["Python"], "summary": "Code talk"}
        rec_b = {"status": "ok", "category": ["Mathematics"], "topic": ["Calculus"], "summary": "Math talk"}
        assert note_signature(chat, rec_a) != note_signature(chat, rec_b)

    def test_different_summary_produces_different_signature(self, make_chat):
        """Summary is part of the hash — changing it must change the signature."""
        chat = make_chat()
        rec_a = {"status": "ok", "category": ["Programming"], "topic": ["Python"], "summary": "Short"}
        rec_b = {"status": "ok", "category": ["Programming"], "topic": ["Python"], "summary": "Longer summary here"}
        assert note_signature(chat, rec_a) != note_signature(chat, rec_b)

    def test_missing_fields_in_rec_do_not_crash(self, make_chat):
        """A minimal rec (e.g. from an error record) should not raise."""
        chat = make_chat()
        sig = note_signature(chat, {"status": "error"})
        assert isinstance(sig, str) and len(sig) == 16

    def test_rec_is_none_unclassified(self, make_chat):
        """None rec (unclassified chat) must produce a deterministic signature."""
        chat = make_chat()
        sig = note_signature(chat, None)
        assert isinstance(sig, str) and len(sig) == 16
        assert note_signature(chat, None) == sig  # deterministic


# ── resolve_note_action ─────────────────────────────────────────────────────


class TestResolveNoteAction:
    """resolve_note_action(cid, chat, rec, existing_vault, used_filenames)
    is a pure function — no I/O — making it directly testable."""

    def test_brand_new_cid_returns_action_new(self, make_chat):
        chat = make_chat(cid="new_cid", title="My Chat")
        action, notename = resolve_note_action("new_cid", chat, None, {}, set())
        assert action == "new"
        # Filename derived from title via make_safe_filename
        assert notename == make_safe_filename("My Chat")

    def test_filename_collision_gets_numeric_suffix(self, make_chat):
        chat = make_chat(cid="new_cid", title="Shared Title")
        used = {"Shared Title"}
        action, notename = resolve_note_action("new_cid", chat, None, {}, used)
        assert action == "new"
        assert notename == "Shared Title-2"

    def test_unchanged_note_returns_skip_and_same_filename(self, make_chat):
        chat = make_chat(cid="existing", title="Keep Me")
        rec = {"status": "ok", "category": ["Programming"], "topic": ["Python"], "summary": "Nice"}
        sig = note_signature(chat, rec)
        existing_vault = {"existing": ("Keep Me", sig)}
        action, notename = resolve_note_action("existing", chat, rec, existing_vault, {"Keep Me"})
        assert action == "skip"
        assert notename == "Keep Me"

    def test_changed_chat_text_returns_rewrite_with_same_filename(self, make_chat):
        """Chat text changed (different content) → rewrite in place,
        NOT a new file for the same cid."""
        orig_turns = [{"role": "user", "text": "old version"}]
        new_turns = [{"role": "user", "text": "new version"}]
        orig_chat = make_chat(cid="editable", title="My Chat", turns=orig_turns)
        rec = {"status": "ok", "category": ["Programming"], "topic": ["Python"], "summary": "Same"}
        orig_sig = note_signature(orig_chat, rec)
        existing_vault = {"editable": ("My Chat", orig_sig)}

        new_chat = make_chat(cid="editable", title="My Chat", turns=new_turns)
        action, notename = resolve_note_action("editable", new_chat, rec, existing_vault, {"My Chat"})
        assert action == "rewrite"
        assert notename == "My Chat"  # same filename, not a new file

    def test_bug_regression_unchanged_text_but_new_classification(self, make_chat):
        """THE BUG: Chat text is IDENTICAL, but rec goes from None to a
        successful classification. chat_fingerprint alone would return the
        same value and the vault would skip the note. note_signature sees
        the change and returns 'rewrite'."""
        chat = make_chat(cid="bug_test", title="Bug Chat")

        # First pass: chat exists but is not yet classified
        sig_unclassified = note_signature(chat, None)
        existing_vault = {"bug_test": ("Bug Chat", sig_unclassified)}

        # Second pass: same chat text, but now a classification exists
        rec = {"status": "ok", "category": ["Programming"], "topic": ["Python"], "summary": "Now classified"}
        action, notename = resolve_note_action(
            "bug_test", chat, rec, existing_vault, {"Bug Chat"}
        )
        assert action == "rewrite", (
            f"Bug regression: chat_fingerprint-only check would skip this "
            f"because chat text hasn't changed. Expected 'rewrite', got '{action}'"
        )
        assert notename == "Bug Chat"  # same filename, rewritten in place

    def test_error_to_ok_transition_triggers_rewrite(self, make_chat):
        """A chat that errored on first pass and succeeded on --retry
        (with unchanged text) must still trigger a rewrite."""
        chat = make_chat(cid="retry_me", title="Retry Chat")

        rec_error = {"status": "error", "error": "API timeout"}
        sig_error = note_signature(chat, rec_error)
        existing_vault = {"retry_me": ("Retry Chat", sig_error)}

        rec_ok = {"status": "ok", "category": ["Programming"], "topic": ["Python"], "summary": "Fixed"}
        action, notename = resolve_note_action(
            "retry_me", chat, rec_ok, existing_vault, {"Retry Chat"}
        )
        assert action == "rewrite"
        assert notename == "Retry Chat"

    def test_unknown_cid_with_empty_title_uses_unnamed(self, make_chat):
        chat = make_chat(cid="no_title", title="")
        action, notename = resolve_note_action("no_title", chat, None, {}, set())
        assert action == "new"
        assert notename != ""


# ── make_safe_filename ───────────────────────────────────────────────────────


class TestMakeSafeFilename:
    def test_removes_invalid_chars(self):
        assert make_safe_filename('foo:bar<baz>"qux"') == "foobarbazqux"

    def test_strips_trailing_dots(self):
        assert make_safe_filename("hello.") == "hello"

    def test_empty_falls_back_to_unnamed(self):
        assert make_safe_filename("") == "unnamed"

    def test_truncates_long_names(self):
        long_name = "a" * 200
        result = make_safe_filename(long_name)
        assert len(result) == 150

    def test_whitespace_only_falls_back(self):
        assert make_safe_filename("   ") == "unnamed"


# ── scaled_node_size ─────────────────────────────────────────────────────────


class TestScaledNodeSize:
    def test_zero_count_returns_floor(self):
        assert scaled_node_size(0, 100, 10, 50) == 10

    def test_zero_max_returns_floor(self):
        assert scaled_node_size(5, 0, 10, 50) == 10

    def test_max_count_returns_ceiling(self):
        assert scaled_node_size(100, 100, 10, 50) == 50

    def test_half_count_returns_midpoint(self):
        # sqrt(50)/sqrt(100) = 0.7071 → 10 + 0.7071*40 = 38.28 → 38
        assert scaled_node_size(50, 100, 10, 50) == 38

    def test_quarter_count(self):
        # sqrt(25)/sqrt(100) = 0.5 → 10 + 0.5*40 = 30
        assert scaled_node_size(25, 100, 10, 50) == 30


# ── normalize_category ───────────────────────────────────────────────────────


class TestNormalizeCategory:
    def test_exact_match(self):
        known = {"programming": "Programming", "math": "Mathematics"}
        assert normalize_category("Programming", known) == "Programming"

    def test_case_insensitive_match(self):
        known = {"programming": "Programming"}
        assert normalize_category("PROGRAMMING", known) == "Programming"

    def test_whitespace_stripped(self):
        known = {"programming": "Programming"}
        assert normalize_category("  Programming  ", known) == "Programming"

    def test_unknown_falls_back_to_general_knowledge(self):
        known = {"programming": "Programming"}
        assert normalize_category("Alchemy", known) == "General Knowledge"


# ── opening_prompt ───────────────────────────────────────────────────────────


class TestOpeningPrompt:
    def test_returns_first_user_turn_text(self):
        turns = [
            {"role": "assistant", "text": "Hello"},
            {"role": "user", "text": "What is Python?"},
        ]
        assert opening_prompt(turns) == "What is Python?"

    def test_truncates_long_text(self):
        turns = [{"role": "user", "text": "x" * 500}]
        result = opening_prompt(turns, max_chars=100)
        # text[:100] = 100 chars, .rstrip() no-op, + "…" = 101
        assert len(result) == 101
        assert result.endswith("…")

    def test_skips_empty_user_turns(self):
        turns = [
            {"role": "user", "text": ""},
            {"role": "user", "text": "Actual question"},
        ]
        assert opening_prompt(turns) == "Actual question"

    def test_no_user_turns_returns_empty(self):
        turns = [{"role": "assistant", "text": "Hello"}]
        assert opening_prompt(turns) == ""


# ── yaml_tag_block ───────────────────────────────────────────────────────────


class TestYamlTagBlock:
    def test_single_tag(self):
        assert yaml_tag_block(["type/conversation"]) == 'tags:\n  - "type/conversation"'

    def test_multiple_tags(self):
        result = yaml_tag_block(["type/conversation", "source/gemini"])
        assert '  - "type/conversation"' in result
        assert '  - "source/gemini"' in result
        assert result.startswith("tags:\n")

    def test_empty_tags(self):
        assert yaml_tag_block([]) == "tags:\n"


# ── format_date ──────────────────────────────────────────────────────────────


class TestFormatDate:
    def test_none_returns_unknown(self):
        assert format_date(None) == "Unknown"

    def test_empty_string_returns_unknown(self):
        assert format_date("") == "Unknown"

    def test_iso_format(self):
        assert format_date("2026-01-15T14:30:00+00:00") == "2026-01-15 14:30"

    def test_z_suffix(self):
        assert format_date("2026-06-01T08:00:00Z") == "2026-06-01 08:00"

    def test_fallback_for_unparseable(self):
        assert format_date("not-a-date") == "not-a-date"


# ── _resolve_link_placeholders ───────────────────────────────────────────────


class TestResolveLinkPlaceholders:
    """_resolve_link_placeholders handles Gemini's placeholder links."""

    def test_old_style_link_placeholder(self):
        """[label](_link) → [label](search_url?q=label)"""
        result = _resolve_link_placeholders(
            "See [Product Name](_link) for details.",
            "https://duckduckgo.com/?q=",
        )
        assert result == "See [Product Name](https://duckduckgo.com/?q=Product%20Name) for details."

    def test_googleusercontent_shopping_link(self):
        """[label](http://googleusercontent.com/shopping_content/N_link) → search link"""
        result = _resolve_link_placeholders(
            "Buy the [OnePlus Pad 2](http://googleusercontent.com/shopping_content/0_link) now.",
            "https://duckduckgo.com/?q=",
        )
        assert result == "Buy the [OnePlus Pad 2](https://duckduckgo.com/?q=OnePlus%20Pad%202) now."

    def test_googleusercontent_map_link(self):
        """[label](http://googleusercontent.com/map_location_reference/N) → search link"""
        result = _resolve_link_placeholders(
            "Go to [Indian Coffee House](http://googleusercontent.com/map_location_reference/0).",
            "https://duckduckgo.com/?q=",
        )
        assert result == "Go to [Indian Coffee House](https://duckduckgo.com/?q=Indian%20Coffee%20House)."

    def test_googleusercontent_youtube_link(self):
        """[label](http://googleusercontent.com/youtube_content/N) → search link"""
        result = _resolve_link_placeholders(
            "Watch [this video](http://googleusercontent.com/youtube_content/0) for details.",
            "https://duckduckgo.com/?q=",
        )
        assert result == "Watch [this video](https://duckduckgo.com/?q=this%20video) for details."

    def test_bare_googleusercontent_url_is_removed(self):
        """Bare http://googleusercontent.com/... URLs are removed entirely."""
        result = _resolve_link_placeholders(
            "Here is the info.\nhttp://googleusercontent.com/youtube_content/0\n",
            "https://duckduckgo.com/?q=",
        )
        assert result == "Here is the info.\n\n"

    def test_bare_googleusercontent_action_card_is_removed(self):
        result = _resolve_link_placeholders(
            "Reminder set.\nhttp://googleusercontent.com/action_card_content/1\n",
            "https://duckduckgo.com/?q=",
        )
        assert result == "Reminder set.\n\n"

    def test_bare_googleusercontent_image_generation_is_removed(self):
        result = _resolve_link_placeholders(
            "Generated image.\nhttp://googleusercontent.com/image_generation_content/0\n\n",
            "https://duckduckgo.com/?q=",
        )
        assert result == "Generated image.\n\n\n"

    def test_real_url_is_untouched(self):
        """Proper URLs like https://www.youtube.com/watch?v=... pass through unchanged."""
        result = _resolve_link_placeholders(
            "See [this video](https://www.youtube.com/watch?v=abc123) for details.",
            "https://duckduckgo.com/?q=",
        )
        assert result == "See [this video](https://www.youtube.com/watch?v=abc123) for details."

    def test_mixed_real_and_placeholder_links(self):
        """Real links survive; googleusercontent markdown links get rewritten."""
        result = _resolve_link_placeholders(
            "First watch [this video](https://www.youtube.com/watch?v=abc123), "
            "then buy [the product](http://googleusercontent.com/shopping_content/1_link).",
            "https://duckduckgo.com/?q=",
        )
        assert result == (
            "First watch [this video](https://www.youtube.com/watch?v=abc123), "
            "then buy [the product](https://duckduckgo.com/?q=the%20product)."
        )

    def test_https_googleusercontent_is_also_caught(self):
        """https:// variant of googleusercontent is also handled."""
        result = _resolve_link_placeholders(
            "Check [this item](https://googleusercontent.com/shopping_content/2_link).",
            "https://duckduckgo.com/?q=",
        )
        assert result == "Check [this item](https://duckduckgo.com/?q=this%20item)."

    def test_bare_https_googleusercontent_is_removed(self):
        result = _resolve_link_placeholders(
            "Info here.\nhttps://googleusercontent.com/deep_research_confirmation_content/0\n",
            "https://duckduckgo.com/?q=",
        )
        assert result == "Info here.\n\n"
