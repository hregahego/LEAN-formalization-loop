"""
formlib — shared plumbing for the autonomous Lean 4 formalization pipeline.

Every agent in this pipeline is a headless CLI subprocess. This module owns the
shared plumbing so setup.py / init.py / loop.py stay thin. In file order:

  * Configuration — config.json, merged over the defaults here.
  * Prompt loading — prompts/*.md with @@MARKER@@ substitution.
  * Control-file integrity — pinning and re-checking the four files that define
    what "verified" means, since they live in the workspace the agents edit.
  * Agent invocation — building and running one (streaming + per-agent logs,
    watchdog timeout, dry-run), and running the 4 workers in parallel. With
    output_format="json" the agent's final message is parsed out of the result.
  * Control signals — the PRIMARY signal is a machine-readable
     `<<<ORCH {...} ORCH>>>` trailer the Plan/Review agent emits in its final
     message (carrying its own iteration number); the FALLBACK is an
    iteration-scoped parse of the append-only TASKS.md / REVIEW.md. Because
    those files are append-only, every parse is scoped to ONE iteration's block,
    so stale entries from earlier iterations are never returned.
  * Progress and stall signals — how many frozen theorems are actually
    discharged, and whether that number has stopped moving.

Configuration is read from config.json next to these scripts. The default file
uses Claude (`claude -p`); set `"agent_cli": "codex"` to use `codex exec`.
"""

from __future__ import annotations

import os
import re
import sys
import json
import hashlib
import threading
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_SCRIPT_DIR, "config.json")

_DEFAULT_CONFIG = {
    "agent_cli": "claude",
    "claude_bin": "claude",
    "codex_bin": "codex",
    "model": None,
    "stall_window": 16,
    "crux_recur_limit": 16,
    "timeouts": {
        "setup": 3600,
        "init": 10800,
        "plan": 10800,
        "worker": 10800,
        "review": 10800,
        "verify": 3600,
    },
}


def _deep_merge(base: dict, overrides: dict) -> dict:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raw = {}
    except json.JSONDecodeError as exc:
        sys.exit(f"ERROR: invalid JSON in {CONFIG_PATH}: {exc}")
    if not isinstance(raw, dict):
        sys.exit(f"ERROR: {CONFIG_PATH} must contain a JSON object.")
    return _deep_merge(_DEFAULT_CONFIG, raw)


CONFIG = _load_config()

AGENT_CLI = str(CONFIG.get("agent_cli", "claude")).lower()
if AGENT_CLI not in ("claude", "codex"):
    sys.exit('ERROR: config.json "agent_cli" must be "claude" or "codex".')

CLAUDE_BIN = str(CONFIG.get("claude_bin") or "claude")
CODEX_BIN = str(CONFIG.get("codex_bin") or "codex")
DEFAULT_MODEL = CONFIG.get("model") or None

# The format-template project, bundled next to these scripts so the repo is
# self-contained and portable.
REFERENCE_DIR = os.path.join(_SCRIPT_DIR, "reference")

# Generous defaults: a Mathlib `lake exe cache get` + cold build (init) can take
# well over an hour; a worker proving a hard lemma can run a long time too.
_timeouts_val = CONFIG.get("timeouts")
_TIMEOUTS = _timeouts_val if isinstance(_timeouts_val, dict) else {}
SETUP_TIMEOUT = int(_TIMEOUTS["setup"])
INIT_TIMEOUT = int(_TIMEOUTS["init"])
PLAN_TIMEOUT = int(_TIMEOUTS["plan"])
WORKER_TIMEOUT = int(_TIMEOUTS["worker"])
REVIEW_TIMEOUT = int(_TIMEOUTS["review"])
# The harness is a plain build + a few greps; generous because a cold Mathlib
# restore can precede it.
VERIFY_TIMEOUT = int(_TIMEOUTS["verify"])

# Headless, non-interactive: the loop runs unattended, so both CLIs use their
# most permissive mode. Run the pipeline only in a directory you trust.
_CLAUDE_PERMISSION_ARGS = ["--dangerously-skip-permissions"]
_CODEX_PERMISSION_ARGS = ["--dangerously-bypass-approvals-and-sandbox"]


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    """Orchestrator-level console line (distinct from agent output)."""
    print(f"\033[1;36m[orchestrator {utc_now()}]\033[0m {msg}", flush=True)


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


PROMPTS_DIR = os.path.join(_SCRIPT_DIR, "prompts")

