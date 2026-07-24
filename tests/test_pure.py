"""Tests for the pure helper functions in brevitybot.

These functions do no I/O (no Redis, no Discord, no HTTP) so they're fully
unit-testable. The bootstrap in conftest.py sets the env vars brevitybot
demands at import time.
"""
import asyncio
import random
from unittest.mock import MagicMock

import discord
import pytest

import brevitybot
from brevitybot import (
    JSONFormatter,
    Term,
    QuizMeta,
    QuizOption,
    build_quiz_question,
    clean_term,
    pick_single_definition,
    sanitize_definition_for_quiz,
    _parse_terms_from_content,
    _truncate_code_block,
)


# -------------------------------
# clean_term
# -------------------------------
class TestCleanTerm:
    def test_strips_asterisks(self):
        assert clean_term("*FOO*") == "FOO"

    def test_strips_whitespace(self):
        assert clean_term("  BAR  ") == "BAR"

    def test_no_op_on_clean(self):
        assert clean_term("BAZ") == "BAZ"

    def test_preserves_internal_brackets(self):
        # Comment in source: "Retain square brackets for terms"
        assert clean_term("[STATE] FOO") == "[STATE] FOO"


# -------------------------------
# pick_single_definition
# -------------------------------
class TestPickSingleDefinition:
    def test_empty_returns_empty(self):
        assert pick_single_definition("") == ""

    def test_whitespace_returns_empty(self):
        assert pick_single_definition("   ") == ""

    def test_single_meaning_returned_intact(self):
        out = pick_single_definition("A short, simple definition.")
        assert "short, simple definition" in out

    def test_numbered_variants_split(self):
        random.seed(1234)
        defn = "1. The first meaning. 2. A second meaning. 3. Yet another."
        seen = {pick_single_definition(defn) for _ in range(50)}
        # We should see at least two distinct variants over many trials.
        assert len(seen) >= 2
        # And no result should still contain the leading number markers.
        for s in seen:
            assert not s.startswith("1.")
            assert not s.startswith("2.")
            assert not s.startswith("3.")

    def test_paren_numbered_variants_split(self):
        random.seed(0)
        defn = "1) First paren meaning. 2) Second paren meaning."
        seen = {pick_single_definition(defn) for _ in range(30)}
        assert len(seen) >= 2

    def test_falls_back_to_first_line_if_no_letters_in_parts(self):
        # Numeric-only fragments are filtered out; should fall back.
        defn = "1.\nReal definition with letters."
        out = pick_single_definition(defn)
        assert "letters" in out

    def test_used_to_be_broken_for_numbered(self):
        """Regression: the private-mode inline version used r'\\\\d' (literal
        backslash-d) which never matched. Now numbered variants must split."""
        random.seed(42)
        out = pick_single_definition("1. Alpha. 2. Beta.")
        assert out in {"Alpha.", "Beta."}


# -------------------------------
# sanitize_definition_for_quiz
# -------------------------------
class TestSanitizeForQuiz:
    def test_masks_bare_term_occurrences(self):
        out = sanitize_definition_for_quiz(
            "GADABOUT means something that includes the GADABOUT itself.",
            term="GADABOUT",
        )
        assert "GADABOUT" not in out

    def test_case_insensitive_masking(self):
        out = sanitize_definition_for_quiz(
            "gadabout is the lowercased GadAbout we want masked.",
            term="GADABOUT",
        )
        assert "gadabout" not in out.lower()

    def test_masks_quoted_examples(self):
        out = sanitize_definition_for_quiz(
            'Used as in "GADABOUT 25" to indicate range.',
            term="GADABOUT",
        )
        assert "GADABOUT 25" not in out
        assert "[example]" in out

    def test_returns_something_for_term_only_definition(self):
        # If masking produces a too-short result, helper falls back.
        out = sanitize_definition_for_quiz("GADABOUT", term="GADABOUT")
        assert out  # not empty
        assert "GADABOUT" not in out

    def test_passes_through_empty(self):
        assert sanitize_definition_for_quiz("", term="X") == ""

    def test_term_that_appears_only_in_other_words_not_masked(self):
        # "BANDIT" inside "BANDITRY" should not be masked because of \b.
        out = sanitize_definition_for_quiz(
            "Banditry levels rising.", term="BANDIT"
        )
        assert "Banditry" in out


