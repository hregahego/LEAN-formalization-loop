#!/usr/bin/env python3
"""
loop.py — drive the autonomous formalization loop.

Usage:
    python3 loop.py [TARGET_DIR] [--max-iterations N] [--start K]

Preconditions:
    setup.py and init.py have been run (BLUEPRINT.md, frozen skeleton, TASKS.md,
    REVIEW.md, PROGRESS.md all present).

Runs UNLIMITED iterations by default (until COMPLETE or paused/stopped) — set
--max-iterations N for an explicit cap. Designed for long overnight sessions.

Pausing / stopping between iterations (checked at each iteration boundary):
    * create a file  <TARGET_DIR>/PAUSE  -> the loop waits (polling) until you
      remove it, then continues in the same process.
    * create a file  <TARGET_DIR>/STOP   -> the loop stops gracefully and exits;
      the file is consumed. Re-run loop.py to resume from where it left off.
    * Ctrl-C / SIGTERM once -> graceful stop at the next boundary; again -> quit.
Because the loop resumes from (highest iteration in TASKS.md) + 1, a graceful
stop and a later re-run continue seamlessly.

Each iteration:
    1. PLAN agent   reads REVIEW.md, PROGRESS.md, SKETCH.md, BLUEPRINT.md and
                    APPENDS a "## Iteration N" block to TASKS.md, one "Agent k:"
                    line per active worker (inactive workers omitted).
    2. WORKERS      up to 4 worker agents run IN PARALLEL, each reads its
                    "Agent k:" task from TASKS.md, does the work, and APPENDS a
                    timestamped report to PROGRESS.md.
    3. REVIEW agent reads PROGRESS.md, audits the work against SKETCH.md /
                    BLUEPRINT.md / TASKS.md by re-running the build & verify.sh,
                    and APPENDS a "## Review -- Iteration N" block (with a
                    Verdict: COMPLETE | INCOMPLETE line) to REVIEW.md.
                    Every 5th iteration this is a FULL-PROJECT audit.

The loop ends when a Review verdict is COMPLETE; a final full-project audit is
then run and its findings reported. The loop is resumable: by default it
continues from (highest iteration in TASKS.md) + 1.

STUCK termination (exit code 3): if the Plan agent assigns NO workers for an
iteration (an empty "## Iteration N" block / empty active_agents list), the loop
stops immediately instead of spinning. Each agent is a fresh, context-free
session, so a no-assignment iteration means the planner found no productive,
dependency-respecting work left — the next iteration would be identical. This is
the "hit a wall, needs human help" signal: inspect REVIEW.md / PROGRESS.md, then
either give a hint, permit an assumed certificate in USER_NOTES.md, fix the
blocker by hand, or relax the plan, and re-run loop.py to resume.

STALL termination (exit code 4): the Plan agent can nearly always invent one more
"support lemma", so the exit-3 valve rarely fires even when the loop is making no
real progress. Independently of the agents, the loop measures the ONLY thing that
counts — how many frozen theorems are discharged in Solution.lean — and records
it to logs/orchestration/progress_ledger.json each iteration. If that number does
not increase for STALL_WINDOW consecutive iterations, or if a single crux keeps
being named as the "Next:" step (>= CRUX_RECUR_LIMIT times) without ever closing,
the loop stops. This catches the "reduce the goal to an equivalent goal forever"
failure mode. Same remedy as exit 3: attack the named crux directly, fix a
false/blocked frozen statement, add a certificate, or change strategy; delete the
ledger to reset the stall window, then re-run loop.py.
"""

from __future__ import annotations

import argparse
import os
import signal
import time

import formlib as F


# --------------------------------------------------------------------------- #
# Pause / stop control (checked between iterations)
# --------------------------------------------------------------------------- #

PAUSE_FILE = "PAUSE"
STOP_FILE = "STOP"

