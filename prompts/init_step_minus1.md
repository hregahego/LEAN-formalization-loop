You are the INIT agent executing **Step -1 ("Part -1 -- Setting up the
repository")** of BLUEPRINT.md for an autonomous Lean 4 + Mathlib formalization.
Your working directory is the project root; it already contains SKETCH.md,
BLUEPRINT.md, PROGRESS.md, and scripts/ (verify.py + a placeholder
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
   scripts/verify.py's reader expects (check how verify.py parses it).

5b. Write scripts/ALLOWED_AXIOMS.txt — the machine-readable axiom allowlist that
   verify.py reads. If USER_NOTES.md permitted assumed-certificate axioms, list
   the FULLY-QUALIFIED name of each axiom you declared (e.g. `<Project>.cert_x`),
   comma- or newline-separated; lines starting with `#` are comments. If the user
   permitted NO axioms, write the file with just a comment line (an empty
   allowlist) so the default strict policy applies. The names here MUST exactly
   match the `axiom` declarations in Defs.lean and what their `#print axioms`
   will report — verify.py permits exactly these (plus the standard three) and
   BANS every other axiom.

6. Append to PROGRESS.md (APPEND-ONLY; real UTC timestamp from
   `date -u +"%Y-%m-%dT%H:%M:%SZ"`), in BLUEPRINT's mandated entry format:
     * one `✅` entry "SETUP frozen, skeleton builds, pins recorded" whose
       `Check:` is the actual `lake build` result;
     * one `📝` entry recording the concrete modeling decisions you committed to
       (so later agents never re-derive or change them), INCLUDING any
       assumed-certificate axioms you declared per USER_NOTES.md (each axiom's
       name, what it assumes, and why it is assumed rather than proved).

7. Run `scripts/verify.py` once. With all proofs still `sorry`, the axiom/gate
   checks (4 and 5) may fail — that is expected at this stage — but checks 1
   (SHA pins), 2 (banned keywords; sorry allowed only in Theorems.lean), and 3
   (build) should pass. Report what passed.

Constraints: do NOT edit BLUEPRINT.md or SKETCH.md. Do NOT begin any Stage A or
later proof. Work autonomously to completion; do not ask questions. When done,
print the project name, the frozen theorem names, and confirm the skeleton
builds with only the expected sorry warnings.