# Placeholders are `@@NAME@@`, not `{name}`. The prompts contain literal JSON and
# Lean braces, so a brace-based template would need escaping that is easy to get
# wrong and impossible to see — one file here previously had to use `.replace`
# while another used `.format` with `{{ }}` doubling, and they could not be
# unified without corrupting one of them. `@@NAME@@` cannot collide with prompt
# content, so there is one mechanism and no escaping rules.
_PROMPT_MARKER = re.compile(r"@@([A-Z_]+)@@")


def load_prompt(name: str, **params: str) -> str:
    """Read prompts/<name>.md and substitute every @@MARKER@@.

    Fails loudly on an unknown parameter or an unsubstituted marker: a prompt
    that silently ships `@@N@@` to an agent is a bug that shows up as confusing
    model behaviour, far from its cause.
    """
    path = os.path.join(PROMPTS_DIR, name + ".md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        sys.exit(f"ERROR: cannot read prompt {name!r}: {exc}")

    wanted = set(_PROMPT_MARKER.findall(text))
    given = {k.upper() for k in params}
    if given - wanted:
        raise KeyError(f"prompt {name!r} has no marker(s) for: "
                       f"{sorted(given - wanted)}")
    for key, value in params.items():
        text = text.replace(f"@@{key.upper()}@@", str(value))

    missing = set(_PROMPT_MARKER.findall(text))
    if missing:
        raise KeyError(f"prompt {name!r} left unsubstituted: {sorted(missing)}")
    return text


# --------------------------------------------------------------------------- #
# Control-file integrity
#
# The harness and everything that configures it live INSIDE the workspace the
# worker agents edit. Nothing else re-reads them, so a worker that whitelists its
# own `axiom`, re-hashes a weakened Theorems.lean, or drops a name from
# ALL_THEOREMS silently changes what "verified" means. These helpers pin the four
# control files after each phase writes them and re-check them every iteration.
#
# This is tamper-EVIDENT, not tamper-proof: the manifest sits in the workspace
# too, so an agent that edits a control file AND the manifest defeats it. What it
# removes is the silent single-file edit, which is the realistic failure.
# --------------------------------------------------------------------------- #

CONTROL_FILES = ("scripts/verify.py", "scripts/harness.json",
                 "scripts/frozen.sha256", "scripts/ALLOWED_AXIOMS.txt")
CONTROL_MANIFEST = "scripts/control_manifest.sha256"
# Stands where a hash would go, recording that a control file did NOT exist when
# the manifest was written — so creating one later is detected rather than free.
_ABSENT = "ABSENT" + "-" * 58   # same width as a sha256 hex digest


def sha256_file(path: str) -> str | None:
    """Hex SHA-256 of a file, or None if it cannot be read."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def write_control_manifest(target: str) -> list[str]:
    """Pin every control file that currently exists. Returns the pinned paths.

    Called after setup (verify.py + harness.json) and again after init has
    written frozen.sha256 and ALLOWED_AXIOMS.txt, so the manifest always
    describes the phase that just completed.
    """
    lines, pinned = [], []
    for rel in CONTROL_FILES:
        digest = sha256_file(os.path.join(target, rel))
        if digest is None:
            # Pin the ABSENCE too. verify.py honours ALLOWED_AXIOMS.txt whenever
            # it exists, so a file left uncreated is an open door: a worker could
            # add one later and, if absence went unrecorded, nothing would notice.
            lines.append(f"{_ABSENT}  {rel}")
        else:
            lines.append(f"{digest}  {rel}")
            pinned.append(rel)
    path = os.path.join(target, CONTROL_MANIFEST)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# SHA-256 of the files that define what verification MEANS.\n"
                 "# Written by setup.py/init.py, re-checked by loop.py each iteration.\n"
                 "# A mismatch means the harness or its config changed mid-run.\n")
        fh.write("\n".join(lines) + "\n")
    return pinned


def check_control_manifest(target: str) -> list[str]:
    """Problems with the control files, newest state vs the manifest.

    Empty list == everything matches. A MISSING manifest is itself a problem:
    absence must not read as success.
    """
    manifest = read_text(os.path.join(target, CONTROL_MANIFEST))
    if not manifest.strip():
        return [f"{CONTROL_MANIFEST} is missing or empty — control files "
                "were never pinned, so tampering cannot be detected"]
    problems = []
    seen = []
    for line in manifest.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            problems.append(f"{CONTROL_MANIFEST}: unparseable line: {line!r}")
            continue
        pinned_hash, rel = parts[0], parts[1].strip()
        seen.append(rel)
        actual = sha256_file(os.path.join(target, rel))
        if pinned_hash == _ABSENT:
            if actual is not None:
                problems.append(f"{rel}: CREATED since pinning (it did not exist "
                                "when the control files were pinned)")
        elif actual is None:
            problems.append(f"{rel}: pinned but now MISSING")
        elif actual != pinned_hash:
            problems.append(f"{rel}: MODIFIED since it was pinned "
                            f"(expected {pinned_hash[:12]}, found {actual[:12]})")
    if not seen:
        problems.append(f"{CONTROL_MANIFEST}: contains no pins")
    return problems


def resolve_target(arg: str | None) -> str:
    target = os.path.abspath(os.path.expanduser(arg or "."))
    if not os.path.isdir(target):
        sys.exit(f"ERROR: target directory does not exist: {target}")
    return target


def require_sketch(target: str) -> None:
    if not os.path.isfile(os.path.join(target, "SKETCH.md")):
        sys.exit(f"ERROR: no SKETCH.md found in {target}. Place SKETCH.md there first.")


def require_blueprint(target: str) -> None:
    if not os.path.isfile(os.path.join(target, "BLUEPRINT.md")):
        sys.exit(f"ERROR: no BLUEPRINT.md found in {target}. Run setup.py first.")


# --------------------------------------------------------------------------- #
# Running one agent
# --------------------------------------------------------------------------- #

@dataclass
class AgentResult:
    label: str
    returncode: int
    output: str            # raw stdout (text mode) / raw JSON blob (json mode)
    timed_out: bool = False
    result_text: str = ""  # the agent's FINAL message (parsed out of json mode)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def build_cmd(prompt: str, add_dirs: list[str] | None = None,
              model: str | None = None,
              output_format: str = "text") -> list[str]:
    chosen = model or DEFAULT_MODEL
    if AGENT_CLI == "codex":
        cmd = [CODEX_BIN, "exec"]
        cmd += _CODEX_PERMISSION_ARGS
        if chosen:
            cmd += ["--model", str(chosen)]
        for d in add_dirs or []:
            cmd += ["--add-dir", d]
        cmd += [prompt]
        return cmd

    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", output_format]
    cmd += _CLAUDE_PERMISSION_ARGS
    if chosen:
        cmd += ["--model", str(chosen)]
    for d in add_dirs or []:
        cmd += ["--add-dir", d]
    cmd += extra or []
    return cmd


def _open_log(log_dir, label, cwd, output_format):
    if not log_dir:
        return None, None
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{label}.log")
    fh = open(path, "w", encoding="utf-8")
    fh.write(f"# {label}  @ {utc_now()}\n# cwd: {cwd}\n# output_format: {output_format}\n\n")
    fh.flush()
    return fh, path


def _result_field(obj):
    """Pull the final-message text out of a parsed Claude JSON result object."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for key in ("result", "text", "content"):
            v = obj.get(key)
            if isinstance(v, str):
                return v
        return json.dumps(obj)
    return ""


def parse_json_result(raw: str) -> str:
    """
    Extract the agent's final message from the JSON that Claude prints with
    `--output-format json`. Falls back to a best-effort `{...}` salvage,
    then to the raw text, so a malformed blob never crashes the loop.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return _result_field(json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        try:
            return _result_field(json.loads(raw[start:end + 1]))
        except (json.JSONDecodeError, ValueError):
            pass
    return raw


def run_agent(label: str, prompt: str, cwd: str, *,
              add_dirs: list[str] | None = None,
              model: str | None = None,
              extra: list[str] | None = None,
              timeout: int | None = None,
              log_dir: str | None = None,
              dry_run: bool = False,
              output_format: str = "text") -> AgentResult:
    """
    Launch one configured agent. A watchdog kills the process after `timeout`s.

    output_format="text"  -> stream stdout live (the final message), prefixed by
                             `label`, to console + per-agent log.
    output_format="json"  -> capture the single JSON result blob, parse the
                             agent's final message into `result_text` (used for
                             the <<<ORCH …>>> control trailer). stderr is kept
                             separate so it cannot corrupt the JSON.
    """
    cmd = build_cmd(prompt, add_dirs=add_dirs, model=model, extra=extra,
                    output_format=output_format)

    if dry_run:
        log(f"[DRY RUN] {label}: would run in {cwd} (output_format={output_format})")
        preview = prompt if len(prompt) < 1200 else prompt[:1200] + " …(truncated)"
        print(f"--- prompt for {label} ---\n{preview}\n--- end prompt ---", flush=True)
        return AgentResult(label, 0, "[dry-run]", result_text="[dry-run]")

    log_fh, log_path = _open_log(log_dir, label, cwd, output_format)
    log(f"launching {label} (cwd={cwd}, timeout={timeout}s, fmt={output_format})"
        + (f", log -> {log_path}" if log_path else ""))

    if output_format == "json":
        raw, rc, timed_out = _exec_capture(cmd, cwd, timeout, log_fh)
        result_text = raw if AGENT_CLI == "codex" else parse_json_result(raw)
        if result_text:
            for line in result_text.splitlines():
                sys.stdout.write(f"\033[2m[{label}]\033[0m {line}\n")
            sys.stdout.flush()
    else:
        raw, rc, timed_out = _exec_stream(cmd, cwd, timeout, log_fh, label)
        result_text = raw

    if log_fh:
        log_fh.write(f"\n# exit={rc} timed_out={timed_out} @ {utc_now()}\n")
        log_fh.close()

    result = AgentResult(label, rc, raw, timed_out=timed_out, result_text=result_text)
    status = "OK" if result.ok else ("TIMEOUT" if result.timed_out else f"exit {rc}")
    log(f"{label} finished: {status}")
    return result


def _exec_stream(cmd, cwd, timeout, log_fh, label):
    """Run streaming (stderr merged into stdout); echo lines live."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    timed_out = {"v": False}

    def _kill():
        timed_out["v"] = True
        try:
            proc.kill()
        except Exception:
            pass

    watchdog = threading.Timer(timeout, _kill) if timeout else None
    if watchdog:
        watchdog.daemon = True
        watchdog.start()

    chunks: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            chunks.append(line)
            if log_fh:
                log_fh.write(line)
                log_fh.flush()
            sys.stdout.write(f"\033[2m[{label}]\033[0m {line}")
            sys.stdout.flush()
        proc.wait()
    finally:
        if watchdog:
            watchdog.cancel()
    rc = proc.returncode if proc.returncode is not None else -1
    return "".join(chunks), rc, timed_out["v"]