# -------------------------------
# _truncate_code_block
# -------------------------------
class TestTruncateCodeBlock:
    def test_short_input_unchanged(self):
        s = "```\nhi\n```"
        assert _truncate_code_block(s, 100) == s

    def test_long_input_fits_under_limit(self):
        big = "```\n" + ("x" * 2000) + "\n```"
        out = _truncate_code_block(big, 1024)
        assert len(out) <= 1024

    def test_long_input_preserves_both_fences(self):
        big = "```\n" + ("x" * 2000) + "\n```"
        out = _truncate_code_block(big, 1024)
        assert out.startswith("```\n")
        assert out.endswith("\n```")
        # Exactly two fence markers (open + close)
        assert out.count("```") == 2

    def test_long_input_includes_marker(self):
        big = "```\n" + ("x" * 2000) + "\n```"
        out = _truncate_code_block(big, 1024)
        assert "..." in out

    def test_unfenced_input_falls_back(self):
        plain = "x" * 2000
        out = _truncate_code_block(plain, 100)
        assert len(out) == 100
        assert out.endswith("...")

    def test_regression_old_truncation_lost_fence(self):
        """The old logic was board[:1021] + '...'. Confirm that loses the
        fence so we know the new logic is necessary."""
        big = "```\n" + ("x" * 2000) + "\n```"
        old_truncated = big[:1021] + "..."
        # The closing fence is gone in the buggy version.
        assert "```" not in old_truncated[-10:]


# -------------------------------
# _parse_terms_from_content
# -------------------------------
class TestParseTerms:
    def test_basic_dt_dd(self):
        html = """
        <div class="mw-parser-output">
          <dl>
            <dt>FOO</dt><dd>Foo definition.</dd>
            <dt>BAR</dt><dd>Bar definition.</dd>
          </dl>
        </div>
        """
        terms = _parse_terms_from_content(html)
        assert [t["term"] for t in terms] == ["FOO", "BAR"]
        assert terms[0]["definition"] == "Foo definition."

    def test_nested_ul_no_duplication(self):
        """Old parser appended nested <ul> bullets twice — once from the dd's
        get_text, once from the find_all([ul]). Should now appear once."""
        html = """
        <div class="mw-parser-output">
          <dl>
            <dt>FOO</dt>
            <dd>Foo body.<ul><li>Detail 1</li><li>Detail 2</li></ul></dd>
          </dl>
        </div>
        """
        terms = _parse_terms_from_content(html)
        defn = terms[0]["definition"]
        assert defn.count("Detail 1") == 1
        assert defn.count("Detail 2") == 1
        assert "- Detail 1" in defn
        assert "- Detail 2" in defn

    def test_stray_ul_not_glued_to_previous_term(self):
        """Old parser's flat traversal would attach a <ul> outside any <dl>
        to whatever the most-recent term was."""
        html = """
        <div class="mw-parser-output">
          <dl>
            <dt>FIRST</dt><dd>First def.</dd>
          </dl>
          <ul><li>Stray nav item</li></ul>
          <dl>
            <dt>SECOND</dt><dd>Second def.</dd>
          </dl>
        </div>
        """
        terms = _parse_terms_from_content(html)
        first = next(t for t in terms if t["term"] == "FIRST")
        assert "Stray nav item" not in first["definition"]
        assert first["definition"] == "First def."

    def test_stops_at_see_also(self):
        html = """
        <div class="mw-parser-output">
          <dl><dt>REAL</dt><dd>Real def.</dd></dl>
          <h2>See also</h2>
          <dl><dt>BOGUS</dt><dd>Bogus def.</dd></dl>
        </div>
        """
        terms = _parse_terms_from_content(html)
        assert [t["term"] for t in terms] == ["REAL"]

    def test_stops_at_references(self):
        html = """
        <div class="mw-parser-output">
          <dl><dt>REAL</dt><dd>Real def.</dd></dl>
          <h2>References</h2>
          <dl><dt>BOGUS</dt><dd>Bogus def.</dd></dl>
        </div>
        """
        terms = _parse_terms_from_content(html)
        assert "BOGUS" not in [t["term"] for t in terms]

    def test_nested_dl_doesnt_leak_as_top_level(self):
        html = """
        <div class="mw-parser-output">
          <dl>
            <dt>OUTER</dt>
            <dd>Outer.<dl><dt>INNER</dt><dd>Inner.</dd></dl></dd>
          </dl>
        </div>
        """
        terms = _parse_terms_from_content(html)
        names = [t["term"] for t in terms]
        assert names == ["OUTER"]
        # And the inner content shouldn't have leaked into OUTER's definition.
        assert "INNER" not in terms[0]["definition"]
        assert "Inner" not in terms[0]["definition"]

    def test_missing_content_div_returns_empty(self):
        terms = _parse_terms_from_content(b"<html><body><p>x</p></body></html>")
        assert terms == []

    def test_empty_input_returns_empty(self):
        assert _parse_terms_from_content(b"") == []

    def test_citation_supscript_removed(self):
        html = """
        <div class="mw-parser-output">
          <dl><dt>FOO</dt><dd>A def<sup>[1]</sup> with citation.</dd></dl>
        </div>
        """
        terms = _parse_terms_from_content(html)
        assert "[1]" not in terms[0]["definition"]
        assert "A def" in terms[0]["definition"]

    def test_orphan_dd_skipped(self):
        html = """
        <div class="mw-parser-output">
          <dl>
            <dd>Orphan dd, no preceding dt.</dd>
            <dt>REAL</dt><dd>Real def.</dd>
          </dl>
        </div>
        """
        terms = _parse_terms_from_content(html)
        assert [t["term"] for t in terms] == ["REAL"]
        assert "Orphan" not in terms[0]["definition"]

    def test_term_with_multiple_dds(self):
        # Wikipedia occasionally has more than one <dd> for the same <dt>
        # (different senses). Both should be kept.
        html = """
        <div class="mw-parser-output">
          <dl>
            <dt>POLY</dt>
            <dd>First sense.</dd>
            <dd>Second sense.</dd>
          </dl>
        </div>
        """
        terms = _parse_terms_from_content(html)
        assert len(terms) == 1
        assert "First sense" in terms[0]["definition"]
        assert "Second sense" in terms[0]["definition"]


