#!/usr/bin/env bash
# Verification harness for a Lean 4 + Mathlib formalization.
#
# TEMPLATE: set PROJECT to your project's source-dir / namespace name and fill
# ALL_THEOREMS with the names you froze in Theorems.lean. Nothing else needs to
# change — the checks below are problem-independent.
#
# Project layout (every project follows this shape):
#   * Sources live under <PROJECT>/.
#   * The frozen pair <PROJECT>/Defs.lean + <PROJECT>/Theorems.lean are pinned by
#     SHA-256 in scripts/frozen.sha256.
#   * Theorems.lean holds the immutable statements as `sorry` stubs; the proofs
#     live in <PROJECT>/Proofs/** and are exposed as clean, named theorems in
#     <PROJECT>/Solution.lean (<PROJECT>.Solution.<name>). <PROJECT>/Discharge.lean
#     pairs each frozen statement with its proof via `@Frozen = @Proof := rfl`.
#
# Usage:
#   scripts/verify.sh [--no-log] [<theorem_name> | --all]
#
# With no theorem (or --all), verifies the whole solution. With a theorem name,
# the axiom check (Check 4) is restricted to that theorem; the project-wide
# checks (pins, banned keywords, build, gates) always run.
#
# Checks:
#   1. Frozen SHA pins      Defs.lean / Theorems.lean match scripts/frozen.sha256.
#   2. Banned keywords      No sorry/sorryAx/native_decide/admit/axiom/unsafe in
#                           any first-party *.lean (comment-aware). `sorry` is
#                           allowed ONLY in Theorems.lean (the frozen stubs). An
#                           `axiom` declaration is allowed ONLY if its name is
#                           whitelisted in scripts/ALLOWED_AXIOMS.txt (assumed
#                           certificates the user permitted in USER_NOTES.md).
#   3. lake build clean     Exit 0, no errors, no warnings except the expected
#                           `declaration uses 'sorry'` from Theorems.lean.
#   4. #print axioms        Each <PROJECT>.Solution.<name> depends only on the
#                           standard axioms {propext, Classical.choice, Quot.sound}
#                           plus any names whitelisted in scripts/ALLOWED_AXIOMS.txt.
#                           ANY other axiom (sorryAx, native_decide, a stray custom
#                           axiom, …) fails this check.
#  4b. Mandatory axioms     If MANDATORY_AXIOMS is non-empty, FINAL_THEOREM must
#                           genuinely DEPEND on every name in it. Check 4 only
#                           rejects EXTRA axioms, so without this a proof that
#                           quietly took a route avoiding a required certificate
#                           passes. Skipped when MANDATORY_AXIOMS is empty.
#   5. Statement gates      Discharge.lean and Solution.lean compile.
#  5b. Frozen ↔ Solution    A gate `@<P>.<t> = @<P>.Solution.<t> := rfl` is
#                           GENERATED here for every frozen theorem and compiled.
#                           Check 5 alone only proves Discharge.lean's own gates
#                           hold — an empty Discharge.lean compiles, and projects
#                           commonly gate `<t>_proof` rather than the
#                           `Solution.<t>` that Check 4 audits.
#
# Exit code = number of failed checks (0 = PASS), or 64 if the harness could not
# run at all (bad usage / missing file / poisoned allowlist). Never conflate the
# two: 64 means NOTHING was verified.

set -euo pipefail

# Exit code for a PRE-FLIGHT failure — bad usage, a missing required file, a
# poisoned allowlist. Deliberately outside the range of "number of failed
# checks", so a caller can tell "the harness could not run" from "the harness ran
# and found N problems". Exiting 1 for both made an unrunnable harness look like
# one ordinary failing check.
EX_PREFLIGHT=64

