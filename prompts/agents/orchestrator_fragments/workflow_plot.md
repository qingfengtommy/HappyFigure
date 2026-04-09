## Plot Workflow

### EXPLORE

#### Step 1 — Quick directory scan (you do this, no subagent)

Run `find`/`ls -R` on the results directory to get file count and top-level subdirectories.

#### Step 2 — Single vs parallel exploration

**Single** (≤100 files OR ≤5 subdirs): Spawn one `@data-explore` → writes `exploration_report.md` + `exploration_summary.json`.

**Parallel** (>100 files AND >5 subdirs):
1. Partition subdirs into groups (~50 files each).
2. Spawn multiple `@data-explore` in one turn (parallel). Each writes `exploration_part_<N>.md`.
3. Merge partial reports yourself: concatenate, deduplicate, build unified experiment table → `exploration_report.md` + `exploration_summary.json`.

#### Step 3 — Optional: spawn `@code-explore` for existing scripts/configs.

#### Step 4 — Split into `experiments/<exp>/description.md` per experiment.

### STYLE

1. Spawn `@planner-stylist` with: run_dir, exploration report + summary, style examples dir, experiment slugs.
2. Verify it wrote: `global_style.md`, `multi_figure_plan.md`, `experiments/<exp>/styled_spec.md` for every experiment.
3. Write `design_summary.json` yourself.
4. For beam mode, also spawn `@style-variant` per experiment × variant → `styled_spec_s{N}.md`.

#### Feedback (when human review is active)

If `feedback/` exists: read `human_style_feedback.md` before styling, `human_data_feedback.md` before re-exploring, `human_code_feedback.md` before code generation, `human_feedback_<exp>.md` for per-experiment corrections.

Also check `configs/feedback/style_preferences.yaml` for persistent project-level rules (applies even without `--review`).
