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
        PROGRESS.md           (append-only log header)
        scripts/verify.sh     (the 5-check verification harness, this problem's
                               theorem names)
        scripts/frozen.sha256 (placeholder; init.py records the real pins)
        TASKS.md              (append-only header; 4-agent delegation)
        REVIEW.md             (append-only header; audit log)

    It also writes USER_NOTES.md directly (from the reference template) — the
    user-editable file for special instructions, in particular any
    assumed-certificate axioms to permit. Fill it in BEFORE running init.py.

Next step after this:  python3 init.py TARGET_DIR
"""

import argparse
import os
import sys

import formlib as F


# Fallback USER_NOTES.md body, used only if the reference template is missing
# (e.g. config.json reference_dir points at a project without one).
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
FORMAT EXACTLY (same sections, same headings, same PROGRESS entry format, the
same five checks in verify.sh), but write CONTENT for the problem in THIS
directory's SKETCH.md. Do NOT copy the prob4b mathematics — design the correct
Lean decomposition for the CURRENT sketch.

== Do this, in order ==

1. Read ./SKETCH.md carefully. Understand the exact theorem and every step of
   the proof sketch. This is the mathematical source of truth.

2. Read these reference files as FORMAT TEMPLATES:
     {ref}/BLUEPRINT.md
     {ref}/PROGRESS.md
     {ref}/scripts/verify.sh
     {ref}/scripts/frozen.sha256

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

4. Write ./PROGRESS.md — ONLY the header (title + the append-only rules summary,
   matching the reference header). NO log entries yet (init.py adds the first).

5. Write ./scripts/verify.sh — adapt the reference harness. Keep ALL FIVE checks
   identical in spirit:
     (1) Frozen SHA pins for Defs.lean + Theorems.lean match scripts/frozen.sha256;
     (2) Banned keywords (sorry / sorryAx / native_decide / admit / unsafe /
         implemented_by / ofReduceBool / `axiom` decl) in any first-party .lean,
         comment-aware, with `sorry` allowed ONLY in Theorems.lean;
     (3) clean `lake build` (only the expected Theorems.lean sorry warnings);
     (4) `#print axioms` for each Solution.<name> ⊆ {propext, Classical.choice,
         Quot.sound} (no sorryAx / native_decide / ofReduceBool);
     (5) statement gates: Discharge.lean + Solution.lean compile.
   Update REPO/SRC paths, the project source-dir name, and the ALL_THEOREMS
   array to the theorem names you froze in step 3. Make it executable
   (`chmod +x scripts/verify.sh`). KEEP the reference harness's allowed-axioms
   logic intact: verify.sh reads scripts/ALLOWED_AXIOMS.txt (written later by
   init.py from USER_NOTES.md) and permits exactly those axiom names in checks
   (2) and (4), banning every other axiom. Do not remove or weaken that.

6. Write ./scripts/frozen.sha256 — a single placeholder comment line, e.g.
   "# pins recorded by init.py after Defs.lean/Theorems.lean are frozen".

7. Write ./TASKS.md — header ONLY:
     # TASKS -- <problem> formalization
     Append-only work-delegation log for 4 parallel worker agents. The Plan
     agent appends one "## Iteration N" block per loop iteration. Each block has
     a one-line goal then "Agent k: ..." lines (one per ACTIVE worker; inactive
     agents are omitted). NEVER edit or delete an existing block.

8. Write ./REVIEW.md — header ONLY:
     # REVIEW -- <problem> formalization
     Append-only audit log. The Review agent appends one "## Review -- Iteration
     N" block per iteration with its findings and a "Verdict: COMPLETE |
     INCOMPLETE" line. NEVER edit or delete an existing block.

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


def main() -> int:
    ap = argparse.ArgumentParser(description="Scaffold orchestration files from SKETCH.md.")
    ap.add_argument("target", nargs="?", default=".", help="target directory (default: .)")
    ap.add_argument("--model", default=None, help="override the claude model")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt/command, do not call claude")
    args = ap.parse_args()

    target = F.resolve_target(args.target)
    F.require_sketch(target)

    if not os.path.isdir(F.REFERENCE_DIR):
        sys.exit(f"ERROR: reference dir not found: {F.REFERENCE_DIR}\n"
                 f"Set config.json reference_dir to a project with the template format.")

    F.log(f"setup: target={target}")
    F.log(f"setup: reference={F.REFERENCE_DIR}")

    # Create USER_NOTES.md (user-editable special instructions) from the reference
    # template if absent. The user fills this in BEFORE running init.py — it is
    # where assumed-certificate axioms are permitted. Never clobber an existing one.
    user_notes = os.path.join(target, "USER_NOTES.md")
    if os.path.exists(user_notes):
        F.log("setup: USER_NOTES.md already present — leaving it untouched.")
    else:
        content = F.read_text(os.path.join(F.REFERENCE_DIR, "USER_NOTES.md")) or _DEFAULT_USER_NOTES
        with open(user_notes, "w", encoding="utf-8") as fh:
            fh.write(content)
        F.log("setup: created USER_NOTES.md — edit it before init.py to permit any "
              "assumed-certificate axioms (default: none).")

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

    # Verify the expected artifacts landed.
    expected = ["BLUEPRINT.md", "PROGRESS.md", "TASKS.md", "REVIEW.md",
                "scripts/verify.sh", "scripts/frozen.sha256"]
    missing = [f for f in expected if not os.path.exists(os.path.join(target, f))]
    if missing:
        F.log(f"WARNING: setup agent did not create: {', '.join(missing)}")
        return 1 if not result.ok else 2

    # Make verify.sh executable in case the agent forgot.
    try:
        os.chmod(os.path.join(target, "scripts", "verify.sh"), 0o755)
    except OSError:
        pass

    F.log("setup complete. Scaffolding written:")
    for f in expected:
        F.log(f"  ✓ {f}")
    F.log("Next: python3 init.py " + target)
    return 0 if result.ok else 0  # files exist; surface agent exit only as info


if __name__ == "__main__":
    raise SystemExit(main())
