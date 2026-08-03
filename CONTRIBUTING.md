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

## Before you touch `reference/scripts/verify.sh`

This file certifies every result the pipeline produces, so it has rules of its own:

- It runs under `set -euo pipefail`. A no-match `grep` — in a command
  substitution *or* a bare pipeline — aborts the script. Guard with `|| true`
  where no match is a legitimate outcome. `setup.py`'s `_lint_verify_sh` checks
  both shapes and runs on every scaffold.
- A check that detects a violation must be able to *report* it. Wrap anything
  whose non-zero exit means "found a problem" in `set +e` / `set -e`.
- Pre-flight failures exit `64`, never `1`: the caller distinguishes "could not
  run" from "ran and found one problem".
- It must stay readable by an auditor. Density is acceptable for portability
  (macOS ships bash 3.2 and BSD userland) and for failure handling; it is not
  acceptable for cleverness.
- `PROJECT=` and `ALL_THEOREMS=(…)` must stay plain literals on their own lines —
  `formlib` parses them back out of the *generated* file to derive the project
  namespace and the frozen-theorem set.

## Determinism

`verify.sh` and the append-only log headers are **rendered** from `reference/` by
`setup.py`; only the values in `scripts/harness.json` vary per problem. They are
deliberately not model-authored, because the harness is what certifies the result.
`BLUEPRINT.md`, `Defs.lean` and `Theorems.lean` *are* model-authored — that is the
formalization, and it is the point of the project.

If you change the harness, change it in `reference/scripts/verify.sh`. Editing a
generated project's copy will be flagged by the control-file manifest.

## Style

`ruff check .` — configured in `pyproject.toml`. Match the surrounding code;
comments should explain *why*, which is the convention here and worth keeping.