# Objective stall guards (repo-derived, independent of what the agents claim).
# STALL_WINDOW: stop if the number of frozen theorems discharged in Solution.lean
#   has not increased across this many consecutive iterations. This is the PRIMARY
#   guard. Because the signal is coarse (0..5) it will not move while the loop
#   grinds legitimately through a multi-iteration wall, so a fire here is a
#   "check in" as much as a "stuck" — raise it if a hard wall genuinely needs more
#   than this many iterations of honest work.
# CRUX_RECUR_LIMIT: SECONDARY guard — stop if one crux identifier is named as the
#   "Next:" step this many times *within the current run* (see recurring_crux's
#   since_iteration; mentions from before a resume do not count). Tuned for a
#   per-run window, not cumulative append-only history.
# Patient defaults: research-grade walls routinely take many honest iterations of
# genuine (crux-advancing) reduction before a leaf is discharged, so give the loop
# room before flagging. These are the twitchiness knobs — lower them to stop sooner
# on a suspected circle, raise them to tolerate longer honest grinds. Set them in
# config.json ("stall_window" / "crux_recur_limit"); the defaults below apply when
# the keys are absent.
STALL_WINDOW = int(F.CONFIG.get("stall_window", 16))
CRUX_RECUR_LIMIT = int(F.CONFIG.get("crux_recur_limit", 16))

_stop_requested = {"v": False}


def _install_signal_handlers():
    def handler(signum, frame):
        if _stop_requested["v"]:
            F.log("loop: second signal received — exiting immediately.")
            os._exit(130)
        _stop_requested["v"] = True
        F.log("loop: stop requested (signal) — will finish at the next iteration "
              "boundary. Send the signal again to force-quit.")
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # e.g. not in the main thread


def _consume(path):
    try:
        os.remove(path)
    except OSError:
        pass


def honor_pause_stop(target: str, poll_interval: int) -> str | None:
    """
    Called at an iteration boundary. Returns 'stop' if a graceful stop was
    requested (signal or STOP file), else None once any PAUSE has been lifted.
    """
    stop_path = os.path.join(target, STOP_FILE)
    pause_path = os.path.join(target, PAUSE_FILE)

    def stop_now():
        return _stop_requested["v"] or os.path.exists(stop_path)

    if stop_now():
        _consume(stop_path)
        return "stop"

    if os.path.exists(pause_path):
        F.log(f"loop: PAUSE file present ({pause_path}) — paused between iterations. "
              "Remove it to resume; create a STOP file or signal to stop.")
        while os.path.exists(pause_path):
            if stop_now():
                _consume(stop_path)
                return "stop"
            time.sleep(poll_interval)
        F.log("loop: PAUSE lifted — resuming.")
    return None


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

