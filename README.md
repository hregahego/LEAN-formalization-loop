# Autonomous Lean 4 formalization pipeline

Turn a natural-language proof sketch into a fully verified **Lean 4 + Mathlib**
formalization, autonomously.

Given a proof sketch, the pipeline scaffolds a Lean project, freezes the
definitions and theorem statements, then runs a **Plan → Workers → Review** loop
of agents that fill in every proof until the whole development compiles with no
`sorry` and clean `#print axioms`.

Every agent is a headless CLI subprocess (Claude Code or Codex); the Python
scripts only orchestrate them and parse the shared markdown files the agents read
and write. There are no dependencies beyond Python's standard library and the
agent CLI you choose.

---

## ⚠️ You must supply a detailed, complete proof sketch

**This is the single most important input, and the one thing the pipeline cannot
do for you.** The agents formalize a proof; they do not discover one. The quality
and correctness of the final Lean development is bounded by the quality of the
sketch you write in `SKETCH.md`.

A good sketch is a *complete* mathematical argument broken into numbered steps —
not an outline, not a hint, not "the result follows by standard techniques." For
every step, spell out:

- **The objects involved** — precise definitions, dimensions, bases, relations.
  These become the frozen `Defs.lean` predicates, and "faithful" is measured
  against your exact wording.
- **The key identities, inequalities, or relations**, stated explicitly.
- **Any explicit witnesses** — the construction, the counterexample, the bound —
  written out, not gestured at.
- **Why each step follows from the previous ones.**

If a step is vague, the agent will either mis-model it or stall. Gaps in the
mathematics become gaps or unfaithfulness in the formalization. Treat writing the
sketch as writing the proof: if a mathematician reading only your sketch could not
reconstruct the full argument, neither can the pipeline. See
[`reference/SKETCH.md`](reference/SKETCH.md) for the expected shape.

---

## Requirements

