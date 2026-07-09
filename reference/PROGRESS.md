# PROGRESS — <problem> formalization

Append-only log. **Never delete, edit, or reword an existing entry.** Correct a
prior entry only by appending a new one (`📝 decision`, noting which it
supersedes). Get timestamps with `date -u +"%Y-%m-%dT%H:%M:%SZ"`. See
`BLUEPRINT.md` Part −1 §4 for the full rules and §5 for the agent-onboarding
ritual.

Entry format (one per event, newest appended at the bottom):

```
## <UTC timestamp> — <stage/item>
Agent: <short label>
Status: ✅ proved | ⚠️ blocked | 🔧 in progress | 📝 decision
Check: <#print axioms result, lake build result, or n/a>
Note: <what you did, key lemma, or exactly what blocks you>
Next: <what this unblocks / where a follow-up agent should start — mandatory on ✅ and ⚠️>
```

---

<!-- The first entry is appended by init.py once SETUP freezes Defs + Theorems
     and records the SHA pins. Do not pre-fill log entries. -->
