# <Problem title>

> **This is a FORMAT TEMPLATE for the pipeline's INPUT, not a worked problem.**
> `SKETCH.md` is the one file you author by hand before running `setup.py`; it is
> the mathematical source of truth that every downstream agent treats as
> authoritative. The pipeline does **not** read this template's content — it shows
> the expected *shape* of a sketch. Replace everything in `<angle brackets>`.

<State the problem and any definitions it relies on. Recall, precisely, every
notion the theorem uses — these become the frozen `Defs.lean` predicates, so the
wording here is what "faithful" is measured against. If the problem is a question
("Is every X also Y?"), state it as a question, then restate the precise logical
content it asks for.>

---

# Theorem

<The exact claim to be formalized — the statement the headline frozen theorem must
render minimally and faithfully. If it is a refutation or an existence result,
state it as such (e.g. "There exists an X that is not Y").>

---

# Proof Sketch

A natural-language proof, **broken into numbered Steps**. Each Step becomes one
Stage of `BLUEPRINT.md` and one `Proofs/<Stage>/` directory, so make the Steps
self-contained and ordered by dependency.

## Step 1. <name>

<What is constructed or proved in this step, with enough detail that an agent can
render it in Lean: the objects, the key identities/relations, and any explicit
witnesses. State the modeling-relevant facts precisely (a dimension, a relation,
a basis) — vagueness here turns into mis-modeling downstream.>

## Step 2. <name>

<…>

## Step N. <name>

<…the final step that assembles the headline claim…>

---

# Conclusion

<Restate what has been constructed/proved and why it resolves the problem. This is
the claim `problem_*` / the headline frozen theorem certifies.>
