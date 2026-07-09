# Blueprint: <one-line problem title>

> **This is a FORMAT TEMPLATE, not a worked problem.** It shows the *structure*,
> *section order*, and *process rules* that every problem's `BLUEPRINT.md` must
> follow. Everything in `<angle brackets>` is a placeholder you replace with
> content derived from THIS problem's `SKETCH.md`. Keep every section heading and
> every process rule; replace only the mathematical content. Do not invent a
> decomposition for a problem you were not given — read `SKETCH.md` first.

A roadmap for formalizing, in **Lean 4 + Mathlib**, the result in `SKETCH.md`.
<One short paragraph: the high-level construction or strategy — what objects are
built, what the final claim is, and the single idea that makes it work.>

- **Headline target (frozen theorem `<headline_name>`).** <State the one theorem
  a reader ultimately cites — usually an existential or a single equation/▸
  refutation. Name the predicates/objects it references; all of them are frozen
  in `Defs.lean`. If the headline is witnessed by a concrete construction, name
  the companion theorem(s) that exhibit the witness, and any half-statements that
  are frozen on their own.>
- **Recommended intermediate milestone (prove first):** <The smallest
  self-contained sub-result that is the *mathematical heart* — the part that, once
  proved, makes the rest bookkeeping/assembly. Prove this before the full
  pipeline; it de-risks the project and is often citable on its own.>
- **Setting / ground assumptions:** <The ambient category and coefficients (e.g.
  which ring/field, finite vs infinite, decidable vs not). Explicitly state what
  is *not* involved (e.g. "no analysis", "no inequalities", "everything finite")
  — bounding the search space is part of the spec.>

> **Why this is tractable.** <2–4 sentences. What makes the problem decidable,
> finite, or cleanly decomposable, and — just as important — where the real risk
> lies. For most formalizations the risk is **mis-modeling** (a wrong definition,
> a weakened statement) and **over-reach** (assuming the thing you must prove),
> not deep mathematics. Name the specific traps for THIS problem.>

---

## Part −1 — Setting up the repository (the SETUP stage)

The goal of this stage is to produce a compiling skeleton in which **every
`Definition` and every `Theorem` statement is written and frozen**, with all
proofs `:= sorry`. Once frozen, `Defs.lean` and `Theorems.lean` are **never
edited again** during the proving phase. Everything proved later lives in
support files and may not change a single character of the frozen statements.

### 1. Create the Lean project

```bash
cd <project-dir>
lake +leanprover/lean4:<toolchain-version> new <project> math   # e.g. v4.31.0
# pin Mathlib in lakefile + lake-manifest to the matching rev, then:
lake exe cache get
lake build            # must succeed on the bare skeleton before anything else
```

Layout (every project follows this shape; rename only the bracketed parts):

```
<project-dir>/
  <Project>/
    Defs.lean          -- FROZEN after this stage: every object the proof needs
    Theorems.lean      -- FROZEN after this stage: the frozen theorem statements (sorry)
    Proofs/
      <StageA>/        -- Stage A: <one-line role>
      <StageB>/        -- Stage B: <one-line role>
      <StageC>/        -- … one subdirectory per stage of Part 2 …
    Discharge.lean     -- pairs each frozen statement with its proof via `@Frozen = @Proof := rfl`
    Solution.lean      -- restates each frozen theorem in `<Project>.Solution`, proven (clean names)
  <Project>.lean       -- imports everything
  SKETCH.md            -- the problem + NL proof sketch (math source of truth)
  BLUEPRINT.md         -- this file
  USER_NOTES.md        -- user's special instructions / permitted assumed-axioms
  PROGRESS.md          -- append-only work log (workers write here; see §4)
  TASKS.md             -- append-only delegation log (the Plan agent writes here; see §5)
  REVIEW.md            -- append-only audit log (the Review agent writes here; see §5)
  scripts/
    verify.sh          -- the verification harness
    frozen.sha256      -- SHA-256 pins of Defs.lean + Theorems.lean
    ALLOWED_AXIOMS.txt -- axiom allowlist init.py derives from USER_NOTES.md
```