- **Python 3.9+** (standard library only)
- A **coding-agent CLI**: [Claude Code](https://claude.com/claude-code)
  (`claude`, the default) or [Codex](https://github.com/openai/codex) (`codex`)
- **Lean 4 toolchain** via [`elan`](https://github.com/leanprover/elan)

## Install

```bash
git clone https://github.com/hregahego/LEAN-formalization-loop.git
cd LEAN-formalization-loop
```

The scripts run in place. Make sure your agent CLI is on your `PATH` (or set
`claude_bin` / `codex_bin` in `config.json`) and that you are logged in.

## Quick start

Author `SKETCH.md` in a fresh directory, then run the three steps:

```bash
python3 setup.py your-problem-dir   # 1. scaffold orchestration files from SKETCH.md
python3 init.py  your-problem-dir   # 2. create the Lean project + freeze Defs/Theorems
python3 loop.py  your-problem-dir   # 3. run the Plan -> Workers -> Review loop to completion
```

The directory argument defaults to `.`, so you can also `cd your-problem-dir` and
run `python3 /path/to/setup.py`, etc.

## How it works

**1. `setup.py` — scaffold.** Reads `SKETCH.md` and the bundled
[`reference/`](reference/) project (used purely as a format template) and writes:
`BLUEPRINT.md` (the Lean decomposition — file layout, frozen Defs & Theorems,
stages), `scripts/verify.py` (the anti-cheat harness) + `scripts/frozen.sha256`,
the append-only logs `PROGRESS.md` / `TASKS.md` / `REVIEW.md`, and
`USER_NOTES.md`. No Lean is written yet.

**2. `init.py` — freeze.** Runs `lake new` + Mathlib, writes the frozen
`Defs.lean` (all definitions) and `Theorems.lean` (every statement `:= sorry`),
the `Proofs/<Stage>/` tree and stubs, makes the skeleton build, and records the
real SHA pins. **No proofs are written** — every theorem stays `:= sorry`.

Then it runs the **faithfulness gate**: an independent adversarial auditor reads
the frozen `Defs.lean` / `Theorems.lean` and checks each definition and statement
against `SKETCH.md` for silent weakening (a dropped clause, a `∀` specialized to
examples, an equality softened to `⊆`, a vacuous headline). On defects it runs a
bounded fix-and-re-freeze loop (up to 2 rounds); if it still can't pass, `init.py`
exits non-zero so an unfaithful skeleton is never handed to `loop.py`.

**3. `loop.py` — prove.** Each iteration:

1. **Plan** reads the logs + `SKETCH.md`/`BLUEPRINT.md` and appends a
   `## Iteration N` block to `TASKS.md`, one `Agent k:` line per active worker.
2. **Workers** — up to 4 in parallel — each does its assigned formalization and
   appends a timestamped report to `PROGRESS.md`.
3. **`verify.py`** is run by the orchestrator — never by an agent — and its
   result is handed to Review as ground truth. Whether the certifying check runs
   is not left to an agent's discretion, and a worker cannot suppress or reword
   what it reports.
4. **Review** audits what the harness cannot see (faithfulness against
   `SKETCH.md`, weakened statements, faked `✅`) and appends a
   `## Review -- Iteration N` block ending in `Verdict: COMPLETE | INCOMPLETE`.
   Every 5th iteration is a full-project audit. A `COMPLETE` verdict that
   contradicts the harness is overridden — the measurement outranks the claim.

The loop ends when a verdict is `COMPLETE` (confirmed by a final full-project
audit). It runs unbounded by default (built for long overnight sessions); it is
**resumable**, continuing from `(highest iteration in TASKS.md) + 1`, so you can
stop and re-run it. It also stops on its own if progress stalls — no new frozen
theorem discharged for `stall_window` iterations, or one crux circled
`crux_recur_limit` times without closing — which signals a wall that needs human
help (a hint, an allowed certificate, or a fixed statement).

Press **Ctrl-C** once to stop gracefully at the next iteration boundary; again to
force-quit.

### `USER_NOTES.md` — assumed-certificate axioms

`setup.py` writes `USER_NOTES.md` for problem-specific guidance. **Fill it in
before `setup.py`** if this problem needs assumed axioms or a mandated proof
route: the architect agent reads it, and it is the only point at which the
"mandatory axioms" check can be configured. Seeding it later (before `init.py`)
still permits the axioms, but leaves that check off. Its main use: if a fact is mathematically routine but
prohibitively expensive to *prove* in Lean (a large factorization, an explicit
interpolant, a numeric certificate), permit it as a Lean `axiom` here. `verify.py`
then allows exactly those named axioms and bans all others. By default the pipeline
permits none.

## Anti-cheat guarantees

The agents run unattended, so the pipeline assumes an agent may take shortcuts and
makes each shortcut fail the build rather than pass silently (enforced by the
prompts + `verify.py`):

- `Defs.lean` and `Theorems.lean` are **frozen** and **SHA-pinned**; never edited
  during proving; `sorry` is allowed **only** in `Theorems.lean`.
- Frozen statements must render the sketch **faithfully and minimally** — no added
  hypotheses, no `∀`→examples specialization, no equality→one-sided inclusion, no
  surrogate definitions.
- Banned keywords: `sorry` (outside `Theorems.lean`), `sorryAx`, `native_decide`,
  `admit`, `unsafe`, `implemented_by`, `ofReduceBool`, and
  `debug.skipKernelTC` (which disables kernel re-typechecking and leaves no trace
  in `#print axioms`). The scanner is comment- *and* string-literal-aware, so a
  keyword cannot be hidden inside either. An `axiom` declaration is not banned
  outright but allowlist-gated, and modifier forms (`private axiom`, `@[simp]
  axiom`, …) are detected. Every solved theorem's
  `#print axioms` must stay within `{propext, Classical.choice, Quot.sound}` (plus
  any axioms you explicitly permit in `USER_NOTES.md`).
- `PROGRESS.md`, `TASKS.md`, `REVIEW.md` are append-only; Review independently
  re-verifies every `✅` rather than trusting the log.
- **The harness and its configuration are pinned.** `scripts/verify.py`,
  `scripts/harness.json`, `scripts/frozen.sha256` and `scripts/ALLOWED_AXIOMS.txt`
  all live in the workspace the agents edit, so their SHA-256 hashes are recorded
  in `scripts/control_manifest.sha256` and re-checked before every harness run. A
  changed control file means the run reports "could not verify", never a pass.
  Absence is pinned too, so a control file cannot be *created* later unnoticed.
  This is tamper-evident, not tamper-proof: the manifest shares the workspace.
- **The frozen statements are bound to what is audited.** For every frozen
  theorem the harness generates and compiles
  `example : @P.<t> = @P.Solution.<t> := rfl`, so the declaration whose axioms are
  checked provably has the frozen statement's type.

## Configuration (`config.json`)

The scripts read `config.json` next to `formlib.py`; the checked-in file holds the
defaults.

| Key | Default | Meaning |
| --- | ------- | ------- |
| `agent_cli` | `claude` | `claude` for `claude -p`, or `codex` for `codex exec` |
| `claude_bin` / `codex_bin` | `claude` / `codex` | path to the chosen CLI |
| `model` | `null` | model passed to the CLI (or use `--model`) |
| `stall_window` | `16` | stop if no new frozen theorem is discharged for this many consecutive iterations |
| `crux_recur_limit` | `16` | stop if one crux is named as the "Next:" step this many times without closing |
| `timeouts.{setup,init,plan,worker,review,verify}` | see below | per-agent wall-clock caps, in seconds |

Each agent is guarded by a **per-agent wall-clock watchdog** (a total-runtime cap,
not an idle timeout): `setup` `3600`, and `init` / `plan` / `worker` / `review`
`10800` each; `verify` `3600`. A cold Mathlib `cache get` + build makes `init` slow, so its cap is
generous. Workers run in parallel, so one iteration's worst case ≈
`plan + worker + review`.

To use Codex instead of Claude, set `{"agent_cli": "codex"}`. Either way the loop
is **non-interactive** — Claude runs with `--dangerously-skip-permissions`, Codex
with `--dangerously-bypass-approvals-and-sandbox`, so the agents edit files and run
shell commands without prompting. **Run it only in a directory you trust.**

## Flags

- `--dry-run` (all scripts): print the exact prompt/command without calling the CLI.
- `--model NAME`: override the model for that run.
- `loop.py --max-iterations N`: cap iterations (unlimited by default).

## Logs & layout

Every agent's full output is streamed to the console and saved under
`your-problem-dir/logs/orchestration/<label>.log`.

| File | Role |
| ---- | ---- |
| `setup.py` | step 1 — scaffold orchestration files |
| `init.py` | step 2 — create the Lean project & freeze the skeleton |
| `loop.py` | step 3 — Plan / Workers / Review loop |
| `formlib.py` | shared agent runner, parallel launcher, file parsers |
| `config.json` | pipeline configuration |
| `reference/` | bundled worked example whose format is copied |

## License

Released under the [MIT License](LICENSE).