PLAN_PROMPT = """\
You are the PLAN agent for iteration {n} of an autonomous Lean 4 + Mathlib
formalization, coordinating up to 4 parallel WORKER agents (Agent 1..4).

Read, in this order (do not skip any):
  ./REVIEW.md     -- the auditor's verdicts/findings on prior iterations; its
                     "Required follow-ups" are your highest priority.
  ./PROGRESS.md   -- append-only log: what is ✅ proved, 🔧 in progress,
                     ⚠️ blocked, 📝 decided.
  ./SKETCH.md     -- the mathematical source of truth.
  ./BLUEPRINT.md  -- the Lean decomposition: stages, dependency order, and the
                     per-stage cheat-watch boxes.
  ./USER_NOTES.md -- the user's special instructions (e.g. any assumed-certificate
                     axioms permitted for this problem). Honor them.

Decide the single most valuable BATCH of work for THIS iteration that respects
the dependency graph in BLUEPRINT (never assign work whose prerequisites are not
yet ✅; first clear any "Required follow-ups" from REVIEW.md). Split it across up
to 4 workers with NON-OVERLAPPING files so they run in parallel without
colliding. If the next useful step needs fewer than 4 workers, assign only those
(e.g. only Agent 1 and Agent 2) and omit the rest.

Prefer work that makes NET PROGRESS. Reductions are welcome and often essential —
breaking a hard goal into simpler pieces is how a formalization advances — so do
NOT refuse a task just because it is a reduction. A reduction COUNTS as progress
when it lands the goal on something STRICTLY SIMPLER: fewer hypotheses or
quantifiers, a more elementary or explicit object, a step now reducible to a named
Mathlib lemma, or one abstraction layer removed (best of all, it discharges a leaf
outright). What does NOT count is a LATERAL re-expression: replacing a goal by an
EQUIVALENT of the same difficulty — the equivalence cheap in BOTH directions, the
real analytic/combinatorial content untouched and merely renamed. That is the
"re-wrapping" that silently stalls a loop.

A single reduction rarely reveals which kind it is — both look like "G became G'".
The distinction is visible only over TIME, against history, which a fresh worker
cannot see but you can: read PROGRESS.md. If the crux a proposed task would produce
is inter-derivable with a crux already named as a "Next:" step in earlier
iterations (the same value/bound/identity wearing a new name), that goal is
CIRCLING — do not assign another lap. If the task lands a genuinely NEW,
strictly-simpler crux, or discharges a leaf, assign it. An objective stall guard
also stops the loop when one crux recurs across the run, so you need not be
paranoid — just do not KNOWINGLY assign a lateral re-expression of a crux already
reduced before.

Before assigning, for each remaining BLUEPRINT goal state its precise CURRENT crux
and ask: is there a step that discharges a leaf or produces a crux STRICTLY simpler
than everything already recorded for that goal? Assign workers to the goals that
pass; a hard/large/slow goal with such a step IS workable.

Assign NO workers ("Agent k:" list omitted entirely) only when every remaining goal
is walled: for each you can name the precise crux, and no available step yields a
strictly-simpler crux or discharges a leaf — the only moves are lateral
re-expressions of an already-reduced crux, OR the crux needs unstated mathematics,
a false/blocked frozen statement, or a certificate the user has not granted. You do
NOT need prior REVIEW.md corroboration; if you can name the wall for every goal and
no genuinely-simplifying step remains, stop now rather than assigning make-work.
Make the "## Iteration {n}" block a concise STUCK note: for each remaining goal
state the precise crux and what a human must change (a hint, a new strategy, the
missing mathematical content, a fix to a false frozen statement, or an assumed
certificate for USER_NOTES.md). "The next step is a reduction" is NOT a reason to
stop; only "the next step is a LATERAL reduction of an already-reduced crux" is.

APPEND (never edit or delete prior content) to ./TASKS.md EXACTLY this block,
using this exact format so the orchestrator can parse it:

## Iteration {n}
<one or two lines: the goal of this iteration and which BLUEPRINT stage(s) it advances>

Agent 1: <files this agent OWNS (exact paths under Proofs/) + the lemma/theorem names to produce + the path to follow + which already-✅ results it may use + the relevant SKETCH step and BLUEPRINT cheat-watch it must respect>
Agent 2: <...>
Agent 3: <...>
Agent 4: <...>

Rules:
- Include an "Agent k:" line ONLY for workers active this iteration. Number them
  from 1 with no gaps (use Agent 1, or Agent 1 + Agent 2, etc.).
- Every task must be SELF-CONTAINED: a fresh agent with no memory must be able to
  execute it from TASKS.md plus the named BLUEPRINT/SKETCH sections alone. Name
  exact files and lemma names.
- NEVER instruct a worker to edit Defs.lean or Theorems.lean, to weaken a frozen
  statement, or to use a banned tactic.
- Do NOT write proofs yourself. Only append the TASKS.md block.

Finally, end your reply with a machine-readable trailer as the VERY LAST lines of
your message — emit it exactly once, listing precisely the agent numbers you
activated this iteration:

<<<ORCH
{{"iteration": {n}, "active_agents": [<the agent numbers you assigned, e.g. 1, 2>]}}
ORCH>>>

The list MUST match the "Agent k:" lines you appended (empty list `[]` if you
assigned no workers).
"""