# === TEMPLATE: fill these two in ============================================
# The project source-dir / root namespace (the directory holding Defs.lean).
PROJECT="<Project>"
# The frozen theorem names (= <PROJECT>.Solution.<name> = <PROJECT>.<name>).
ALL_THEOREMS=("theorem_one" "theorem_two" "theorem_three")
# Check 4b — the POSITIVE axiom requirement. Check 4 only bounds the axiom set
# from ABOVE: it rejects EXTRA axioms, so a proof that uses FEWER passes it
# trivially. When USER_NOTES.md requires an assumed certificate to actually be
# USED — e.g. "follow the paper's route, not the elementary one that needs no
# certificate" — name the headline theorem and the axioms its `#print axioms`
# MUST contain. An empty MANDATORY_AXIOMS disables the check (the normal case).
FINAL_THEOREM=""
MANDATORY_AXIOMS=()
# ============================================================================

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO_ROOT/$PROJECT"
DEFS_FILE="$SRC_DIR/Defs.lean"
THEOREMS_FILE="$SRC_DIR/Theorems.lean"
PINS_FILE="$REPO_ROOT/scripts/frozen.sha256"
# Whitelisted axioms: fully-qualified names, comma- and/or newline-separated, that
# the user permitted in USER_NOTES.md and init.py recorded here. Absent/empty =>
# the default strict policy (only the three standard axioms, no custom `axiom`).
ALLOWED_AXIOMS_FILE="$REPO_ROOT/scripts/ALLOWED_AXIOMS.txt"

usage() { echo "Usage: $0 [--no-log] [<theorem_name> | --all]"; }

NO_LOG=0
TARGET="--all"
while [ $# -gt 0 ]; do
    case "$1" in
        --no-log|--dry-run) NO_LOG=1; shift ;;
        -h|--help) usage; exit 0 ;;
        --all) TARGET="--all"; shift ;;
        -*) echo "ERROR: unknown option: $1"; usage; exit $EX_PREFLIGHT ;;
        *) TARGET="$1"; shift ;;
    esac
done

# Resolve target theorem list.
if [ "$TARGET" = "--all" ]; then
    TARGETS=("${ALL_THEOREMS[@]}")
else
    found=0
    for t in "${ALL_THEOREMS[@]}"; do [ "$t" = "$TARGET" ] && found=1; done
    if [ "$found" -eq 0 ]; then
        echo "ERROR: unknown theorem '$TARGET'. Known: ${ALL_THEOREMS[*]}"
        exit $EX_PREFLIGHT
    fi
    TARGETS=("$TARGET")
fi

for required in "$DEFS_FILE" "$THEOREMS_FILE" "$PINS_FILE"; do
    [ -f "$required" ] || { echo "ERROR: required file not found: $required"; exit $EX_PREFLIGHT; }
done

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    else shasum -a 256 "$1" | awk '{print $1}'; fi
}

# Run `lake env lean` / `lake build`, sourcing elan if present.
run_lake() {
    if [ -f "$HOME/.elan/env" ]; then ( . "$HOME/.elan/env" && cd "$REPO_ROOT" && lake "$@" );
    else ( cd "$REPO_ROOT" && lake "$@" ); fi
}

# Standard axioms always permitted, plus any whitelisted via ALLOWED_AXIOMS.txt
# (comments with `#` and blank lines ignored; comma- or newline-separated names).
STD_AXIOMS=(propext Classical.choice Quot.sound)
# Proof-hole axioms that can NEVER be whitelisted, whatever ALLOWED_AXIOMS.txt
# says. That file is generated, so the allowlist must not be able to authorise the
# very things the harness exists to detect: it is for assumed MATHEMATICAL
# certificates only. Without this, one bad line makes Check 4 vacuous.
DENIED_AXIOMS=" sorryAx ofReduceBool ofReduceNat Lean.ofReduceBool Lean.ofReduceNat "
ALLOWED_AXIOMS=()
if [ -f "$ALLOWED_AXIOMS_FILE" ]; then
    while IFS= read -r _ax; do
        [ -z "$_ax" ] && continue
        case "$DENIED_AXIOMS" in
            *" $_ax "*)
                echo "ERROR: $ALLOWED_AXIOMS_FILE tries to whitelist \`$_ax\`, which"
                echo "       is a proof hole, not an assumed certificate. Refusing to run."
                exit $EX_PREFLIGHT ;;
        esac
        ALLOWED_AXIOMS+=("$_ax")
    done < <(sed 's/#.*//' "$ALLOWED_AXIOMS_FILE" | tr ',' '\n' | tr -d ' \t\r' | grep -v '^$' || true)
fi

echo "=== Verifying $PROJECT formalization ==="
echo "  Target: $TARGET"
if [ "${#ALLOWED_AXIOMS[@]}" -gt 0 ]; then
    echo "  Whitelisted axioms (USER_NOTES.md): ${ALLOWED_AXIOMS[*]}"
fi
[ "$NO_LOG" -eq 1 ] && echo "  Log mode: disabled"
echo ""

ERRORS=0
START_TIME=$(date +%s)

