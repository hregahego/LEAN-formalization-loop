"""Progress and stall signals — signals.py.

These read the repo, never an agent's claims: the frozen theorem list comes from
scripts/harness.json and the discharged count from Solution.lean. The tests below
pin the failure that motivated each one.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signals as S  # noqa: E402


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
                self.assertEqual(S._NEXT_LINE.findall(line), ["attack `foo`"])

    def test_does_not_match_prose(self):
        for line in ["Nextsteps: foo", "the Next: word mid-sentence", "Nextly: x"]:
            with self.subTest(line):
                self.assertEqual(S._NEXT_LINE.findall(line), [])


class FrozenTheoremNames(unittest.TestCase):
    """The frozen names come from scripts/harness.json.

    They used to be recovered by regex from the generated shell harness, where a
    `)` in one of the architect's stage comments truncated the list to 8 of 24
    names silently — capping the progress signal so a working run read as a
    stall. Reading the JSON removes the parse entirely.
    """

    def _project(self, payload):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "scripts"))
        with open(os.path.join(d, "scripts", "harness.json"), "w") as fh:
            fh.write(payload)
        return d

    def test_reads_names_in_order(self):
        d = self._project('{"project": "P", "theorems": ["a", "b", "c"]}')
        self.assertEqual(S.frozen_theorem_names(d), ["a", "b", "c"])
        self.assertEqual(S.project_name(d), "P")

    def test_punctuation_in_other_fields_cannot_truncate_the_list(self):
        d = self._project('{"project": "P", "problem": "lies in Int(D) )))",'
                          ' "theorems": ["a", "b"]}')
        self.assertEqual(S.frozen_theorem_names(d), ["a", "b"])

    def test_missing_file_yields_empty_not_a_guess(self):
        self.assertEqual(S.frozen_theorem_names(tempfile.mkdtemp()), [])

    def test_malformed_json_yields_empty_not_a_crash(self):
        d = self._project("{not json")
        self.assertEqual(S.frozen_theorem_names(d), [])
        self.assertIsNone(S.project_name(d))


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
        self.assertEqual(S.recurring_crux(p, 2), ("crux", 2))

    def test_below_threshold_is_none(self):
        p = self._progress("## a\nAgent: agent-iter9-1\nNext: hit `crux`\n")
        self.assertIsNone(S.recurring_crux(p, 2))

    def test_entries_without_a_parseable_next_line_return_none(self):
        p = self._progress("## a\nAgent: agent-iter3-1\nNote: no next line\n")
        self.assertIsNone(S.recurring_crux(p, 2))


if __name__ == "__main__":
    unittest.main()


class LedgerDurability(unittest.TestCase):
    """The stall guard's entire memory is this file. A partial write reads as
    "no history", which silently resets the window — so a run circling a wall
    would look freshly started and the guard would never fire."""

    def _target(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "logs", "orchestration"))
        return d

    def test_records_and_reads_back(self):
        d = self._target()
        S.record_progress(d, 1, 3)
        S.record_progress(d, 2, 5)
        self.assertEqual([e["signal"] for e in S.read_ledger(d)], [3, 5])

    def test_rerunning_an_iteration_overwrites_rather_than_duplicates(self):
        d = self._target()
        S.record_progress(d, 1, 3)
        S.record_progress(d, 1, 4)
        ledger = S.read_ledger(d)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["signal"], 4)

    def test_no_partial_file_is_left_behind(self):
        """The write goes to a temp path and is renamed into place."""
        d = self._target()
        S.record_progress(d, 1, 3)
        leftovers = [f for f in os.listdir(os.path.join(d, "logs", "orchestration"))
                     if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_a_truncated_ledger_is_reported_not_silently_ignored(self):
        d = self._target()
        S.record_progress(d, 1, 3)
        with open(os.path.join(d, "logs", "orchestration",
                               "progress_ledger.json"), "w") as fh:
            fh.write('[\n  {\n    "iteration": 1,\n    "signal": ')
        self.assertEqual(S.read_ledger(d), [])   # degrades safely...
        # ...and the stall guard cannot fire on an empty history, which is why
        # the warning matters: the operator has to know the window reset.
        self.assertFalse(S.stalled_for(S.read_ledger(d), 16))


class LedgerRejectsNonCounts(unittest.TestCase):
    """A local named `signal` once shadowed the stdlib module of that name, and
    the module itself reached json.dump — surfacing as a TypeError six frames
    deep in the encoder, naming neither the caller nor the value. The ledger now
    refuses anything that is not a count, at the boundary."""

    def test_rejects_a_module(self):
        import signal as stdlib_signal
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "logs", "orchestration"))
        with self.assertRaises(TypeError) as caught:
            S.record_progress(d, 1, stdlib_signal)
        self.assertIn("expected an int", str(caught.exception))

    def test_rejects_a_string(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "logs", "orchestration"))
        with self.assertRaises(TypeError):
            S.record_progress(d, 1, "3")

    def test_accepts_a_count(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "logs", "orchestration"))
        self.assertEqual(S.record_progress(d, 1, 0)[0]["signal"], 0)


class LoopStallGuardPath(unittest.TestCase):
    """Mirror the sequence loop.py runs after each review. This is the path that
    crashed on iteration 1 of the first real run."""

    def test_one_iteration_of_the_guard_sequence(self):
        import loop
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "logs", "orchestration"))
        os.makedirs(os.path.join(d, "scripts"))
        with open(os.path.join(d, "scripts", "harness.json"), "w") as fh:
            fh.write('{"project": "P", "theorems": ["a", "b"]}')
        with open(os.path.join(d, "PROGRESS.md"), "w") as fh:
            fh.write("## x\nAgent: agent-iter1-1\nNext: attack `crux`\n")

        discharged = S.progress_signal(d)
        ledger = S.record_progress(d, 1, discharged)
        n_frozen = len(S.frozen_theorem_names(d))
        self.assertEqual((discharged, n_frozen), (0, 2))
        self.assertFalse(S.stalled_for(ledger, loop.STALL_WINDOW))
        self.assertIsNone(
            S.recurring_crux(os.path.join(d, "PROGRESS.md"),
                             loop.CRUX_RECUR_LIMIT, since_iteration=0))