WORKER_PROMPT = """\
You are WORKER Agent {k} for iteration {n} of an autonomous Lean 4 + Mathlib
formalization. Your label for the log is "agent-iter{n}-{k}".

ONBOARDING RITUAL (BLUEPRINT §5) -- do this BEFORE writing any code:
1. Read ./TASKS.md and find "## Iteration {n}", then the line "Agent {k}: ...".
   That line is YOUR assignment. Ignore the other agents' lines.
2. Read ./PROGRESS.md end-to-end. Respect every ✅ (done -- reuse, don't redo),
   🔧 (another agent holds it -- do NOT touch), ⚠️ (blocked), and 📝 (a fixed
   modeling/proof decision you must follow).
3. Read the BLUEPRINT.md stage(s) your task names -- INCLUDING the "Cheat watch"
   box -- and the cited SKETCH.md step(s). Also skim ./USER_NOTES.md and
   ./scripts/ALLOWED_AXIOMS.txt: the ONLY axioms you may depend on are the
   standard three plus any names listed there (assumed certificates frozen in
   Defs.lean). You may USE those; you may NOT introduce any new axiom.
4. Append a `🔧 in progress` PROGRESS.md entry claiming your work (real UTC
   timestamp from `date -u +"%Y-%m-%dT%H:%M:%SZ"`), so the other 3 agents don't
   collide with you.

THEN DO THE WORK:
- Prioritize a FAITHFUL formalization above all else. Never weaken or trivialize
  a frozen statement; never edit Defs.lean or Theorems.lean; never add a
  hypothesis to a frozen statement; never use a banned tactic (no `sorry` outside
  Theorems.lean, no native_decide, admit, unsafe, implemented_by, ofReduceBool).
  Do NOT introduce any new `axiom`. Keep `#print axioms` of anything you prove
  within {{propext, Classical.choice, Quot.sound}} PLUS any axiom names listed in
  ./scripts/ALLOWED_AXIOMS.txt (the user-permitted certificates); any OTHER axiom
  is forbidden and will fail verify.sh.
- Work ONLY on the file(s) your task assigns, to avoid colliding with the other
  workers running in parallel right now.
- Use the Lean tooling: edit, `lake build` your target module, read the goal /
  search Mathlib, and iterate until your files compile cleanly -- OR until you
  hit a GENUINE blocker (a real mathematical or Lean obstacle, not impatience or
  a long build). Work straight through to completion; do NOT stop to ask
  questions.

WHEN FINISHED (success OR genuine blocker), APPEND to ./PROGRESS.md a timestamped
entry in BLUEPRINT's mandated format:

  ## <UTC timestamp> -- <stage/item you worked on>
  Agent: agent-iter{n}-{k}
  Status: ✅ proved | ⚠️ blocked | 🔧 in progress | 📝 decision
  Check: <#print axioms result, or lake build result, or n/a>
  Note: <what you did, key lemma used, or the EXACT failing goal/error that blocks you>
  Next: <what this unblocks / what a follow-up agent should do, with exact lemma & file names>

Only mark `✅` what ACTUALLY compiles with a clean `#print axioms` -- never fake a
✅. Finally, print a one-paragraph report of what you accomplished or what blocked
you.
"""

