#!/usr/bin/env python3
"""Verification harness for a Lean 4 + Mathlib formalization.

This file is IDENTICAL in every generated project. Everything problem-specific
lives in scripts/harness.json, so there is nothing to fill in and nothing to
edit — and `scripts/control_manifest.sha256` pins it, so an edit is detected.

    scripts/verify.py [--no-log] [<theorem_name> | --all]

Exit code = number of reported issues (0 = PASS), or 64 if the harness could not
run at all. Never conflate the two: 64 means NOTHING was verified.

The checks, in order:

  1   Frozen SHA pins      Defs.lean / Theorems.lean match scripts/frozen.sha256,
                           and BOTH are actually named there.
  2   Banned keywords      No sorry / sorryAx / native_decide / admit / unsafe /
                           implemented_by / ofReduceBool / debug.skipKernelTC in
                           any first-party .lean. Comment- AND string-aware.
                           `sorry` is allowed only in Theorems.lean. An `axiom`
                           is allowed only if allowlisted.
  3   Clean build          `lake build` succeeds with no errors, and no warnings
                           beyond the expected sorry warnings from Theorems.lean.
  4   Axiom allowlist      Every <PROJECT>.Solution.<name> depends only on the
                           three standard axioms plus allowlisted names.
  4b  Mandatory axioms     If harness.json sets mandatory_axioms, the headline
                           theorem must GENUINELY DEPEND on each. Check 4 only
                           bounds the axiom set from above, so without this a
                           proof that skipped a required certificate passes.
  5   Modules compile      Discharge.lean and Solution.lean build.
  5b  Frozen <-> Solution  A gate `@P.<t> = @P.Solution.<t> := rfl` is GENERATED
                           here for every frozen theorem and compiled. Check 5
                           alone only proves whatever gates Discharge.lean
                           happens to contain — an empty one compiles.

Two invariants worth keeping in mind when editing:

  * A check that finds nothing to parse must FAIL, never pass. Most historical
    bugs in this harness were a parse that came back empty and was read as
    "nothing wrong" rather than "I could not look".
  * A check must be able to REPORT what it finds. Anything that aborts the run
    on detection turns a violation into silence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

# Exit code for a pre-flight failure — bad usage, missing file, poisoned
# allowlist. Deliberately outside the range of "number of issues" so a caller can
# tell "could not run" from "ran and found N problems".
EX_PREFLIGHT = 64

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The three axioms every Lean/Mathlib proof may use.
STD_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})

# Proof holes that can NEVER be allowlisted, whatever ALLOWED_AXIOMS.txt says.
# That file is generated, so it must not be able to authorise the very things
# this harness exists to detect: it is for assumed MATHEMATICAL certificates.
DENIED_AXIOMS = frozenset({
    "sorryAx", "ofReduceBool", "ofReduceNat",
    "Lean.ofReduceBool", "Lean.ofReduceNat",
})

BANNED_KEYWORDS = (
    "sorry", "sorryAx", "native_decide", "admit", "unsafe",
    "implemented_by", "ofReduceBool",
    # Disables kernel re-typechecking: a proof accepted only by the elaborator,
    # leaving NO trace in `#print axioms`. Nothing else here would catch it.
    "debug.skipKernelTC",
)

# `private axiom foo` is ordinary Lean, so modifiers and attributes must be
# allowed before the keyword or every one of them slips past.
AXIOM_DECL = re.compile(
    r"""(?m)^[ \t]*
        (?:@\[[^\]]*\][ \t]*)*                                   # @[simp], ...
        (?:(?:private|protected|noncomputable|unsafe|partial|scoped|local)[ \t]+)*
        axiom[ \t]+([A-Za-z_][\w'.]*)""",
    re.VERBOSE,
)

# Lean prints `'Name' depends on axioms: [a, b, c]`, WRAPPING the bracketed list
# across lines once it is long — which is exactly what happens for declarations
# carrying a custom axiom. Whitespace is normalised before this runs, so the
# pattern never has to care where the line breaks fell.
AXIOM_REPORT = re.compile(r"'([\w.]+)' depends on axioms:?\s*\[([^\]]*)\]")
NO_AXIOMS = re.compile(r"'([\w.]+)' does not depend on any axioms")


class Harness:
    """Problem-specific configuration, read from scripts/harness.json."""

    def __init__(self, repo_root: str):
        self.repo_root = repo_root
        path = os.path.join(repo_root, "scripts", "harness.json")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            die(f"cannot read scripts/harness.json: {exc}")

        self.project: str = data["project"]
        self.theorems: list[str] = list(data["theorems"])
        self.final_theorem: str = data.get("final_theorem") or ""
        self.mandatory_axioms: list[str] = list(data.get("mandatory_axioms") or [])

        self.src_dir = os.path.join(repo_root, self.project)
        self.defs = os.path.join(self.src_dir, "Defs.lean")
        self.theorems_file = os.path.join(self.src_dir, "Theorems.lean")
        self.pins_file = os.path.join(repo_root, "scripts", "frozen.sha256")
        self.allowed_axioms = self._read_allowlist()

    def _read_allowlist(self) -> set[str]:
        """Axiom names the user permitted, from scripts/ALLOWED_AXIOMS.txt."""
        path = os.path.join(self.repo_root, "scripts", "ALLOWED_AXIOMS.txt")
        names: set[str] = set()
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return names            # absent means the strict default
        for line in text.splitlines():
            for name in line.split("#", 1)[0].replace(",", " ").split():
                if name in DENIED_AXIOMS:
                    die(f"{path} tries to whitelist `{name}`, which is a proof "
                        "hole, not an assumed certificate. Refusing to run.")
                names.add(name)
        return names

    def qualified(self, theorem: str) -> str:
        return f"{self.project}.Solution.{theorem}"


def die(message: str) -> None:
    """Pre-flight failure: nothing was verified."""
    print(f"ERROR: {message}")
    raise SystemExit(EX_PREFLIGHT)


def sha256_of(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def run_lake(repo_root: str, *args: str) -> tuple[int, str]:
    """Run `lake ...` in the project, returning (exit code, combined output).

    Never raises on a non-zero exit: a failing build is a finding to report, not
    an error to propagate.
    """
    env = dict(os.environ)
    elan_bin = os.path.expanduser("~/.elan/bin")
    if os.path.isdir(elan_bin):
        env["PATH"] = elan_bin + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(["lake", *args], cwd=repo_root, env=env,
                              capture_output=True, text=True)
    except OSError as exc:
        return 127, f"could not run lake: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def error_lines(output: str) -> list[str]:
    """Diagnostic lines from a lake/lean run.

    Matches `error:` anywhere in the line, not just at the start: lake reports
    some diagnostics bare (`error: ...`) and others prefixed with a location
    (`./Foo.lean:3:0: error: ...`), and anchoring to the start missed the second
    kind entirely.
    """
    return [ln for ln in output.splitlines() if "error:" in ln]


def strip_comments_and_strings(source: str) -> str:
    """Lean source with comments removed and string literals blanked.

    String literals are dropped whole rather than scanned: a `sorry` inside a
    string is not a proof hole, and — the reason this needs string state at all —
    `def m : String := "/-"` would otherwise open a block comment that swallows
    every following line, hiding a real `sorry` from the scan.
    """
    out: list[str] = []
    i, n, depth, in_string = 0, len(source), 0, False
    while i < n:
        ch, pair = source[i], source[i:i + 2]
        if in_string:
            if ch == "\\":                      # escape: skip both characters
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
        elif depth == 0 and ch == '"':
            in_string = True
            i += 1
        elif depth == 0 and pair == "--":
            newline = source.find("\n", i)
            if newline == -1:
                break
            i = newline
        elif pair == "/-":
            depth += 1
            i += 2
        elif depth > 0 and pair == "-/":
            depth -= 1
            i += 2
        elif depth > 0:
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def lean_sources(harness: Harness) -> list[str]:
    """Every first-party .lean file: the project tree plus the root import."""
    found = []
    for dirpath, _dirnames, filenames in os.walk(harness.src_dir):
        found.extend(os.path.join(dirpath, f)
                     for f in filenames if f.endswith(".lean"))
    root = os.path.join(harness.repo_root, f"{harness.project}.lean")
    if os.path.isfile(root):
        found.append(root)
    return sorted(found)


# --------------------------------------------------------------------------- #
# Checks
#
# Each returns a list of failure lines: empty means the check passed. None of
# them raise on a finding — a check that cannot report what it found is worse
# than no check.
# --------------------------------------------------------------------------- #

def check_frozen_pins(harness: Harness, say) -> list[str]:
    """1. Defs.lean and Theorems.lean match their recorded hashes."""
    try:
        with open(harness.pins_file, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        return [f"cannot read scripts/frozen.sha256: {exc}"]

    failures, pinned = [], {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            failures.append(f"frozen.sha256: unparseable line: {line!r}")
            continue
        digest, relpath = parts[0], parts[1].strip()
        pinned[relpath] = digest
        actual = sha256_of(os.path.join(harness.repo_root, relpath))
        if actual == digest:
            say(f"PASS: {relpath} pin matches")
        else:
            failures.append(f"{relpath} SHA pin mismatch"
                            f" (pinned {digest[:12]}, actual "
                            f"{(actual or 'missing file')[:12]})")

    # A pins file with no pin lines would otherwise report nothing and pass,
    # having verified nothing. Both frozen files must be named explicitly:
    # pinning one and omitting the other left the other editable all run.
    for required in (f"{harness.project}/Defs.lean",
                     f"{harness.project}/Theorems.lean"):
        if required not in pinned:
            failures.append(f"{required} is NOT pinned in scripts/frozen.sha256 "
                            "— it could be edited without detection")
    return failures


def check_banned_keywords(harness: Harness, say) -> list[str]:
    """2. No banned keyword or un-allowlisted axiom in any first-party source."""
    failures = []
    allowed_short = {name.split(".")[-1] for name in harness.allowed_axioms}

    for path in lean_sources(harness):
        try:
            with open(path, encoding="utf-8") as fh:
                code = strip_comments_and_strings(fh.read())
        except OSError as exc:
            failures.append(f"{path}: cannot read: {exc}")
            continue
        shown = os.path.relpath(path, harness.repo_root)
        is_theorems = os.path.abspath(path) == os.path.abspath(harness.theorems_file)

        for match in AXIOM_DECL.finditer(code):
            name = match.group(1)
            if name.split(".")[-1] not in allowed_short:
                failures.append(f"{shown}: non-whitelisted `axiom {name}`")

        for keyword in BANNED_KEYWORDS:
            if keyword == "sorry" and is_theorems:
                continue            # the frozen stubs are allowed to be `sorry`
            if re.search(r"\b" + re.escape(keyword) + r"\b", code):
                failures.append(f"{shown}: contains banned `{keyword}`")

    if not failures:
        say("PASS: no banned keywords (sorry allowed only in Theorems.lean)")
    return failures


def check_build(harness: Harness, say) -> list[str]:
    """3. `lake build` is clean apart from Theorems.lean's expected sorries."""
    code, output = run_lake(harness.repo_root, "build")
    errors = error_lines(output)

    # Only Theorems.lean may emit a sorry warning. Filtering the message
    # everywhere once made a `sorry` in Proofs/** invisible here too.
    warnings = [ln for ln in output.splitlines()
                if "warning:" in ln
                and not ("Theorems.lean" in ln and "declaration uses" in ln
                         and "sorry" in ln)]

    if code == 0 and not errors and not warnings:
        say("PASS: build clean (only expected Theorems.lean sorry warnings)")
        return []
    failures = [f"build exit={code}, errors={len(errors)}, "
                f"unexpected warnings={len(warnings)}"]
    failures.extend(ln for ln in (errors + warnings)[:20])
    return failures


def _axiom_reports(harness: Harness, names: list[str]) -> tuple[dict, str]:
    """Map declaration -> set of axioms, by asking Lean about `names`.

    Returns ({} , error text) when Lean could not be run at all, which the
    caller must treat as a failure rather than as "no axioms found".
    """
    with tempfile.TemporaryDirectory() as tmp:
        probe = os.path.join(tmp, "axioms.lean")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write(f"import {harness.project}\n")
            for name in names:
                fh.write(f"#print axioms {name}\n")
        _code, output = run_lake(harness.repo_root, "env", "lean", probe)

    # Normalise whitespace first: Lean wraps a long axiom list across lines, and
    # a line-oriented parse would find nothing on exactly the declarations that
    # carry the most axioms — reporting them as clean.
    flat = " ".join(output.split())
    reports: dict[str, set[str]] = {}
    for name, names_blob in AXIOM_REPORT.findall(flat):
        reports[name] = {a.strip() for a in names_blob.split(",") if a.strip()}
    for name in NO_AXIOMS.findall(flat):
        reports.setdefault(name, set())
    return reports, output


def check_axioms(harness: Harness, targets: list[str], say) -> list[str]:
    """4 + 4b. Allowlist compliance, and mandatory-axiom dependence."""
    wanted = [harness.qualified(t) for t in targets]
    # 4b inspects the headline theorem, so ask about it even when the caller
    # named a single OTHER theorem — otherwise single-theorem mode could never
    # pass, because the headline's record would simply be absent.
    if harness.mandatory_axioms:
        head = harness.qualified(harness.final_theorem)
        if head not in wanted:
            wanted.append(head)

    reports, raw = _axiom_reports(harness, wanted)
    if not reports:
        return ["no axiom output at all — `lake env lean` failed:\n"
                + "\n".join(raw.splitlines()[:10])]

    permitted = STD_AXIOMS | harness.allowed_axioms
    failures = []
    for theorem in targets:
        qualified = harness.qualified(theorem)
        if qualified not in reports:
            failures.append(f"{theorem} — no axiom output (build/name error)")
            continue
        axioms = reports[qualified]
        disallowed = sorted(axioms - permitted)
        if disallowed:
            failures.append(f"{theorem} — non-whitelisted axiom(s): "
                            + ", ".join(disallowed))
        else:
            # Print the PARSED axioms, not the allowlist: an auditor must be able
            # to see the check really read something rather than finding nothing.
            say(f"PASS: {theorem} — axioms within allowlist: "
                f"[{', '.join(sorted(axioms))}]")

    if harness.mandatory_axioms:
        head = harness.qualified(harness.final_theorem)
        found = reports.get(head, set())
        missing = [a for a in harness.mandatory_axioms if a not in found]
        if missing:
            failures.append(
                f"{harness.final_theorem} — missing MANDATORY axiom "
                f"dependency: {', '.join(missing)}. harness.json requires the "
                "headline theorem to genuinely depend on every assumed "
                f"certificate; it depends on: {', '.join(sorted(found)) or 'none'}")
        else:
            say(f"PASS: {harness.final_theorem} — depends on all mandatory "
                f"axioms {{{' '.join(harness.mandatory_axioms)}}}")
    return failures


def check_statement_gates(harness: Harness, targets: list[str], say) -> list[str]:
    """5 + 5b. The modules compile, and every frozen statement is bound to the
    declaration Check 4 audited."""
    failures = []
    for module in (f"{harness.project}.Discharge", f"{harness.project}.Solution"):
        code, output = run_lake(harness.repo_root, "build", module)
        errors = error_lines(output)
        if code == 0 and not errors:
            say(f"PASS: {module} compiles")
        else:
            failures.append(f"{module} did not compile: " + "; ".join(errors[:3]))

    # Compiling Discharge.lean only proves whatever gates it happens to contain:
    # an empty one compiles, and projects commonly gate `<t>_proof` rather than
    # the `Solution.<t>` that Check 4 audits. So generate the gates here — each
    # type-checks only if the audited declaration has EXACTLY the frozen type.
    with tempfile.TemporaryDirectory() as tmp:
        gates = os.path.join(tmp, "gates.lean")
        with open(gates, "w", encoding="utf-8") as fh:
            fh.write(f"import {harness.project}\n")
            for theorem in targets:
                fh.write(f"example : @{harness.project}.{theorem} = "
                         f"@{harness.qualified(theorem)} := rfl\n")
        code, output = run_lake(harness.repo_root, "env", "lean", gates)

    if code == 0 and not output.strip():
        say(f"PASS: all {len(targets)} frozen statement(s) bind to "
            f"{harness.project}.Solution.*")
    else:
        # Line N+1 of the generated file is targets[N-1] (line 1 is the import),
        # so a reported line number names the theorem whose binding failed.
        blamed = set()
        for line_no in re.findall(r"gates\.lean:(\d+):", output):
            index = int(line_no) - 2
            if 0 <= index < len(targets):
                blamed.add(targets[index])
        for theorem in sorted(blamed):
            failures.append(f"{theorem} — {harness.qualified(theorem)} does not "
                            "have the frozen statement's type")
        if not blamed:
            failures.append("generated statement gates did not compile:\n"
                            + "\n".join(output.splitlines()[:5]))
    return failures


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify.py",
        description="Verify a Lean 4 formalization against its frozen statements.")
    parser.add_argument("theorem", nargs="?", default="--all",
                        help="restrict the axiom check to one frozen theorem "
                             "(default: all of them)")
    parser.add_argument("--no-log", action="store_true",
                        help="do not append a line to logs/verify_log.jsonl")
    args = parser.parse_args(argv)

    harness = Harness(REPO_ROOT)

    if args.theorem == "--all":
        targets = list(harness.theorems)
    elif args.theorem in harness.theorems:
        targets = [args.theorem]
    else:
        die(f"unknown theorem {args.theorem!r}. "
            f"Known: {' '.join(harness.theorems)}")

    for required in (harness.defs, harness.theorems_file, harness.pins_file):
        if not os.path.isfile(required):
            die(f"required file not found: {required}")

    started = time.time()
    print(f"=== Verifying {harness.project} formalization ===")
    print(f"  Target: {args.theorem}")
    if harness.allowed_axioms:
        print(f"  Whitelisted axioms: {' '.join(sorted(harness.allowed_axioms))}")
    print()

    checks = [
        ("Check 1: Frozen SHA pins", lambda say: check_frozen_pins(harness, say)),
        ("Check 2: Banned keywords", lambda say: check_banned_keywords(harness, say)),
        ("Check 3: lake build", lambda say: check_build(harness, say)),
        ("Check 4: #print axioms", lambda say: check_axioms(harness, targets, say)),
        ("Check 5: Statement gates", lambda say: check_statement_gates(harness, targets, say)),
    ]

    issues = 0
    for title, run_check in checks:
        print(f"--- {title} ---")
        failures = run_check(print)
        for failure in failures:
            print(f"FAIL: {failure}")
        # One issue per failing CHECK, not per failing line, so the exit code
        # stays a small count of problem areas.
        issues += 1 if failures else 0
        print()

    duration = int(time.time() - started)
    verdict = "PASS" if issues == 0 else "FAIL"
    print(f"=== RESULT: {verdict} ({issues} issue(s), {duration}s) ===")

    if not args.no_log:
        log_dir = os.path.join(harness.repo_root, "logs")
        os.makedirs(log_dir, exist_ok=True)
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "target": args.theorem, "issues": issues,
            "duration_sec": duration, "success": issues == 0,
        }
        with open(os.path.join(log_dir, "verify_log.jsonl"), "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    return issues


if __name__ == "__main__":
    sys.exit(main())
