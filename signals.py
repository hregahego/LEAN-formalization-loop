"""signals — how much of the formalization is actually done, and when to stop.

Everything here answers one of two questions for loop.py:

  * What does this project consist of, and how much of it is discharged?
    (read from scripts/harness.json and Solution.lean — never from what an
    agent claims in a log)
  * Has it stopped making progress?  Two independent guards: the discharged
    count flat-lining across a window of iterations, and a single crux being
    named as the next step over and over while nothing closes it.

This is deliberately separate from formlib, which is the plumbing every stage
shares. Nothing here is used by setup.py or init.py — it exists for the loop.
"""

from __future__ import annotations

import json
import os
import re

from formlib import MARKDOWN_LEAD, log, read_text, utc_now

# The loop's only native stop-valve is the Plan agent voluntarily assigning zero
# workers — which it almost never does, because it is always able to invent one
# more "support lemma". These helpers give loop.py an objective, agent-independent
# progress metric so it can detect a stall from the repository itself.

def harness_config(target: str) -> dict:
    """scripts/harness.json — the single source of per-problem configuration.

    This used to be recovered by regex from the generated harness, which meant a
    `)` inside one of the architect's stage comments could truncate the theorem
    list and cap the progress signal. The values were always available here as
    structured data; there is nothing to parse.
    """
    try:
        with open(os.path.join(target, "scripts", "harness.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def frozen_theorem_names(target: str) -> list[str]:
    """The frozen theorem names, in the order the stages prove them."""
    names = harness_config(target).get("theorems")
    if not isinstance(names, list) or not names:
        log("warning: scripts/harness.json lists no theorems — the progress "
            "signal will read 0")
        return []
    return [n for n in names if isinstance(n, str)]


def project_name(target: str) -> str | None:
    """The project namespace / source directory name."""
    project = harness_config(target).get("project")
    return project if isinstance(project, str) and project else None


def progress_signal(target: str) -> int:
    """Number of frozen theorems genuinely discharged in Solution.lean.

    This is the project's true-north: the deliverable is the frozen theorems
    proved sorry-free, not the count of auxiliary lemmas. Scaffolding/wrapper/
    equivalence churn does NOT move this number, which is exactly what we want a
    stall detector to key on. Project-agnostic: the project directory is read from
    verify.py's `PROJECT=` (NOT hardcoded), so the signal works on any project the
    harness set up, not only the one it was first developed on.
    """
    proj = project_name(target)
    if proj is None:
        return 0
    sol = read_text(os.path.join(target, proj, "Solution.lean"))
    if not sol:
        return 0
    count = 0
    for name in frozen_theorem_names(target):
        # top-level `theorem <name>` (allow leading indentation but not a `--`/
        # `/-` comment prefix on the same construct — a plain regex on the token
        # is adequate because Solution.lean only ever *states* a frozen theorem
        # when it is actually discharging it `:= <name>_proof`).
        if re.search(rf"(?m)^\s*theorem\s+{re.escape(name)}\b", sol):
            count += 1
    return count


def _ledger_path(target: str) -> str:
    return os.path.join(target, "logs", "orchestration", "progress_ledger.json")


def read_ledger(target: str) -> list[dict]:
    """The recorded (iteration, discharged) history, or [] if there is none.

    A CORRUPT ledger is reported, not swallowed. It silently reads as "no
    history", which resets the stall window — so a run that has been circling a
    wall for twenty iterations would look freshly started, and the guard that
    exists to stop it would never fire.
    """
    raw = read_text(_ledger_path(target))
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        log(f"warning: {_ledger_path(target)} is unreadable ({exc}). The stall "
            "window starts over from here; delete the file to silence this.")
        return []
    return data if isinstance(data, list) else []


def record_progress(target: str, n: int, signal: int) -> list[dict]:
    """Append (iteration, signal) to the ledger and persist it. Idempotent per n:
    a re-run of iteration n overwrites its prior entry rather than duplicating."""
    ledger = [e for e in read_ledger(target) if e.get("iteration") != n]
    ledger.append({"iteration": n, "signal": signal, "at": utc_now()})
    ledger.sort(key=lambda e: e.get("iteration", 0))
    path = _ledger_path(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Write, then rename. `open(path, "w")` truncates before json.dump streams
    # into it, so an interrupt mid-write leaves a half-written file — which
    # read_ledger then treats as no history at all. rename(2) is atomic within a
    # filesystem, so the ledger is only ever seen whole or untouched.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2)
    os.replace(tmp, path)
    return ledger


def stalled_for(ledger: list[dict], k: int) -> bool:
    """True when the progress signal has NOT increased across the last k
    iterations (i.e. k+1 recorded points all at or below the earliest of them).
    Needs at least k+1 data points before it can fire."""
    if k <= 0 or len(ledger) < k + 1:
        return False
    window = ledger[-(k + 1):]
    baseline = window[0]["signal"]
    return all(e["signal"] <= baseline for e in window[1:])


# Backticked identifier on a PROGRESS.md "Next:" line — the crux a worker says a
# follow-up must attack. A crux name that recurs across many iterations' Next:
# lines is a hard wall the loop is circling rather than closing.
_NEXT_LINE = re.compile(r"(?mi)^" + MARKDOWN_LEAD + r"Next\*{0,2}\s*:\s*\*{0,2}\s*(.*)$")
_IDENT = re.compile(r"`([A-Za-z_][A-Za-z0-9_']*)`")
# The iteration an entry belongs to, from its "Agent: agent-iterNNN-k" line.
_ENTRY_ITER = re.compile(r"agent-iter0*(\d+)")


def recurring_crux(progress_path: str, threshold: int,
                   since_iteration: int | None = None) -> tuple[str, int] | None:
    """The identifier that appears on the most PROGRESS.md 'Next:' lines, if it
    recurs on at least `threshold` distinct lines. Returns (name, count) or None.

    This targets the observed failure mode directly: a single crux (e.g. a
    transform-value or domination lemma) named as the 'next step' dozens of times
    while the loop only ever produces reductions around it.

    PROGRESS.md is append-only, so a crux resolved (proved or certificated)
    between runs keeps its historical mentions forever. Pass `since_iteration`
    (the highest iteration that existed when THIS run started) to count only
    'Next:' lines from strictly later entries — otherwise the guard would re-fire
    on the first iteration of every resume, on a wall that is already broken. Each
    entry is attributed to the iteration in its 'Agent: agent-iterNNN' line;
    entries with no such line are skipped when `since_iteration` is set."""
    text = read_text(progress_path)
    if not text:
        return None
    counts: dict[str, int] = {}
    # Split into per-agent entries (each begins with a '## ' header) so each
    # 'Next:' line can be attributed to its entry's iteration.
    total_next = len(_NEXT_LINE.findall(text))
    for entry in re.split(r"(?m)^(?=\s*#{2,6}\s)", text):
        if since_iteration is not None:
            m = _ENTRY_ITER.search(entry)
            if m is None or int(m.group(1)) <= since_iteration:
                continue
        for line in _NEXT_LINE.findall(entry):
            for name in set(_IDENT.findall(line)):
                counts[name] = counts.get(name, 0) + 1
    if not counts:
        # Distinguish "nothing recurs" from "this guard is blind". A PROGRESS.md
        # with entries but no parseable `Next:` line means the format drifted and
        # the crux guard is silently inert — say so rather than returning a
        # confident None.
        if total_next == 0 and text.strip():
            log("warning: no parseable 'Next:' line in PROGRESS.md — the "
                "recurring-crux guard cannot see anything")
        return None
    name, count = max(counts.items(), key=lambda kv: kv[1])
    return (name, count) if count >= threshold else None