REVIEW_PROMPT = """\
You are the REVIEW / AUDIT agent for iteration {n} of an autonomous Lean 4
formalization. You are INDEPENDENT of the workers and deliberately SKEPTICAL and
ADVERSARIAL: assume a ✅ is wrong until you have reproduced it yourself.

Read: ./PROGRESS.md (the workers' latest entries for iteration {n}), ./TASKS.md
(what was assigned this iteration), ./SKETCH.md + ./BLUEPRINT.md (the
faithfulness ground truth and the cheat-watch boxes), and ./USER_NOTES.md +
./scripts/ALLOWED_AXIOMS.txt (the axioms the user explicitly permitted, if any).

Audit by running the ACTUAL tooling -- do NOT trust PROGRESS.md's claims. Open
the Lean files, run `lake build`, run `#print axioms`, and run
`scripts/verify.sh` yourself. Specifically check:
- Does every ✅ from this iteration actually compile, with a clean `#print axioms`?
  "Clean" = within {{propext, Classical.choice, Quot.sound}} PLUS exactly the
  axiom names listed in scripts/ALLOWED_AXIOMS.txt. Flag any faked ✅, any axiom
  NOT on that allowlist, and any allowed axiom whose Lean statement does not
  faithfully match what USER_NOTES.md describes.
- Are Defs.lean and Theorems.lean still byte-frozen (the SHA pins in
  scripts/frozen.sha256 still match)? Flag any tampering with frozen files or
  with earlier PROGRESS.md history.
- Any banned tactic? Any frozen statement that was weakened, given an extra
  hypothesis, specialized from `∀` to examples, or had an equality replaced by a
  one-sided inclusion? Any definition that secretly trivializes the math vs
  SKETCH.md? Any `sorry` outside Theorems.lean?
- Did workers respect file ownership and append-only PROGRESS.md?
- NET PROGRESS vs RE-WRAPPING: reductions ARE legitimate progress when they land a
  strictly-simpler crux (fewer hypotheses/quantifiers, a more elementary object, a
  named-library step, or a discharged leaf) — do NOT flag those as scaffolding.
  Flag only LATERAL re-wrapping: a ✅ whose "Next:" crux is inter-derivable with a
  crux already named as the "Next:" step in earlier iterations (the same
  value/bound/identity renamed, the hard content untouched). Compare this
  iteration's "Next:" cruxes against earlier PROGRESS.md entries: if the SAME crux
  has been the stated next step across several iterations while only equivalent
  re-expressions land around it, the loop is CIRCLING A WALL — say so explicitly,
  name the crux, and count the iterations it has recurred.
{full_block}
APPEND (append-only) to ./REVIEW.md EXACTLY this block:

## Review -- Iteration {n}{full_tag}
Auditor: review-iter{n}
Checks run: <the verify.sh / lake build / #print axioms commands you actually ran and their results>
Findings: <bullets: confirmed-good items, AND every cheat / regression / faked ✅ / faithfulness gap, each with file:line; AND a NET-PROGRESS verdict for the iteration — "net progress toward <goal>" or "SCAFFOLDING ONLY: circled crux `<name>` for N iterations">
Required follow-ups: <concrete fixes the Plan agent must assign next iteration, or "none". When the iteration was scaffolding-only on a recurring wall, write "STALLED on `<crux>`: <the direct mathematical attempt needed, or the human decision required>" so the planner does not re-assign more indirection.>
Verdict: COMPLETE | INCOMPLETE

Set "Verdict: COMPLETE" ONLY IF ALL of these hold: every frozen theorem is proved
sorry-free; `scripts/verify.sh` passes with 0 issues (clean `#print axioms` for
every Solution.<name>, frozen SHA pins intact, Discharge.lean + Solution.lean
gates compile); and the formalization faithfully matches SKETCH.md with no
detected cheat or weakening. OTHERWISE set "Verdict: INCOMPLETE". Be
conservative: when in any doubt, INCOMPLETE.

Finally, end your reply with a machine-readable trailer as the VERY LAST lines of
your message — emit it exactly once, and its verdict MUST equal the "Verdict:"
line you appended to REVIEW.md:

<<<ORCH
{{"iteration": {n}, "verdict": "COMPLETE"}}
ORCH>>>

(use "INCOMPLETE" instead of "COMPLETE" when not done). Before the trailer, print
a one-paragraph summary of your audit.
"""

FULL_AUDIT_BLOCK = """\
FULL-PROJECT AUDIT (this is a checkpoint/final audit): review the ENTIRE
formalization end-to-end, not just this iteration. Re-run `scripts/verify.sh` over
ALL theorems, re-read every frozen statement in Theorems.lean against SKETCH.md
for faithfulness, and hunt for any LARGE-SCALE trivialization, cheat, or
weakening that may have accumulated across iterations (a definition quietly
softened, a `∀` that became a special case, an axiom-dirty proof, a one-sided
inclusion standing in for an equality). Report everything you find.
"""


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #

def run_plan(target: str, n: int, model, dry_run, log_dir):
    # JSON mode: we read the active-agents trailer straight from the result.
    return F.run_agent(
        f"iter{n:03d}-plan", PLAN_PROMPT.format(n=n), cwd=target,
        model=model, timeout=F.PLAN_TIMEOUT, log_dir=log_dir, dry_run=dry_run,
        output_format="json",
    )


def run_workers(target: str, n: int, agents: list[int], model, dry_run, log_dir):
    specs = []
    for k in agents:
        specs.append(dict(
            label=f"iter{n:03d}-worker{k}",
            prompt=WORKER_PROMPT.format(n=n, k=k),
            cwd=target, model=model, timeout=F.WORKER_TIMEOUT,
            log_dir=log_dir, dry_run=dry_run,
        ))
    return F.run_agents_parallel(specs, max_workers=4)