All support declarations live in `namespace <Project>` (never shadow a frozen
name). The frozen theorems are the only "theorem-facing" surface;
`Solution.lean` re-exposes each as `<Project>.Solution.<name>` after it is
proven, and `Discharge.lean` machine-checks that each proof has *exactly* the
frozen type (`@Frozen = @Proof := rfl`).

### 2. Freeze the Definitions (`Defs.lean`)

Define every object the proof needs, **in dependency order**. **Make decisive
modeling choices here and write them down — they cannot change later.** For each
definition record:

- the Lean rendering (the `def` / `abbrev` / `structure`);
- the **MODELING DECISION**: which of several faithful encodings you chose and
  *why*; which alternatives you rejected (and the trade-off — e.g. "concrete
  model gives `DecidableEq` definitionally but you must discharge associativity;
  quotient model gives the ring structure for free but is `noncomputable`"); any
  instance / finiteness consequences a later stage must respect.

A definition once frozen is binding: later stages may *characterize* it with
support lemmas but may never redefine or silently swap it.

> **Cheat watch (Defs).** Every predicate must be the **genuine textbook
> notion**, quantified exactly as the source states it. Do not weaken a `∀` to a
> finite or fixed family, do not replace an equality with a one-sided inclusion,
> do not swap a textbook definition (finite generation, exactness, …) for a
> convenient surrogate, and do not add a hypothesis that the source claim does
> not have. A "simplification" here can make the headline vacuous.

### 3. Freeze the Theorems (`Theorems.lean`)

Write the **COMPLETE** list of frozen theorem statements, all `:= sorry`. After
writing them, `Theorems.lean` is frozen. Each statement must:

- render a claim of `SKETCH.md` **faithfully and minimally** — no weakening, no
  added hypotheses, no specializing a universal to examples, no proving a special
  case and naming it the general one;
- have a **stable, binding name** (these names are referenced by `verify.sh`,
  `Discharge.lean`, `Solution.lean`, and `init.py` — they cannot drift);
- include the **headline existential / final claim**, plus any intermediate
  milestone statements and any "easy converse"/sanity lemmas that make the
  headline airtight.

For each frozen theorem, add a one-line note mapping it to the Step(s) of
`SKETCH.md` it formalizes, and (in a short "**Why these N**" paragraph) which are
the decidable *heart*, which are support lemmas, and which is the payoff.

**Re-build gate.** After freezing, `lake build` must succeed (everything is
`sorry`, but the *statements* must typecheck). Do not write a line of proof until
the skeleton compiles. Record the SHA-256 of `Defs.lean` and `Theorems.lean`
into `scripts/frozen.sha256`, then log a PROGRESS entry: "SETUP frozen, skeleton
builds, pins recorded."

### 4. Progress logging (`PROGRESS.md`, append-only — MANDATORY)

There is a single shared log at the repo root, **`PROGRESS.md`**. It is the
project's memory: it is read by an auditor (see below) and by freshly launched
agents that have **no prior context** and must figure out, from the log alone,
what is done, what is in progress, and where they should start. Treat it as the
first thing you read and the last thing you write.

**Inviolable rules (read before doing anything):**

- **APPEND ONLY. NEVER delete, edit, overwrite, reword, or "tidy up" any
  existing entry — not your own, not anyone else's, not ever.** The log is an
  immutable history. If something you wrote earlier turns out to be wrong, do
  **not** remove it: append a *new* entry that corrects it (`📝 decision`,
  noting "supersedes the entry at <timestamp>"). Deleting or rewriting history
  is itself treated as a cheating signal in the audit.
- **Every entry is timestamped and stage-annotated.** Get the real UTC time with
  `date -u +"%Y-%m-%dT%H:%M:%SZ"` — do not invent or approximate timestamps.
- **One entry per event, newest appended at the bottom**, in this exact format:

  ```
  ## <UTC timestamp> — <stage/item, e.g. "Stage C · <lemma name>">
  Agent: <your short label, e.g. "agent-stageC">
  Status: ✅ proved | ⚠️ blocked | 🔧 in progress | 📝 decision
  Check: <#print axioms result, lake build result, or n/a>
  Note: <one or two lines — what you did, key lemma used, or exactly what blocks you>
  Next: <for a ✅/⚠️ entry: what work this unblocks or what a follow-up agent should
         do next, with exact lemma/file names to build on; "n/a" only if truly terminal>
  ```

- **The `Next:` line is mandatory on every `✅` and `⚠️` entry.** Point the next
  agent at the related work: which stage is now unblocked, the exact names of the
  lemmas/defs you produced that they will consume, and any gotcha you hit. A fresh
  agent with no context should be able to read the latest entries and know exactly
  where to start — this is what makes sessions resumable.

- **Log at least when you:** (a) start work on a stage (`🔧 in progress`, so two
  agents don't collide on the same lemma), (b) finish/close a lemma or stage
  (`✅`, with the `#print axioms` output as `Check:`), (c) hit a blocker (`⚠️` —
  write the *exact* failing goal/error, then move to the next independent
  target rather than thrashing), or (d) make a non-obvious modeling or proof
  decision (`📝`). Append a prominent entry at each milestone.
- **Never fake a `✅`.** Only mark proved what compiles with a clean
  `#print axioms` (only `propext`, `Classical.choice`, `Quot.sound`; no
  `sorryAx`, no `native_decide`/`Lean.ofReduceBool`). A `✅` that does not match
  the actual build state is the most serious audit failure.
- **Do not stop to ask for confirmation between stages — work straight through**,
  logging as you go.

**Why this matters (do not skip):** `PROGRESS.md` is the input to a
**faithfulness / cheating audit conducted by the orchestrator, not by you.** The
auditor cross-checks every `✅` entry against the actual Lean source and axiom
output, and checks that no frozen file or earlier log entry was tampered with.
An accurate, complete, append-only log protects your work from being thrown out;
a log with gaps, edited history, or unsupported `✅`s causes the whole stage to
be re-audited or discarded.

### 5. The iteration loop & agent-onboarding protocol

After SETUP, the project is driven by an **orchestrator** that runs repeated
iterations. Each iteration has three phases, executed by short-lived agents that
share **no memory** beyond the files on disk — they coordinate entirely through
`TASKS.md`, `PROGRESS.md`, and `REVIEW.md` (all append-only):

1. **PLAN** — one agent reads `REVIEW.md` (the auditor's prior findings — its
   "Required follow-ups" are top priority), `PROGRESS.md` (what is `✅`/`🔧`/`⚠️`/
   `📝`), `SKETCH.md`, and `BLUEPRINT.md`. It chooses the most valuable batch of
   work that respects the dependency graph, splits it across **up to 4 parallel
   workers with NON-OVERLAPPING files**, and **appends a `## Iteration N` block to
   `TASKS.md`** with one `Agent k:` line per active worker (inactive workers
   omitted). It writes no proofs.
2. **WORKERS** — up to 4 agents run **in parallel**, one per `Agent k:` line.
   Each owns only the files its line assigns (this is what makes parallelism
   collision-free).
3. **REVIEW** — one independent, adversarial auditor re-runs the build, `#print
   axioms`, and `scripts/verify.sh`, checks faithfulness against `SKETCH.md`/
   `BLUEPRINT.md`, and **appends a `## Review — Iteration N` block to `REVIEW.md`**
   ending in `Verdict: COMPLETE | INCOMPLETE`. Every 5th iteration is a full-
   project audit. The loop ends when a verdict is `COMPLETE` and a final full
   audit confirms it.

**Worker onboarding ritual — do this BEFORE writing any code:**

1. **Read `TASKS.md`**, find `## Iteration N`, then your own `Agent k:` line.
   *That line is your assignment* — the files you own and the lemmas to produce.
   Ignore the other agents' lines (they are running right now in parallel).
2. **Read `PROGRESS.md` end to end.** Respect every `✅` (done — reuse, don't
   redo), `🔧` (another agent holds it — do not touch), `⚠️` (blocked), and `📝`
   (a fixed modeling/proof decision you must follow).
3. **Read the `BLUEPRINT.md` stage(s) your task names — including the Cheat-watch
   box — and the cited `SKETCH.md` step(s).** Do not work from the stage title
   alone; the cheat-watch boxes are binding.
4. **Append a `🔧 in progress` entry** to `PROGRESS.md` claiming your work, then
   work only on your assigned files, then append `✅`/`⚠️` as you go.

**Dependency discipline.** Respect the order graph in "Suggested formalization
order"; the Plan agent must never assign work whose prerequisites are not yet
`✅`. Workers must **never edit the frozen `Defs.lean`/`Theorems.lean`** and must
**never weaken a frozen statement** (see the cardinal cheat rule below) — if a
task seems to need that, append a `⚠️` entry describing the obstacle and stop,
rather than touching a frozen file.

---

## Part 0 — What Mathlib already gives you (reuse, do not rebuild)

List the Mathlib objects and lemmas THIS problem can reuse instead of rebuilding.
Fill the table with one row per object/operation the proof needs:

| Need                                  | Mathlib handle                          |
| ------------------------------------- | --------------------------------------- |
| <the object / operation you need>     | <the Mathlib def/lemma/typeclass>       |
| …                                     | …                                       |

Call out the **one or two nontrivial Mathlib dependencies** the whole proof
hinges on, and note any machinery you can *avoid* (e.g. "the sketch mentions
`Tor`, but the identity can be proved directly as …, so you do not need that
machinery"). The point is to keep the proof inside well-supported Mathlib API.

---

## Part 1 — New objects to define (all in `Defs.lean`, frozen)

A cross-reference table of the objects frozen in Part −1 §2 (summarize; the
modeling decisions live in §2):

| #  | Object            | Role                                        |
| -- | ----------------- | ------------------------------------------- |
| D1 | <name : Type>     | <what it is and why the proof needs it>     |
| D2 | <name>            | …                                           |
| …  | …                 | …                                           |

---

## Part 2 — Theorems and lemmas to prove (in order)

Break the proof into **Stages**, each mapped to a `Proofs/<Stage>/` directory.
Order the stages by dependency. **Every stage ends with a "Cheat watch" box** that
names the specific trivializations/weakenings to avoid for THIS problem — this is
the most important per-stage content. Use this template for each stage:

### Stage <X> — <one-line goal> (`Proofs/<Stage>/`)

Goal: <what this stage delivers, and which frozen theorem(s) / support lemmas it
produces>.

**<X1> — `<lemma name>`.** <statement and proof strategy: the key Mathlib lemmas,
the decidable vs structural route, the explicit witnesses if any>.

**<X2> — `<lemma name>`.** <…>

> **Cheat watch (Stage X).** <The specific dishonest shortcuts to forbid here:
> proving a `∀` only on generators/examples and claiming the general case; adding
> a hypothesis to a frozen statement; replacing a genuine equality with a one-way
> inclusion; substituting a weaker/decidable surrogate for the real definition;
> asserting a non-membership/non-equality instead of *computing* it. Name the
> guardrail checks (small `example`s) that catch each one.>

Repeat for every stage. Mark the **milestone** stage (the mathematical heart) and
the **headline** stage prominently, and require a clean `#print axioms` at each.

### Discharge & Solution (after the frozen theorems are proved)

In `<Project>/Solution.lean`, restate each frozen theorem **verbatim** in
`namespace <Project>.Solution` and set it `:= <name>_proof` (the sorry-free
declaration from `Proofs/`). In `<Project>/Discharge.lean`, for each pair write
`example : @<Frozen> = @<Proof> := rfl` — this compiles **iff** the proof has
*exactly* the frozen proposition (machine-checked no-drift). `verify.sh` checks
both modules build and that `#print axioms <Project>.Solution.<name>` is clean for
every frozen name.

---

## Suggested formalization order

Give a dependency diagram so parallel agents know what gates what. Shape:

```
SETUP (freeze Defs + Theorems, skeleton builds, pins recorded)
      │
      ▼
Stage A ──► Stage B ──┐
      └──► Stage C ──┤   (B and C independent → run in parallel)
                     ▼
                  Stage D ──► … ──► HEADLINE  (#print axioms clean)
                                        │
                                        ▼
                       Discharge.lean + Solution.lean
```

State which stages are independent (parallelizable), which is the milestone after
which a citable result already exists, and which stage is the hardest engineering
(budget effort accordingly).

---

## Notes, risks, and cheats to watch out for

These are **general anti-cheat principles** — keep them, and append any problem-
specific traps below them.

- **★ NEVER assume something as a hypothesis (the cardinal rule).** Every frozen
  theorem must be hypothesis-free wherever the source claim is unconditional.
  Forbidden moves: adding `(h : …)` to a frozen statement; proving a `∀ x`
  (or `∀ x y`) claim only for generators / a finite subset and claiming the
  general case; replacing an equality with a one-sided inclusion. If a sub-proof
  seems to need an assumption, **derive it or restructure** — do not weaken the
  statement. (Downstream stages typically instantiate these at *arbitrary*
  elements, so a quiet weakening breaks the assembly silently.)

- **Keep every predicate the textbook definition — do not soften it.** Quantifiers
  must match the source exactly (e.g. "for all finite families" must not become
  "for `n ≤ 2`" or "for one fixed family"); genuine finite generation /
  exactness / etc. must not be replaced by a cardinality-bounded or
  "I-couldn't-find-it" surrogate. A softened predicate can make the headline
  vacuous.

- **Get the modeling right once, in SETUP, and freeze it.** A dropped or extra
  relation/hypothesis in a frozen definition silently changes the object and can
  break the proof downstream. Validate the core modeling facts (a basis, a
  dimension, a key relation) *before* freezing, with small guardrail `example`s
  that confirm the structure is the intended one (and that "live" relations do
  not secretly collapse distinct nonzero elements to zero, etc.).

- **Discharge ring/structure axioms once — never `sorry` them.** Inherit them
  from Mathlib (quotients, matrices, existing instances) or discharge them by
  `decide` on a finite model. If you feel the urge to `sorry` associativity or
  commutativity, you modeled the object wrong. Use Mathlib's existing `CommRing`/
  module instances rather than hand-rolling multiplication.

- **Keep module-side and ring-side objects distinct.** When a proof bridges two
  kinds of object (an ideal vs a submodule, a ring element vs its image under a
  hom), fix the precise Lean form of each in `Defs.lean` and do not conflate them
  — mixing them up is the most likely *silent* modeling bug.

- **`decide` budget — and `native_decide` is BANNED.** Finite single-element
  identities are kernel-`decide`-able; a doubly-quantified `∀` over a large finite
  type is usually **not** (it can blow up exponentially) — prove those
  structurally and reserve `decide` for the small finite lemma underneath. Only
  `decide` over genuinely **computable** types (e.g. `ZMod n`, `Fin n`,
  `Fin n → ZMod m`); a `noncomputable` model will not reduce. `native_decide`
  adds a compiler-trust axiom and would dirty `#print axioms` — never use it.

- **Don't touch the frozen files after SETUP.** `Defs.lean` and `Theorems.lean`
  are byte-frozen during proving (pinned in `scripts/frozen.sha256`). If a
  *definition* seems missing, it belongs in a `Proofs/` support file. If a
  *statement* seems wrong, stop and re-read `SKETCH.md` — the frozen statements
  are deliberately the minimal faithful rendering of the sketch, so a mismatch
  means a modeling bug to fix *before* re-freezing, not a hypothesis to bolt on.

- **Keep `#print axioms` clean.** Every solved theorem must depend only on
  `{propext, Classical.choice, Quot.sound}` — no `sorryAx`, no `native_decide`/
  `ofReduceBool` — **plus** any assumed-certificate axioms the user permitted in
  `USER_NOTES.md` (their names are recorded in `scripts/ALLOWED_AXIOMS.txt`). Any
  axiom outside that allowlist is banned. This is checked per theorem by
  `verify.sh` (checks 2 and 4).

- **Assumed certificates go in as `axiom`s, never as hypotheses.** A fact that is
  routine but prohibitively expensive to prove in Lean (a large factorization, an
  explicit interpolant, a numeric certificate) may be assumed — but ONLY if the
  user described it in `USER_NOTES.md`, and ONLY as a Lean `axiom` declared in
  `Defs.lean` during SETUP (so it shows up in `#print axioms` and is checked
  deterministically). It is then exempt from the axiom ban via
  `scripts/ALLOWED_AXIOMS.txt`. This NEVER relaxes the cardinal rule above:
  certificates are never bolted onto a frozen theorem as a hypothesis `(h : …)`.

- **<Problem-specific traps.>** <Append the concrete over-reach / mis-modeling
  risks for THIS construction — e.g. assuming a property you must prove,
  confusing two similar objects, swapping one construction for a neighbor that
  changes the answer.>
