"""Tests for the pure helper functions in brevitybot.

These functions do no I/O (no Redis, no Discord, no HTTP) so they're fully
unit-testable. The bootstrap in conftest.py sets the env vars brevitybot
demands at import time.
"""
import random

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
        """Per chunk (c): config commands must be admin-gated and guild-only."""
        gated_admin = ["setup", "reloadterms", "setfrequency", "disableposting", "enableposting"]
        guild_only_no_admin = ["nextterm", "quiz", "greenieboard", "checkperms"]
        no_gate = ["define"]
        for name in gated_admin:
            cmd = brevitybot.tree.get_command(name)
            assert cmd is not None, f"missing /{name}"
            assert cmd.guild_only is True, f"/{name} should be guild_only"
            perms = cmd.default_permissions
            assert perms is not None and perms.manage_guild, f"/{name} missing manage_guild"
        for name in guild_only_no_admin:
            cmd = brevitybot.tree.get_command(name)
            assert cmd is not None
            assert cmd.guild_only is True
            assert cmd.default_permissions is None or not cmd.default_permissions.manage_guild
        for name in no_gate:
            cmd = brevitybot.tree.get_command(name)
            assert cmd is not None
            assert cmd.guild_only is False
