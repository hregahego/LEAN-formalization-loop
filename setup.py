#!/usr/bin/env python3
"""
setup.py — scaffold the orchestration files for an autonomous Lean 4
formalization, from a natural-language proof sketch.

Usage:
    python3 setup.py [TARGET_DIR]        (default: current directory)

Preconditions:
    TARGET_DIR contains SKETCH.md (problem statement + NL proof sketch).

What it does:
    Runs a single configured "architect" agent that reads SKETCH.md and the
    reference project set in config.json (default: the bundled ./reference)
    and writes, into TARGET_DIR, following the reference *format* exactly but with
    content derived for THIS problem:

        BLUEPRINT.md          (Part -1 SETUP / file layout / frozen Defs &
                               Theorems / stages with cheat-watches / order)
        scripts/harness.json  (project namespace, problem title, theorem names)
        scripts/frozen.sha256 (placeholder; init.py records the real pins)

    setup.py itself then renders, from the reference:

        scripts/verify.sh     (the 7-check verification harness)
        PROGRESS.md           (append-only log header)
        TASKS.md              (append-only header; 4-agent delegation)
        REVIEW.md             (append-only header; audit log)

    Only the values in harness.json vary between problems. The harness and the
    append-only rules are deliberately NOT model-authored: the harness certifies
    every result, and the log rules are pipeline policy, so both stay byte-identical
    across runs. BLUEPRINT.md stays model-authored — it IS the problem-specific work.

    It also writes USER_NOTES.md directly (from the reference template) — the
    user-editable file for special instructions, in particular any
    assumed-certificate axioms to permit. An existing USER_NOTES.md is never
    overwritten.

    Fill it in BEFORE running setup.py if the problem needs assumed axioms or a
    mandated proof route: the architect reads it, and only it can record the
    Check 4b parameters (final_theorem / mandatory_axioms) into harness.json.
    Seeding it later — before init.py — still permits the axioms, but Check 4b
    (which enforces that a permitted certificate is actually USED) stays off.

Next step after this:  python3 init.py TARGET_DIR
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

import formlib as F


# Fallback USER_NOTES.md body, used only if the bundled reference template is
# missing its USER_NOTES.md.
_DEFAULT_USER_NOTES = """\
# USER_NOTES — special instructions for this formalization

Created by setup.py; read by init.py and loop.py. Put problem-specific guidance
here BEFORE running init.py.

By default the pipeline is maximally strict: solved theorems may depend only on
{propext, Classical.choice, Quot.sound}; no custom `axiom`s; and no frozen
theorem may carry an added hypothesis (this last rule is never relaxed).

## Allowed axioms (assumed certificates)

If a fact is mathematically routine but prohibitively expensive to PROVE in Lean
(a large factorization, an explicit interpolant, a numeric certificate), you may
assume it as a Lean `axiom` — never as a hypothesis on a frozen theorem. For each
one, describe what it asserts, why it is assumed, and which theorem uses it.
init.py declares the axiom(s) in Defs.lean and records their names in
scripts/ALLOWED_AXIOMS.txt; verify.sh then permits exactly those and bans all
other axioms.

None — no assumed axioms.
"""


PROMPT = """\
You are the ARCHITECT agent for an autonomous Lean 4 + Mathlib formalization
pipeline. Your working directory contains SKETCH.md — a math problem statement
plus a natural-language proof sketch. Your job is to SCAFFOLD the orchestration
files that a team of 4 parallel worker agents will later use to formalize this
sketch in Lean 4. You do NOT write any Lean proofs now.

A REFERENCE project that already scaffolded a DIFFERENT problem lives at:
    {ref}
Read its files as strict FORMAT TEMPLATES. You must COPY THEIR STRUCTURE AND
FORMAT EXACTLY (same sections, same headings), but write CONTENT for the problem
in THIS directory's SKETCH.md. Do NOT copy the prob4b mathematics — design the
correct Lean decomposition for the CURRENT sketch.

