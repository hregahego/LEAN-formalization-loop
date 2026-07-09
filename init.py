#!/usr/bin/env python3
"""
init.py — execute Step -1 of BLUEPRINT.md: create the Lean4 project and FREEZE
the definitions and theorem statements.

Usage:
    python3 init.py [TARGET_DIR]        (default: current directory)

Preconditions:
    setup.py has been run, so TARGET_DIR has BLUEPRINT.md / PROGRESS.md /
    scripts/.

What it does:
    1. Runs one configured agent that performs exactly the "Part -1 -- Setting
       up the repository" stage of BLUEPRINT.md:
         * `lake new` a Lean4 + Mathlib project with the planned layout & namespace,
           `lake exe cache get`, build the bare skeleton;
         * write Defs.lean (all frozen definitions) and Theorems.lean (every frozen
           theorem statement `:= sorry`), the Proofs/<Stage>/ tree, Discharge.lean
           and Solution.lean stubs, and the root import file;
         * make `lake build` succeed (only expected Theorems.lean sorry warnings);
         * record SHA-256 pins into scripts/frozen.sha256;
         * append the SETUP PROGRESS.md entries.
       It writes NO proofs — every theorem stays `:= sorry`.

    2. FAITHFULNESS GATE (the key anti-cheat checkpoint). Because Defs.lean and
       Theorems.lean are where the math can be silently weakened — a dropped
       clause, a `∀` specialized to examples, an equality softened to an
       inclusion, a vacuous headline — an INDEPENDENT auditor reviews the frozen
       files against SKETCH.md and BLUEPRINT.md. If it finds defects, a bounded
       repair loop fixes the statements and RE-FREEZES (legitimate here, since no
       proofs exist yet) before the pipeline is allowed to proceed. If the gate
       cannot be made to pass, init exits non-zero so loop.py is not run on an
       unfaithful skeleton.

Note: a cold Mathlib `lake exe cache get` + build can take a long time; the
default timeout is config.json timeouts.init (2h). Increase it if needed.

Flags:
    --no-repair                  audit only; do not auto-fix/re-freeze on defects
    --max-faithfulness-attempts  audit/repair rounds before giving up (default 2)
    --skip-faithfulness-review   skip the gate entirely (not recommended)

Next step after this:  python3 loop.py TARGET_DIR
"""

from __future__ import annotations

import argparse
import os

import formlib as F


