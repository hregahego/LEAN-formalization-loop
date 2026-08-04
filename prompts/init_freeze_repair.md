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
   updated), in the exact format scripts/verify.py expects.
6. Append a PROGRESS.md `📝 decision` entry (append-only, real UTC timestamp from
   `date -u +"%Y-%m-%dT%H:%M:%SZ"`) describing EXACTLY what you changed and why,
   referencing the audit.

Do not write any proof. Work autonomously to completion; do not ask questions.
Print a summary of every change you made.
