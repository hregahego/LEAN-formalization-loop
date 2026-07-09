"""
formlib — shared plumbing for the autonomous Lean 4 formalization pipeline.

Every agent in this pipeline is a headless CLI subprocess. This module
owns three concerns so that setup.py / init.py / loop.py stay thin:

  1. Building and running an agent invocation (streaming + per-agent logs,
     watchdog timeout, dry-run). With output_format="json" the agent's final
     message is parsed out of the structured result.
  2. Running several agents in parallel (the 4 workers of one iteration).
  3. Reading the control signals: the PRIMARY signal is a machine-readable
     `<<<ORCH {...} ORCH>>>` trailer the Plan/Review agent emits in its final
     message (carrying its own iteration number); the FALLBACK is an
     iteration-scoped parse of the append-only TASKS.md / REVIEW.md. Because
     those files are append-only, every file parse is scoped to ONE iteration's
     block so stale entries from earlier iterations are never returned.

Configuration is read from config.json next to these scripts. The default file
uses Claude (`claude -p`); set `"agent_cli": "codex"` to use `codex exec`.
"""

from __future__ import annotations

import os
import re
import sys
import json
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
    "reference_dir": "reference",
    "permission_mode": "skip",
    "stall_window": 16,
    "crux_recur_limit": 16,
    "timeouts": {
        "setup": 3600,
        "init": 10800,
        "plan": 10800,
        "worker": 10800,
        "review": 10800,
    },
    "codex_extra_args": [],
    "claude_extra_args": [],
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

# The format-template project. Relative paths are resolved next to these
# scripts, so the repo is self-contained and portable.
_ref = os.path.expanduser(str(CONFIG.get("reference_dir") or "reference"))
REFERENCE_DIR = os.path.abspath(_ref if os.path.isabs(_ref) else os.path.join(_SCRIPT_DIR, _ref))
PERMISSION_MODE = str(CONFIG.get("permission_mode") or "skip")

# Generous defaults: a Mathlib `lake exe cache get` + cold build (init) can take
# well over an hour; a worker proving a hard lemma can run a long time too.
_timeouts_val = CONFIG.get("timeouts")
_TIMEOUTS = _timeouts_val if isinstance(_timeouts_val, dict) else {}
SETUP_TIMEOUT = int(_TIMEOUTS.get("setup", 1800))
INIT_TIMEOUT = int(_TIMEOUTS.get("init", 7200))
PLAN_TIMEOUT = int(_TIMEOUTS.get("plan", 1200))
WORKER_TIMEOUT = int(_TIMEOUTS.get("worker", 3600))
REVIEW_TIMEOUT = int(_TIMEOUTS.get("review", 2400))

def _list_config(key: str) -> list[str]:
    value = CONFIG.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        sys.exit(f'ERROR: config.json "{key}" must be a list of strings.')
    return list(value)


def _claude_permission_args() -> list[str]:
    if PERMISSION_MODE == "skip":
        return ["--dangerously-skip-permissions"]
    if PERMISSION_MODE in ("acceptEdits", "bypassPermissions", "plan", "default"):
        return ["--permission-mode", PERMISSION_MODE]
    # Unknown value: fall back to the most permissive headless mode.
    return ["--dangerously-skip-permissions"]


def _codex_permission_args() -> list[str]:
    if PERMISSION_MODE in ("skip", "bypassPermissions"):
        return ["--dangerously-bypass-approvals-and-sandbox"]
    if PERMISSION_MODE == "default":
        return []
    if PERMISSION_MODE == "workspace-write":
        return ["--sandbox", "workspace-write"]
    if PERMISSION_MODE == "read-only":
        return ["--sandbox", "read-only"]
    if PERMISSION_MODE == "danger-full-access":
        return ["--sandbox", "danger-full-access"]
    # Claude-specific modes such as acceptEdits/plan do not have a direct Codex
    # exec equivalent; use Codex's configured defaults.
    return []


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
              model: str | None = None, extra: list[str] | None = None,
              output_format: str = "text") -> list[str]:
    chosen = model or DEFAULT_MODEL
    if AGENT_CLI == "codex":
        cmd = [CODEX_BIN, "exec"]
        cmd += _codex_permission_args()
        if chosen:
            cmd += ["--model", str(chosen)]
        for d in add_dirs or []:
            cmd += ["--add-dir", d]
        cmd += _list_config("codex_extra_args")
        cmd += extra or []
        cmd += [prompt]
        return cmd

    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", output_format]
    cmd += _claude_permission_args()
    if chosen:
        cmd += ["--model", str(chosen)]
    for d in add_dirs or []:
        cmd += ["--add-dir", d]
    cmd += _list_config("claude_extra_args")
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
              stream: bool = True,
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
        if stream and result_text:
            for line in result_text.splitlines():
                sys.stdout.write(f"\033[2m[{label}]\033[0m {line}\n")
            sys.stdout.flush()
    else:
        raw, rc, timed_out = _exec_stream(cmd, cwd, timeout, log_fh, label, stream)
        result_text = raw

    if log_fh:
        log_fh.write(f"\n# exit={rc} timed_out={timed_out} @ {utc_now()}\n")
        log_fh.close()

    result = AgentResult(label, rc, raw, timed_out=timed_out, result_text=result_text)
    status = "OK" if result.ok else ("TIMEOUT" if result.timed_out else f"exit {rc}")
    log(f"{label} finished: {status}")
    return result


