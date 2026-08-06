# Contributing

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

No dependencies, no test runner to install. CI runs the same command on Linux and
macOS, because several past bugs were platform-specific (BSD `mktemp` template
handling, bash 3.2 empty-array semantics under `set -u`).

## What the tests are for

This project's product is *verification*, so the tests exist to stop a check from
passing without having checked anything. Nearly every test corresponds to a real
bug where the harness reported success while verifying nothing:

- a `#print axioms` list that wrapped across lines, so the parse found nothing and
  the "no disallowed axiom" loop iterated zero times and printed PASS;
- a pins file with no pins, so Check 1 looped zero times and passed silently;
- `def m : String := "/-"` opening a comment that hid a following `sorry`;
- a no-match `grep` on a failure path aborting the harness under `set -e`, so it
  died exactly when it had something to report.

If you change any parser, matcher, or check, add a test for the shape that would
defeat it. **A parse that finds nothing must never be reported as "nothing to
report".**

## Before you touch `reference/scripts/verify.py`

This file certifies every result the pipeline produces, so it has rules of its own:

- **A check that cannot look must FAIL, not pass.** If a parse comes back empty,
  that is "I could not check", never "nothing is wrong". Most historical bugs
  here were an empty parse read as success — an axiom list that wrapped across
  lines, a pins file with no pins, a scan blinded by a string literal.
- **A check must be able to report what it found.** Return findings; do not
  raise. The one place that exits early is a *pre-flight* failure, which means
  nothing was verified at all.
- **Pre-flight failures exit `64`, never `1`**, so the caller can distinguish
  "could not run" from "ran and found one problem". `loop.py` maps anything
  `>= 64` to "did not run" and refuses to accept a `COMPLETE` verdict on it.
- **It is static.** Everything problem-specific lives in `scripts/harness.json`,
  which the harness reads at run time, so the file is byte-identical in every
  project. Do not add substitution or per-project branching.
- **Prefer plain Python to clever parsing.** This used to be 470 lines of bash;
  most of its bugs were shell semantics (`set -e` aborts, word splitting, BSD vs
  GNU) or `grep`/`sed` pipelines. Where a regex is genuinely the right tool
  (Lean's `#print axioms` output), give it a name, a comment, and a test.

Each check is a plain function taking `(harness, say)` and returning a list of
failure strings, so it can be tested directly — see `tests/test_harness.py`. Add
a test with any new check.

## Determinism

`scripts/verify.py` is **copied** verbatim from `reference/`, and the append-only
log headers are rendered from it with only the problem title filled in. They are
deliberately not model-authored, because the harness is what certifies the
result. `BLUEPRINT.md`, `Defs.lean` and `Theorems.lean` *are* model-authored —
that is the formalization, and it is the point of the project.

If you change the harness, change it in `reference/scripts/verify.py`. Editing a
generated project's copy will be flagged by the control-file manifest.

## Where things live

- `formlib.py` — plumbing every stage shares: config, prompt loading, control-file
  integrity, launching an agent.
- `signals.py` — how much of the formalization is discharged and whether it has
  stalled. Read from `scripts/harness.json` and `Solution.lean`, never from an
  agent's claims. Used only by `loop.py`.
- `reference/scripts/verify.py` — the harness. See the section above.

## Style

`ruff check .` — configured in `pyproject.toml`. Match the surrounding code;
comments should explain *why*, which is the convention here and worth keeping.
