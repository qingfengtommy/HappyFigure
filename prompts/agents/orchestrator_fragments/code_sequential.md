### CODE — Sequential

**Early-stop rule:** Once an experiment's critic returns **ACCEPT** (score ≥ threshold), that experiment is **done** — promote its figure and remove it from the pending set. Do NOT retry accepted experiments.

- For each experiment:
  1. Read `description.md` and `styled_spec.md`
  2. Write `figure_code.py`, execute, review with `@figure-critic`
  3. If ACCEPT → promote to `outputs/<exp>/figure.png`, move to next experiment
  4. If NEEDS_IMPROVEMENT and iteration < max → targeted edits, re-execute, re-critique
  5. After max iterations without ACCEPT → promote best-scoring iteration's figure
