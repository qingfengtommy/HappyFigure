# Research proposal

{{proposal}}

# Lightweight pre-scan report

{{pre_data_overview}}

# Directory tree (ALL data)

{{data_tree}}

# Data schemas (ALL data — column definitions + sample rows)

{{data_schemas}}

# Data structure semantics

{{data_semantics}}

# Results root directory

`{{results_dir}}`

Analyze the data above and write a Python script that transforms it into figure-ready CSV files. The script should read from the results root and write processed CSVs to a `processed_data/` subdirectory. Organize output into meaningful subdirectories (one per logical figure/section). Use long-form (tidy) format where appropriate. Print each output file path.

If tools are available, use them first to inspect real files before finalizing the script.