def run_review(target: str, n: int, full: bool, model, dry_run, log_dir):
    prompt = REVIEW_PROMPT.format(
        n=n,
        full_block=(FULL_AUDIT_BLOCK + "\n") if full else "",
        full_tag="  (FULL PROJECT AUDIT)" if full else "",
    )
    label = f"iter{n:03d}-review" + ("-FULL" if full else "")
    # JSON mode: we read the verdict trailer straight from the result.
    return F.run_agent(
        label, prompt, cwd=target,
        model=model, timeout=F.REVIEW_TIMEOUT, log_dir=log_dir, dry_run=dry_run,
        output_format="json",
    )


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Run the autonomous formalization loop.")
    ap.add_argument("target", nargs="?", default=".", help="target directory (default: .)")
    ap.add_argument("--max-iterations", type=int, default=None,
                    help="optional safety cap on iterations (default: unlimited)")
    ap.add_argument("--start", type=int, default=None,
                    help="iteration number to start at (default: resume from TASKS.md)")
    ap.add_argument("--poll-interval", type=int, default=20,
                    help="seconds between PAUSE-file checks while paused (default 20)")
    ap.add_argument("--model", default=None, help="override the claude model")
    ap.add_argument("--dry-run", action="store_true", help="print prompts/commands, do not call claude")
    args = ap.parse_args()

    _install_signal_handlers()

    target = F.resolve_target(args.target)
    F.require_sketch(target)
    F.require_blueprint(target)

    tasks_path = os.path.join(target, "TASKS.md")
    review_path = os.path.join(target, "REVIEW.md")
    for required in (tasks_path, review_path):
        if not os.path.isfile(required):
            return F.log(f"ERROR: {required} missing — run setup.py first.") or 1

    log_dir = os.path.join(target, "logs", "orchestration")

    start = args.start if args.start is not None else F.highest_iteration(tasks_path) + 1
    # Highest iteration that already existed when THIS run started. The recurrence
    # guard counts only cruxes circled AFTER this point, so a wall resolved (proved
    # or certificated) between runs does not re-fire the guard on iteration 1 from
    # append-only PROGRESS.md history.
    resume_baseline = start - 1
    last = (start + args.max_iterations - 1) if args.max_iterations else None
    F.log(f"loop: target={target}")
    if last is None:
        F.log(f"loop: starting at iteration {start}, unlimited (until COMPLETE or paused/stopped)")
    else:
        F.log(f"loop: starting at iteration {start}, cap {args.max_iterations} (through {last})")
    F.log(f"loop: pause with `touch {os.path.join(target, PAUSE_FILE)}`, "
          f"stop with `touch {os.path.join(target, STOP_FILE)}`")

    n = start
    while last is None or n <= last:
        # Iteration boundary: honor a pause/stop request before starting work.
        if not args.dry_run and honor_pause_stop(target, args.poll_interval) == "stop":
            F.log(f"loop: graceful stop before iteration {n}. "
                  f"Re-run `python3 loop.py {target}` to resume from here.")
            return 0

        F.log(f"================  ITERATION {n}  ================")

        # 1. PLAN
        plan_result = run_plan(target, n, args.model, args.dry_run, log_dir)

        # Which workers did the Plan agent activate? Trailer first (scoped to this
        # iteration), iteration-scoped TASKS.md parse as fallback.
        if args.dry_run:
            agents = [1, 2, 3, 4]
            F.log("loop: [dry-run] assuming agents 1-4 active")
        else:
            agents = F.plan_active_agents(plan_result, tasks_path, n)
            if not agents:
                # No assignments => the Plan agent found no productive,
                # dependency-respecting work left. Because every agent is a fresh
                # context-free session, the NEXT iteration would be identical: the
                # loop is stuck on a wall that needs human intervention (a new
                # strategy, a USER_NOTES.md certificate, or a relaxed plan). Stop
                # gracefully now rather than spinning no-op iterations forever.
                F.log(f"loop: Plan agent assigned NO workers for iteration {n} — "
                      "the loop is STUCK (no productive work left).")
                F.log("loop: stopping. Inspect REVIEW.md (latest 'Required "
                      "follow-ups') and PROGRESS.md (the ⚠️ blockers) to see what "
                      "wall was hit. Options: provide a hint/strategy, allow an "
                      "assumed certificate in USER_NOTES.md, or fix the blocker by "
                      "hand, then re-run loop.py to resume.")
                return 3
        F.log(f"loop: active workers this iteration: {agents}")

        # 2. WORKERS (parallel)
        if agents:
            results = run_workers(target, n, agents, args.model, args.dry_run, log_dir)
            for r in results:
                if not r.ok:
                    F.log(f"loop: worker {r.label} did not exit cleanly "
                          f"({'timeout' if r.timed_out else r.returncode}); "
                          "its PROGRESS.md entry (if any) will be audited by Review.")

        # 3. REVIEW (full-project audit every 5th iteration)
        full = (n % 5 == 0)
        review_result = run_review(target, n, full, args.model, args.dry_run, log_dir)

        if args.dry_run:
            F.log("loop: [dry-run] stopping after one iteration.")
            return 0

        # Verdict from THIS review's trailer (scoped to iteration n), with an
        # iteration-scoped REVIEW.md parse as fallback — never the whole file.
        verdict = F.review_verdict(review_result, review_path, n)
        F.log(f"loop: review verdict after iteration {n}: {verdict}")

        if verdict == "COMPLETE":
            F.log("loop: Review reports COMPLETE — running final full-project audit.")
            final = run_review(target, n, full=True, model=args.model,
                               dry_run=False, log_dir=log_dir)
            final_verdict = F.review_verdict(final, review_path, n)
            F.log(f"loop: final audit verdict: {final_verdict}")
            if final_verdict == "COMPLETE":
                F.log("==================================================")
                F.log("FORMALIZATION COMPLETE. Final findings in REVIEW.md.")
                F.log("Run scripts/verify.sh to confirm independently.")
                F.log("==================================================")
                return 0
            else:
                F.log("loop: final audit DOWNGRADED the verdict to INCOMPLETE. "
                      "Continuing the loop to address its follow-ups.")
                # fall through to next iteration

        # -- Objective stall guard (agent-independent) --------------------- #
        # The Plan agent can almost always invent one more "support lemma", so
        # the empty-assignment STUCK exit rarely fires. Independently measure the
        # ONLY thing that matters — frozen theorems discharged in Solution.lean —
        # and stop if it has flat-lined, or if a single crux keeps being named as
        # the next step while nothing closes it.
        signal = F.progress_signal(target)
        ledger = F.record_progress(target, n, signal)
        n_frozen = len(F._solution_frozen_names(target))
        F.log(f"loop: progress signal after iteration {n}: "
              f"{signal}/{n_frozen} frozen theorems discharged in Solution.lean")

        # Recurrence guard: count only cruxes circled SINCE this run started, so a
        # wall resolved between runs does not re-fire from append-only history.
        crux = F.recurring_crux(os.path.join(target, "PROGRESS.md"),
                                CRUX_RECUR_LIMIT, since_iteration=resume_baseline)
        if F.stalled_for(ledger, STALL_WINDOW):
            F.log(f"loop: STALLED — no new frozen theorem discharged in the last "
                  f"{STALL_WINDOW} iterations (signal stuck at {signal}/{n_frozen}). "
                  "The loop is producing scaffolding without net progress.")
            if crux:
                F.log(f"loop: since this run started, the crux `{crux[0]}` has been "
                      f"named as the next step {crux[1]} times — the wall to break.")
            F.log("loop: stopping (exit 4). This needs human intervention: attack "
                  "the named crux directly, fix a false/blocked frozen statement, "
                  "add an assumed certificate to USER_NOTES.md, or change strategy. "
                  "Delete logs/orchestration/progress_ledger.json to reset the "
                  "stall window after you have changed something.")
            return 4
        if crux and crux[1] >= CRUX_RECUR_LIMIT:
            F.log(f"loop: STALLED — since this run started, the crux `{crux[0]}` has "
                  f"been named as the 'Next:' step {crux[1]} times; the loop is "
                  "circling one wall instead of closing it.")
            F.log("loop: stopping (exit 4). Attack that crux directly or change "
                  "strategy, then re-run to resume.")
            return 4

        n += 1

    F.log(f"loop: reached the iteration cap ({args.max_iterations}) without a "
          "confirmed-complete verdict. Inspect REVIEW.md / PROGRESS.md and "
          "re-run loop.py to continue (the default is now unlimited).")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
