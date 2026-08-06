#!/usr/bin/env python3
"""
loop.py — drive the autonomous formalization loop.

Usage:
    python3 loop.py [TARGET_DIR] [--max-iterations N]

Preconditions:
    setup.py and init.py have been run (BLUEPRINT.md, frozen skeleton, TASKS.md,
    REVIEW.md, PROGRESS.md all present).

Runs UNLIMITED iterations by default (until COMPLETE or stopped) — set
--max-iterations N for an explicit cap. Designed for long overnight sessions.

Stopping between iterations:
    * Ctrl-C / SIGTERM once -> graceful stop at the next iteration boundary;
      again -> force-quit immediately.
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
                    BLUEPRINT.md / TASKS.md by re-running the build & verify.py,
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
import subprocess
import sys

import formlib as F
import signals as S


# --------------------------------------------------------------------------- #
# Stop control (checked between iterations)
# --------------------------------------------------------------------------- #

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


def stop_requested() -> bool:
    """
    Called at an iteration boundary. True if a graceful stop was requested via
    Ctrl-C / SIGTERM.
    """
    return _stop_requested["v"]


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #

_MAX_FAIL_LINES = 40  # failure lines quoted to the Review agent before truncating


def run_verify(target: str, dry_run: bool = False) -> tuple[int, str]:
    """Run scripts/verify.py and return (issues, combined output).

    The ORCHESTRATOR runs the harness, every iteration — never the Review agent.
    Whether the certifying check ran must not depend on an agent's discretion, and
    a worker cannot suppress or reword what it reports. The harness exits with the number of
    failing checks, so the exit code IS the issue count; -1 means the harness
    itself could not be run, which is a failure, not a pass.
    """
    # --dry-run inspects prompts; it does not inspect the target.
    if dry_run:
        return 0, "[dry-run] verify.py not executed"
    path = os.path.join(target, "scripts", "verify.py")
    if not os.path.isfile(path):
        return -1, "scripts/verify.py not found"

    # The harness and its config live in the workspace the workers edit, so a
    # result is only meaningful if they are still the files that were pinned.
    # Checked BEFORE running: a tampered harness must not get to report a pass.
    tampered = F.check_control_manifest(target)
    if tampered:
        return -1, "CONTROL FILES CHANGED SINCE THEY WERE PINNED:\n  " + \
                   "\n  ".join(tampered)
    try:
        proc = subprocess.run([sys.executable, path], cwd=target, capture_output=True, text=True,
                              timeout=F.VERIFY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return -1, f"verify.py timed out after {F.VERIFY_TIMEOUT}s"
    except OSError as exc:
        return -1, f"verify.py could not be run: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    # 64 = pre-flight failure: the harness verified NOTHING. Reporting that as
    # "1 issue" would make it indistinguishable from one ordinary failing check.
    if proc.returncode >= 64:
        return -1, f"verify.py could not run (exit {proc.returncode}):\n{out}"
    return proc.returncode, out


def verify_digest(issues: int, output: str) -> str:
    """The harness output condensed for the Review prompt.

    Keeps every failure and the check headers, drops the PASS spam (which can run
    to hundreds of lines) but records how many passed, so the agent can still see
    the shape of the run.
    """
    if issues < 0:
        return "HARNESS DID NOT RUN: " + output.strip()
    if output.startswith("[dry-run]"):
        return output.strip()
    lines = output.splitlines()
    headers = [l for l in lines if l.startswith("--- Check")]
    fails = [l for l in lines if l.startswith(("FAIL:", "ERROR"))]
    result = [l for l in lines if l.startswith("=== RESULT")]
    n_pass = sum(1 for l in lines if l.startswith("PASS:"))
    out = list(headers)
    if fails:
        out.append("")
        out.extend(fails[:_MAX_FAIL_LINES])
        if len(fails) > _MAX_FAIL_LINES:
            out.append(f"... and {len(fails) - _MAX_FAIL_LINES} more FAIL line(s)")
    out.append("")
    out.append(f"({n_pass} PASS line(s) omitted)")
    out.extend(result or [f"=== exit code {issues} ==="])
    return "\n".join(out)


def run_plan(target: str, n: int, model, dry_run, log_dir):
    # JSON mode: we read the active-agents trailer straight from the result.
    return F.run_agent(
        f"iter{n:03d}-plan", F.load_prompt("loop_plan", n=n), cwd=target,
        model=model, timeout=F.PLAN_TIMEOUT, log_dir=log_dir, dry_run=dry_run,
        output_format="json",
    )


def run_workers(target: str, n: int, agents: list[int], model, dry_run, log_dir):
    specs = []
    for k in agents:
        specs.append(dict(
            label=f"iter{n:03d}-worker{k}",
            prompt=F.load_prompt("loop_worker", n=n, k=k),
            cwd=target, model=model, timeout=F.WORKER_TIMEOUT,
            log_dir=log_dir, dry_run=dry_run,
        ))
    return F.run_agents_parallel(specs, max_workers=4)


def confirm_complete(target: str, n: int, model, log_dir, digest: str) -> bool:
    """Run the final full-project audit and re-measure the harness.

    Both routes to "done" end here — the Review agent declaring COMPLETE, and the
    Plan agent assigning no workers because every frozen theorem is discharged —
    so the two cannot drift apart. The harness is re-run AFTER the audit because
    the audit is the last thing to touch the tree, and COMPLETE must rest on a
    fresh measurement rather than on the digest taken before it.
    """
    final = run_review(target, n, full=True, model=model, dry_run=False,
                       log_dir=log_dir, verify_digest_text=digest)
    verdict = F.review_verdict(final, os.path.join(target, "REVIEW.md"), n)
    issues, _ = run_verify(target)
    F.log(f"loop: final audit verdict: {verdict}; verify.py: "
          + ("PASS (0 issues)" if issues == 0
             else "could not run" if issues < 0 else f"{issues} issue(s)"))

    if verdict == "COMPLETE" and issues == 0:
        return True
    # Report which condition actually failed. Saying "the audit did not confirm"
    # when it did — and the harness was the problem — sends the reader to the
    # wrong file.
    if verdict != "COMPLETE":
        F.log("loop: the final audit did NOT confirm COMPLETE"
              + (" (no verdict could be read from it)" if verdict is None else "")
              + " — see REVIEW.md.")
    if issues != 0:
        F.log("loop: verify.py does not pass, so this is not COMPLETE regardless "
              "of the audit's verdict.")
    return False


def run_review(target: str, n: int, full: bool, model, dry_run, log_dir,
               verify_digest_text: str = ""):
    full_block = F.load_prompt("loop_full_audit") + "\n" if full else ""
    prompt = F.load_prompt(
        "loop_review", n=n,
        verify_block=F.load_prompt("loop_verify_block", digest=verify_digest_text),
        full_block=full_block,
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

    start = F.highest_iteration(tasks_path) + 1
    # Highest iteration that already existed when THIS run started. The recurrence
    # guard counts only cruxes circled AFTER this point, so a wall resolved (proved
    # or certificated) between runs does not re-fire the guard on iteration 1 from
    # append-only PROGRESS.md history.
    resume_baseline = start - 1
    last = (start + args.max_iterations - 1) if args.max_iterations else None
    F.log(f"loop: target={target}")
    if last is None:
        F.log(f"loop: starting at iteration {start}, unlimited (until COMPLETE or stopped)")
    else:
        F.log(f"loop: starting at iteration {start}, cap {args.max_iterations} (through {last})")
    F.log("loop: press Ctrl-C once to stop gracefully at the next iteration boundary.")

    n = start
    while last is None or n <= last:
        # Iteration boundary: honor a graceful stop request before starting work.
        if stop_requested():
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
                # No workers assigned — but for two very different reasons: either
                # every frozen theorem is ALREADY discharged (the deliverable is
                # DONE), or the Plan agent hit a wall it cannot get past (STUCK).
                # Distinguish them by the objective progress signal, so a finished
                # run is reported COMPLETE instead of being mislabeled as stuck.
                discharged = S.progress_signal(target)
                n_frozen = len(S.frozen_theorem_names(target))
                if n_frozen > 0 and discharged >= n_frozen:
                    # DONE: all frozen theorems are discharged in Solution.lean.
                    # Confirm with a full-project audit, then report COMPLETE.
                    F.log(f"loop: Plan assigned no workers and all {n_frozen} frozen "
                          "theorems are discharged — confirming COMPLETE (not stuck).")
                    issues, out = run_verify(target)
                    if confirm_complete(target, n, args.model, log_dir,
                                        verify_digest(issues, out)):
                        F.log("FORMALIZATION COMPLETE — verify.py passes with 0 issues.")
                        return 0
                    F.log("loop: every frozen theorem is discharged but completion "
                          "could not be confirmed — stopping for inspection.")
                    return 3
                # STUCK: a genuine wall. Because every agent is a fresh, context-free
                # session, the NEXT iteration would be identical, so stop gracefully
                # now rather than spinning no-op iterations forever. Needs human
                # intervention (a new strategy, a USER_NOTES.md certificate, or a
                # relaxed plan).
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

        # 2.5 VERIFY — the orchestrator runs the harness every iteration, so the
        # certifying check never depends on an agent choosing to run it.
        verify_issues, verify_out = run_verify(target, args.dry_run)
        digest = verify_digest(verify_issues, verify_out)
        if args.dry_run:
            F.log(f"loop: verify.py after iteration {n}: not run (--dry-run)")
        elif verify_issues == 0:
            F.log(f"loop: verify.py after iteration {n}: PASS (0 issues)")
        elif verify_issues < 0:
            F.log(f"loop: verify.py after iteration {n}: DID NOT RUN — {verify_out.strip()}")
        else:
            F.log(f"loop: verify.py after iteration {n}: {verify_issues} issue(s) "
                  "(expected until every frozen theorem is discharged)")

        # 3. REVIEW (full-project audit every 5th iteration)
        full = (n % 5 == 0)
        review_result = run_review(target, n, full, args.model, args.dry_run, log_dir,
                                   verify_digest_text=digest)

        if args.dry_run:
            F.log("loop: [dry-run] stopping after one iteration.")
            return 0

        # Verdict from THIS review's trailer (scoped to iteration n), with an
        # iteration-scoped REVIEW.md parse as fallback — never the whole file.
        verdict = F.review_verdict(review_result, review_path, n)
        F.log(f"loop: review verdict after iteration {n}: {verdict}")

        # The harness outranks the verdict: COMPLETE is a claim about an objective
        # state the orchestrator just measured, so a mismatch is the agent's error.
        if verdict == "COMPLETE" and verify_issues != 0:
            F.log(f"loop: Review claimed COMPLETE but verify.py reports "
                  f"{verify_issues if verify_issues > 0 else 'that it could not run'}"
                  " — OVERRIDING to INCOMPLETE and continuing.")
            verdict = "INCOMPLETE"

        if verdict == "COMPLETE":
            F.log("loop: Review reports COMPLETE — running final full-project audit.")
            if confirm_complete(target, n, args.model, log_dir, digest):
                F.log("==================================================")
                F.log("FORMALIZATION COMPLETE — verify.py passes with 0 issues.")
                F.log("Final findings in REVIEW.md.")
                F.log("==================================================")
                return 0
            F.log("loop: completion not confirmed — continuing to address the "
                  "audit's follow-ups.")

        # -- Objective stall guard (agent-independent) --------------------- #
        # The Plan agent can almost always invent one more "support lemma", so
        # the empty-assignment STUCK exit rarely fires. Independently measure the
        # ONLY thing that matters — frozen theorems discharged in Solution.lean —
        # and stop if it has flat-lined, or if a single crux keeps being named as
        # the next step while nothing closes it.
        discharged = S.progress_signal(target)
        ledger = S.record_progress(target, n, signal)
        n_frozen = len(S.frozen_theorem_names(target))
        F.log(f"loop: progress signal after iteration {n}: "
              f"{signal}/{n_frozen} frozen theorems discharged in Solution.lean")

        # Recurrence guard: count only cruxes circled SINCE this run started, so a
        # wall resolved between runs does not re-fire from append-only history.
        crux = S.recurring_crux(os.path.join(target, "PROGRESS.md"),
                                CRUX_RECUR_LIMIT, since_iteration=resume_baseline)
        if S.stalled_for(ledger, STALL_WINDOW):
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
