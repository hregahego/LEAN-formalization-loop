"""Prompt loading.

Prompts live in prompts/*.md and use `@@MARKER@@` placeholders rather than
`{field}`: their bodies contain literal JSON and Lean braces, so a brace-based
template needs escaping that is easy to get wrong and invisible when it is. The
tests below pin the property that made the change worth it — a marker can never
be confused with prompt content, and a missed substitution fails loudly instead
of shipping `@@N@@` to an agent.
"""
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import formlib as F  # noqa: E402

# Every prompt the pipeline loads, with the parameters its call site supplies.
PROMPTS = {
    "setup_architect": {"ref": "/tmp/reference"},
    "init_step_minus1": {},
    "init_faithfulness_review": {},
    "init_freeze_repair": {},
    "loop_plan": {"n": 3},
    "loop_worker": {"n": 3, "k": 2},
    "loop_verify_block": {"digest": "DIGEST"},
    "loop_full_audit": {},
    "loop_review": {"n": 3, "verify_block": "VB", "full_block": "FB",
                    "full_tag": "FT"},
}


class EveryPromptLoads(unittest.TestCase):
    def test_all_render_with_no_marker_left_behind(self):
        for name, params in PROMPTS.items():
            with self.subTest(name):
                text = F.load_prompt(name, **params)
                self.assertNotRegex(text, r"@@[A-Z_]+@@")
                self.assertTrue(text.strip())

    def test_every_prompt_file_is_declared_here(self):
        """A new prompt with no test is a prompt nobody checks renders."""
        on_disk = {f[:-3] for f in os.listdir(F.PROMPTS_DIR) if f.endswith(".md")}
        self.assertEqual(on_disk, set(PROMPTS))


class LoaderIsStrict(unittest.TestCase):
    def test_missing_parameter_raises(self):
        with self.assertRaises(KeyError):
            F.load_prompt("loop_worker", n=1)  # `k` omitted

    def test_unknown_parameter_raises(self):
        """Catches a renamed marker: silently ignoring the extra argument would
        leave the real marker unsubstituted."""
        with self.assertRaises(KeyError):
            F.load_prompt("loop_full_audit", nonexistent="x")

    def test_substitution_is_literal_and_brace_safe(self):
        """A digest containing braces must survive verbatim — the old
        `.format()` template would have raised or mangled it."""
        digest = 'RESULT {"iteration": 3} {{not a field}}'
        self.assertIn(digest, F.load_prompt("loop_verify_block", digest=digest))


class PromptContent(unittest.TestCase):
    def test_review_prompt_keeps_its_machine_readable_trailer(self):
        text = F.load_prompt("loop_review", n=9, verify_block="", full_block="",
                             full_tag="")
        self.assertIn("<<<ORCH", text)
        self.assertIn('"iteration": 9', text)

    def test_worker_prompt_shows_the_entry_schema_flush_left(self):
        """The schema a worker copies must be the one the parsers accept."""
        text = F.load_prompt("loop_worker", n=1, k=1)
        for line in ("Agent: agent-iter1-1", "Next: "):
            self.assertRegex(text, r"(?m)^" + re.escape(line))


if __name__ == "__main__":
    unittest.main()
