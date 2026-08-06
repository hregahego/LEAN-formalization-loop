"""Control-file integrity, and the exit-code contract with the harness.

The harness and the files that configure it live in the workspace the worker
agents edit. These tests pin the property that matters: a change to any of them
is detected, and "could not verify" never reads as "verified".
"""
import contextlib
import io
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
        out = "\n".join(f"FAIL: t{i}" for i in range(60))
        self.assertIn("20 more FAIL line(s)", loop.verify_digest(1, out))

    def test_reports_that_the_harness_did_not_run(self):
        self.assertIn("HARNESS DID NOT RUN", loop.verify_digest(-1, "boom"))


if __name__ == "__main__":
    unittest.main()


class CanonicalHarnessJson(unittest.TestCase):
    """scripts/harness.json is hashed into the control manifest, so its bytes
    must be a function of its values. Agents format JSON inconsistently — the
    existing runs vary in indentation and key order — which would otherwise make
    two identically-configured projects pin different hashes."""

    def _canonical(self, payload):
        import setup as S
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "scripts"))
        _write(os.path.join(d, "scripts", "harness.json"), payload)
        project, problem, theorems, final, mandatory = S._read_harness_params(d)
        S.write_harness_json(d, project, problem, theorems, final, mandatory)
        with open(os.path.join(d, "scripts", "harness.json"), encoding="utf-8") as fh:
            return fh.read()

    def test_formatting_of_the_source_does_not_affect_the_result(self):
        compact = self._canonical('{"project":"P","problem":"t","theorems":["a","b"]}')
        sprawling = self._canonical(
            '{\n "theorems" : [ "a" , "b" ],\n  "problem":"t",\n "project" : "P"\n}\n')
        self.assertEqual(compact, sprawling)

    def test_unknown_keys_are_dropped(self):
        out = self._canonical('{"project":"P","problem":"t","theorems":["a"],'
                              '"_comment":"from the template"}')
        self.assertNotIn("_comment", out)

    def test_the_optional_check_4b_pair_is_omitted_when_unused(self):
        out = json.loads(self._canonical(
            '{"project":"P","problem":"t","theorems":["a"]}'))
        self.assertNotIn("final_theorem", out)
        self.assertNotIn("mandatory_axioms", out)

    def test_the_optional_pair_survives_when_set(self):
        out = json.loads(self._canonical(json.dumps(
            {"project": "P", "problem": "t", "theorems": ["a"],
             "final_theorem": "a", "mandatory_axioms": ["P.cert"]})))
        self.assertEqual(out["final_theorem"], "a")
        self.assertEqual(out["mandatory_axioms"], ["P.cert"])

    def test_ends_with_a_trailing_newline(self):
        self.assertTrue(self._canonical(
            '{"project":"P","problem":"t","theorems":["a"]}').endswith("\n"))


class AgentCommandConstruction(unittest.TestCase):
    """build_cmd is what actually launches every agent, but nothing else in the
    suite touches it — a dead-code removal once left a reference to a deleted
    parameter here, and every invocation on the default CLI raised NameError
    until someone ran the pipeline. These tests are the smoke alarm."""

    def test_claude_command_is_constructible(self):
        cmd = F.build_cmd("do the thing")
        self.assertIn("do the thing", cmd)
        self.assertIn("-p", cmd)

    def test_codex_command_is_constructible(self):
        saved = F.AGENT_CLI
        try:
            F.AGENT_CLI = "codex"
            cmd = F.build_cmd("do the thing")
            self.assertEqual(cmd[1], "exec")
            self.assertIn("do the thing", cmd)
        finally:
            F.AGENT_CLI = saved

    def test_optional_arguments_are_threaded_through(self):
        cmd = F.build_cmd("p", add_dirs=["/ref"], model="some-model",
                          output_format="json")
        self.assertIn("/ref", cmd)
        self.assertIn("some-model", cmd)
        self.assertIn("json", cmd)

    def test_every_element_is_a_string(self):
        """subprocess rejects a non-string argv entry at launch time."""
        for cmd in (F.build_cmd("p"),
                    F.build_cmd("p", add_dirs=["/a", "/b"], model="m")):
            self.assertTrue(all(isinstance(part, str) for part in cmd), cmd)


class AgentLaunchSignatures(unittest.TestCase):
    """Smoke-test the launch path itself, in --dry-run so nothing is spawned.

    build_cmd having tests was not enough: a parameter was removed from it but
    left in run_agent's signature and forwarding call, so every caller failed
    with TypeError while build_cmd's own tests passed. These mirror the real
    call shapes in setup.py, init.py and loop.py.
    """

    def setUp(self):
        self._stdout = contextlib.redirect_stdout(io.StringIO())
        self._stdout.__enter__()

    def tearDown(self):
        self._stdout.__exit__(None, None, None)

    def test_setup_architect_shape(self):
        r = F.run_agent("setup-architect", "prompt", cwd="/tmp",
                        add_dirs=["/ref"], model=None,
                        timeout=60, log_dir=None, dry_run=True)
        self.assertTrue(r.ok)

    def test_init_shapes(self):
        for fmt in ("text", "json"):
            with self.subTest(fmt):
                r = F.run_agent("init-step", "prompt", cwd="/tmp", model=None,
                                timeout=60, log_dir=None, dry_run=True,
                                output_format=fmt)
                self.assertTrue(r.ok)

    def test_loop_plan_and_review_shape(self):
        r = F.run_agent("iter001-plan", "prompt", cwd="/tmp", model=None,
                        timeout=60, log_dir=None, dry_run=True,
                        output_format="json")
        self.assertTrue(r.ok)

    def test_parallel_worker_shape(self):
        specs = [dict(label=f"iter001-worker{k}", prompt="p", cwd="/tmp",
                      model=None, timeout=60, log_dir=None, dry_run=True)
                 for k in (1, 2)]
        results = F.run_agents_parallel(specs, max_workers=2)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.ok for r in results))
