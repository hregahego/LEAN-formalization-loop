# USER_NOTES — special instructions for this formalization

This file is **created by `setup.py`** and read by **`init.py`** (when it freezes
`Defs.lean`/`Theorems.lean`) and by **`loop.py`** (when it proves and audits).
Put any problem-specific guidance here **before you run `init.py`**. Everything in
this file is free-form prose that the init, worker, and review agents read for
context — write to a human, not to a parser.

By default the pipeline is **maximally strict**:

- the ONLY axioms any solved theorem may depend on are the Lean/Mathlib standard
  `{propext, Classical.choice, Quot.sound}`;
- no custom `axiom` declarations are allowed anywhere;
- **no frozen theorem may carry an extra hypothesis** that `SKETCH.md` does not
  state (the cardinal anti-cheat rule — this is NEVER relaxed).

Use the section below to widen the **axiom** policy in a controlled way. Anything
you do not describe here stays banned.

## Allowed axioms (assumed certificates)

Some facts are mathematically routine but **prohibitively expensive to PROVE in
Lean** — a specific large factorization, an explicit interpolant, the result of a
finite but huge case-check, a numeric certificate verified by external
computation. You may **assume such a fact as a Lean `axiom`** instead of proving
it, but ONLY if you describe it here.

> **Axioms, not hypotheses.** A certificate you want to assume must be introduced
> as an `axiom` (so it shows up in `#print axioms` and is checked deterministically
> by `verify.sh`). Do NOT bolt it onto a frozen theorem as a hypothesis `(h : …)`
> — added hypotheses remain forbidden and the faithfulness gate will reject them.

For each axiom you permit, describe in plain words:

- **what** it asserts (the exact mathematical statement being assumed);
- **why** it is assumed rather than proved (e.g. "verified by external
  computation; reproving in Lean is prohibitively slow");
- **where** it is used (which frozen theorem(s) depend on it).

`init.py` reads this section, declares the corresponding `axiom`(s) in
`Defs.lean` with faithful statements, and writes their fully-qualified names into
`scripts/ALLOWED_AXIOMS.txt`. From then on `verify.sh` permits exactly those
axiom names (in addition to the standard three) and **bans every other axiom**.

<!-- Describe the axioms you allow below, or write "None — no assumed axioms." -->

None — no assumed axioms.
