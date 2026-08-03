"""The verification harness itself.

These exercise the SHIPPED reference/scripts/verify.sh — the banned-keyword
scanner is extracted from its heredoc rather than copied, so the test cannot
drift from the code it claims to cover.

Checks needing a Lean toolchain (3, 4, 4b, 5, 5b) are not covered here; what is
covered is every path where the harness could previously report success without
having verified anything.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, "reference", "scripts", "verify.sh")
sys.path.insert(0, REPO)
import setup as S  # noqa: E402

EX_PREFLIGHT = 64


def _harness_source():
    with open(HARNESS, encoding="utf-8") as fh:
        return fh.read()


def _scanner():
    """The Check 2 scanner, exec'd out of the harness's own heredoc."""
    src = _harness_source()
    body = src[src.index("import os, re, sys, glob"):]
    body = body[:body.index("\nPY\n")]
    # The scanner reads its inputs from the environment, as the harness sets them.
    env = {"SRC_DIR": "/nonexistent", "THEOREMS_FILE": "/nonexistent/Theorems.lean",
           "ROOT_LEAN": "/nonexistent/P.lean", "ALLOWED_AXIOMS": ""}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        ns = {}
        exec(compile(body[:body.index("files = sorted")], "<check2>", "exec"),
             {"os": os, "re": re, "sys": sys}, ns)
        return ns
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class BannedKeywordScanner(unittest.TestCase):
    def setUp(self):
        ns = _scanner()
        self.strip = ns["strip_comments"]
        self.banned = ns["banned"]

    def test_a_string_literal_cannot_hide_a_sorry(self):
        """`def m : String := "/-"` once opened a block comment that swallowed
        every following line, hiding a sorry from the scanner entirely."""
        code = 'def m : String := "/-"\ntheorem bad : True := by sorry\n'
        self.assertRegex(self.strip(code), r"\bsorry\b")

    def test_a_sorry_inside_a_string_is_not_a_finding(self):
        self.assertNotRegex(self.strip('def s := "sorry"\n'), r"\bsorry\b")

    def test_real_block_comments_are_still_stripped(self):
        self.assertNotRegex(self.strip("/- sorry -/\n"), r"\bsorry\b")

    def test_escaped_quotes_do_not_desync_the_parser(self):
        code = 'def s := "a\\"b"\ntheorem t : True := by sorry\n'
        self.assertRegex(self.strip(code), r"\bsorry\b")

    def test_kernel_typecheck_bypass_is_banned(self):
        """debug.skipKernelTC leaves no trace in #print axioms, so nothing else
        in the pipeline could catch it."""
        self.assertIn("debug.skipKernelTC", self.banned)


class AxiomDeclarationDetector(unittest.TestCase):
    """`private axiom` is ordinary Lean; matching only a bare `axiom` at line
    start let every modifier form through."""

    def setUp(self):
        src = _harness_source()
        m = re.search(r'_AXIOM_RE = \(\s*(r"[^\n]*"\s*\n\s*r"[^\n]*"\s*\n\s*r"[^\n]*")\)',
                      src)
        self.assertIsNotNone(m, "could not find _AXIOM_RE in the harness")
        self.rx = re.compile(eval("(" + m.group(1) + ")"))

    def test_detects_every_modifier_form(self):
        for decl in ["axiom foo : True", "private axiom foo : True",
                     "protected axiom foo : True", "noncomputable axiom foo : True",
                     "@[simp] axiom foo : True", "scoped axiom foo : True",
                     "  local axiom foo : True",
                     "@[simp, norm_cast] private axiom foo : True"]:
            with self.subTest(decl):
                self.assertTrue(self.rx.search(decl))

    def test_does_not_fire_on_a_theorem_named_like_an_axiom(self):
        self.assertIsNone(self.rx.search("theorem axiomatic : True := trivial"))


class PreflightExitCodes(unittest.TestCase):
    """Pre-flight failures must be distinguishable from "ran and found N
    problems" — 64, never 1."""

    def _run(self, args, project="Nope", allowed=None):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "scripts"))
        src = re.sub(r"(?m)^PROJECT=.*$", 'PROJECT="%s"' % project, _harness_source(), 1)
        p = os.path.join(d, "scripts", "verify.sh")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(src)
        os.chmod(p, 0o755)
        if allowed is not None:
            # The required-files pre-flight runs first, so give it what it needs.
            os.makedirs(os.path.join(d, project), exist_ok=True)
            for f in ("Defs.lean", "Theorems.lean"):
                with open(os.path.join(d, project, f), "w") as fh:
                    fh.write("-- x\n")
            with open(os.path.join(d, "scripts", "frozen.sha256"), "w") as fh:
                fh.write("# pins\n")
            with open(os.path.join(d, "scripts", "ALLOWED_AXIOMS.txt"), "w") as fh:
                fh.write(allowed)
        return subprocess.run([p] + args, capture_output=True, text=True, cwd=d)

    def test_unknown_option(self):
        self.assertEqual(self._run(["--bogus"]).returncode, EX_PREFLIGHT)

    def test_missing_frozen_files(self):
        self.assertEqual(self._run(["--no-log"]).returncode, EX_PREFLIGHT)

    def test_allowlist_may_not_whitelist_a_proof_hole(self):
        """One bad line in the generated allowlist would otherwise make Check 4
        vacuous."""
        for hole in ("sorryAx", "ofReduceBool"):
            with self.subTest(hole):
                r = self._run(["--no-log"], allowed=hole + "\n")
                self.assertEqual(r.returncode, EX_PREFLIGHT)
                self.assertIn("proof hole", r.stdout + r.stderr)