BLUEPRINT.md is the one substantial file you author: it IS the problem-specific
work. The verification harness and the append-only log headers are rendered by
setup.py from the reference and are identical in every run.

== Do this, in order ==

1. Read ./SKETCH.md carefully. Understand the exact theorem and every step of
   the proof sketch. This is the mathematical source of truth. Then read
   ./USER_NOTES.md — the user's standing instructions for THIS problem. If it
   permits assumed-certificate axioms, or requires a particular proof route, that
   constrains the decomposition you design in step 3 and the harness parameters
   you record in step 5. If it still reads "None — no assumed axioms", the
   default strict policy applies and there is nothing extra to honour.

2. Read the reference BLUEPRINT as your FORMAT TEMPLATE:
     {ref}/BLUEPRINT.md
   You do not need to read the reference verify.sh or log headers — you are not
   writing those.

3. Write ./BLUEPRINT.md with the SAME section structure as the reference:
   - Title + a "headline target" paragraph naming the frozen headline theorem.
   - "## Part -1 -- Setting up the repository (the SETUP stage)" containing the
     concrete **Step -1** instructions that init.py will execute:
       * the exact `lake` commands to create a Lean4+Mathlib project (pin a
         lean-toolchain + matching Mathlib rev, `lake exe cache get`, build the
         bare skeleton);
       * the FULL file-tree layout under a project namespace, with
         Defs.lean (FROZEN), Theorems.lean (FROZEN, every proof `:= sorry`),
         Proofs/<Stage*>/ subdirectories, Discharge.lean, Solution.lean, the
         root <Project>.lean import file, SKETCH.md/BLUEPRINT.md/PROGRESS.md,
         and scripts/verify.sh + scripts/frozen.sha256;
       * "Freeze the Definitions (Defs.lean)" — every def the proof needs, each
         with an explicit, recorded MODELING DECISION (so no later agent
         re-derives or silently changes it);
       * "Freeze the Theorems (Theorems.lean)" — the COMPLETE list of frozen
         theorem statements as `:= sorry`, each faithfully + minimally rendering
         a claim of the sketch, plus the headline existential. Give every frozen
         theorem a stable name; these names are BINDING (verify.sh and init.py
         depend on them).
       * the re-build gate ("after freezing, `lake build` must succeed; record
         SHA-256 pins") ;
       * the PROGRESS.md append-only rules (§4) reproduced in the SAME format as
         the reference (the `## <UTC> -- <stage>` / Agent / Status / Check /
         Note / Next entry schema, the inviolable append-only rule, "never fake
         a ✅");
       * the agent onboarding & parallel-execution protocol (§5).
   - "## Part 0 -- What Mathlib already gives you" (a reuse table for THIS
     problem's objects).
   - "## Part 1 -- New objects to define".
   - "## Part 2 -- Theorems and lemmas to prove (in order)", broken into Stages
     mapped to Proofs/<Stage>/ directories, and — CRITICAL — each stage ending
     with a "**Cheat watch (Stage X)**" box that names the specific
     trivializations/weakenings to avoid for THIS problem.
   - "## Suggested formalization order" (a dependency diagram).
   - "## Notes, risks, and cheats to watch out for".

4. Do NOT write ./PROGRESS.md, ./TASKS.md or ./REVIEW.md. setup.py copies those
   three append-only log headers from the reference itself — their wording is
   fixed pipeline policy, identical in every run, and not yours to restate.

5. Write ./scripts/harness.json — the ONLY harness artifact you produce. Exactly:
     {"project": "<source-dir / root namespace name>",
      "problem": "<short human-readable problem title, one line>",
      "theorems": ["<frozen name 1>", "<frozen name 2>", ...],
      "final_theorem": "<headline theorem name, or omit>",
      "mandatory_axioms": ["<Project>.<axiom>", ...]}
   Include `final_theorem` + `mandatory_axioms` ONLY when USER_NOTES.md requires
   an assumed certificate to actually be USED (e.g. it forbids an alternative
   route that would not need it). Check 4 alone rejects only EXTRA axioms, so a
   proof that quietly took the cheaper route would otherwise pass. Omit both
   (the normal case) and the check is skipped.
   `problem` titles the generated logs (e.g. "Problem 20 (θ_n : Int(D)^⊗n →
   Int(D^n))"); keep it to one line.
   `theorems` must list EVERY frozen theorem name from step 3, in the order the
   stages of BLUEPRINT.md prove them, and must match those names byte for byte.
   Plain identifiers only — no namespace prefix, no `)` or `"` characters.

   Do NOT write ./scripts/verify.sh. setup.py renders the verification harness
   itself from the reference, substituting only the values above. Its checks
     (1) frozen SHA pins for Defs.lean + Theorems.lean;
     (2) banned keywords (sorry / sorryAx / native_decide / admit / unsafe /
         implemented_by / ofReduceBool / `axiom` decl), comment-aware, with
         `sorry` allowed ONLY in Theorems.lean;
     (3) clean `lake build`;
     (4) `#print axioms` for each Solution.<name> within the allowlist;
     (5) statement gates: Discharge.lean + Solution.lean compile
   are FIXED LOGIC, identical across every problem. They are not yours to adapt,
   weaken, re-derive, or reason about. The harness is what certifies the final
   result, so it is never model-authored.

6. Write ./scripts/frozen.sha256 — a single placeholder comment line, e.g.
   "# pins recorded by init.py after Defs.lean/Theorems.lean are frozen".

== Cheat-prevention you MUST bake into BLUEPRINT.md (adapt to the problem) ==
- Defs.lean + Theorems.lean are FROZEN and byte-pinned by SHA; never edited
  during proving; `sorry` is allowed ONLY in Theorems.lean.
- Frozen theorem statements must render the sketch's claims FAITHFULLY and
  MINIMALLY with NO weakening: no added hypotheses, no specializing a `∀` to
  finitely many examples, no replacing an equality with a one-sided inclusion,
  no swapping genuine finite-generation / textbook definitions for convenient
  surrogates, no proving a special case and claiming the general one.
- Banned tactics/keywords as in verify.sh check (2). `#print axioms` of every
  solved theorem must stay within {propext, Classical.choice, Quot.sound} — PLUS
  any assumed-certificate axioms the user permits in USER_NOTES.md (init.py
  records their names in scripts/ALLOWED_AXIOMS.txt; verify.sh enforces the
  allowlist). Certificates may be assumed ONLY as `axiom`s, NEVER as a hypothesis
  on a frozen theorem. Do NOT edit or overwrite USER_NOTES.md; the user owns it.
- PROGRESS.md is append-only; never fake a ✅ (only mark proved what compiles
  with a clean `#print axioms`).

Work autonomously and decisively — make and RECORD modeling choices; do NOT ask
questions. Use your file tools to write all the files above. When finished,
print a short summary listing (a) the project source-dir / namespace name and
(b) the exact frozen theorem names you chose.
"""


# A frozen theorem name as it may appear in the bash array and in formlib's
# ALL_THEOREMS regex: no quotes, no `)`, no whitespace.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.']*$")


def _read_harness_params(target: str) -> tuple[str, str, list[str]]:
    """The project namespace, problem title, and frozen theorem names.

    Validated strictly: project/theorems are substituted into shell source, and
    formlib re-parses them back out of verify.sh, so a stray quote or paren would
    corrupt both the harness and the progress signal.
    """
    path = os.path.join(target, "scripts", "harness.json")
    try:
        data = json.loads(F.read_text(path))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"scripts/harness.json unreadable: {exc}") from exc

    project = data.get("project")
    if not isinstance(project, str) or not _NAME_RE.match(project):
        raise RuntimeError(f"harness.json: bad 'project' name: {project!r}")

    # Free-form prose (it names the problem for a human), so only sanity-checked:
    # single line, non-empty. Falls back to the namespace if the agent omits it.
    problem = data.get("problem") or project
    if not isinstance(problem, str) or "\n" in problem or not problem.strip():
        raise RuntimeError(f"harness.json: bad 'problem' title: {problem!r}")
    problem = problem.strip()

    theorems = data.get("theorems")
    if not isinstance(theorems, list) or not theorems:
        raise RuntimeError("harness.json: 'theorems' must be a non-empty list")
    bad = [t for t in theorems if not isinstance(t, str) or not _NAME_RE.match(t)]
    if bad:
        raise RuntimeError(f"harness.json: bad theorem name(s): {bad!r}")
    dupes = sorted({t for t in theorems if theorems.count(t) > 1})
    if dupes:
        raise RuntimeError(f"harness.json: duplicate theorem name(s): {dupes!r}")

    # Optional Check 4b: which axioms the headline theorem MUST depend on.
    final_theorem = data.get("final_theorem") or ""
    mandatory = data.get("mandatory_axioms") or []
    if final_theorem and not _NAME_RE.match(final_theorem):
        raise RuntimeError(f"harness.json: bad 'final_theorem': {final_theorem!r}")
    if not isinstance(mandatory, list):
        raise RuntimeError("harness.json: 'mandatory_axioms' must be a list")
    bad_ax = [a for a in mandatory if not isinstance(a, str) or not _NAME_RE.match(a)]
    if bad_ax:
        raise RuntimeError(f"harness.json: bad mandatory axiom name(s): {bad_ax!r}")
    if mandatory and not final_theorem:
        raise RuntimeError("harness.json: 'mandatory_axioms' needs 'final_theorem' "
                           "(the check inspects that theorem's dependencies)")
    if final_theorem and final_theorem not in theorems:
        raise RuntimeError(f"harness.json: 'final_theorem' {final_theorem!r} is not "
                           "one of 'theorems'")
    return project, problem, theorems, final_theorem, mandatory


# Append-only scaffolding whose wording is fixed pipeline policy, not per-problem
# content. Copied from the reference with only the problem title filled in, so the
# rules every later agent reads are identical in every run.
_BOILERPLATE = ("PROGRESS.md", "TASKS.md", "REVIEW.md")


def _render_boilerplate(target: str, problem: str) -> list[str]:
    """Write the append-only log headers verbatim from the reference."""
    written = []
    for name in _BOILERPLATE:
        src = F.read_text(os.path.join(F.REFERENCE_DIR, name))
        if not src:
            raise RuntimeError(f"reference/{name} is missing or empty")
        with open(os.path.join(target, name), "w", encoding="utf-8") as fh:
            fh.write(src.replace("<problem>", problem))
        written.append(name)
    return written


def _render_verify_sh(target: str, project: str, theorems: list[str],
                      final_theorem: str = "", mandatory: list[str] | None = None) -> str:
    """Write scripts/verify.sh from the reference harness.

    Only the harness.json values vary per problem; the checks are fixed
    logic and are never re-authored per run. Keeping both as plain literals is
    also required by formlib._project_name / _solution_frozen_names, which read
    them back out of the generated file.
    """
    src = F.read_text(os.path.join(F.REFERENCE_DIR, "scripts", "verify.sh"))
    if not src:
        raise RuntimeError("reference scripts/verify.sh is missing or empty")

    names = " ".join('"%s"' % t for t in theorems)
    src, n_proj = re.subn(r'(?m)^PROJECT=.*$', 'PROJECT="%s"' % project, src, count=1)
    src, n_thms = re.subn(r'(?m)^ALL_THEOREMS=\(.*\)$', "ALL_THEOREMS=(%s)" % names,
                          src, count=1)
    if not n_proj or not n_thms:
        raise RuntimeError("reference verify.sh lost its PROJECT / ALL_THEOREMS "
                           "parameter block — cannot render the harness")

    axioms = " ".join('"%s"' % a for a in (mandatory or []))
    src, n_fin = re.subn(r'(?m)^FINAL_THEOREM=.*$',
                         'FINAL_THEOREM="%s"' % final_theorem, src, count=1)
    src, n_max = re.subn(r'(?m)^MANDATORY_AXIOMS=\(.*\)$',
                         "MANDATORY_AXIOMS=(%s)" % axioms, src, count=1)
    if not n_fin or not n_max:
        raise RuntimeError("reference verify.sh lost its FINAL_THEOREM / "
                           "MANDATORY_AXIOMS parameter block")

    path = os.path.join(target, "scripts", "verify.sh")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    os.chmod(path, 0o755)
    return path


def _lint_verify_sh(path: str) -> list[str]:
    """Reject a harness that would silently skip checks instead of failing loudly.

    Guards the two things that make a broken harness look like a passing one, plus
    the literals formlib parses back out. Kept even though the file is now rendered
    rather than model-authored: it also covers hand edits and future template work.
    """
    problems: list[str] = []
    proc = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
    if proc.returncode != 0:
        problems.append("verify.sh: bash syntax error: %s" % proc.stderr.strip())

    text = F.read_text(path)
    # Join backslash-continuations first, so a guard on the following physical
    # line still counts as guarding the statement that opened it.
    statements, buf, start = [], "", 1
    for i, line in enumerate(text.splitlines(), 1):
        if not buf:
            start = i
        buf += line.rstrip("\\") if line.rstrip().endswith("\\") else line
        if not line.rstrip().endswith("\\"):
            statements.append((start, buf))
            buf = ""
    if buf:
        statements.append((start, buf))

    # Track whether `set -e` is in force, so we can tell a deliberately unguarded
    # substitution (wrapped in set +e, as checks 3-5 do) from an accidental one.
    errexit = False
    heredoc = None
    for i, stmt in statements:
        stripped = stmt.strip()

        # Skip heredoc bodies — they are another language (the Check 2 scanner is
        # Python) and must not be read as shell.
        if heredoc is not None:
            if stripped == heredoc:
                heredoc = None
            continue
        hd = re.search(r"<<-?'?([A-Za-z_]\w*)'?", stripped)
        if hd:
            heredoc = hd.group(1)

        if re.match(r"^set\s+-\w*e", stripped):
            errexit = True
        elif re.match(r"^set\s+\+\w*e", stripped):
            errexit = False
        if not errexit:
            continue

        guarded = "|| true" in stripped

        # Under `set -e` + `pipefail`, a no-match grep aborts the whole run. This
        # bites in TWO shapes, and checking only the first missed three live cases:
        #   VAR=$(... grep ...)          — command substitution
        #   echo "$x" | grep ... | head  — a bare pipeline, typically on a FAILURE
        #                                  path printing diagnostics, so the harness
        #                                  dies exactly when it has something to say.
        if not guarded and re.search(r"\bgrep\b", stripped) \
                and not stripped.startswith("#"):
            if re.match(r"^\w+=\$\(", stripped):
                problems.append("verify.sh:%d: unguarded grep in command "
                                "substitution (aborts under set -e on no match)" % i)
            elif "|" in stripped and not re.match(r"^(if|while|until|case)\b", stripped):
                problems.append("verify.sh:%d: unguarded grep pipeline (aborts "
                                "under set -e + pipefail on no match)" % i)

        # Capturing `$?` only makes sense if the command was allowed to fail.
        # Under `set -e` it never gets the chance — the harness dies first, so the
        # check can only ever report success. This is how a check becomes pass-only.
        if re.match(r"^\w+=\$\?", stripped):
            problems.append("verify.sh:%d: captures $? while set -e is in force — "
                            "the failure path can never report "
                            "(wrap the command in set +e / set -e)" % i)

    if not re.search(r'(?m)^\s*PROJECT\s*=\s*"', text):
        problems.append("verify.sh: no PROJECT=\"...\" line "
                        "(formlib._project_name parses it)")
    if not re.search(r'ALL_THEOREMS=\([^)]*"', text):
        problems.append("verify.sh: no ALL_THEOREMS=(\"...\") array "
                        "(formlib._solution_frozen_names parses it)")
    for check in ("Check 4", "Check 5"):
        if check not in text:
            problems.append("verify.sh: %s is missing" % check)
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Scaffold orchestration files from SKETCH.md.")
    ap.add_argument("target", nargs="?", default=".", help="target directory (default: .)")
    ap.add_argument("--model", default=None, help="override the claude model")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt/command, do not call claude")
    args = ap.parse_args()

    target = F.resolve_target(args.target)
    F.require_sketch(target)

    if not os.path.isdir(F.REFERENCE_DIR):
        sys.exit(f"ERROR: bundled reference dir not found: {F.REFERENCE_DIR}")

    F.log(f"setup: target={target}")
    F.log(f"setup: reference={F.REFERENCE_DIR}")

    # Create USER_NOTES.md (user-editable special instructions) from the reference
    # template if absent. The user fills this in BEFORE running init.py — it is
    # where assumed-certificate axioms are permitted. Never clobber an existing one.
    user_notes = os.path.join(target, "USER_NOTES.md")
    if os.path.exists(user_notes):
        F.log("setup: USER_NOTES.md already present — leaving it untouched; the "
              "architect will read it.")
    else:
        content = F.read_text(os.path.join(F.REFERENCE_DIR, "USER_NOTES.md")) or _DEFAULT_USER_NOTES
        with open(user_notes, "w", encoding="utf-8") as fh:
            fh.write(content)
        # The architect runs in a moment and reads this file, so a template
        # created now means it sees the strict default. Editing it later still
        # permits axioms (init.py reads it) but cannot switch Check 4b on.
        F.log("setup: created USER_NOTES.md from the template (strict default: no "
              "assumed axioms). If this problem needs assumed axioms or a mandated "
              "proof route, stop, fill it in, and re-run setup.py — the architect "
              "reads it NOW, and Check 4b can only be configured here.")

    prompt = PROMPT.replace("{ref}", F.REFERENCE_DIR)
    result = F.run_agent(
        "setup-architect", prompt, cwd=target,
        add_dirs=[F.REFERENCE_DIR],
        model=args.model,
        timeout=F.SETUP_TIMEOUT,
        log_dir=os.path.join(target, "logs", "orchestration"),
        dry_run=args.dry_run,
    )
    if args.dry_run:
        return 0

    # Verify the expected agent artifacts landed. The architect now writes only
    # the problem-specific content; everything else is rendered below.
    expected = ["BLUEPRINT.md", "scripts/harness.json", "scripts/frozen.sha256"]
    missing = [f for f in expected if not os.path.exists(os.path.join(target, f))]
    if missing:
        F.log(f"WARNING: setup agent did not create: {', '.join(missing)}")
        return 1 if not result.ok else 2

    # Render the harness and the append-only headers ourselves — never the model.
    try:
        project, problem, theorems, final_thm, mandatory = _read_harness_params(target)
        verify_path = _render_verify_sh(target, project, theorems, final_thm, mandatory)
        boilerplate = _render_boilerplate(target, problem)
    except RuntimeError as exc:
        F.log(f"ERROR: {exc}")
        return 1
    F.log(f"setup: rendered scripts/verify.sh (PROJECT={project}, "
          f"{len(theorems)} frozen theorems)")
    F.log(f"setup: rendered {', '.join(boilerplate)} from the reference")

    # Pin the control files now. init.py re-pins once it has written
    # frozen.sha256 and ALLOWED_AXIOMS.txt; loop.py re-checks every iteration.
    pinned = F.write_control_manifest(target)
    F.log(f"setup: pinned control files ({', '.join(pinned)})")

    problems = _lint_verify_sh(verify_path)
    if problems:
        for p in problems:
            F.log(f"ERROR: {p}")
        return 1

    F.log("setup complete. Scaffolding written:")
    for f in expected + ["scripts/verify.sh"] + list(_BOILERPLATE):
        F.log(f"  ✓ {f}")
    F.log("Next: python3 init.py " + target)
    return 0 if result.ok else 0  # files exist; surface agent exit only as info


if __name__ == "__main__":
    raise SystemExit(main())
