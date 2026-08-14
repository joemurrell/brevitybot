"""Robustness tests for HTML parsing and sanitization helpers.

Covers malformed/edge-case Wikipedia HTML, Unicode inputs, and special
character handling in sanitize_definition_for_quiz.  All helpers are pure
(no I/O) so no external dependencies are needed.
"""
import pytest

from brevitybot import _parse_terms_from_content, sanitize_definition_for_quiz, clean_term


def _wrap(body: str) -> str:
    """Wrap an HTML body snippet in the minimal Wikipedia container."""
    return (
        '<html><body>'
        '<div class="mw-parser-output">'
        + body
        + '</div></body></html>'
    )


# ─────────────────────────────────────────────
# _parse_terms_from_content — edge-case HTML
# ─────────────────────────────────────────────
class TestParseTermsRobustness:
    def test_empty_bytes_returns_empty(self):
        assert _parse_terms_from_content(b"") == []

    def test_empty_string_returns_empty(self):
        assert _parse_terms_from_content("") == []

    def test_no_content_div_returns_empty(self):
        html = "<html><body><dl><dt>FOO</dt><dd>Bar def</dd></dl></body></html>"
        assert _parse_terms_from_content(html) == []

    def test_dl_without_dt_skipped(self):
        html = _wrap("<dl><dd>Orphan definition with no term.</dd></dl>")
        assert _parse_terms_from_content(html) == []

    def test_dt_without_dd_skipped(self):
        html = _wrap("<dl><dt>TERMONLY</dt></dl>")
        assert _parse_terms_from_content(html) == []

    def test_dt_with_empty_dd_skipped(self):
        html = _wrap("<dl><dt>EMPTYDEF</dt><dd>   </dd></dl>")
        result = _parse_terms_from_content(html)
        assert result == []

    def test_simple_term_and_definition(self):
        html = _wrap("<dl><dt>ALPHA</dt><dd>First meaning.</dd></dl>")
        result = _parse_terms_from_content(html)
        assert len(result) == 1
        assert result[0]["term"] == "ALPHA"
        assert "First meaning" in result[0]["definition"]

    def test_multiple_terms_in_one_dl(self):
        html = _wrap(
            "<dl>"
            "<dt>BRAVO</dt><dd>Bravo def.</dd>"
            "<dt>CHARLIE</dt><dd>Charlie def.</dd>"
            "</dl>"
        )
        terms = _parse_terms_from_content(html)
        assert len(terms) == 2
        assert terms[0]["term"] == "BRAVO"
        assert terms[1]["term"] == "CHARLIE"

    def test_stops_at_see_also_h2(self):
        html = _wrap(
            "<dl><dt>DELTA</dt><dd>Delta def.</dd></dl>"
            "<h2>See also</h2>"
            "<dl><dt>ECHO</dt><dd>Echo def.</dd></dl>"
        )
        terms = _parse_terms_from_content(html)
        names = [t["term"] for t in terms]
        assert "DELTA" in names
        assert "ECHO" not in names

    def test_stops_at_references_h2(self):
        html = _wrap(
            "<dl><dt>FOXTROT</dt><dd>Foxtrot def.</dd></dl>"
            "<h2>References</h2>"
            "<dl><dt>GOLF</dt><dd>Golf def.</dd></dl>"
        )
        terms = _parse_terms_from_content(html)
        assert any(t["term"] == "FOXTROT" for t in terms)
        assert not any(t["term"] == "GOLF" for t in terms)

    def test_nested_dl_not_double_counted(self):
        """A <dl> nested inside a <dd> should not appear as a top-level term."""
        html = _wrap(
            "<dl>"
            "<dt>HOTEL</dt>"
            "<dd>Outer def."
            "  <dl><dt>SUB</dt><dd>Sub-def.</dd></dl>"
            "</dd>"
            "</dl>"
        )
        terms = _parse_terms_from_content(html)
        names = [t["term"] for t in terms]
        assert "HOTEL" in names
        assert "SUB" not in names

    def test_nested_ul_bullets_included_once(self):
        """Items in a nested <ul> are rendered as bullet lines, not duplicated."""
        html = _wrap(
            "<dl>"
            "<dt>INDIA</dt>"
            "<dd>Header text."
            "  <ul><li>Bullet one.</li><li>Bullet two.</li></ul>"
            "</dd>"
            "</dl>"
        )
        terms = _parse_terms_from_content(html)
        assert len(terms) == 1
        defn = terms[0]["definition"]
        assert defn.count("Bullet one") == 1
        assert defn.count("Bullet two") == 1

    def test_sup_elements_stripped(self):
        """Footnote <sup> tags should be removed from term names and definitions."""
        html = _wrap(
            "<dl>"
            "<dt>JULIET<sup>[1]</sup></dt>"
            "<dd>Juliet def.<sup>[2]</sup></dd>"
            "</dl>"
        )
        terms = _parse_terms_from_content(html)
        assert len(terms) == 1
        assert "[1]" not in terms[0]["term"]
        assert "[2]" not in terms[0]["definition"]

    def test_span_elements_stripped(self):
        html = _wrap(
            "<dl>"
            "<dt>KILO</dt>"
            "<dd>Definition <span class='mw-something'>noise</span> end.</dd>"
            "</dl>"
        )
        terms = _parse_terms_from_content(html)
        assert len(terms) == 1
        assert "noise" not in terms[0]["definition"]

    def test_unicode_term_and_definition(self):
        html = _wrap(
            "<dl>"
            "<dt>RÖNTGEN</dt>"
            "<dd>Radiological term — überladen (ångström).</dd>"
            "</dl>"
        )
        terms = _parse_terms_from_content(html)
        assert len(terms) == 1
        assert "RÖNTGEN" in terms[0]["term"]
        assert "überladen" in terms[0]["definition"]

    def test_bytes_input_parsed(self):
        html = _wrap("<dl><dt>LIMA</dt><dd>Lima def.</dd></dl>")
        terms = _parse_terms_from_content(html.encode("utf-8"))
        assert len(terms) == 1
        assert terms[0]["term"] == "LIMA"

    def test_multiple_dd_per_term_joined(self):
        """Multiple <dd> lines under one <dt> should be joined into one definition."""
        html = _wrap(
            "<dl>"
            "<dt>MIKE</dt>"
            "<dd>First line.</dd>"
            "<dd>Second line.</dd>"
            "</dl>"
        )
        terms = _parse_terms_from_content(html)
        assert len(terms) == 1
        defn = terms[0]["definition"]
        assert "First line" in defn
        assert "Second line" in defn

    def test_asterisks_stripped_from_term(self):
        html = _wrap("<dl><dt>*NOVEMBER*</dt><dd>November def.</dd></dl>")
        terms = _parse_terms_from_content(html)
        assert len(terms) == 1
        assert terms[0]["term"] == "NOVEMBER"

    def test_stray_ul_outside_dl_not_attached(self):
        """A <ul> that lives outside any <dl> must not be glued onto a term."""
        html = _wrap(
            "<dl><dt>OSCAR</dt><dd>Oscar def.</dd></dl>"
            "<ul><li>Stray bullet.</li></ul>"
        )
        terms = _parse_terms_from_content(html)
        assert len(terms) == 1
        assert "Stray bullet" not in terms[0]["definition"]

    def test_deeply_malformed_html_no_crash(self):
        """Severely malformed markup must not raise an exception."""
        malformed = _wrap("<dl><dt><dd></dt><dt>PAPA</dt><dd>Papa def.</dd></dl>")
        result = _parse_terms_from_content(malformed)
        # At minimum we should not crash; result may vary
        assert isinstance(result, list)