PROMPT = """\
You are the INIT agent executing **Step -1 ("Part -1 -- Setting up the
repository")** of BLUEPRINT.md for an autonomous Lean 4 + Mathlib formalization.
Your working directory is the project root; it already contains SKETCH.md,
BLUEPRINT.md, PROGRESS.md, and scripts/ (verify.sh + a placeholder
frozen.sha256).

Do EXACTLY what BLUEPRINT.md "Part -1" specifies, and nothing beyond it. Do NOT
write any Stage A+ proof — every theorem must remain `:= sorry` when you finish.

1. Read BLUEPRINT.md "Part -1" IN FULL: the file-tree layout, the project /
   namespace name, every definition to freeze in Defs.lean, and every theorem
   statement to freeze in Theorems.lean. Also skim SKETCH.md so the frozen
   statements are faithful. Then read ./USER_NOTES.md: it states any
   problem-specific instructions and, in particular, any ASSUMED-CERTIFICATE
   AXIOMS the user has permitted (facts too expensive to prove in Lean that may
   be taken as `axiom`s). Note: the user may permit AXIOMS only — never add a
   hypothesis to a frozen theorem.

2. Create the Lean 4 + Mathlib project with the EXACT layout and project name
   from BLUEPRINT.md: run the `lake` commands, pin the lean-toolchain and a
   matching Mathlib revision, `lake exe cache get`, and confirm the bare
   skeleton builds BEFORE writing any of your own files.

3. Write the frozen sources exactly as BLUEPRINT.md "Part -1" prescribes:
     * Defs.lean — every frozen definition, in the project namespace, with the
       modeling choices BLUEPRINT records. No `sorry` in Defs.lean. If (and ONLY
       if) USER_NOTES.md permits assumed-certificate axioms, declare each as a
       faithful `axiom <name> : <statement>` here (in the project namespace),
       matching exactly what USER_NOTES.md describes; give each a stable name.
       Do NOT invent axioms the user did not permit, and never weaken a frozen
       theorem with an added hypothesis.
     * Theorems.lean — every frozen theorem statement, each `:= sorry`.
     * the Proofs/<Stage>/ directory tree (create the stage subdirectories, with
       a minimal compiling placeholder module in each if BLUEPRINT lists one),
       Discharge.lean and Solution.lean stubs, and the root <Project>.lean import
       file that imports everything.
   Support declarations go in the project namespace; NEVER shadow or alter a
   frozen name.

4. Make `lake build` SUCCEED: the only acceptable warnings are the expected
   `declaration uses 'sorry'` warnings from Theorems.lean — no errors, no other
   warnings. Iterate (fix typechecking of the STATEMENTS only) until the
   skeleton compiles. Do not prove anything.

5. Record the SHA-256 of Defs.lean and Theorems.lean into scripts/frozen.sha256,
   one "<sha256>  <relative/path>" per line, in the exact format that
   scripts/verify.sh's reader expects (check how verify.sh parses it).

5b. Write scripts/ALLOWED_AXIOMS.txt — the machine-readable axiom allowlist that
   verify.sh reads. If USER_NOTES.md permitted assumed-certificate axioms, list
   the FULLY-QUALIFIED name of each axiom you declared (e.g. `<Project>.cert_x`),
   comma- or newline-separated; lines starting with `#` are comments. If the user
   permitted NO axioms, write the file with just a comment line (an empty
   allowlist) so the default strict policy applies. The names here MUST exactly
   match the `axiom` declarations in Defs.lean and what their `#print axioms`
   will report — verify.sh permits exactly these (plus the standard three) and
   BANS every other axiom.

6. Append to PROGRESS.md (APPEND-ONLY; real UTC timestamp from
   `date -u +"%Y-%m-%dT%H:%M:%SZ"`), in BLUEPRINT's mandated entry format:
     * one `✅` entry "SETUP frozen, skeleton builds, pins recorded" whose
       `Check:` is the actual `lake build` result;
     * one `📝` entry recording the concrete modeling decisions you committed to
       (so later agents never re-derive or change them), INCLUDING any
       assumed-certificate axioms you declared per USER_NOTES.md (each axiom's
       name, what it assumes, and why it is assumed rather than proved).

7. Run `scripts/verify.sh` once. With all proofs still `sorry`, the axiom/gate
   checks (4 and 5) may fail — that is expected at this stage — but checks 1
   (SHA pins), 2 (banned keywords; sorry allowed only in Theorems.lean), and 3
   (build) should pass. Report what passed.

Constraints: do NOT edit BLUEPRINT.md or SKETCH.md. Do NOT begin any Stage A or
later proof. Work autonomously to completion; do not ask questions. When done,
print the project name, the frozen theorem names, and confirm the skeleton
builds with only the expected sorry warnings.
"""