def _exec_capture(cmd, cwd, timeout, log_fh):
    """Run with stdout/stderr separate (for clean JSON); no live streaming."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        timed_out = True
    if log_fh:
        log_fh.write(out or "")
        if err:
            log_fh.write("\n# --- stderr ---\n" + err)
        log_fh.flush()
    rc = proc.returncode if proc.returncode is not None else -1
    return (out or ""), rc, timed_out


def run_agents_parallel(specs: list[dict], max_workers: int = 4) -> list[AgentResult]:
    """
    Run several agents concurrently. Each spec is a kwargs dict for run_agent
    (must include `label`, `prompt`, `cwd`). Returns results in input order.
    """
    results: list[AgentResult | None] = [None] * len(specs)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(run_agent, **spec): i for i, spec in enumerate(specs)}
        for fut, i in futures.items():
            results[i] = fut.result()
    return results  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Control signals
# --------------------------------------------------------------------------- #
#
# PRIMARY signal: the Plan/Review agent ends its final message with a trailer
#   <<<ORCH
#   {"iteration": 3, "active_agents": [1, 2]}      (Plan)
#   {"iteration": 3, "verdict": "COMPLETE"}        (Review)
#   ORCH>>>
# which we read straight from the agent's JSON result — i.e. from the process we
# just ran, never from a stale append-only file.
#
# FALLBACK signal: an iteration-SCOPED parse of TASKS.md / REVIEW.md. Those files
# are append-only, so each parser slices out exactly the requested iteration's
# block(s) and ignores everything from earlier iterations.

# These files are written by LLM agents, so the format is a request, not a
# guarantee. Accept the ordinary drift a model introduces when reproducing a
# schema — leading indentation, a list bullet, `**bold**` — because a matcher
# that only accepts the pristine form turns a cosmetic slip into a SILENTLY
# disabled guard rather than a visible error. `_LEAD` is that tolerance.
_LEAD = r"[ \t>]*(?:[-*+]\s+)?\*{0,2}\s*"
_ITER_HEADER = re.compile(r"(?m)^\s*#{1,6}\s*Iteration\s+(\d+)\b")
_AGENT_LINE = re.compile(r"(?m)^" + _LEAD + r"Agent\s*(\d+)\s*\*{0,2}\s*:")
_VERDICT = re.compile(r"(?mi)^" + _LEAD + r"Verdict\*{0,2}\s*:\s*\*{0,2}\s*(COMPLETE|INCOMPLETE)\b")
_REVIEW_HEADER = re.compile(r"(?m)^\s*#{1,6}\s*Review\b[^\n]*?Iteration\s+(\d+)\b")
_TRAILER = re.compile(r"<<<ORCH\s*(\{.*?\})\s*ORCH>>>", re.S)


def extract_trailer(text: str) -> dict | None:
    """Parse the LAST `<<<ORCH {json} ORCH>>>` trailer from an agent's message."""
    if not text:
        return None
    matches = _TRAILER.findall(text)
    if not matches:
        return None
    try:
        obj = json.loads(matches[-1])
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def plan_active_agents(result: "AgentResult", tasks_path: str, n: int) -> list[int]:
    """
    Active workers for iteration n: trust the Plan agent's trailer first (if it
    is for THIS iteration), else fall back to the iteration-scoped TASKS.md parse.
    """
    tr = extract_trailer(result.result_text)
    if tr and tr.get("iteration") == n and isinstance(tr.get("active_agents"), list):
        nums = {int(k) for k in tr["active_agents"] if str(k).isdigit()}
        scoped = sorted(k for k in nums if 1 <= k <= 4)
        if scoped:
            return scoped
    return active_agents(tasks_path, n)


