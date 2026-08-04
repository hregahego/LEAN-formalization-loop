"""Control-file integrity, and the exit-code contract with the harness.

The harness and the files that configure it live in the workspace the worker
agents edit. These tests pin the property that matters: a change to any of them
is detected, and "could not verify" never reads as "verified".
"""
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import formlib as F  # noqa: E402
import loop  # noqa: E402


def _write(path, body):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def _workspace():
    """A scaffolded-looking workspace with all four control files."""
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "scripts"))
    _write(os.path.join(d, "scripts", "verify.py"), "raise SystemExit(0)\n")
    _write(os.path.join(d, "scripts", "harness.json"),
           json.dumps({"project": "P", "theorems": ["a"]}))
    _write(os.path.join(d, "scripts", "frozen.sha256"), "abc  P/Defs.lean\n")
    _write(os.path.join(d, "scripts", "ALLOWED_AXIOMS.txt"), "# none\n")
    return d


class ControlManifest(unittest.TestCase):
    def test_clean_workspace_reports_no_problems(self):
        d = _workspace()
        F.write_control_manifest(d)
        self.assertEqual(F.check_control_manifest(d), [])

    def test_detects_modification_of_each_control_file(self):
        for name in ("verify.py", "harness.json", "frozen.sha256", "ALLOWED_AXIOMS.txt"):
            with self.subTest(name):
                d = _workspace()
                F.write_control_manifest(d)
                with open(os.path.join(d, "scripts", name), "a") as fh:
                    fh.write("\n# tampered\n")
                problems = F.check_control_manifest(d)
                self.assertTrue(any(name in p and "MODIFIED" in p for p in problems),
                                problems)

    def test_detects_deletion(self):
        d = _workspace()
        F.write_control_manifest(d)
        os.remove(os.path.join(d, "scripts", "ALLOWED_AXIOMS.txt"))
        self.assertTrue(any("MISSING" in p for p in F.check_control_manifest(d)))

    def test_detects_creation_of_a_file_that_was_absent_when_pinned(self):
        """verify.py honours ALLOWED_AXIOMS.txt whenever it exists, so a file
        left uncreated is an open door unless its absence is pinned too."""
        d = _workspace()
        os.remove(os.path.join(d, "scripts", "ALLOWED_AXIOMS.txt"))
        F.write_control_manifest(d)
        _write(os.path.join(d, "scripts", "ALLOWED_AXIOMS.txt"), "P.my_axiom\n")
        self.assertTrue(any("CREATED" in p for p in F.check_control_manifest(d)))

    def test_a_missing_manifest_is_a_problem_not_a_pass(self):
        d = _workspace()
        self.assertNotEqual(F.check_control_manifest(d), [])


class RunVerifyContract(unittest.TestCase):
    """`run_verify` returns (issues, output); issues < 0 means NOTHING was
    verified. Anything that blurs that lets an unrun harness look like a
    near-pass."""

    def _harness(self, body):
        d = _workspace()
        p = os.path.join(d, "scripts", "verify.py")
        _write(p, body)
        os.chmod(p, 0o755)
        F.write_control_manifest(d)
        return d

    def test_zero_exit_is_a_pass(self):
        d = self._harness("print('ok')\nraise SystemExit(0)\n")
        self.assertEqual(loop.run_verify(d)[0], 0)

    def test_small_exit_is_an_issue_count(self):
        d = self._harness("print('bad')\nraise SystemExit(2)\n")
        self.assertEqual(loop.run_verify(d)[0], 2)

    def test_preflight_exit_64_means_did_not_run(self):
        d = self._harness("print('ERROR')\nraise SystemExit(64)\n")
        self.assertEqual(loop.run_verify(d)[0], -1)

    def test_tampering_is_detected_before_the_harness_runs(self):
        d = self._harness("print('ok')\nraise SystemExit(0)\n")
        with open(os.path.join(d, "scripts", "ALLOWED_AXIOMS.txt"), "a") as fh:
            fh.write("P.sneaky\n")
        issues, out = loop.run_verify(d)
        self.assertEqual(issues, -1)
        self.assertIn("CONTROL FILES CHANGED", out)

    def test_missing_harness_is_not_a_pass(self):
        self.assertEqual(loop.run_verify(tempfile.mkdtemp())[0], -1)

    def test_dry_run_does_not_execute_and_does_not_claim_a_pass(self):
        d = self._harness("raise SystemExit(3)\n")
        issues, out = loop.run_verify(d, dry_run=True)
        self.assertIn("dry-run", out)
        self.assertIn("dry-run", loop.verify_digest(issues, out))


class VerifyDigest(unittest.TestCase):
    def test_keeps_failures_and_counts_passes(self):
        out = ("--- Check 4 ---\n" + "PASS: a\nPASS: b\n"
               + "FAIL: c broke\n=== RESULT: FAIL (1 issue(s), 3s) ===")
        d = loop.verify_digest(1, out)
        self.assertIn("FAIL: c broke", d)
        self.assertIn("2 PASS line(s) omitted", d)
        self.assertIn("=== RESULT", d)

    def test_caps_runaway_failure_lists(self):
        out = "\n".join("FAIL: t%d" % i for i in range(60))
        self.assertIn("20 more FAIL line(s)", loop.verify_digest(1, out))

    def test_reports_that_the_harness_did_not_run(self):
        self.assertIn("HARNESS DID NOT RUN", loop.verify_digest(-1, "boom"))


if __name__ == "__main__":
    unittest.main()
