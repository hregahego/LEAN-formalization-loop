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