def _exec_stream(cmd, cwd, timeout, log_fh, label, stream):
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
            if stream:
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
        for fut in futures:
            pass
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

_ITER_HEADER = re.compile(r"(?m)^##\s*Iteration\s+(\d+)\b")
_AGENT_LINE = re.compile(r"(?m)^\s*Agent\s+(\d+)\s*:")
_VERDICT = re.compile(r"(?mi)^\s*Verdict:\s*(COMPLETE|INCOMPLETE)\b")
_REVIEW_HEADER = re.compile(r"(?m)^##\s*Review\b[^\n]*?Iteration\s+(\d+)\b")
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
        nums = {int(k) for k in tr["active_agents"] if str(k).isdigit() or isinstance(k, int)}
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
    header = re.search(rf"(?m)^##\s*Iteration\s+{n}\b", text)
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

# `theorem <name>` at top level (not inside a block comment / not doc-commented
# away). We approximate "actually discharged" as: the name appears as a top-level
# `theorem <name>` in Solution.lean AND is assigned a proof term (`:= ...`) rather
# than left `sorry`. Solution.lean is the single source of truth for what has been
# genuinely exposed as a clean, frozen-signature theorem.
def _solution_frozen_names(target: str) -> list[str]:
    """The frozen theorem names, read from scripts/verify.sh's ALL_THEOREMS.

    Project-agnostic: the names come from the harness-generated verify.sh, never
    hardcoded. If ALL_THEOREMS cannot be parsed (e.g. a verify.sh refactor), returns
    [] so the progress signal reads 0 and the stall guard surfaces the broken
    harness — rather than silently assuming another project's theorem names."""
    verify = read_text(os.path.join(target, "scripts", "verify.sh"))
    m = re.search(r"ALL_THEOREMS=\(([^)]*)\)", verify)
    if m:
        names = re.findall(r'"([^"]+)"', m.group(1))
        if names:
            return names
    log("warning: could not parse ALL_THEOREMS from scripts/verify.sh — "
        "progress signal will read 0")
    return []


def _project_name(target: str) -> str | None:
    """The project namespace/directory name, read from scripts/verify.sh's
    `PROJECT="..."` line. This is what `verify.sh` itself uses, so it is the
    authoritative source; falling back to a directory scan for a `Solution.lean`
    keeps the signal working even if `PROJECT=` is absent. Returns None only if no
    project directory can be identified (then `progress_signal` reports 0)."""
    verify = read_text(os.path.join(target, "scripts", "verify.sh"))
    m = re.search(r'(?m)^\s*PROJECT\s*=\s*"?([A-Za-z0-9_]+)"?', verify)
    if m:
        return m.group(1)
    # Fallback: the (single) top-level directory that contains a Solution.lean.
    try:
        for name in sorted(os.listdir(target)):
            if name.startswith(".") or name == "scripts":
                continue
            if os.path.isfile(os.path.join(target, name, "Solution.lean")):
                return name
    except OSError:
        pass
    return None


def progress_signal(target: str) -> int:
    """Number of frozen theorems genuinely discharged in Solution.lean.

    This is the project's true-north: the deliverable is the frozen theorems
    proved sorry-free, not the count of auxiliary lemmas. Scaffolding/wrapper/
    equivalence churn does NOT move this number, which is exactly what we want a
    stall detector to key on. Project-agnostic: the project directory is read from
    verify.sh's `PROJECT=` (NOT hardcoded), so the signal works on any project the
    harness set up, not only the one it was first developed on.
    """
    proj = _project_name(target)
    if proj is None:
        return 0
    sol = read_text(os.path.join(target, proj, "Solution.lean"))
    if not sol:
        return 0
    count = 0
    for name in _solution_frozen_names(target):
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
_NEXT_LINE = re.compile(r"(?mi)^Next:\s*(.*)$")
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
    for entry in re.split(r"(?m)^(?=##\s)", text):
        if since_iteration is not None:
            m = _ENTRY_ITER.search(entry)
            if m is None or int(m.group(1)) <= since_iteration:
                continue
        for line in _NEXT_LINE.findall(entry):
            for name in set(_IDENT.findall(line)):
                counts[name] = counts.get(name, 0) + 1
    if not counts:
        return None
    name, count = max(counts.items(), key=lambda kv: kv[1])
    return (name, count) if count >= threshold else None