# -------------------------------
# build_quiz_question
# -------------------------------
class TestBuildQuizQuestion:
    """The shared helper used by both private and public /quiz modes."""

    def _terms(self, n=10):
        return [
            {"term": f"TERM_{i}", "definition": f"Definition number {i} with text."}
            for i in range(n)
        ]

    def test_returns_four_options(self):
        random.seed(0)
        terms = self._terms()
        embed, options, _correct_idx, _qtype = build_quiz_question(
            terms[0], terms, title="t", footer="f"
        )
        assert len(options) == 4

    def test_exactly_one_correct_option(self):
        random.seed(0)
        terms = self._terms()
        _embed, options, _correct_idx, _qtype = build_quiz_question(
            terms[0], terms, title="t", footer="f"
        )
        assert sum(1 for o in options if o["is_correct"]) == 1

    def test_correct_idx_points_to_correct_option(self):
        random.seed(0)
        terms = self._terms()
        _embed, options, correct_idx, _qtype = build_quiz_question(
            terms[0], terms, title="t", footer="f"
        )
        assert options[correct_idx]["is_correct"] is True

    def test_correct_term_is_the_one_passed_in(self):
        random.seed(0)
        terms = self._terms()
        _embed, options, correct_idx, _qtype = build_quiz_question(
            terms[3], terms, title="t", footer="f"
        )
        assert options[correct_idx]["term"] == "TERM_3"

    def test_distractors_are_distinct_from_correct(self):
        random.seed(0)
        terms = self._terms()
        _embed, options, correct_idx, _qtype = build_quiz_question(
            terms[0], terms, title="t", footer="f"
        )
        wrong = [o["term"] for i, o in enumerate(options) if i != correct_idx]
        assert "TERM_0" not in wrong
        # And distractors are distinct from each other
        assert len(set(wrong)) == 3

    def test_question_type_is_one_of_two_values(self):
        for seed in range(20):
            random.seed(seed)
            terms = self._terms()
            _embed, _options, _correct_idx, qtype = build_quiz_question(
                terms[0], terms, title="t", footer="f"
            )
            assert qtype in ("term_to_definition", "definition_to_term")

    def test_term_to_definition_masks_correct_term_in_options(self):
        # Force question_type by seeding until we get term_to_definition.
        for seed in range(50):
            random.seed(seed)
            terms = [
                {"term": "GADABOUT", "definition": "GADABOUT is the term and GADABOUT appears."},
                {"term": "OTHER1", "definition": "Other one."},
                {"term": "OTHER2", "definition": "Other two."},
                {"term": "OTHER3", "definition": "Other three."},
            ]
            _embed, options, correct_idx, qtype = build_quiz_question(
                terms[0], terms, title="t", footer="f"
            )
            if qtype == "term_to_definition":
                # The correct option's display must NOT contain the term name.
                correct_display = options[correct_idx]["display"]
                assert "GADABOUT" not in correct_display
                return
        pytest.fail("Never got term_to_definition in 50 seeds")

    def test_options_have_display_field(self):
        random.seed(0)
        terms = self._terms()
        _embed, options, _correct_idx, _qtype = build_quiz_question(
            terms[0], terms, title="t", footer="f"
        )
        for o in options:
            assert "display" in o

    def test_embed_uses_passed_title_and_footer(self):
        random.seed(0)
        terms = self._terms()
        embed, _options, _correct_idx, _qtype = build_quiz_question(
            terms[0], terms, title="My Title", footer="My Footer"
        )
        assert embed.title == "My Title"
        assert embed.footer.text == "My Footer"

    def test_with_timestamp_sets_embed_timestamp(self):
        random.seed(0)
        terms = self._terms()
        embed, _o, _c, _q = build_quiz_question(
            terms[0], terms, title="t", footer="f", with_timestamp=True
        )
        assert embed.timestamp is not None

    def test_without_timestamp_leaves_embed_timestamp_unset(self):
        random.seed(0)
        terms = self._terms()
        embed, _o, _c, _q = build_quiz_question(
            terms[0], terms, title="t", footer="f", with_timestamp=False
        )
        assert embed.timestamp is None

    def test_embed_has_four_option_fields(self):
        random.seed(0)
        terms = self._terms()
        embed, _o, _c, _q = build_quiz_question(
            terms[0], terms, title="t", footer="f"
        )
        # Each of A/B/C/D is one field on the embed.
        assert len(embed.fields) == 4

    def test_deterministic_under_seed(self):
        random.seed(99)
        terms = self._terms(20)
        _e1, opts1, idx1, qt1 = build_quiz_question(
            terms[5], terms, title="t", footer="f"
        )
        random.seed(99)
        _e2, opts2, idx2, qt2 = build_quiz_question(
            terms[5], terms, title="t", footer="f"
        )
        assert qt1 == qt2
        assert idx1 == idx2
        assert [o["term"] for o in opts1] == [o["term"] for o in opts2]


