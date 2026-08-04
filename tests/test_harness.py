"""The verification harness, reference/scripts/verify.py.

Every check is an ordinary Python function, so each is tested directly rather
than by running the whole harness against a Lean toolchain.

What these pin above all is the invariant that broke repeatedly in the shell
version this replaced: a check that finds nothing to parse must FAIL, never pass.
"""
import importlib.util
import json
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS_PATH = os.path.join(REPO, "reference", "scripts", "verify.py")

_spec = importlib.util.spec_from_file_location("verify_harness", HARNESS_PATH)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

DEFAULT_SOURCES = {"Defs.lean": "-- defs\n",
                   "Theorems.lean": "theorem t_one : True := sorry\n"}


def _write(path, body):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def _project(theorems=("t_one",), allowed="", pins=None, sources=None,
             extra_config=None):
    """A minimal project tree the harness can be pointed at."""
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "scripts"))
    os.makedirs(os.path.join(root, "P"))
    config = {"project": "P", "theorems": list(theorems)}
    config.update(extra_config or {})
    _write(os.path.join(root, "scripts", "harness.json"), json.dumps(config))
    _write(os.path.join(root, "scripts", "ALLOWED_AXIOMS.txt"), allowed)
    for name, body in (sources or DEFAULT_SOURCES).items():
        _write(os.path.join(root, "P", name), body)
    if pins is None:
        pins = "".join(
            "%s  P/%s\n" % (verify.sha256_of(os.path.join(root, "P", f)), f)
            for f in ("Defs.lean", "Theorems.lean"))
    _write(os.path.join(root, "scripts", "frozen.sha256"), pins)
    return root


def _silent(*_args, **_kwargs):
    """Swallow the PASS lines a check prints."""


class CommentAndStringScanner(unittest.TestCase):
    def test_a_string_literal_cannot_hide_a_sorry(self):
        code = 'def m : String := "/-"\ntheorem bad : True := by sorry\n'
        self.assertIn("sorry", verify.strip_comments_and_strings(code))

    def test_a_sorry_inside_a_string_is_not_a_finding(self):
        self.assertNotIn("sorry",
                         verify.strip_comments_and_strings('def s := "sorry"\n'))

    def test_real_block_comments_are_stripped(self):
        self.assertNotIn("sorry", verify.strip_comments_and_strings("/- sorry -/\n"))

    def test_escaped_quotes_do_not_desync_the_parser(self):
        code = 'def s := "a\\"b"\ntheorem t : True := by sorry\n'
        self.assertIn("sorry", verify.strip_comments_and_strings(code))


class AxiomDeclarations(unittest.TestCase):
    def test_every_modifier_form_is_detected(self):
        for decl in ["axiom foo : True", "private axiom foo : True",
                     "protected axiom foo : True", "noncomputable axiom foo : True",
                     "@[simp] axiom foo : True", "scoped axiom foo : True",
                     "  local axiom foo : True",
                     "@[simp, norm_cast] private axiom foo : True"]:
            with self.subTest(decl):
                self.assertTrue(verify.AXIOM_DECL.search(decl))

    def test_a_theorem_named_like_an_axiom_is_not_a_finding(self):
        self.assertIsNone(
            verify.AXIOM_DECL.search("theorem axiomatic : True := trivial"))


class AxiomReportParsing(unittest.TestCase):
    """`#print axioms` wraps long lists across lines. A line-oriented parse found
    nothing on exactly the declarations carrying the most axioms — and reported
    them as clean."""

    def test_parses_a_wrapped_list(self):
        raw = ("'P.Solution.t' depends on axioms: [propext,\n"
               " Classical.choice,\n P.cert,\n sorryAx]")
        name, blob = verify.AXIOM_REPORT.findall(" ".join(raw.split()))[0]
        found = {a.strip() for a in blob.split(",")}
        self.assertEqual(name, "P.Solution.t")
        self.assertEqual(found, {"propext", "Classical.choice", "P.cert", "sorryAx"})

    def test_recognises_the_no_axioms_phrasing(self):
        self.assertEqual(
            verify.NO_AXIOMS.findall("'P.Solution.t' does not depend on any axioms"),
            ["P.Solution.t"])


