# Autonomous Lean 4 formalization pipeline

Turn a natural-language proof sketch into a fully verified **Lean 4 + Mathlib**
formalization, autonomously.

Given a proof sketch, the pipeline scaffolds a Lean project, freezes the definitions and theorem
statements, and then runs a Plan → Workers → Review loop of agents that fill
in every proof until the whole development compiles with no `sorry` and clean
`#print axioms`.

Every agent in the pipeline is a headless CLI subprocess
(Claude Code or Codex); the Python scripts only orchestrate them and parse the
shared markdown files that the agents read and write. There are no runtime
dependencies beyond Python's standard library and the agent CLI you choose.

---

## ⚠️ You must supply a detailed, complete proof sketch

**This is the single most important input to the pipeline, and the one thing it
cannot do for you.** The agents formalize a proof; they do not discover one. The
quality, correctness, and completeness of the final Lean development is bounded
by the quality of the sketch you write in `SKETCH.md`.

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
mathematics become gaps or unfaithfulness in the formalization.
Treat writing the sketch as writing the proof: if a human mathematician reading
only your sketch could not reconstruct the full argument, neither can the
pipeline. See [`reference/SKETCH.md`](reference/SKETCH.md) for the expected
shape.

---

## Requirements

- **Python 3.9+**
- **Coding-agent CLI**
  - [Claude Code](https://claude.com/claude-code) (`claude`) — the default, or
  - [Codex](https://github.com/openai/codex) (`codex`) — set `agent_cli: "codex"`
- **Lean 4 toolchain** via [`elan`](https://github.com/leanprover/elan) 

## Install

```bash
git clone https://github.com/hregahego/LEAN-formalization-loop.git 
cd LEAN-formalization-loop
```

The scripts are run from within the directory. Make sure your chosen agent CLI is on your
`PATH` (or set `claude_bin` / `codex_bin` in `config.json`) and that you are
logged in.

## Quick start

```
your-problem-dir/
  SKETCH.md          <-- problem statement + complete proof sketch
```

```bash
# 1. Scaffold the orchestration files from SKETCH.md
python3 setup.py your-problem-dir

# 2. Create the Lean project + freeze Defs.lean / Theorems.lean (Step -1)
python3 init.py your-problem-dir

# 3. Run the autonomous Plan -> 4 Workers -> Review loop until complete
python3 loop.py your-problem-dir
```

The directory argument defaults to `.`, so you can also `cd your-problem-dir`
and run `python3 /path/to/setup.py` etc.

`setup.py` derives the format (BLUEPRINT/PROGRESS/scripts layout, the
frozen-statement + SHA-pin + `#print axioms` anti-cheat regime) from a blank 
**reference project**. This reference is contained in
[`reference/`](reference/).

### What each step produces

| Step | Script | Creates / does |
| ---- | ------ | -------------- |
| 1 | `setup.py` | `BLUEPRINT.md` (Part -1 SETUP, file layout, frozen Defs & Theorems, per-stage cheat-watches), `PROGRESS.md` (append-only log header), `scripts/verify.sh` (5-check harness) + `scripts/frozen.sha256` (placeholder), `TASKS.md`, `REVIEW.md`, `USER_NOTES.md` — all in the reference format. |
| 2 | `init.py` | Executes BLUEPRINT Step -1: `lake new` + Mathlib, writes the frozen `Defs.lean` and `Theorems.lean` (`:= sorry`), the `Proofs/<Stage>/` tree, `Discharge.lean` / `Solution.lean` / root import, makes the skeleton build, records the real SHA pins, logs the SETUP entries. **No proofs written.** Then runs the **faithfulness gate** (below). |
| 3 | `loop.py` | The iteration loop (below). |

### Special instructions (`USER_NOTES.md`)

`setup.py` also writes `USER_NOTES.md`, the user-editable file for
problem-specific guidance — read by `init.py` and `loop.py`. **Fill it in before
running `init.py`.** Its most important use is declaring *assumed-certificate
axioms*: if a fact is mathematically routine but prohibitively expensive to
*prove* in Lean (a large factorization, an explicit interpolant, a numeric
certificate), you may permit it as a Lean `axiom` here. `verify.sh` then allows
exactly those named axioms and bans all others. By default the pipeline is
maximally strict and permits none.

### The faithfulness gate (`init.py`)

`Defs.lean` + `Theorems.lean` are where cheating is cheapest and most damaging: a
weakened definition, a `∀` specialized to examples, an equality softened to an
inclusion, or a vacuous headline makes every later "proof" trivial while still
passing the build and the `#print axioms` checks. So immediately after the freeze
(and **before** any proving), `init.py` runs an **independent adversarial
auditor** that reads the actual frozen `Defs.lean` / `Theorems.lean` and judges
each definition and theorem against `SKETCH.md` (math truth) and `BLUEPRINT.md`
(the plan), appending a `## Review -- INIT faithfulness audit` block to
`REVIEW.md` with a per-item verdict.

If it finds defects, a **bounded repair loop** runs a fix-and-re-freeze agent
(legitimate here, since nothing is proved yet — re-freezing updates the SHA
pins), then re-audits. 

- `--no-repair` — audit only; report defects without auto-fixing.
- `--max-faithfulness-attempts N` — audit/repair rounds (default 2).
- `--skip-faithfulness-review` — skip the gate entirely (not recommended).

### The loop (`loop.py`)

Each iteration:

1. **Plan agent** — reads `REVIEW.md`, `PROGRESS.md`, `SKETCH.md`, `BLUEPRINT.md`;
   appends a `## Iteration N` block to `TASKS.md` with one `Agent k: …` line per
   active worker (inactive workers are omitted). Append-only.
2. **Worker agents** — up to 4, launched **in parallel**. Each reads its
   `Agent k:` task from `TASKS.md`, does a faithful formalization on the files it
   owns, works until done or a genuine blocker, and appends a timestamped report
   to `PROGRESS.md`.
3. **Review agent** — re-runs the build / `#print axioms` / `verify.sh`, audits
   the work against `SKETCH.md` / `BLUEPRINT.md` / `TASKS.md`, and appends a
   `## Review -- Iteration N` block to `REVIEW.md` ending in
   `Verdict: COMPLETE | INCOMPLETE`. **Every 5th iteration is a full-project
   audit** to catch large-scale trivializations.

The loop ends when a verdict is `COMPLETE`; it then runs a **final
full-project audit** and reports. It is **resumable** — by default it continues
from `(highest iteration in TASKS.md) + 1`, so you can stop and re-run `loop.py`.

**Unlimited by default.** `loop.py` runs until `COMPLETE` (or paused/stopped) with
no iteration cap — built for long overnight sessions on hard problems. Pass
`--max-iterations N` for an explicit cap.

**Pause / stop between iterations.** At each iteration boundary the loop checks
for control files in `TARGET_DIR`:

| Action | How | Effect |
| ------ | --- | ------ |
| Pause  | `touch TARGET_DIR/PAUSE` | loop waits (polling every `--poll-interval`s, default 20) until you `rm` the file, then continues in the same process |
| Stop   | `touch TARGET_DIR/STOP`  | loop stops gracefully and exits; the file is consumed. Re-run `loop.py` to resume |
| Stop   | Ctrl-C / SIGTERM once    | graceful stop at the next boundary (again = force-quit) |

Because the loop resumes from `(highest iteration in TASKS.md) + 1`, a graceful
stop and a later re-run continue seamlessly — including across separate overnight
sessions. Pause/stop take effect at the **next iteration boundary** (a worker
batch already in flight finishes first).

### How the orchestrator reads control signals

The loop never guesses from prose. The **primary** signal is a machine-readable
trailer the Plan and Review agents emit as the last lines of their final message
(captured from the configured agent's final message), each carrying its own iteration
number:

```
<<<ORCH
{"iteration": 7, "active_agents": [1, 2]}     # Plan: which workers to launch
{"iteration": 7, "verdict": "COMPLETE"}       # Review: done or not
ORCH>>>
```

The orchestrator parses this straight from the process it just ran, and only
trusts it when `iteration` matches the current one. The **fallback** (if the
trailer is missing/malformed) is an *iteration-scoped* parse of the append-only
files: `active_agents` reads only the current `## Iteration N` block of
`TASKS.md`, and `verdict_for_iteration` reads only the `## Review -- Iteration N`
block(s) of `REVIEW.md` (taking the last verdict within iteration N, so a
later full-project-audit block wins over the normal review). Because both files
are append-only, this scoping is essential — a stale `COMPLETE` from an earlier
iteration can never be mistaken for the current verdict.

> Workers still run with streaming `text` output so you see their progress live;
> only Plan/Review use `json` (their output is a single result blob anyway).

## Anti-cheat guarantees (enforced by the prompts + `verify.sh`)

Because the agents run unattended, the pipeline is built around the assumption
that an agent may take shortcuts. These invariants make a shortcut fail the build
rather than pass silently:

- `Defs.lean` and `Theorems.lean` are **frozen** and **SHA-pinned**; never edited
  during proving; `sorry` is allowed **only** in `Theorems.lean`.
- Frozen statements must render the sketch **faithfully and minimally** — no
  added hypotheses, no `∀`→examples specialization, no equality→one-sided
  inclusion, no surrogate definitions.
- Banned tactics/keywords: `sorry` (outside `Theorems.lean`), `native_decide`,
  `admit`, `axiom`, `unsafe`, `implemented_by`, `ofReduceBool`. Every solved
  theorem's `#print axioms` must stay within `{propext, Classical.choice,
  Quot.sound}` (plus any axioms you explicitly permit in `USER_NOTES.md`).
- `PROGRESS.md`, `TASKS.md`, `REVIEW.md` are append-only; the Review agent
  independently re-verifies every `✅` rather than trusting the log.

## Configuration (`config.json`)

The scripts read `config.json` next to `formlib.py`. The checked-in file contains
the default values.

| Key | Default | Meaning |
| --- | ------- | ------- |
| `agent_cli` | `claude` | `claude` for `claude -p`, or `codex` for `codex exec` |
| `claude_bin` | `claude` | path to the Claude Code CLI |
| `codex_bin` | `codex` | path to the Codex CLI |
| `model` | `null` | model passed to the selected CLI (or use `--model`) |
| `reference_dir` | `reference` | the worked example whose format is copied |
| `permission_mode` | `skip` | for Claude: `skip`, `acceptEdits`, `bypassPermissions`, `plan`, or `default`; for Codex: `skip`, `bypassPermissions`, `default`, `workspace-write`, `read-only`, or `danger-full-access` |
| `timeouts.setup` | `3600` | architect timeout, in seconds |
| `timeouts.init` | `10800` | init timeout, in seconds (cold Mathlib `cache get` + build is slow) |
| `timeouts.plan` | `10800` | plan-agent timeout, in seconds |
| `timeouts.worker` | `10800` | worker-agent timeout, in seconds |
| `timeouts.review` | `10800` | review-agent timeout, in seconds |
| `claude_extra_args` | `[]` | extra strings appended to every Claude command |
| `codex_extra_args` | `[]` | extra strings appended to every Codex command before the prompt |

To use Codex instead of Claude, set:

```json
{
  "agent_cli": "codex"
}
```

With `permission_mode: "skip"`, Claude uses `--dangerously-skip-permissions` and
Codex uses `--dangerously-bypass-approvals-and-sandbox`, so the loop is
non-interactive. **Run it only in a directory you trust** — the agents execute
shell commands and edit files without prompting.

## Timeouts

Every agent is a configured CLI subprocess guarded by a **per-agent, wall-clock
watchdog**: it is `SIGKILL`-ed once it has run for its timeout, measured from
launch. Key points:

- It is a **total-runtime cap, not an idle timeout** — the agent is killed at the
  limit even if it is still actively producing output. A generous cap protects
  long-but-productive proof searches; but a genuinely hung agent is only reaped
  *after* the full timeout, so don't set it absurdly high.
- The caps are **per agent, not per iteration and not per loop**. Each phase below
  is a separate agent with its own budget, and every iteration gets a fresh
  budget. The loop itself is unbounded (runs until COMPLETE or stopped).
- Timeouts are set in **config.json** (there is no `--timeout` flag).

| Phase / agent | Config key | Default | Used by |
| ------------- | ---------- | ------- | ------- |
| Architect (scaffold) | `timeouts.setup` | `3600` | `setup.py`; also the **repair** sub-phase of `init.py`'s faithfulness gate |
| Build + freeze | `timeouts.init` | `10800` | `init.py` (cold Mathlib `cache get` + build is slow) |
| Plan | `timeouts.plan` | `10800` | `loop.py` (1 agent per iteration) |
| Workers | `timeouts.worker` | `10800` | `loop.py` (up to 4 **in parallel**; the worker phase ends when the slowest finishes) |
| Review | `timeouts.review` | `10800` | `loop.py` (1 per iteration, 2 on every 5th / on completion); also the **audit** sub-phase of `init.py`'s faithfulness gate |

Worst-case wall-clock for **one** loop iteration ≈ `PLAN + WORKER + REVIEW`
(workers run in parallel, so it's the slowest worker, not the sum of four).

### Setting them

Edit `config.json` before running the scripts:

```json
{
  "timeouts": {
    "setup": 7200,
    "init": 10800,
    "plan": 10800,
    "worker": 10800,
    "review": 10800
  }
}
```

## Useful flags

- `--dry-run` (all three scripts): print the exact agent prompt/command without
  calling the configured CLI — good for inspecting what will run.
- `--model NAME`: override the model for that run.
- `loop.py --max-iterations N`: safety cap (unlimited by default).
- `loop.py --start K`: force the starting iteration number.

## Logs

Every agent's full output is streamed to the console (prefixed with its label)
and saved under `your-problem-dir/logs/orchestration/<label>.log`.

## Repository layout

| File | Role |
| ---- | ---- |
| `setup.py` | step 1 — scaffold orchestration files |
| `init.py` | step 2 — execute BLUEPRINT Step -1 (freeze skeleton) |
| `loop.py` | step 3 — Plan / Workers / Review loop |
| `formlib.py` | shared agent runner, parallel launcher, file parsers |
| `config.json` | pipeline configuration (see above) |
| `reference/` | the bundled worked example whose format is copied |

## License

Released under the [MIT License](LICENSE).