# -------------------------------
# JSONFormatter
# -------------------------------
class TestJSONFormatter:
    def _record(self, msg, level=20, extra=None):
        import logging
        record = logging.LogRecord(
            name="brevitybot.test", level=level, pathname=__file__, lineno=1,
            msg=msg, args=(), exc_info=None,
        )
        if extra:
            for k, v in extra.items():
                setattr(record, k, v)
        return record

    def test_emits_valid_json(self):
        import json as _json
        out = JSONFormatter().format(self._record("hello world"))
        parsed = _json.loads(out)
        assert parsed["message"] == "hello world"
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "brevitybot.test"

    def test_extra_fields_included(self):
        import json as _json
        out = JSONFormatter().format(
            self._record("vote", extra={"guild_id": 42, "user_id": 99, "quiz_id": "q1"})
        )
        parsed = _json.loads(out)
        assert parsed["guild_id"] == 42
        assert parsed["user_id"] == 99
        assert parsed["quiz_id"] == "q1"

    def test_unserializable_extra_falls_back_to_repr(self):
        import json as _json
        class Weird:
            def __repr__(self):
                return "<Weird>"
        out = JSONFormatter().format(self._record("x", extra={"obj": Weird()}))
        parsed = _json.loads(out)
        assert parsed["obj"] == "<Weird>"

    def test_reserved_log_attrs_not_emitted(self):
        import json as _json
        out = JSONFormatter().format(self._record("hi"))
        parsed = _json.loads(out)
        # We don't want noisy reserved fields in the JSON output.
        for noisy in ("args", "msg", "lineno", "pathname", "filename"):
            assert noisy not in parsed


# -------------------------------
# TypedDict surface
# -------------------------------
class TestTypedDicts:
    def test_term_typeddict_accepts_dict(self):
        # TypedDict is a runtime-friendly type alias; building from a dict
        # literal must work and round-trip cleanly.
        t: Term = {"term": "FOO", "definition": "Foo def."}
        assert t["term"] == "FOO"

    def test_quiz_option_typeddict_accepts_partial(self):
        # total=False allows some keys to be omitted (display is set later).
        o: QuizOption = {"term": "FOO", "definition": "x", "is_correct": True}
        assert o["is_correct"] is True

    def test_quiz_meta_round_trips_via_json(self):
        import json as _json
        meta: QuizMeta = {
            "guild_id": 1,
            "channel_id": 2,
            "initiator_id": 3,
            "initiator_name": "tester",
            "deadline": 1234.5,
            "duration": 5,
            "quiz_terms": [{"term": "FOO", "definition": "x"}],
            "used_options": [[{"term": "A", "definition": "a", "is_correct": False, "display": "a"}]],
            "correct_indices": [0],
            "question_types": ["term_to_definition"],
            "message_ids": [99],
        }
        round_tripped = _json.loads(_json.dumps(meta))
        assert round_tripped == meta