class FrozenPins(unittest.TestCase):
    def test_matching_pins_pass(self):
        self.assertEqual(
            verify.check_frozen_pins(verify.Harness(_project()), _silent), [])

    def test_edited_frozen_file_is_caught(self):
        root = _project()
        with open(os.path.join(root, "P", "Theorems.lean"), "a") as fh:
            fh.write("-- edited\n")
        failures = verify.check_frozen_pins(verify.Harness(root), _silent)
        self.assertTrue(any("pin mismatch" in f for f in failures), failures)

    def test_a_pins_file_with_no_pins_fails_rather_than_passing_silently(self):
        failures = verify.check_frozen_pins(
            verify.Harness(_project(pins="# only a comment\n")), _silent)
        self.assertEqual(len(failures), 2, failures)
        self.assertTrue(all("NOT pinned" in f for f in failures))

    def test_pinning_only_one_frozen_file_fails(self):
        root = _project()
        digest = verify.sha256_of(os.path.join(root, "P", "Defs.lean"))
        _write(os.path.join(root, "scripts", "frozen.sha256"),
               "%s  P/Defs.lean\n" % digest)
        failures = verify.check_frozen_pins(verify.Harness(root), _silent)
        self.assertTrue(any("Theorems.lean is NOT pinned" in f for f in failures))


class BannedKeywords(unittest.TestCase):
    def test_clean_project_passes(self):
        self.assertEqual(
            verify.check_banned_keywords(verify.Harness(_project()), _silent), [])

    def test_sorry_is_allowed_only_in_theorems(self):
        root = _project(sources=dict(DEFAULT_SOURCES,
                                     **{"Proofs.lean": "theorem h : True := by sorry\n"}))
        failures = verify.check_banned_keywords(verify.Harness(root), _silent)
        self.assertTrue(any("Proofs.lean" in f and "sorry" in f for f in failures))

    def test_kernel_typecheck_bypass_is_banned(self):
        root = _project(sources=dict(
            DEFAULT_SOURCES,
            **{"Defs.lean": "set_option debug.skipKernelTC true in\ndef d := 1\n"}))
        failures = verify.check_banned_keywords(verify.Harness(root), _silent)
        self.assertTrue(any("skipKernelTC" in f for f in failures))

    def test_unlisted_axiom_is_caught_but_a_listed_one_is_not(self):
        src = dict(DEFAULT_SOURCES, **{"Defs.lean": "private axiom cert : True\n"})
        self.assertTrue(verify.check_banned_keywords(
            verify.Harness(_project(sources=src)), _silent))
        self.assertEqual(verify.check_banned_keywords(
            verify.Harness(_project(sources=src, allowed="P.cert\n")), _silent), [])


class AllowlistPoisoning(unittest.TestCase):
    def test_a_proof_hole_may_never_be_allowlisted(self):
        for hole in ("sorryAx", "ofReduceBool"):
            with self.subTest(hole):
                with self.assertRaises(SystemExit) as caught:
                    verify.Harness(_project(allowed=hole + "\n"))
                self.assertEqual(caught.exception.code, verify.EX_PREFLIGHT)


class ErrorLineDetection(unittest.TestCase):
    """lake reports some diagnostics bare and others location-prefixed; anchoring
    to the start of the line missed the second kind entirely."""

    def test_matches_both_shapes(self):
        out = "error: boom\n./P/Foo.lean:3:0: error: type mismatch\nfine\n"
        self.assertEqual(len(verify.error_lines(out)), 2)

    def test_clean_output_has_none(self):
        self.assertEqual(verify.error_lines("Build completed successfully.\n"), [])


class HarnessConfig(unittest.TestCase):
    def test_reads_check_4b_configuration(self):
        root = _project(extra_config={"final_theorem": "t_one",
                                      "mandatory_axioms": ["P.cert"]})
        h = verify.Harness(root)
        self.assertEqual(h.final_theorem, "t_one")
        self.assertEqual(h.mandatory_axioms, ["P.cert"])

    def test_absent_check_4b_configuration_disables_it(self):
        self.assertEqual(verify.Harness(_project()).mandatory_axioms, [])

    def test_qualifies_names_against_the_project_namespace(self):
        self.assertEqual(verify.Harness(_project()).qualified("t"), "P.Solution.t")


if __name__ == "__main__":
    unittest.main()
