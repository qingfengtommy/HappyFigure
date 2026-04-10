# Statistical Figure Examples

Reference figures used as few-shot styling examples in the figure generation pipeline. Each example is a `.png` image paired with a `.txt` style description (and optionally a `.py` source script).

## Image Sources & Attribution

All images are reproduced here for non-commercial research use as few-shot prompting examples.

### figures4papers images (`f4p_` prefix)

Images prefixed with `f4p_` are from the [figures4papers](https://github.com/ChenLiu-1996/figures4papers) repository (Chen Liu, Yale University). Original papers:

| Paper | Full Title | Venue | DOI / URL |
|-------|-----------|-------|-----------|
| **ImmunoStruct** | ImmunoStruct enables multimodal deep learning for immunogenicity prediction | *Nature Machine Intelligence* 8, 70–83 (2026) | [10.1038/s42256-025-01163-y](https://doi.org/10.1038/s42256-025-01163-y) |
| **RNAGenScape** | RNAGenScape: Property-Guided, Optimized Generation of mRNA Sequences with Manifold Langevin Dynamics | *arXiv* 2510.24736 (2025) | [arXiv:2510.24736](https://arxiv.org/abs/2510.24736) |
| **CellSpliceNet** | CellSpliceNet: Interpretable Multimodal Modeling of Alternative Splicing Across Neurons in C. elegans | *bioRxiv* (2025) | [10.1101/2025.06.22.660966](https://doi.org/10.1101/2025.06.22.660966) |
| **Cflows** | Revealing Dynamic Temporal Trajectories and Underlying Regulatory Networks with Cflows | *bioRxiv* (2025) | [10.1101/2023.03.28.534644](https://doi.org/10.1101/2023.03.28.534644) |
| **Dispersion** | Dispersion Loss Counteracts Embedding Condensation and Improves Generalization in Small Language Models | *arXiv* 2602.00217 (2026) | [arXiv:2602.00217](https://arxiv.org/abs/2602.00217) |
| **FPGM** | Frequency Prior Guided Matching: A Data Augmentation Approach for Generalizable Semi-Supervised Polyp Segmentation | *arXiv* 2508.06517 (2025) | [arXiv:2508.06517](https://arxiv.org/abs/2508.06517) |
| **brainteaser** | Creativity or Brute Force? Using Brainteasers as a Window into LLM Problem-Solving | *NeurIPS 2025* | [arXiv:2505.10844](https://arxiv.org/abs/2505.10844) |
| **ophthal_review** | Evaluating Large Language Models in Ophthalmology: Systematic Review | *JMIR* 2025;27:e76947 | [10.2196/76947](https://doi.org/10.2196/76947) |

### Composite graph examples (`composite_graphs_plots/`)

Generic publication-style multi-panel figure examples demonstrating Nature/Science-quality layout and styling conventions.

## Directory Structure

Examples are organized by plot type:

```
statistical_examples/
├── bar_ablation/           # Ablation study bar charts
├── bar_group_plots/        # Grouped comparison bar charts
├── composite_graphs_plots/ # Multi-panel composite figures
├── heatmap/                # Heatmaps and matrix visualizations
├── line_chart/             # Line charts and curves
├── others/                 # Sweep plots, specialized charts
├── scatter_plots/          # Scatter plots
├── trend_plots/            # Time-series trends
└── README.md               # This file
```

Each example has up to three files:
- `.png` — the reference figure image
- `.txt` — style description (color palette, fonts, layout, aesthetic)
- `.py` — source Python script that generated the figure (when available)

## How It Works

1. The pipeline discovers image + description pairs in this directory
2. Each pair is sent to the stylist LLM as a few-shot example
3. The LLM analyzes the example images and descriptions
4. It generates a style spec that matches the references
5. The code agent uses this spec to generate publication-matching figures

## Tips

- Use 2–5 examples for best results (too many may slow down the API call)
- Include figures from the same journal/venue you're targeting
- Descriptions should be specific (hex colors, exact font sizes)
- If images are unclear, a detailed description helps more than the image