# -------------------------------
# Module surface checks
# -------------------------------
class TestModuleSurface:
    """Smoke tests so a regression that breaks module structure fails fast."""

    def test_exports_pure_helpers(self):
        for name in [
            "build_quiz_question",
            "clean_term",
            "sanitize_definition_for_quiz",
            "pick_single_definition",
            "_parse_terms_from_content",
            "_truncate_code_block",
            "_invalidate_terms_cache",
            "WIKIPEDIA_URL",
            "QuizButton",
            "_make_quiz_view",
            "close_and_summarize",
            "_cleanup_quiz_keys",
            "JSONFormatter",
            "Term",
            "QuizOption",
            "QuizMeta",
            "ACTIVE_QUIZ_KEY_PREFIX",
            "QUIZ_USER_COOLDOWN_KEY_PREFIX",
            "QUIZ_USER_COOLDOWN_SECONDS",
        ]:
            assert hasattr(brevitybot, name), f"missing {name}"

    def test_quizbutton_is_dynamic_item(self):
        """Per chunk (j): public-quiz buttons are persistent DynamicItems so
        they survive bot restarts."""
        import discord
        assert hasattr(brevitybot, "QuizButton")
        assert issubclass(brevitybot.QuizButton, discord.ui.DynamicItem)

    def test_quizbutton_template_matches_expected_custom_id(self):
        """The custom_id pattern must match the format we produce on send."""
        import re
        # Build a button instance and pull its custom_id from the underlying
        # discord.ui.Button — confirms the template encodes (quiz_id, q_idx,
        # answer_idx) in the round-trippable shape we depend on for
        # from_custom_id at click time.
        btn = brevitybot.QuizButton("test-guild-123-456", 2, 1)
        cid = btn.item.custom_id
        assert cid == "q:test-guild-123-456:2:1"
        # The regex template should be able to parse what we just generated.
        pattern = brevitybot.QuizButton.__discord_ui_compiled_template__
        m = pattern.match(cid)
        assert m is not None
        assert m["quiz_id"] == "test-guild-123-456"
        assert m["q_idx"] == "2"
        assert m["answer_idx"] == "1"

    def test_make_quiz_view_has_four_buttons(self):
        view = brevitybot._make_quiz_view("qid", 0)
        assert len(view.children) == 4
        # Persistent views must have timeout=None
        assert view.timeout is None
        # Each child should be a QuizButton wrapping a Button
        for i, child in enumerate(view.children):
            assert isinstance(child, brevitybot.QuizButton)
            assert child.answer_idx == i

    def test_command_admin_gates(self):
        """Per chunks (c) and (m): config and admin-recovery commands must be
        admin-gated and guild-only. /quizstop uses manage_messages, /quizpurge
        uses manage_guild (more restrictive — purge is a recovery hammer)."""
        gated_manage_guild = ["setup", "reloadterms", "setfrequency", "disableposting", "enableposting", "quizpurge"]
        gated_manage_messages = ["quizstop"]
        guild_only_no_admin = ["nextterm", "quiz", "greenieboard", "checkperms"]
        no_gate = ["define"]
        for name in gated_manage_guild:
            cmd = brevitybot.tree.get_command(name)
            assert cmd is not None, f"missing /{name}"
            assert cmd.guild_only is True, f"/{name} should be guild_only"
            perms = cmd.default_permissions
            assert perms is not None and perms.manage_guild, f"/{name} missing manage_guild"
        for name in gated_manage_messages:
            cmd = brevitybot.tree.get_command(name)
            assert cmd is not None, f"missing /{name}"
            assert cmd.guild_only is True
            perms = cmd.default_permissions
            assert perms is not None and perms.manage_messages, f"/{name} missing manage_messages"
        for name in guild_only_no_admin:
            cmd = brevitybot.tree.get_command(name)
            assert cmd is not None
            assert cmd.guild_only is True
            assert cmd.default_permissions is None or not cmd.default_permissions.manage_guild
        for name in no_gate:
            cmd = brevitybot.tree.get_command(name)
            assert cmd is not None
            assert cmd.guild_only is False

    def test_quizstop_quizpurge_registered(self):
        """Surface-only: /quizstop and /quizpurge exist on the tree."""
        for name in ("quizstop", "quizpurge"):
            assert brevitybot.tree.get_command(name) is not None, f"missing /{name}"


# -------------------------------
# pick_single_definition — edge cases
# -------------------------------
class TestPickSingleDefinitionEdgeCases:
    def test_all_non_letter_lines_returns_original(self):
        """When every line/part contains no letters (only digits, punctuation),
        pick_single_definition must fall back to returning the original string."""
        defn = "123\n456\n789"
        out = brevitybot.pick_single_definition(defn)
        assert out == defn

    def test_single_short_part_still_returned(self):
        """A single numbered part that is just barely ≥5 chars and has letters
        should be returned (no randomness needed)."""
        out = brevitybot.pick_single_definition("1. Alpha.")
        assert "Alpha" in out

    def test_no_numbered_markers_returns_full_string(self):
        """Multi-line input with no numbered markers is a single part, so the
        whole string is returned unchanged (no split occurs)."""
        defn = "First valid line.\nSecond valid line."
        out = brevitybot.pick_single_definition(defn)
        assert out == defn

    def test_lines_fallback_when_numbered_parts_are_too_short(self):
        """When numbered splitting produces only very short parts (< 5 chars),
        good_parts stays empty and the lines fallback returns the original line."""
        # "1. AB. 2. CD." splits into ["AB.", "CD."] — each only 3 chars → too
        # short for good_parts; the lines fallback then picks from splitlines().
        out = brevitybot.pick_single_definition("1. AB. 2. CD.")
        assert out == "1. AB. 2. CD."


