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

The faithfulness gate audits the frozen Defs/Theorems and, on defects, runs a
bounded fix-and-re-freeze repair loop (up to MAX_FAITHFULNESS_ATTEMPTS rounds).
If it cannot be made to pass, init exits non-zero so loop.py is never run on an
unfaithful skeleton.

Next step after this:  python3 loop.py TARGET_DIR
"""

from __future__ import annotations

import argparse
import os

import formlib as F


# Audit/repair rounds the faithfulness gate runs before giving up.
MAX_FAITHFULNESS_ATTEMPTS = 2


def faithfulness_verdict(result: F.AgentResult) -> str | None:
    """FAITHFUL / UNFAITHFUL from the auditor's trailer, else None (inconclusive)."""
    tr = F.extract_trailer(result.result_text)
    if tr:
        v = str(tr.get("verdict", "")).upper()
        if v in ("FAITHFUL", "UNFAITHFUL"):
            return v
    return None


def run_faithfulness_gate(target, model, log_dir, *, max_attempts) -> bool:
    """
    Independent audit of the frozen Defs/Theorems, with a bounded repair loop.
    Returns True iff the audit confirms FAITHFUL.
    """
    for attempt in range(1, max_attempts + 1):
        suffix = "" if attempt == 1 else f"-{attempt}"
        review = F.run_agent(
            f"init-faithfulness-review{suffix}", F.load_prompt("init_faithfulness_review"), cwd=target,
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
        if attempt == max_attempts:
            break
        F.log(f"init: faithfulness audit found defects — running freeze-repair "
              f"(attempt {attempt}).")
        F.run_agent(
            f"init-freeze-repair-{attempt}", F.load_prompt("init_freeze_repair"), cwd=target,
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
    args = ap.parse_args()

    target = F.resolve_target(args.target)
    F.require_sketch(target)
    F.require_blueprint(target)

    F.log(f"init: executing Step -1 in {target}")
    F.log(f"init: timeout is {F.INIT_TIMEOUT}s (a cold Mathlib build can be slow)")

    log_dir = os.path.join(target, "logs", "orchestration")
    result = F.run_agent(
        "init-step-minus1", F.load_prompt("init_step_minus1"), cwd=target,
        model=args.model,
        timeout=F.INIT_TIMEOUT,
        log_dir=log_dir,
        dry_run=args.dry_run,
    )
    if not result.ok:
        # Not fatal on its own — the pins check below is the real gate — but say
        # WHY, so a timeout is not reported as "the agent wrote no pins".
        F.log(f"init: the Step -1 agent did not exit cleanly "
              f"({'timeout' if result.timed_out else f'exit {result.returncode}'}).")

    if args.dry_run:
        F.run_agent("init-faithfulness-review", F.load_prompt("init_faithfulness_review"),
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
    F.log("init: running the Defs/Theorems faithfulness gate "
          "(the key anti-cheat checkpoint).")
    faithful = run_faithfulness_gate(
        target, args.model, log_dir,
        max_attempts=MAX_FAITHFULNESS_ATTEMPTS,
    )
    if not faithful:
        F.log("init: FAITHFULNESS GATE DID NOT PASS. Do NOT run loop.py yet — "
              "review the latest REVIEW.md audit block and fix Defs/Theorems.")
        return 1
    F.log("init: faithfulness gate PASSED — Defs/Theorems faithfully match SKETCH.md.")

    # Re-pin the control files: the SHA pins and the axiom allowlist exist only
    # now, and the gate may have re-frozen Defs/Theorems (which rewrites
    # frozen.sha256). From here on any change to these is tampering, and loop.py
    # re-checks them every iteration.
    pinned = F.write_control_manifest(target)
    F.log(f"init: pinned control files ({', '.join(pinned)})")

    F.log("init complete.")
    F.log("Next: python3 loop.py " + target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