FAITHFULNESS_REVIEW_PROMPT = """\
You are an INDEPENDENT FAITHFULNESS AUDITOR. The init step has just created and
FROZEN this project's Defs.lean and Theorems.lean. This is the single
highest-leverage anti-cheat checkpoint in the whole pipeline: if a DEFINITION is
silently weakened, or a THEOREM STATEMENT is quietly trivialized HERE, then every
later "proof" can be vacuous or trivial while still passing the build and the
`#print axioms` checks. Be skeptical and adversarial — assume a statement is
cheating until you have convinced yourself it is faithful.

Read:
- ./SKETCH.md      -- the mathematical source of truth (the claims to formalize).
- ./BLUEPRINT.md   -- Part -1 "Freeze the Definitions" and "Freeze the Theorems"
                      (the intended statements + the per-stage cheat-watch boxes).
- ./USER_NOTES.md  -- the user's special instructions, including any
                      assumed-certificate AXIOMS explicitly permitted for this
                      problem (axioms only — hypotheses on frozen theorems are
                      NEVER permitted).
- ./scripts/ALLOWED_AXIOMS.txt -- the machine-readable list of permitted axiom
                      names init wrote (must correspond to USER_NOTES.md).
- the ACTUAL frozen Lean sources: locate Defs.lean and Theorems.lean (per
  BLUEPRINT's file layout, under the project source directory) and read them
  IN FULL.

Audit EVERY definition in Defs.lean and EVERY theorem statement in Theorems.lean
against SKETCH.md (math truth) and BLUEPRINT.md (the plan). For each, judge
FAITHFUL or UNFAITHFUL with a reason and file:line. Hunt specifically for:
- Definitions that are NOT the textbook definition: a missing/weakened clause
  (e.g. a dropped bound like `2 ≤ p`), a black-box alias that changes meaning
  (e.g. defining the predicate as a Mathlib alias when the sketch states an
  explicit definition), a surrogate object (Finset / cardinality / List) where
  the sketch needs a different one, or an extra clause that makes the predicate
  stronger or weaker than stated.
- Theorem statements that DON'T faithfully + minimally render the sketch's claim:
    * a `∀` specialized to finitely many examples or to a convenient subset;
    * an added hypothesis the sketch's claim does not have (a silent weakening);
    * an equality replaced by a one-sided inclusion / `≤` / `⊆`;
    * the headline replaced by a weaker or vacuous proposition (e.g. a plain
      non-unique `∃` where the claim is `∃!`, or something trivially true);
    * wrong quantifier order/domain, or the wrong object quantified.
- Drift between Theorems.lean and what BLUEPRINT froze.
- Any `sorry` in Defs.lean (only Theorems.lean may contain `sorry`).
- AXIOMS. Every `axiom` declaration in the frozen sources must be (a) explicitly
  permitted in USER_NOTES.md, (b) listed in scripts/ALLOWED_AXIOMS.txt by its
  fully-qualified name, and (c) a FAITHFUL rendering of the certificate the user
  described (not stronger/broader than stated, and certainly not a disguised
  restatement of a frozen theorem's conclusion that would make it vacuous). Flag
  as UNFAITHFUL any axiom that is not permitted, not listed, or does not match
  USER_NOTES.md — and flag any allowlist name that has no matching axiom.
A statement is UNFAITHFUL if PROVING IT would not establish the sketch's claim,
or if it could be proved WITHOUT the actual mathematics (e.g. by leaning on an
over-broad assumed axiom).

APPEND (append-only) to ./REVIEW.md exactly this block:

## Review -- INIT faithfulness audit (Defs + Theorems)
Auditor: init-faithfulness
Files audited: <Defs.lean / Theorems.lean paths>
Per-item verdicts:
  - <each definition name>: FAITHFUL | UNFAITHFUL -- <reason, file:line>
  - <each theorem name>:    FAITHFUL | UNFAITHFUL -- <reason, file:line>
Findings: <the specific defects, each with file:line and the EXACT fix needed, or "none">
Verdict: FAITHFUL | UNFAITHFUL

Set "Verdict: FAITHFUL" ONLY IF every definition and every theorem statement is a
faithful, minimal, non-weakened rendering of SKETCH.md that matches BLUEPRINT's
intent. If ANY item is UNFAITHFUL, set "Verdict: UNFAITHFUL". When in doubt,
UNFAITHFUL.

Do NOT edit any file except appending to REVIEW.md. Print a one-paragraph
summary, then end your message with this trailer as the VERY LAST lines:

<<<ORCH
{"stage": "init-faithfulness", "verdict": "FAITHFUL"}
ORCH>>>

(use "UNFAITHFUL" when any item failed; emit the trailer exactly once).
"""


FREEZE_REPAIR_PROMPT = """\
You are the FREEZE-REPAIR agent. An independent faithfulness audit found defects
in the FROZEN Defs.lean / Theorems.lean. Because NO proofs exist yet (everything
is `:= sorry`), re-freezing now is correct and expected — BLUEPRINT says a
faithfulness mismatch "means a modeling bug to fix BEFORE re-freezing, not a
hypothesis to bolt on".

1. Read the MOST RECENT "## Review -- INIT faithfulness audit" block in
   ./REVIEW.md — those are the defects to fix. Re-read the relevant parts of
   ./SKETCH.md and ./BLUEPRINT.md.
2. FIX each identified defect by editing Defs.lean and/or Theorems.lean so that
   every definition is the textbook definition and every theorem statement
   faithfully + minimally renders SKETCH.md. Make the statement CORRECT — do NOT
   add hypotheses or weaken it to dodge the audit. Keep every proof `:= sorry`.
3. If a defect originates in BLUEPRINT's PLANNED statement (the plan itself was
   weak), also update the corresponding "Freeze the Definitions" / "Freeze the
   Theorems" text in ./BLUEPRINT.md so the plan and the code agree, and say so.
4. Rebuild: `lake build` must still succeed with only the expected Theorems.lean
   `declaration uses 'sorry'` warnings — no errors, no other warnings.
5. Re-record the SHA-256 of Defs.lean and Theorems.lean into
   scripts/frozen.sha256 (the frozen statements changed, so the pins MUST be
   updated), in the exact format scripts/verify.sh expects.
6. Append a PROGRESS.md `📝 decision` entry (append-only, real UTC timestamp from
   `date -u +"%Y-%m-%dT%H:%M:%SZ"`) describing EXACTLY what you changed and why,
   referencing the audit.

Do not write any proof. Work autonomously to completion; do not ask questions.
Print a summary of every change you made.
"""


def faithfulness_verdict(result: F.AgentResult) -> str | None:
    """FAITHFUL / UNFAITHFUL from the auditor's trailer, else None (inconclusive)."""
    tr = F.extract_trailer(result.result_text)
    if tr:
        v = str(tr.get("verdict", "")).upper()
        if v in ("FAITHFUL", "UNFAITHFUL"):
            return v
    return None