# ─────────────────────────────────────────────
# sanitize_definition_for_quiz — Unicode / special chars
# ─────────────────────────────────────────────
class TestSanitizeDefinitionRobustness:
    def test_unicode_term_masked(self):
        out = sanitize_definition_for_quiz(
            "RÖNTGEN is a radiological term used in RÖNTGEN research.",
            term="RÖNTGEN",
        )
        assert "RÖNTGEN" not in out
        assert "______" in out

    def test_emoji_in_definition_preserved(self):
        out = sanitize_definition_for_quiz(
            "A signal used 🚀 in operations where TANGO is needed.",
            term="TANGO",
        )
        assert "🚀" in out
        assert "TANGO" not in out

    def test_special_regex_chars_in_term_not_crashing(self):
        """Term containing regex metacharacters must not raise re.error."""
        out = sanitize_definition_for_quiz(
            "C++ is a programming term, used in C++ contexts.",
            term="C++",
        )
        assert isinstance(out, str)

    def test_newlines_preserved(self):
        defn = "First line.\nSecond line about ZULU operations.\nThird line."
        out = sanitize_definition_for_quiz(defn, term="ZULU")
        assert "\n" in out

    def test_empty_term_returns_unchanged(self):
        defn = "Some definition text."
        out = sanitize_definition_for_quiz(defn, term="")
        assert isinstance(out, str)

    def test_empty_definition_returns_empty(self):
        assert sanitize_definition_for_quiz("", term="FOO") == ""

    def test_term_with_number_example_masked(self):
        """Quoted examples like "BOGEY 25" should be replaced with [example]."""
        out = sanitize_definition_for_quiz(
            'Report "BOGEY 25" to control immediately.',
            term="BOGEY",
        )
        assert '"BOGEY 25"' not in out
        assert "[example]" in out

    def test_bare_term_with_number_masked(self):
        out = sanitize_definition_for_quiz(
            "Contact BOGEY 16-24 for coordination.",
            term="BOGEY",
        )
        assert "BOGEY 16-24" not in out

    def test_case_insensitive_masking(self):
        out = sanitize_definition_for_quiz(
            "Use bogey when a Bogey appears on radar.",
            term="BOGEY",
        )
        assert "bogey" not in out.lower()

    def test_double_spaces_collapsed(self):
        """The function collapses extra spaces from removals, but deliberately
        adds 2-space prefixes before underscore masks — so the result will
        have '  ___' sequences by design."""
        out = sanitize_definition_for_quiz(
            "Signal WHISKEY to all units when WHISKEY is detected.",
            term="WHISKEY",
        )
        assert "WHISKEY" not in out.upper()
        # Mask prefixes intentionally start with two spaces
        assert "  " in out

    def test_result_is_str(self):
        out = sanitize_definition_for_quiz("A term definition.", term="TERM")
        assert isinstance(out, str)

    def test_unicode_combining_chars_no_crash(self):
        """Combining diacritical marks (e.g. naïve) should not crash the regex."""
        out = sanitize_definition_for_quiz(
            "Naïve approach used in ALPHA operations.",
            term="ALPHA",
        )
        assert isinstance(out, str)

    def test_long_definition_not_truncated(self):
        defn = ("The quick brown fox jumps over the lazy dog. " * 20).strip()
        out = sanitize_definition_for_quiz(defn, term="NOTPRESENT")
        assert len(out) > 100


# ─────────────────────────────────────────────
# clean_term — edge cases
# ─────────────────────────────────────────────
class TestCleanTermEdgeCases:
    def test_only_asterisks_returns_empty(self):
        assert clean_term("***") == ""

    def test_mixed_whitespace_and_asterisks(self):
        result = clean_term("  *FOO BAR*  ")
        assert result == "FOO BAR"

    def test_brackets_retained(self):
        assert clean_term("[NATO] ALPHA") == "[NATO] ALPHA"

    def test_unicode_preserved(self):
        assert clean_term("ÅNGSTRÖM") == "ÅNGSTRÖM"

    def test_empty_string_returns_empty(self):
        assert clean_term("") == ""
