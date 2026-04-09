### CODE — Beam Search

Style consistency is critical — all figures in a paper must use the same palette. The beam must pick a **winning style group** across experiments before early-stopping individual experiments.

**Iteration 1 — Style Selection (run all candidates):**

1. For each experiment × style variant, spawn a **separate** `@code-agent`. Each code-agent must have its own `Work directory` so candidates don't overwrite each other:
   - Work dir: `<run_dir>/experiments/<exp>/beam/s<SV>` (e.g., `experiments/ablation/beam/s0`, `experiments/ablation/beam/s1`)
   - Output dir: same work dir (promoted later)
   - Styled spec: `<run_dir>/experiments/<exp>/styled_spec_s<SV>.md`
   - Beam variant: `s<SV>`
   - **Do NOT write code yourself** — each code-agent writes its own `figure_code.py` and runs it.
2. After all code-agents complete, spawn `@figure-critic` for each candidate (one per experiment × variant), pointing at the figure in that candidate's work dir.
3. Read all `critic_result.json` files from each candidate's work dir.
4. **Group by style variant** (`s0`, `s1`, ...) and compute per-style aggregate score (mean across experiments' best-per-style candidate).
5. **Lock the winning style** — the style variant with the highest aggregate score. All subsequent iterations use only this style's spec. This ensures palette consistency across all figures.
6. Within the winning style: experiments whose best candidate scored ACCEPT are **done** — promote figure to `outputs/<exp>/figure.png` and copy `figure_code.py` + `critic_result.json` to `experiments/<exp>/`.

**Iterations 2+ — Refinement (winning style only):**

7. Re-spawn `@code-agent` only for pending experiments, using the locked style variant's spec, incrementing `iteration` and passing prior critic feedback from `critic_result.json`.
8. After each round, spawn `@figure-critic`, then early-stop any experiment that reaches ACCEPT.
9. Repeat until `pending` is empty or `iteration == max_iterations`.
10. For experiments still pending, promote their best-scoring candidate.

**Critical rules:**
- Each beam candidate MUST have its own work directory — never share `figure_code.py` across variants.
- Do NOT write figure code yourself — always delegate to `@code-agent` subagents.
- After critics run, `critic_result.json` MUST exist in each candidate's work dir. If missing, the candidate is treated as FAILED (score 0).

#### Spawning beam `@code-agent`

Each candidate gets its own work dir to avoid collisions:

```
Work directory: <run_dir>/experiments/<experiment_slug>/beam/s<SV>
Output directory: <run_dir>/experiments/<experiment_slug>/beam/s<SV>
Styled spec: <run_dir>/experiments/<experiment_slug>/styled_spec_s<SV>.md
Beam variant: s<SV>
```