# -------------------------------
# _truncate_code_block — additional edge cases
# -------------------------------
class TestTruncateCodeBlockEdgeCases:
    def test_limit_smaller_than_fence_overhead_falls_back(self):
        """When limit < len('```\\n') + len('\\n```') + len('...') (= 11),
        max_inner is negative and the function returns text[:limit]."""
        big = "```\n" + ("x" * 50) + "\n```"
        out = brevitybot._truncate_code_block(big, 5)
        assert out == big[:5]
        assert len(out) == 5


# -------------------------------
# _invalidate_terms_cache
# -------------------------------
class TestInvalidateTermsCache:
    def test_resets_cache_to_none(self):
        """_invalidate_terms_cache must set _terms_cache to None."""
        brevitybot._terms_cache = [{"term": "FOO", "definition": "bar"}]
        brevitybot._invalidate_terms_cache()
        assert brevitybot._terms_cache is None

    def test_resets_cache_timestamp_to_zero(self):
        """_invalidate_terms_cache must reset the cache timestamp to 0.0."""
        brevitybot._terms_cache_at = 9999999.0
        brevitybot._invalidate_terms_cache()
        assert brevitybot._terms_cache_at == 0.0


# -------------------------------
# _parse_terms_from_content — additional paths
# -------------------------------
class TestParseTermsAdditionalPaths:
    def test_non_stop_h2_heading_continues_parsing(self):
        """An <h2> whose text is NOT in the stop-list must not halt parsing;
        terms after it should still be collected."""
        html = """
        <div class="mw-parser-output">
          <h2>Introduction</h2>
          <dl><dt>ALPHA</dt><dd>Alpha definition.</dd></dl>
        </div>
        """
        terms = brevitybot._parse_terms_from_content(html)
        assert any(t["term"] == "ALPHA" for t in terms)

    def test_sup_in_dt_is_stripped(self):
        """Citation <sup> tags inside a <dt> must be removed from the term name."""
        html = """
        <div class="mw-parser-output">
          <dl><dt>FOO<sup>[1]</sup></dt><dd>Foo definition.</dd></dl>
        </div>
        """
        terms = brevitybot._parse_terms_from_content(html)
        assert terms[0]["term"] == "FOO"
        assert "[1]" not in terms[0]["term"]

    def test_span_in_dd_is_stripped(self):
        """<span> elements inside a <dd> must not appear in the definition text."""
        html = """
        <div class="mw-parser-output">
          <dl>
            <dt>BAR</dt>
            <dd>Bar body.<span class="mw-editsection">[edit]</span> Continued.</dd>
          </dl>
        </div>
        """
        terms = brevitybot._parse_terms_from_content(html)
        assert "[edit]" not in terms[0]["definition"]
        assert "Bar body" in terms[0]["definition"]
        assert "Continued" in terms[0]["definition"]

    def test_stops_at_footnotes(self):
        """Parser must halt at an <h2>Footnotes</h2> section."""
        html = """
        <div class="mw-parser-output">
          <dl><dt>REAL</dt><dd>Real def.</dd></dl>
          <h2>Footnotes</h2>
          <dl><dt>BOGUS</dt><dd>Bogus def.</dd></dl>
        </div>
        """
        terms = brevitybot._parse_terms_from_content(html)
        assert all(t["term"] != "BOGUS" for t in terms)
        assert any(t["term"] == "REAL" for t in terms)

    def test_wiki_link_markup_unwrapped(self):
        """[[Target|Display]] markup in definition text is replaced with Display."""
        html = """
        <div class="mw-parser-output">
          <dl>
            <dt>LINK</dt>
            <dd>See [[some article|the article]] for details.</dd>
          </dl>
        </div>
        """
        terms = brevitybot._parse_terms_from_content(html)
        assert "the article" in terms[0]["definition"]
        assert "[[" not in terms[0]["definition"]