def review_verdict(result: "AgentResult", review_path: str, n: int) -> str | None:
    """
    Verdict for iteration n: trust the Review agent's trailer first (if it is for
    THIS iteration), else fall back to the iteration-scoped REVIEW.md parse.
    """
    tr = extract_trailer(result.result_text)
    if tr and tr.get("iteration") == n:
        v = str(tr.get("verdict", "")).upper()
        if v in ("COMPLETE", "INCOMPLETE"):
            return v
    return verdict_for_iteration(review_path, n)


def highest_iteration(tasks_path: str) -> int:
    text = read_text(tasks_path)
    nums = [int(m) for m in _ITER_HEADER.findall(text)]
    return max(nums) if nums else 0


def iteration_block(tasks_path: str, n: int) -> str:
    """The text of the '## Iteration n' block, up to the next iteration header."""
    text = read_text(tasks_path)
    if not text:
        return ""
    # Same tolerance as _ITER_HEADER, which finds this block's END: a strict
    # start matcher would fail to find a block the end matcher can terminate.
    header = re.search(rf"(?m)^\s*#{{1,6}}\s*Iteration\s+{n}\b", text)
    if not header:
        return ""
    rest = text[header.end():]
    nxt = _ITER_HEADER.search(rest)
    end = header.end() + nxt.start() if nxt else len(text)
    return text[header.start():end]