# --- Check 1: Frozen SHA pins ---
echo "--- Check 1: Frozen SHA pins ---"
# `|| [ -n "$pinned" ]` so a pins file with no trailing newline still checks its
# LAST line — that line is conventionally Theorems.lean, the file most worth
# pinning, and skipping it would leave Check 1 reporting PASS on an unverified pin.
PINNED_PATHS=""
while read -r pinned relpath || [ -n "$pinned" ]; do
    [ -z "$pinned" ] && continue
    case "$pinned" in \#*) continue ;; esac    # skip comment lines
    PINNED_PATHS="$PINNED_PATHS $relpath"
    # A missing/unreadable pinned file must FAIL the check, not abort the harness.
    actual=$(sha256_of "$REPO_ROOT/$relpath" 2>/dev/null || true)
    if [ "$pinned" = "$actual" ]; then
        echo "PASS: $relpath pin matches"
    else
        echo "FAIL: $relpath SHA pin mismatch"
        echo "  Pinned: $pinned"
        echo "  Actual: $actual"
        ERRORS=$((ERRORS + 1))
    fi
done < "$PINS_FILE"

# A pins file with no pin lines produced ZERO iterations above and therefore a
# silent PASS: Check 1 would report nothing while verifying nothing. Both frozen
# files must be named explicitly — pinning one and omitting the other left the
# omitted one free to be edited for the rest of the run.
for _req in "$PROJECT/Defs.lean" "$PROJECT/Theorems.lean"; do
    case " $PINNED_PATHS " in
        *" $_req "*) ;;
        *) echo "FAIL: $_req is NOT pinned in scripts/frozen.sha256 — it could be"
           echo "      edited without detection"; ERRORS=$((ERRORS + 1)) ;;
    esac
done

# --- Check 2: Banned keywords (comment-aware) ---
echo ""
echo "--- Check 2: Banned keywords ---"
# The embedded script exits 1 when it FINDS violations, so the substitution must
# run unguarded by `set -e` — otherwise a genuine cheat aborts the harness before
# it can report, and the check becomes pass-only.
set +e
BANNED_OUT=$(SRC_DIR="$SRC_DIR" ROOT_LEAN="$REPO_ROOT/$PROJECT.lean" THEOREMS_FILE="$THEOREMS_FILE" \
    ALLOWED_AXIOMS="$(printf '%s\n' ${ALLOWED_AXIOMS[@]+"${ALLOWED_AXIOMS[@]}"})" python3 - <<'PY'
import os, re, sys, glob
src_dir = os.environ["SRC_DIR"]
theorems = os.environ["THEOREMS_FILE"]
root_lean = os.environ["ROOT_LEAN"]
# Whitelisted axioms (fully-qualified). Compare by short name, since a declaration
# `axiom cert_x` inside `namespace MyProj` is the FQN `MyProj.cert_x`.
allowed_axioms = {a.strip() for a in os.environ.get("ALLOWED_AXIOMS", "").split() if a.strip()}
allowed_axiom_short = {a.split(".")[-1] for a in allowed_axioms}
banned = ["sorry", "sorryAx", "native_decide", "admit", "unsafe",
          "implemented_by", "ofReduceBool",
          # Disables kernel re-typechecking: a proof accepted only by the
          # elaborator, leaving NO trace in `#print axioms`. Nothing else here
          # would catch it.
          "debug.skipKernelTC"]
def strip_comments(s):
    """Remove comments. String literals are dropped whole — never scanned, and
    never able to open a comment.

    Without string state, `def m : String := "/-"` opens a block comment that
    swallows every following line until a `"-/"` appears, hiding a `sorry` from
    this scanner entirely."""
    out=[]; i=0; n=len(s); depth=0; in_str=False
    while i<n:
        c=s[i]; two=s[i:i+2]
        if in_str:
            if c=="\\" and i+1<n: i+=2; continue   # escape: skip both chars
            if c=='"': in_str=False
            i+=1                                    # drop string contents
        elif depth==0 and c=='"':
            in_str=True; i+=1
        elif depth==0 and two=="--":
            j=s.find("\n", i)
            if j==-1: break
            i=j
        elif two=="/-":
            depth+=1; i+=2
        elif depth>0 and two=="-/":
            depth-=1; i+=2
        elif depth>0:
            i+=1
        else:
            out.append(c); i+=1
    return "".join(out)
files = sorted(glob.glob(os.path.join(src_dir, "**", "*.lean"), recursive=True))
if os.path.isfile(root_lean): files.append(root_lean)
bad=0
for f in files:
    code = strip_comments(open(f, encoding="utf-8").read())
    allow_sorry = (os.path.abspath(f) == os.path.abspath(theorems))
    # `axiom` declarations: allowed ONLY if the declared name is whitelisted
    # (an assumed certificate the user permitted in USER_NOTES.md).
    # Modifiers and attributes are ordinary Lean: `private axiom foo` is still an
    # axiom. Matching only a bare `axiom` at line start let every one of them past.
    _AXIOM_RE = (r"(?m)^\s*(?:@\[[^\]]*\]\s*)*"
                 r"(?:(?:private|protected|noncomputable|unsafe|partial|scoped|local)\s+)*"
                 r"axiom\s+([A-Za-z_][\w'.]*)")
    for am in re.finditer(_AXIOM_RE, code):
        if am.group(1).split(".")[-1] not in allowed_axiom_short:
            print(f"  {f}: contains non-whitelisted `axiom {am.group(1)}`"); bad+=1
    for kw in banned:
        if kw == "sorry" and allow_sorry:
            continue
        if re.search(r'\b'+re.escape(kw)+r'\b', code):
            print(f"  {f}: contains banned `{kw}`"); bad+=1
sys.exit(1 if bad else 0)
PY
)
BANNED_EXIT=$?
set -e
if [ "$BANNED_EXIT" -eq 0 ]; then
    echo "PASS: no banned keywords (sorry allowed only in Theorems.lean)"
