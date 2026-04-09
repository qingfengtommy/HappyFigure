### CODE — Parallel

**Early-stop rule:** Once an experiment's critic returns **ACCEPT** (score ≥ threshold), that experiment is **done** — promote its figure and remove it from the pending set. Do NOT retry accepted experiments.

- Spawn `@code-agent` for all pending experiments in one turn.
- After all complete, spawn `@figure-critic` for each.
- Partition results:
  - **Accepted** (ACCEPT): promote figure, remove from pending set.
  - **Pending** (NEEDS_IMPROVEMENT): keep for next iteration.
- **On retry:** re-spawn `@code-agent` **only for pending experiments**, incrementing `iteration` and including prior critic feedback.
- Repeat until `pending` is empty or `iteration == max_iterations`.
- For experiments still pending after max iterations, promote best-scoring iteration's figure.
