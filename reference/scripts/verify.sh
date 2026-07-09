#!/usr/bin/env bash
# Verification harness for a Lean 4 + Mathlib formalization.
#
# TEMPLATE: set PROJECT to your project's source-dir / namespace name and fill
# ALL_THEOREMS with the names you froze in Theorems.lean. Nothing else needs to
# change — the five checks below are problem-independent.
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
#   5. Statement gates      Discharge.lean (`@Frozen = @Proof := rfl`) and
#                           Solution.lean (`:= <proof>`) compile — machine proof
#                           that each clean theorem has exactly the frozen type.
#
# Exit code = number of failed checks (0 = PASS).

set -euo pipefail

# === TEMPLATE: fill these two in ============================================
# The project source-dir / root namespace (the directory holding Defs.lean).
PROJECT="<Project>"
# The frozen theorem names (= <PROJECT>.Solution.<name> = <PROJECT>.<name>).
ALL_THEOREMS=("theorem_one" "theorem_two" "theorem_three")
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
        -*) echo "ERROR: unknown option: $1"; usage; exit 1 ;;
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
        exit 1
    fi
    TARGETS=("$TARGET")
fi

for required in "$DEFS_FILE" "$THEOREMS_FILE" "$PINS_FILE"; do
    [ -f "$required" ] || { echo "ERROR: required file not found: $required"; exit 1; }
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
ALLOWED_AXIOMS=()
if [ -f "$ALLOWED_AXIOMS_FILE" ]; then
    while IFS= read -r _ax; do
        [ -n "$_ax" ] && ALLOWED_AXIOMS+=("$_ax")
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
while read -r pinned relpath; do
    [ -z "$pinned" ] && continue
    case "$pinned" in \#*) continue ;; esac    # skip comment lines
    actual=$(sha256_of "$REPO_ROOT/$relpath")
    if [ "$pinned" = "$actual" ]; then
        echo "PASS: $relpath pin matches"
    else
        echo "FAIL: $relpath SHA pin mismatch"
        echo "  Pinned: $pinned"
        echo "  Actual: $actual"
        ERRORS=$((ERRORS + 1))
    fi
done < "$PINS_FILE"

# --- Check 2: Banned keywords (comment-aware) ---
echo ""
echo "--- Check 2: Banned keywords ---"
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
          "implemented_by", "ofReduceBool"]
def strip_comments(s):
    out=[]; i=0; n=len(s); depth=0
    while i<n:
        two=s[i:i+2]
        if depth==0 and two=="--":
            j=s.find("\n", i);
            if j==-1: break
            i=j
        elif two=="/-":
            depth+=1; i+=2
        elif depth>0 and two=="-/":
            depth-=1; i+=2
        elif depth>0:
            i+=1
        else:
            out.append(s[i]); i+=1
    return "".join(out)
files = sorted(glob.glob(os.path.join(src_dir, "**", "*.lean"), recursive=True))
if os.path.isfile(root_lean): files.append(root_lean)
bad=0
for f in files:
    code = strip_comments(open(f, encoding="utf-8").read())
    allow_sorry = (os.path.abspath(f) == os.path.abspath(theorems))
    # `axiom` declarations: allowed ONLY if the declared name is whitelisted
    # (an assumed certificate the user permitted in USER_NOTES.md).
    for am in re.finditer(r"(?m)^\s*axiom\s+([A-Za-z_][\w'.]*)", code):
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
BUILD_WARNINGS=$(echo "$BUILD_OUTPUT" | grep "warning:" \
    | grep -v "declaration uses .sorry." | wc -l | tr -d '[:space:]' || true)
echo "$BUILD_OUTPUT" | tail -1
if [ "$BUILD_EXIT" -eq 0 ] && [ "$BUILD_ERRORS" -eq 0 ] && [ "$BUILD_WARNINGS" -eq 0 ]; then
    echo "PASS: build clean (only expected Theorems.lean sorry warnings)"
else
    echo "FAIL: build exit=$BUILD_EXIT, errors=$BUILD_ERRORS, unexpected warnings=$BUILD_WARNINGS"
    echo "$BUILD_OUTPUT" | grep -E "^error:|warning:" | grep -v "declaration uses .sorry." | head -20
    ERRORS=$((ERRORS + 1))
fi

# --- Check 4: #print axioms ---
echo ""
echo "--- Check 4: #print axioms ($PROJECT.Solution.*) ---"
AX_FILE=$(mktemp /tmp/verify_ax_XXXX.lean)
trap 'rm -f "$AX_FILE"' EXIT
{ echo "import $PROJECT"; for t in "${TARGETS[@]}"; do echo "#print axioms $PROJECT.Solution.$t"; done; } > "$AX_FILE"
set +e
AX_OUTPUT=$(run_lake env lean "$AX_FILE" 2>&1)
set -e
AX_FAIL=0
# The full allowlist: the three standard axioms plus any whitelisted names.
ALLOW_SET=" ${STD_AXIOMS[*]} ${ALLOWED_AXIOMS[*]+${ALLOWED_AXIOMS[*]}} "
for t in "${TARGETS[@]}"; do
    line=$(echo "$AX_OUTPUT" | grep "$PROJECT.Solution.$t' depends on axioms")
    noax=$(echo "$AX_OUTPUT" | grep "$PROJECT.Solution.$t' does not depend on any axioms")
    if [ -z "$line" ] && [ -z "$noax" ]; then
        echo "FAIL: $t — no axiom output (build/name error)"; AX_FAIL=$((AX_FAIL+1)); continue
    fi
    # Parse the bracketed axiom list; every name must be in the allowlist. Split
    # on commas into whitespace and let the for-loop word-split (keeps dotted
    # names like Classical.choice intact, drops surrounding spaces).
    axlist=$(echo "$line" | sed -n 's/.*\[\(.*\)\].*/\1/p' | tr ',' ' ')
    bad=""
    for ax in $axlist; do
        case "$ALLOW_SET" in *" $ax "*) ;; *) bad="$bad $ax" ;; esac
    done
    if [ -n "$bad" ]; then
        echo "FAIL: $t — non-whitelisted axiom(s):$bad"; echo "   $line"; AX_FAIL=$((AX_FAIL+1))
    else
        echo "PASS: $t — axioms within allowlist {${STD_AXIOMS[*]}${ALLOWED_AXIOMS[*]+ + ${ALLOWED_AXIOMS[*]}}}"
    fi
done
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
        echo "FAIL: $mod did not compile"; echo "$GOUT" | grep "^error:" | head; GATE_FAIL=$((GATE_FAIL+1))
    fi
done
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