else
    echo "FAIL: banned keywords detected"
    echo "$BANNED_OUT"
    ERRORS=$((ERRORS + 1))
fi

# --- Check 3: lake build clean ---
echo ""
echo "--- Check 3: lake build ---"
set +e
BUILD_OUTPUT=$(run_lake build 2>&1)
BUILD_EXIT=$?
set -e
BUILD_ERRORS=$(echo "$BUILD_OUTPUT" | grep -c "^error:" || true)
# Only Theorems.lean is allowed to emit a sorry warning. Filtering the message
# everywhere made a `sorry` in Proofs/** invisible here too — the PASS line says
# "only expected Theorems.lean sorry warnings", so scope the filter to that file.
BUILD_WARNINGS=$(echo "$BUILD_OUTPUT" | grep "warning:" \
    | grep -v "Theorems\.lean.*declaration uses .sorry." | wc -l | tr -d '[:space:]' || true)
echo "$BUILD_OUTPUT" | tail -1
if [ "$BUILD_EXIT" -eq 0 ] && [ "$BUILD_ERRORS" -eq 0 ] && [ "$BUILD_WARNINGS" -eq 0 ]; then
    echo "PASS: build clean (only expected Theorems.lean sorry warnings)"
else
    echo "FAIL: build exit=$BUILD_EXIT, errors=$BUILD_ERRORS, unexpected warnings=$BUILD_WARNINGS"
    { echo "$BUILD_OUTPUT" | grep -E "^error:|warning:" | grep -v "declaration uses .sorry." | head -20; } || true
    ERRORS=$((ERRORS + 1))
fi

# --- Check 4: #print axioms ---
echo ""
echo "--- Check 4: #print axioms ($PROJECT.Solution.*) ---"
# mktemp only substitutes X's at the END of a template, so a "*_XXXX.lean" name
# is taken literally on BSD/macOS. Use a temp dir to keep the .lean extension.
AX_DIR=$(mktemp -d)
AX_FILE="$AX_DIR/verify_ax.lean"
trap 'rm -rf "$AX_DIR"' EXIT
# Check 4b inspects FINAL_THEOREM's axioms, which must therefore be printed even
# when a single OTHER theorem was named on the command line — otherwise
# `verify.sh <some_lemma>` finds no record for it and reports every mandatory
# axiom missing, so single-theorem mode could never pass.
{ echo "import $PROJECT"
  for t in "${TARGETS[@]}"; do echo "#print axioms $PROJECT.Solution.$t"; done
  if [ "${#MANDATORY_AXIOMS[@]}" -gt 0 ] && [ "$TARGET" != "--all" ] \
     && [ "$TARGET" != "$FINAL_THEOREM" ]; then
      echo "#print axioms $PROJECT.Solution.$FINAL_THEOREM"
  fi
} > "$AX_FILE"
set +e
AX_OUTPUT=$(run_lake env lean "$AX_FILE" 2>&1)
set -e
AX_FAIL=0
# The full allowlist: the three standard axioms plus any whitelisted names.
ALLOW_SET=" ${STD_AXIOMS[*]} ${ALLOWED_AXIOMS[*]+${ALLOWED_AXIOMS[*]}} "