def active_agents(tasks_path: str, n: int) -> list[int]:
    """Which worker agents (1..4) the Plan agent assigned in iteration n."""
    block = iteration_block(tasks_path, n)
    nums = sorted({int(x) for x in _AGENT_LINE.findall(block)})
    return [k for k in nums if 1 <= k <= 4]


def verdict_for_iteration(review_path: str, n: int) -> str | None:
    """
    The last verdict written for iteration n ONLY. REVIEW.md is append-only and
    may hold two iteration-n blocks (the normal review + a full-project audit);
    earlier iterations' verdicts are ignored entirely.
    """
    text = read_text(review_path)
    if not text:
        return None
    headers = [(m.start(), int(m.group(1))) for m in _REVIEW_HEADER.finditer(text)]
    verdicts: list[str] = []
    for i, (pos, it) in enumerate(headers):
        end = headers[i + 1][0] if i + 1 < len(headers) else len(text)
        if it == n:
            verdicts.extend(_VERDICT.findall(text[pos:end]))
    return verdicts[-1].upper() if verdicts else None


# --------------------------------------------------------------------------- #
# Objective progress signal + stall detection (repo-derived, NOT agent-reported)
# --------------------------------------------------------------------------- #
#
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
    raw = read_text(_ledger_path(target))
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def record_progress(target: str, n: int, signal: int) -> list[dict]:
    """Append (iteration, signal) to the ledger and persist it. Idempotent per n:
    a re-run of iteration n overwrites its prior entry rather than duplicating."""
    ledger = [e for e in read_ledger(target) if e.get("iteration") != n]
    ledger.append({"iteration": n, "signal": signal, "at": utc_now()})
    ledger.sort(key=lambda e: e.get("iteration", 0))
    path = _ledger_path(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2)
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
_NEXT_LINE = re.compile(r"(?mi)^" + _LEAD + r"Next\*{0,2}\s*:\s*\*{0,2}\s*(.*)$")
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
