# Research proposal

{{proposal}}

# Directory tree (ALL experiments)

{{data_tree}}

# Data schemas (ALL experiments)

{{data_schemas}}

# Data exploration report (tool-grounded)

{{data_exploration_report}}

# Data structure semantics (ALL experiments)

{{data_semantics}}

# Experiment names

{{experiment_names}}


Produce a concrete figure plan using `<!-- FIGURE_SPEC: name -->` delimiters. Begin with a shared preamble (palette choice, color-to-category mapping). Each figure spec must include: figure type, data mapping, axes, labels, title, legend, size tier, and palette choice.

When the exploration report is present, treat it as the primary summary of plot-ready columns/groups and potential data risks.

**Delimiter rules:**
- If there are **multiple experiments**, emit one `<!-- FIGURE_SPEC: experiment_name -->` per experiment.
- If there is **one experiment** but the data contains **multiple distinct evaluation tasks or data sections** (e.g., separate CSV files in subdirectories, or separate tables for different analyses), emit **one `<!-- FIGURE_SPEC: section_name -->` per section/task**. Each section becomes its own figure. Do NOT combine multiple sections into a single composition figure — keep them as separate figures so each is clean and focused. Use the subdirectory name or a descriptive short name as the section_name.

# Available figure categories and subcategories

Single-panel types: `statistical`, `visualization`
Statistical subcategories: `bar_group_plots`, `bar_ablation`, `heatmap`, `line_chart`, `scatter_plots`, `trend_plots`, `composite_graphs_plots`, `others`

Include a `<!-- FIGURE_ROUTING -->` block when possible (see system instructions for format).