# `#print axioms` WRAPS its bracketed list across lines as soon as the list is
# long — exactly what happens for declarations carrying a custom axiom. A
# line-oriented grep + `sed 's/.*\[\(.*\)\].*/\1/'` then extracts NOTHING, the
# loop below finds no bad names, and it reports PASS *vacuously* on precisely the
# declarations this check exists to police. So flatten to one line FIRST and match
# each declaration from its name to the first `]`.
AX_FLAT=$(printf '%s' "$AX_OUTPUT" | tr '\n\t' '  ' | tr -s ' ')

# axioms_of <fully.qualified.name> — that declaration's axioms, one per word.
# Prints nothing when there is no `depends on axioms` record (missing name, build
# error, or "does not depend on any axioms").
axioms_of() {
    # The colon is optional so the parse survives either Lean phrasing; the name
    # is BRE-escaped so `.` cannot match a stray character.
    _aq=$(printf '%s' "$1" | sed 's/[.[\]*^$\\]/\\&/g')
    # `|| true` here, not only at the call sites: "no match" is a normal result
    # for this function, and a future caller that forgets to guard the call would
    # otherwise abort the whole harness under `set -e`.
    { printf '%s\n' "$AX_FLAT" \
        | grep -o "'$_aq' depends on axioms[:]* \[[^]]*\]" \
        | sed -e 's/^.*\[//' -e 's/\]$//' | tr ',' ' '; } || true
}
for t in "${TARGETS[@]}"; do
    # Extraction must tolerate a miss — a miss is a real, reportable state here.
    axlist=$(axioms_of "$PROJECT.Solution.$t" || true)
    noax=$(printf '%s\n' "$AX_FLAT" \
        | grep -o "'$PROJECT\.Solution\.$t' does not depend on any axioms" || true)
    if [ -z "${axlist// /}" ] && [ -z "$noax" ]; then
        echo "FAIL: $t — no axiom output (build/name error)"; AX_FAIL=$((AX_FAIL+1)); continue
    fi
    # Every name must be in the allowlist. The `for` loop word-splits `$axlist`
    # (keeps dotted names like Classical.choice intact, drops surrounding spaces).
    bad=""
    for ax in $axlist; do
        case "$ALLOW_SET" in *" $ax "*) ;; *) bad="$bad $ax" ;; esac
    done
    if [ -n "$bad" ]; then
        echo "FAIL: $t — non-whitelisted axiom(s):$bad"; echo "   axioms: $axlist"
        AX_FAIL=$((AX_FAIL+1))
    else
        # Print the PARSED list, not the allowlist template: an auditor must be
        # able to see that the extraction really got the axioms rather than
        # finding nothing — the exact failure mode this parse used to have.
        echo "PASS: $t — axioms within allowlist: [$(echo $axlist | tr ' ' ',' | sed 's/,/, /g')]"
    fi
done

# --- Check 4b: the mandatory axioms are really USED by the headline theorem ---
# A formalization that proves FINAL_THEOREM without invoking an assumed
# certificate took a different route from the one USER_NOTES.md requires, and is
# unfaithful even though every proof compiles. Deliberately NO escape hatch: if
# the headline theorem is missing, this FAILS rather than being skipped.
if [ "${#MANDATORY_AXIOMS[@]}" -gt 0 ]; then
    echo ""
    echo "--- Check 4b: mandatory axiom dependencies ($FINAL_THEOREM) ---"
    final_ax=" $(axioms_of "$PROJECT.Solution.$FINAL_THEOREM" || true) "
    missing=""
    for ax in "${MANDATORY_AXIOMS[@]}"; do
        case "$final_ax" in *" $ax "*) ;; *) missing="$missing $ax" ;; esac
    done
    if [ -n "$missing" ]; then
        echo "FAIL: $FINAL_THEOREM — missing MANDATORY axiom dependency:$missing"
        echo "   (USER_NOTES.md requires the headline theorem to genuinely depend on"
        echo "    every assumed certificate; found:$final_ax)"
        AX_FAIL=$((AX_FAIL + 1))
    else
        echo "PASS: $FINAL_THEOREM — depends on all mandatory axioms {${MANDATORY_AXIOMS[*]}}"
    fi
