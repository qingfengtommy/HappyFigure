# Data catalog (file inventory with sample data and statistics)

{{data_catalog}}

# Styled figure specification (plan + style combined — implement exactly)

{{styled_figure_spec}}

# Paths

- **Results root** (relative to repo root, use as base for all data paths): `{{results_dir}}`
- **Working directory** when the script runs: repository root. So full path for a file is `os.path.join(results_dir, "sub", "path", "file.tsv")` or `f"{results_dir}/sub/path/file.tsv"`.
- **Output**: Save figure under `figures/` using a descriptive filename and print `Saved: <path>` when feasible. The runner archives the render into the per-experiment artifact directory.

Generate the complete Python script. Include multi-panel layout, panel labels (a), (b), (c), (d) if the plan has multiple panels; data loading with validation; filters and column names as in the plan; and annotations (text, reference lines, significance markers) if specified. Use matplotlib with pandas/numpy; seaborn is optional. Set `matplotlib.use('Agg')` for headless/non-interactive runs.

**color_map:** Extract the `COLOR_MAP_PYTHON` block from the styled figure spec above and copy the `color_map` dict verbatim into your script. If seaborn is used, prefer `palette=[color_map[m] for m in hue_order]`; for matplotlib, use explicit `color_map[...]` lookups.

**Style reference:** If reference figure images are attached, match their visual style (colors, layout, line weights, fonts) as closely as possible while implementing the specification above.

**Code reference:** If reference code is attached, adapt similar coding patterns.