def run_faithfulness_gate(target, model, log_dir, *, max_attempts, repair_enabled) -> bool:
    """
    Independent audit of the frozen Defs/Theorems, with a bounded repair loop.
    Returns True iff the audit confirms FAITHFUL.
    """
    for attempt in range(1, max_attempts + 1):
        suffix = "" if attempt == 1 else f"-{attempt}"
        review = F.run_agent(
            f"init-faithfulness-review{suffix}", FAITHFULNESS_REVIEW_PROMPT, cwd=target,
            model=model, timeout=F.REVIEW_TIMEOUT, log_dir=log_dir,
            output_format="json",
        )
        verdict = faithfulness_verdict(review)
        F.log(f"init: faithfulness verdict (attempt {attempt}/{max_attempts}): {verdict}")

        if verdict == "FAITHFUL":
            return True
        if verdict is None:
            F.log("init: could not determine a faithfulness verdict from the audit "
                  "(no/garbled trailer). Inspect REVIEW.md before running loop.py.")
            return False
        # UNFAITHFUL
        if not repair_enabled:
            F.log("init: faithfulness audit found defects and --no-repair is set. "
                  "See the latest REVIEW.md audit block.")
            return False
        if attempt == max_attempts:
            break
        F.log(f"init: faithfulness audit found defects — running freeze-repair "
              f"(attempt {attempt}).")
        F.run_agent(
            f"init-freeze-repair-{attempt}", FREEZE_REPAIR_PROMPT, cwd=target,
            model=model, timeout=F.SETUP_TIMEOUT, log_dir=log_dir,
        )

    F.log("init: faithfulness audit still reports UNFAITHFUL after "
          f"{max_attempts} attempt(s). See REVIEW.md; fix Defs/Theorems and "
          "re-run init.py before loop.py.")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Execute Step -1 of BLUEPRINT: scaffold & freeze the Lean project.")
    ap.add_argument("target", nargs="?", default=".", help="target directory (default: .)")
    ap.add_argument("--model", default=None, help="override the claude model")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt/command, do not call claude")
    ap.add_argument("--no-repair", action="store_true",
                    help="faithfulness gate audits only; do not auto-fix/re-freeze on defects")
    ap.add_argument("--max-faithfulness-attempts", type=int, default=2,
                    help="audit/repair rounds before giving up (default 2)")
    ap.add_argument("--skip-faithfulness-review", action="store_true",
                    help="skip the Defs/Theorems faithfulness gate entirely (not recommended)")
    args = ap.parse_args()

    target = F.resolve_target(args.target)
    F.require_sketch(target)
    F.require_blueprint(target)

    F.log(f"init: executing Step -1 in {target}")
    F.log(f"init: timeout is {F.INIT_TIMEOUT}s (a cold Mathlib build can be slow)")

    result = F.run_agent(
        "init-step-minus1", PROMPT, cwd=target,
        model=args.model,
        timeout=F.INIT_TIMEOUT,
        log_dir=os.path.join(target, "logs", "orchestration"),
        dry_run=args.dry_run,
    )
    log_dir = os.path.join(target, "logs", "orchestration")

    if args.dry_run:
        if not args.skip_faithfulness_review:
            F.run_agent("init-faithfulness-review", FAITHFULNESS_REVIEW_PROMPT,
                        cwd=target, model=args.model, log_dir=log_dir, dry_run=True)
        return 0

    pins = F.read_text(os.path.join(target, "scripts", "frozen.sha256"))
    have_pins = any(line.strip() and not line.lstrip().startswith("#")
                    for line in pins.splitlines())
    if not have_pins:
        F.log("WARNING: scripts/frozen.sha256 has no real pins — Step -1 may not have completed.")
        return 1

    F.log("init: Lean skeleton frozen, SHA pins recorded.")

    # --- Faithfulness gate: audit (and bounded-repair) the frozen Defs/Theorems ---
    if args.skip_faithfulness_review:
        F.log("init: SKIPPING faithfulness review (--skip-faithfulness-review).")
    else:
        F.log("init: running the Defs/Theorems faithfulness gate "
              "(the key anti-cheat checkpoint).")
        faithful = run_faithfulness_gate(
            target, args.model, log_dir,
            max_attempts=max(1, args.max_faithfulness_attempts),
            repair_enabled=not args.no_repair,
        )
        if not faithful:
            F.log("init: FAITHFULNESS GATE DID NOT PASS. Do NOT run loop.py yet — "
                  "review the latest REVIEW.md audit block and fix Defs/Theorems.")
            return 1
        F.log("init: faithfulness gate PASSED — Defs/Theorems faithfully match SKETCH.md.")

    F.log("init complete.")
    F.log("Next: python3 loop.py " + target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