# -------------------------------
# sanitize_definition_for_quiz — additional cases
# -------------------------------
class TestSanitizeForQuizAdditionalCases:
    def test_bare_numeric_example_replaced(self):
        """TERM 25 (no surrounding quotes) is replaced with [example]."""
        out = brevitybot.sanitize_definition_for_quiz(
            "Call GADABOUT 25 when ready.", term="GADABOUT"
        )
        assert "[example]" in out
        assert "GADABOUT 25" not in out

    def test_range_example_replaced(self):
        """TERM 16-24 range notation is replaced with [example]."""
        out = brevitybot.sanitize_definition_for_quiz(
            "Altitude band is GADABOUT 16-24.", term="GADABOUT"
        )
        assert "[example]" in out
        assert "GADABOUT 16-24" not in out

    def test_multiline_definition_newlines_preserved(self):
        """Newlines in the definition should survive masking so blockquote
        formatting isn't destroyed."""
        out = brevitybot.sanitize_definition_for_quiz(
            "First line.\nSecond line without the term.", term="GADABOUT"
        )
        assert "\n" in out

    def test_mask_length_at_least_six_underscores(self):
        """Even a short two-letter term should be masked with ≥6 underscores."""
        out = brevitybot.sanitize_definition_for_quiz("Go GO now.", term="GO")
        # After normalization underscores are prefixed with two spaces
        assert "______" in out


# -------------------------------
# build_greenie_board_text
# -------------------------------
class TestBuildGreeniesBoard:
    """Tests for the greenie-board text builder.

    Async calls are driven with asyncio.run() so we don't need pytest-asyncio
    to be configured. The guild argument is a MagicMock so member lookups
    resolve instantly without hitting Discord.
    """

    def _run(self, coro):
        return asyncio.run(coro)

    def _entry(self, correct, total, ts=1_000_000):
        return {"correct": correct, "total": total, "ts": ts}

    def _guild(self, display_name="TestUser"):
        guild = MagicMock()
        member = MagicMock()
        member.display_name = display_name
        guild.get_member.return_value = member
        return guild

    def test_empty_list_contains_code_block(self):
        result = self._run(brevitybot.build_greenie_board_text(None, []))
        assert "```" in result

    def test_as_field_false_includes_header(self):
        result = self._run(
            brevitybot.build_greenie_board_text(None, [], as_field=False)
        )
        assert result.startswith("**Greenie Board (Last 10 Quizzes):**")

    def test_as_field_true_omits_header(self):
        result = self._run(
            brevitybot.build_greenie_board_text(None, [], as_field=True)
        )
        assert result.startswith("```")
        assert "Greenie Board" not in result

    def test_green_emoji_for_80_percent(self):
        guild = self._guild("Ace")
        entries = [self._entry(8, 10)]  # 80% → 🟢
        result = self._run(
            brevitybot.build_greenie_board_text(guild, [("1", entries, 0.8)])
        )
        assert "🟢" in result

    def test_yellow_emoji_for_50_percent(self):
        guild = self._guild("Mid")
        entries = [self._entry(5, 10)]  # 50% → 🟡
        result = self._run(
            brevitybot.build_greenie_board_text(guild, [("1", entries, 0.5)])
        )
        assert "🟡" in result

    def test_red_emoji_for_low_score(self):
        guild = self._guild("Low")
        entries = [self._entry(3, 10)]  # 30% → 🔴
        result = self._run(
            brevitybot.build_greenie_board_text(guild, [("1", entries, 0.3)])
        )
        assert "🔴" in result

    def test_blank_slots_padded_with_white_square(self):
        """Fewer than 10 quiz results → remaining slots filled with ⬜."""
        guild = self._guild("Ace")
        entries = [self._entry(10, 10)]  # only 1 entry
        result = self._run(
            brevitybot.build_greenie_board_text(guild, [("1", entries, 1.0)])
        )
        assert "⬜" in result
        assert result.count("⬜") == 9  # 10 - 1 = 9 blank slots

    def test_name_truncated_when_longer_than_12(self):
        """Names with more than 12 characters are trimmed to 11 chars + '…'."""
        guild = self._guild("VeryLongUsername")  # 16 chars
        entries = [self._entry(10, 10)]
        result = self._run(
            brevitybot.build_greenie_board_text(guild, [("1", entries, 1.0)])
        )
        assert "…" in result
        assert "VeryLongUsername" not in result

    def test_short_name_shown_in_full(self):
        """Names with ≤12 characters appear unchanged."""
        guild = self._guild("ShortName")  # 9 chars
        entries = [self._entry(10, 10)]
        result = self._run(
            brevitybot.build_greenie_board_text(guild, [("1", entries, 1.0)])
        )
        assert "ShortName" in result

    def test_percentage_shown_in_output(self):
        """Average percentage is displayed in the output line."""
        guild = self._guild("User")
        entries = [self._entry(7, 10), self._entry(8, 10)]
        avg = (0.7 + 0.8) / 2  # 75%
        result = self._run(
            brevitybot.build_greenie_board_text(guild, [("1", entries, avg)])
        )
        assert "75%" in result

    def test_zero_total_entry_shows_red(self):
        """An entry with total=0 must not crash and must produce a 🔴."""
        guild = self._guild("User")
        entries = [self._entry(0, 0)]  # total=0, pct falls back to 0
        result = self._run(
            brevitybot.build_greenie_board_text(guild, [("1", entries, 0.0)])
        )
        assert "🔴" in result

    def test_caps_output_at_ten_users(self):
        """When more than 10 users are supplied, only the first 10 appear."""
        guild = self._guild("User")
        user_greenies = [
            (str(i), [self._entry(10, 10)], 1.0) for i in range(12)
        ]
        result = self._run(
            brevitybot.build_greenie_board_text(guild, user_greenies)
        )
        # One row per user inside the code block (each row contains "|")
        lines = [ln for ln in result.splitlines() if "|" in ln]
        assert len(lines) == 10

    def test_guild_none_falls_back_to_client_user_lookup(self):
        """Passing guild=None skips guild.get_member and uses client.get_user."""
        mock_user = MagicMock()
        mock_user.name = "CachedUser"
        original_get_user = brevitybot.client.get_user
        try:
            brevitybot.client.get_user = MagicMock(return_value=mock_user)
            entries = [self._entry(10, 10)]
            result = self._run(
                brevitybot.build_greenie_board_text(None, [("99", entries, 1.0)])
            )
            assert "CachedUser" in result
        finally:
            brevitybot.client.get_user = original_get_user


