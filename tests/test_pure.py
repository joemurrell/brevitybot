"""Tests for the pure helper functions in brevitybot.

These functions do no I/O (no Redis, no Discord, no HTTP) so they're fully
unit-testable. The bootstrap in conftest.py sets the env vars brevitybot
demands at import time.
"""
import random

import pytest

import brevitybot
from brevitybot import (
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
# Module surface checks
# -------------------------------
class TestModuleSurface:
    """Smoke tests so a regression that breaks module structure fails fast."""

    def test_exports_pure_helpers(self):
        for name in [
            "clean_term",
            "sanitize_definition_for_quiz",
            "pick_single_definition",
            "_parse_terms_from_content",
            "_truncate_code_block",
            "WIKIPEDIA_URL",
        ]:
            assert hasattr(brevitybot, name), f"missing {name}"

    def test_only_one_publicquizview(self):
        assert sum(1 for n in dir(brevitybot) if n == "PublicQuizView") == 1

    def test_publicquizview_takes_ttl_seconds(self):
        import inspect
        sig = inspect.signature(brevitybot.PublicQuizView.__init__)
        assert "ttl_seconds" in sig.parameters

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