class FrozenPinCompleteness(unittest.TestCase):
    """A pins file with no pin lines yielded zero loop iterations and a silent
    PASS: Check 1 reported nothing while verifying nothing."""

    def _check1(self, pins):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "P"))
        for f in ("Defs.lean", "Theorems.lean"):
            with open(os.path.join(d, "P", f), "w") as fh:
                fh.write("-- x\n")
        with open(os.path.join(d, "pins"), "w") as fh:
            fh.write(pins)
        src = _harness_source()
        block = src[src.index("# --- Check 1"):src.index("# --- Check 2")]
        script = ("set -euo pipefail\nPROJECT=P\nREPO_ROOT=%s\nERRORS=0\n"
                  "sha256_of() { shasum -a 256 \"$1\" | awk '{print $1}'; }\n"
                  "PINS_FILE=%s/pins\n%s\necho ERRORS=$ERRORS\n" % (d, d, block))
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        return r.stdout

    def test_empty_pins_file_fails_both_frozen_files(self):
        out = self._check1("# only a comment\n")
        self.assertIn("ERRORS=2", out)
        self.assertIn("Defs.lean is NOT pinned", out)
        self.assertIn("Theorems.lean is NOT pinned", out)

    def test_pinning_only_one_file_still_fails(self):
        out = self._check1("deadbeef  P/Defs.lean\n")
        self.assertIn("Theorems.lean is NOT pinned", out)


class HarnessLint(unittest.TestCase):
    """The linter guards the two shapes in which a no-match grep aborts the
    harness under `set -e`. Checking only command substitutions missed three
    live bare pipelines on failure paths."""

    def test_shipped_harness_is_clean(self):
        self.assertEqual(S._lint_verify_sh(HARNESS), [])

    def test_catches_an_unguarded_command_substitution(self):
        old = 'BUILD_ERRORS=$(echo "$BUILD_OUTPUT" | grep -c "^error:" || true)'
        self.assertIn(old, _harness_source())
        src = _harness_source().replace(old, old.replace(" || true", ""), 1)
        self.assertTrue(self._lint_text(src))

    def test_catches_an_unguarded_bare_pipeline(self):
        src = _harness_source().replace(
            "{ printf '%s\\n' \"$BOUT\" | grep -E 'error' | head -5; } || true",
            "printf '%s\\n' \"$BOUT\" | grep -E 'error' | head -5", 1)
        self.assertTrue(self._lint_text(src))

    def _lint_text(self, src):
        fd, p = tempfile.mkstemp(suffix=".sh")
        os.close(fd)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(src)
        return S._lint_verify_sh(p)


if __name__ == "__main__":
    unittest.main()


class RegexAnchoring(unittest.TestCase):
    """Python's `$` also matches before a trailing newline, so a whole-string
    validator must use `\\Z`. A name carrying `\\n` would otherwise validate and
    then be written into the harness's bash array."""

    def test_name_validator_rejects_a_trailing_newline(self):
        self.assertTrue(S._NAME_RE.match("good_name"))
        self.assertIsNone(S._NAME_RE.match("bad\n"))
        self.assertIsNone(S._NAME_RE.match("bad\nmore"))

    def test_harness_params_reject_a_newline_bearing_theorem(self):
        import json
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "scripts"))
        with open(os.path.join(d, "scripts", "harness.json"), "w") as fh:
            json.dump({"project": "P", "problem": "x", "theorems": ["ok\n"]}, fh)
        with self.assertRaises(RuntimeError):
            S._read_harness_params(d)
