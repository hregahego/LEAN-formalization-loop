#!/usr/bin/env python3
"""
setup.py — scaffold the orchestration files for an autonomous Lean 4
formalization, from a natural-language proof sketch.

Usage:
    python3 setup.py [TARGET_DIR]        (default: current directory)

Preconditions:
    TARGET_DIR contains SKETCH.md (problem statement + NL proof sketch).

What it does:
    Runs a single configured "architect" agent that reads SKETCH.md and the
    reference project set in config.json (default: the bundled ./reference)
    and writes, into TARGET_DIR, following the reference *format* exactly but with
    content derived for THIS problem:

        BLUEPRINT.md          (Part -1 SETUP / file layout / frozen Defs &
                               Theorems / stages with cheat-watches / order)
        scripts/harness.json  (project namespace, problem title, theorem names)

    setup.py itself then writes, from the reference:

        scripts/verify.py     (the 7-check harness, copied verbatim — it is
                               byte-identical in every project and reads
                               scripts/harness.json at run time)
        scripts/harness.json  (rewritten in canonical form from the validated
                               values, so its bytes — which are hashed into the
                               control manifest — depend only on the values)
        scripts/frozen.sha256 (placeholder; init.py records the real pins)
        PROGRESS.md           (append-only log header)
        TASKS.md              (append-only header; 4-agent delegation)
        REVIEW.md             (append-only header; audit log)

    Only the values in harness.json vary between problems. The harness and the
    append-only rules are deliberately NOT model-authored: the harness certifies
    every result, and the log rules are pipeline policy, so both stay byte-identical
    across runs. BLUEPRINT.md stays model-authored — it IS the problem-specific work.

    It also writes USER_NOTES.md directly (from the reference template) — the
    user-editable file for special instructions, in particular any
    assumed-certificate axioms to permit. An existing USER_NOTES.md is never
    overwritten.

    Fill it in BEFORE running setup.py if the problem needs assumed axioms or a
    mandated proof route: the architect reads it, and only it can record the
    Check 4b parameters (final_theorem / mandatory_axioms) into harness.json.
    Seeding it later — before init.py — still permits the axioms, but Check 4b
    (which enforces that a permitted certificate is actually USED) stays off.

Next step after this:  python3 init.py TARGET_DIR
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys

import formlib as F


# A frozen theorem name as it may appear in the bash array and in formlib's
# ALL_THEOREMS regex: no quotes, no `)`, no whitespace.
#
# `\Z`, NOT `$`: in Python `$` also matches just before a trailing newline, so
# `"name\n"` — which JSON can carry — would validate and then be written into the
# bash array, breaking the harness the values are supposed to configure.
_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_.']*\Z")


def _read_harness_params(
    target: str,
) -> tuple[str, str, list[str], str, list[str]]:
    """(project, problem title, frozen theorem names, final theorem,
    mandatory axioms) from scripts/harness.json.

    Validated strictly: project/theorems are substituted into shell source, and
    formlib re-parses them back out of verify.py, so a stray quote or paren would
    corrupt both the harness and the progress signal.
    """
    path = os.path.join(target, "scripts", "harness.json")
    try:
        data = json.loads(F.read_text(path))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"scripts/harness.json unreadable: {exc}") from exc

    project = data.get("project")
    if not isinstance(project, str) or not _NAME_RE.match(project):
        raise RuntimeError(f"harness.json: bad 'project' name: {project!r}")

    # Free-form prose (it names the problem for a human), so only sanity-checked:
    # single line, non-empty. Falls back to the namespace if the agent omits it.
    problem = data.get("problem") or project
    if not isinstance(problem, str) or "\n" in problem or not problem.strip():
        raise RuntimeError(f"harness.json: bad 'problem' title: {problem!r}")
    problem = problem.strip()

    theorems = data.get("theorems")
    if not isinstance(theorems, list) or not theorems:
        raise RuntimeError("harness.json: 'theorems' must be a non-empty list")
    bad = [t for t in theorems if not isinstance(t, str) or not _NAME_RE.match(t)]
    if bad:
        raise RuntimeError(f"harness.json: bad theorem name(s): {bad!r}")
    dupes = sorted({t for t in theorems if theorems.count(t) > 1})
    if dupes:
        raise RuntimeError(f"harness.json: duplicate theorem name(s): {dupes!r}")

    # Optional Check 4b: which axioms the headline theorem MUST depend on.
    final_theorem = data.get("final_theorem") or ""
    mandatory = data.get("mandatory_axioms") or []
    if final_theorem and not _NAME_RE.match(final_theorem):
        raise RuntimeError(f"harness.json: bad 'final_theorem': {final_theorem!r}")
    if not isinstance(mandatory, list):
        raise RuntimeError("harness.json: 'mandatory_axioms' must be a list")
    bad_ax = [a for a in mandatory if not isinstance(a, str) or not _NAME_RE.match(a)]
    if bad_ax:
        raise RuntimeError(f"harness.json: bad mandatory axiom name(s): {bad_ax!r}")
    if mandatory and not final_theorem:
        raise RuntimeError("harness.json: 'mandatory_axioms' needs 'final_theorem' "
                           "(the check inspects that theorem's dependencies)")
    if final_theorem and final_theorem not in theorems:
        raise RuntimeError(f"harness.json: 'final_theorem' {final_theorem!r} is not "
                           "one of 'theorems'")
    return project, problem, theorems, final_theorem, mandatory


def write_harness_json(target: str, project: str, problem: str,
                       theorems: list[str], final_theorem: str,
                       mandatory: list[str]) -> None:
    """Rewrite scripts/harness.json in canonical form.

    The architect supplies the VALUES; this decides the bytes. Agents format JSON
    however they like — the existing runs vary in indentation and key order — and
    this file is hashed into the control manifest, so without a canonical rewrite
    two projects with identical configuration would pin different hashes. Keys
    outside the schema (a stray "_comment", say) are dropped.
    """
    config = {"project": project, "problem": problem, "theorems": list(theorems)}
    # Omit the optional pair entirely when unused, so its absence is unambiguous
    # rather than an empty value that looks like a half-finished edit.
    if mandatory:
        config["final_theorem"] = final_theorem
        config["mandatory_axioms"] = list(mandatory)
    path = os.path.join(target, "scripts", "harness.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")


# Append-only scaffolding whose wording is fixed pipeline policy, not per-problem
# content. Copied from the reference with only the problem title filled in, so the
# rules every later agent reads are identical in every run.
_BOILERPLATE = ("PROGRESS.md", "TASKS.md", "REVIEW.md")


def _render_boilerplate(target: str, problem: str) -> list[str]:
    """Write the append-only log headers verbatim from the reference."""
    written = []
    for name in _BOILERPLATE:
        src = F.read_text(os.path.join(F.REFERENCE_DIR, name))
        if not src:
            raise RuntimeError(f"reference/{name} is missing or empty")
        with open(os.path.join(target, name), "w", encoding="utf-8") as fh:
            fh.write(src.replace("<problem>", problem))
        written.append(name)
    return written


def install_harness(target: str) -> str:
    """Copy the verification harness into the project.

    It is a STATIC file — identical in every project — because everything
    problem-specific lives in scripts/harness.json, which the harness reads at
    run time. Nothing is substituted, so there is nothing to get wrong, and the
    control manifest pins the copy.
    """
    src = os.path.join(F.REFERENCE_DIR, "scripts", "verify.py")
    if not os.path.isfile(src):
        raise RuntimeError("reference scripts/verify.py is missing")
    dest = os.path.join(target, "scripts", "verify.py")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(src, dest)
    os.chmod(dest, 0o755)

    # The pins placeholder is fixed text that init.py overwrites with real
    # hashes. Copying it is one less artifact for an agent to get wrong.
    pins = os.path.join(target, "scripts", "frozen.sha256")
    if not os.path.exists(pins):
        shutil.copyfile(os.path.join(F.REFERENCE_DIR, "scripts", "frozen.sha256"),
                        pins)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="Scaffold orchestration files from SKETCH.md.")
    ap.add_argument("target", nargs="?", default=".", help="target directory (default: .)")
    ap.add_argument("--model", default=None, help="override the claude model")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt/command, do not call claude")
    args = ap.parse_args()

    target = F.resolve_target(args.target)
    F.require_sketch(target)

    if not os.path.isdir(F.REFERENCE_DIR):
        sys.exit(f"ERROR: bundled reference dir not found: {F.REFERENCE_DIR}")

    F.log(f"setup: target={target}")
    F.log(f"setup: reference={F.REFERENCE_DIR}")

    # Create USER_NOTES.md (user-editable special instructions) from the reference
    # template if absent. The user fills this in BEFORE running init.py — it is
    # where assumed-certificate axioms are permitted. Never clobber an existing one.
    user_notes = os.path.join(target, "USER_NOTES.md")
    if os.path.exists(user_notes):
        F.log("setup: USER_NOTES.md already present — leaving it untouched; the "
              "architect will read it.")
    else:
        content = F.read_text(os.path.join(F.REFERENCE_DIR, "USER_NOTES.md"))
        with open(user_notes, "w", encoding="utf-8") as fh:
            fh.write(content)
        # The architect runs in a moment and reads this file, so a template
        # created now means it sees the strict default. Editing it later still
        # permits axioms (init.py reads it) but cannot switch Check 4b on.
        F.log("setup: created USER_NOTES.md from the template (strict default: no "
              "assumed axioms). If this problem needs assumed axioms or a mandated "
              "proof route, stop, fill it in, and re-run setup.py — the architect "
              "reads it NOW, and Check 4b can only be configured here.")

    result = F.run_agent(
        "setup-architect", F.load_prompt("setup_architect", ref=F.REFERENCE_DIR),
        cwd=target,
        add_dirs=[F.REFERENCE_DIR],
        model=args.model,
        timeout=F.SETUP_TIMEOUT,
        log_dir=os.path.join(target, "logs", "orchestration"),
        dry_run=args.dry_run,
    )
    if args.dry_run:
        return 0

    # Verify the expected agent artifacts landed. The architect now writes only
    # the problem-specific content; everything else is rendered below.
    expected = ["BLUEPRINT.md", "scripts/harness.json"]
    missing = [f for f in expected if not os.path.exists(os.path.join(target, f))]
    if missing:
        F.log(f"WARNING: setup agent did not create: {', '.join(missing)}")
        return 1 if not result.ok else 2

    # Render the harness and the append-only headers ourselves — never the model.
    try:
        project, problem, theorems, final_thm, mandatory = _read_harness_params(target)
        install_harness(target)
        write_harness_json(target, project, problem, theorems, final_thm, mandatory)
        boilerplate = _render_boilerplate(target, problem)
    except RuntimeError as exc:
        F.log(f"ERROR: {exc}")
        return 1
    F.log(f"setup: installed scripts/verify.py (project={project}, "
          f"{len(theorems)} frozen theorems"
          + (f", {len(mandatory)} mandatory axioms" if mandatory else "") + ")")
    F.log(f"setup: rendered {', '.join(boilerplate)} from the reference")

    # Pin the control files now. init.py re-pins once it has written
    # frozen.sha256 and ALLOWED_AXIOMS.txt; loop.py re-checks every iteration.
    # Pinned only after the harness passes its lint — a manifest vouching for a
    # file we just rejected would be worse than no manifest.
    pinned = F.write_control_manifest(target)
    F.log(f"setup: pinned control files ({', '.join(pinned)})")

    F.log("setup complete. Scaffolding written:")
    for f in expected + ["scripts/verify.py", "scripts/frozen.sha256"] + list(_BOILERPLATE):
        F.log(f"  ✓ {f}")
    F.log("Next: python3 init.py " + target)
    # The scaffold is complete even if the agent exited untidily; its status
    # is information, not a failure of this step.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
