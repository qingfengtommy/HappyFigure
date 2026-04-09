### CODE — Paper Composite (Unified)

Services (SAM3:8001, OCR:8002, BEN2:8003) are running if any panel needs them.

1. Read `figure_classification.json` to partition panels by type.
2. **Statistical panels**: spawn `@code-agent` per panel. Each panel maps to an experiment slug (`{figure_id}__{panel_id}`). Include: run_dir, experiment slug, work_dir (`experiments/<slug>`), output_dir (`outputs/<slug>`), styled_spec path, **global_style.md path**, **color_registry.json path** (if exists), iteration number, max_iterations. **Every code-agent MUST receive the global style and color registry to ensure cross-figure consistency.**
3. **Diagram panels**: **MUST use the service-based pipeline (preferred).** For EACH diagram panel, create a per-panel working directory at `experiments/<slug>/diagram/` to avoid artifact collisions. Copy `method_description.md` and `state.json` there. Spawn `@svg-builder` with `Run directory: <run_dir>/experiments/<slug>/diagram/` (NOT the main run_dir). Then spawn `@svg-refiner` in the same directory. After completion, copy `method_architecture.png` and `final.svg` to `panels/<figure>/<panel>/panel.{png,svg}`. Services are already running (SAM3:8001, OCR:8002, BEN2:8003). **Only fall back to `@svg-author` (sketch mode) if services are confirmed unavailable** — never as the default.
4. **Data recovery panels**: If any panel was reclassified from placeholder to statistical during the data recovery step, treat it as a statistical panel — spawn `@code-agent` with the recovered data path.
5. **Placeholder panels**: for each REMAINING placeholder (only truly non-generatable content), write a labeled gray placeholder PNG to `panels/<figure>/<panel>/panel.png`.
5. After all panels complete: copy each panel's output to `panels/<figure>/<panel>/panel.png`.
6. For each figure: read `assembly_specs/<figure>.json`, write a **PIL-based** assembly script (preferred — avoids blur from matplotlib imshow re-rendering). Use PIL `Image.open/paste` to compose panels at native resolution. If PIL unavailable, use matplotlib GridSpec with `aspect='equal'`. **Tight spacing**, 14pt bold panel labels, 300 DPI.
7. Run `@figure-critic` in assembly mode on each assembled paper figure (view the assembled PNG + the spec).
8. If NEEDS_IMPROVEMENT: adjust the assembly script (spacing, label positioning), re-execute (max 3 iterations).
9. Save final assembled figures to `outputs/paper_figures/<figure_id>.png`.

### Cross-Figure Style Consistency

- **Before spawning any `@code-agent`**: verify `global_style.md` exists. If not, create one with shared palette, font settings, spine rules.
- **color_registry.json**: write this BEFORE code generation. Map each data category (methods, datasets, conditions) to specific hex colors. All code-agents must use these colors.
- **rcParams consistency**: every code-agent receives the same base rcParams block from global_style.md. Enforce: `font.size`, `axes.spines.right=False`, `axes.spines.top=False`, `legend.frameon=False`, `figure.dpi=300`.
- **Font size harmony**: when panels will be assembled together, coordinate font sizes. Panels in the same figure should use the same base font size.
- After generation, verify color consistency across all panels before assembly.