fi

[ "$AX_FAIL" -ne 0 ] && ERRORS=$((ERRORS + 1))

# --- Check 5: Statement gates (Discharge + Solution compile) ---
echo ""
echo "--- Check 5: Statement gates (Discharge / Solution) ---"
GATE_FAIL=0
for mod in "$PROJECT.Discharge" "$PROJECT.Solution"; do
    set +e
    GOUT=$(run_lake build "$mod" 2>&1); GEXIT=$?
    set -e
    GERR=$(echo "$GOUT" | grep -c "^error:" || true)
    if [ "$GEXIT" -eq 0 ] && [ "$GERR" -eq 0 ]; then
        echo "PASS: $mod compiles (statement↔proof gate holds)"
    else
        echo "FAIL: $mod did not compile"; { echo "$GOUT" | grep "^error:" | head; } || true
        GATE_FAIL=$((GATE_FAIL+1))
    fi
done

# --- Check 5b: EVERY frozen statement is bound to the audited declaration ---
# Compiling Discharge.lean only proves whatever gates it HAPPENS to contain: an
# empty Discharge.lean compiles, and a frozen theorem with no gate is bound to
# nothing. Worse, projects gate `@P.<t> = @P.<t>_proof`, while Check 4 audits
# `P.Solution.<t>` — so a clean axiom report can describe a declaration that
# never had to match the frozen statement.
#
# So generate the gates HERE rather than trusting the project's file. Each
# `example : @P.<t> = @P.Solution.<t> := rfl` type-checks only when the audited
# declaration has EXACTLY the frozen type (proof irrelevance makes the terms
# equal precisely when the statements are defeq). Lean does the checking; there
# is no text parsing of Discharge.lean to be defeated.
echo ""
echo "--- Check 5b: frozen ↔ Solution binding (generated gates) ---"
GATE_LEAN="$AX_DIR/gates.lean"
{ echo "import $PROJECT"
  for t in "${TARGETS[@]}"; do echo "example : @$PROJECT.$t = @$PROJECT.Solution.$t := rfl"; done
} > "$GATE_LEAN"
set +e
BOUT=$(run_lake env lean "$GATE_LEAN" 2>&1); BEXIT=$?
set -e
if [ "$BEXIT" -eq 0 ] && [ -z "$(printf '%s' "$BOUT" | tr -d '[:space:]')" ]; then
    echo "PASS: all ${#TARGETS[@]} frozen statement(s) bind to $PROJECT.Solution.*"
else
    # Line N+1 of the generated file is TARGETS[N-1] (line 1 is the import), so a
    # reported line number names the theorem whose binding failed.
    for ln in $(printf '%s\n' "$BOUT" | sed -n 's/.*gates\.lean:\([0-9]*\):.*/\1/p' | sort -un); do
        idx=$((ln - 2))
        if [ "$idx" -ge 0 ] && [ "$idx" -lt "${#TARGETS[@]}" ]; then
            echo "FAIL: ${TARGETS[$idx]} — $PROJECT.Solution.${TARGETS[$idx]} does not have the frozen statement's type"
        fi
    done
    { printf '%s\n' "$BOUT" | grep -E 'error' | head -5; } || true
    GATE_FAIL=$((GATE_FAIL+1))
fi

[ "$GATE_FAIL" -ne 0 ] && ERRORS=$((ERRORS + 1))

# --- Summary ---
END_TIME=$(date +%s); DURATION=$((END_TIME - START_TIME))
SUCCESS="false"; [ "$ERRORS" -eq 0 ] && SUCCESS="true"
echo ""
echo "=== RESULT: $([ "$SUCCESS" = "true" ] && echo PASS || echo FAIL) ($ERRORS issue(s), ${DURATION}s) ==="

if [ "$NO_LOG" -eq 0 ]; then
    LOG_DIR="$REPO_ROOT/logs"; mkdir -p "$LOG_DIR"
    TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    echo "{\"timestamp\":\"$TS\",\"target\":\"$TARGET\",\"build_errors\":$BUILD_ERRORS,\"build_warnings\":$BUILD_WARNINGS,\"issues\":$ERRORS,\"duration_sec\":$DURATION,\"success\":$SUCCESS}" \
        >> "$LOG_DIR/verify_log.jsonl"
fi

exit "$ERRORS"