class TestResolvePostChannel:
    """Regression tests for the scheduled-post channel resolution.

    Background: the three tasks.loop tasks are started from setup_hook, which
    runs after login but BEFORE the gateway connects. At that moment
    client.get_channel() misses for every configured channel because the cache
    is still empty. The old code treated that miss as "stale config" and did
    HDEL post_channels <guild_id>, permanently destroying a valid /setup and
    silently stopping scheduled posts for that guild.

    A cache miss must therefore never delete config on its own — only a
    definitive 404 from the API may.
    """

    @staticmethod
    def _response(status):
        return type("R", (), {"status": status, "reason": "err"})()

    class _Redis:
        def __init__(self):
            self.hdel_calls = []

        async def hdel(self, key, field):
            self.hdel_calls.append((key, field))

    def _run(self, *, cached, fetch_exc=None):
        """Resolve a channel with the given cache/API behavior.

        Returns (result_channel, hdel_calls).
        """
        fake_redis = self._Redis()
        sentinel = object()

        original_r = brevitybot.r
        original_get = brevitybot.client.get_channel
        original_fetch = getattr(brevitybot.client, "fetch_channel", None)
        try:
            brevitybot.r = fake_redis
            brevitybot.client.get_channel = lambda cid: sentinel if cached else None

            async def fake_fetch(cid):
                if fetch_exc is not None:
                    raise fetch_exc
                return sentinel

            brevitybot.client.fetch_channel = fake_fetch

            result = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                brevitybot._resolve_post_channel(111, 222)
            )
            return result, fake_redis.hdel_calls, sentinel
        finally:
            brevitybot.r = original_r
            brevitybot.client.get_channel = original_get
            if original_fetch is not None:
                brevitybot.client.fetch_channel = original_fetch

    def test_cache_hit_returns_channel_without_api_call(self):
        result, hdel_calls, sentinel = self._run(cached=True)
        assert result is sentinel
        assert hdel_calls == []

    def test_cache_miss_does_not_delete_config_when_channel_alive(self):
        """The startup race: cache empty, but the channel really exists.

        This is the exact condition that wiped users' /setup configs.
        """
        result, hdel_calls, sentinel = self._run(cached=False)
        assert result is sentinel, "should fall back to an API fetch"
        assert hdel_calls == [], "a cache miss must never delete config"

    def test_genuinely_deleted_channel_is_cleaned_up(self):
        exc = discord.NotFound(self._response(404), "gone")
        result, hdel_calls, _ = self._run(cached=False, fetch_exc=exc)
        assert result is None
        assert hdel_calls == [(brevitybot.CHANNEL_MAP_KEY, "111")]

    def test_forbidden_keeps_config(self):
        """Permissions can be restored by an admin — don't discard config."""
        exc = discord.Forbidden(self._response(403), "denied")
        result, hdel_calls, _ = self._run(cached=False, fetch_exc=exc)
        assert result is None
        assert hdel_calls == []

    def test_transient_http_error_keeps_config(self):
        exc = discord.HTTPException(self._response(500), "boom")
        result, hdel_calls, _ = self._run(cached=False, fetch_exc=exc)
        assert result is None
        assert hdel_calls == []


class TestLoopReadinessGuards:
    """The posting/stats loops must wait for the Discord cache before ticking.

    Started from setup_hook (pre-gateway), an unguarded first tick sees an
    empty guild cache — which is why startup logged "Servers: 0" and why the
    posting loop found no channels.
    """

    def test_post_brevity_term_waits_for_ready(self):
        assert brevitybot.post_brevity_term._before_loop is not None

    def test_log_bot_stats_waits_for_ready(self):
        assert brevitybot.log_bot_stats._before_loop is not None
