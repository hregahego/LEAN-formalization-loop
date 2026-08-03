"""Parsers that read files written by LLM agents.

Every test here corresponds to a real failure: the format an agent produced was
not the format the parser accepted, and the caller read the empty result as
"nothing to report" rather than "I could not read this".
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import formlib as F  # noqa: E402


class NextLineTolerance(unittest.TestCase):
    """`Next:` feeds the recurring-crux stall guard. The worker prompt once
    showed this schema indented, which no matcher accepted."""

    def test_accepts_the_drift_an_agent_actually_produces(self):
        for label, line in [
            ("flush left", "Next: attack `foo`"),
            ("indented", "  Next: attack `foo`"),
            ("bulleted", "- Next: attack `foo`"),
            ("bold", "**Next:** attack `foo`"),
            ("bold+indent", "  **Next:** attack `foo`"),
            ("blockquote", "> Next: attack `foo`"),
            ("lowercase", "next: attack `foo`"),
        ]:
            with self.subTest(label):
                self.assertEqual(F._NEXT_LINE.findall(line), ["attack `foo`"])

    def test_does_not_match_prose(self):
        for line in ["Nextsteps: foo", "the Next: word mid-sentence", "Nextly: x"]:
            with self.subTest(line):
                self.assertEqual(F._NEXT_LINE.findall(line), [])


class AgentAndVerdictTolerance(unittest.TestCase):
    def test_agent_line_variants(self):
        for line, want in [("Agent 1: x", ["1"]), ("**Agent 2:** x", ["2"]),
                           ("- Agent 3: x", ["3"]), ("* **Agent 4**: x", ["4"])]:
            with self.subTest(line):
                self.assertEqual(F._AGENT_LINE.findall(line), want)

    def test_agent_line_ignores_lookalikes(self):
        self.assertEqual(F._AGENT_LINE.findall("Agenda 1: x"), [])

    def test_verdict_variants(self):
        for line in ["Verdict: COMPLETE", "**Verdict:** COMPLETE",
                     "Verdict: **COMPLETE**", "  Verdict: COMPLETE"]:
            with self.subTest(line):
                self.assertEqual(F._VERDICT.findall(line), ["COMPLETE"])

    def test_iteration_header_accepts_deeper_headings(self):
        for line in ["## Iteration 3", "### Iteration 3", "  ## Iteration 3"]:
            with self.subTest(line):
                self.assertEqual(F._ITER_HEADER.findall(line), ["3"])


class FrozenTheoremNames(unittest.TestCase):
    """ALL_THEOREMS is parsed back out of the generated verify.sh. A `)` inside a
    stage comment once truncated the list to 8 of 24 names silently, which capped
    the progress signal and read as a stall."""

    def _names(self, body):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "scripts"))
        with open(os.path.join(d, "scripts", "verify.sh"), "w") as fh:
            fh.write(body)
        return F._solution_frozen_names(d)

    def test_single_line_array(self):
        self.assertEqual(
            self._names('PROJECT="P"\nALL_THEOREMS=("a" "b" "c")\n'), ["a", "b", "c"])

    def test_comment_containing_a_paren_does_not_truncate(self):
        body = ('PROJECT="P"\nALL_THEOREMS=(\n'
                '  # Stage C: the map p lies in Int(D)\n  "a"\n  "b"\n)\n')
        self.assertEqual(self._names(body), ["a", "b"])

    def test_quoted_word_in_a_comment_is_not_a_theorem(self):
        body = 'PROJECT="P"\nALL_THEOREMS=(\n  "a"  # the "main" one\n  "b"\n)\n'
        self.assertEqual(self._names(body), ["a", "b"])

    def test_missing_array_yields_empty_not_a_guess(self):
        self.assertEqual(self._names('PROJECT="P"\n'), [])


class RecurringCruxBlindness(unittest.TestCase):
    """An unparseable PROGRESS.md must be distinguishable from a quiet one."""

    def _progress(self, text):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "PROGRESS.md")
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def test_counts_a_recurring_backticked_crux(self):
        p = self._progress("## a\nAgent: agent-iter9-1\nNext: hit `crux`\n"
                           "## b\nAgent: agent-iter9-2\nNext: hit `crux`\n")
        self.assertEqual(F.recurring_crux(p, 2), ("crux", 2))

    def test_below_threshold_is_none(self):
        p = self._progress("## a\nAgent: agent-iter9-1\nNext: hit `crux`\n")
        self.assertIsNone(F.recurring_crux(p, 2))

    def test_entries_without_a_parseable_next_line_return_none(self):
        p = self._progress("## a\nAgent: agent-iter3-1\nNote: no next line\n")
        self.assertIsNone(F.recurring_crux(p, 2))


if __name__ == "__main__":
    unittest.main()
