# Architecture Diagram Examples

Reference architecture diagrams used as few-shot examples in the method drawing pipeline. Each example is a `.png` image paired with a `.txt` description.

## Image Sources & Attribution

All images are reproduced here for non-commercial research use as few-shot prompting examples.

| File | Source | Paper | DOI |
|------|--------|-------|-----|
| `arch1.png` | Figure 1 | Xu et al., "A whole-slide foundation model for digital pathology from real-world data," *Nature* 630, 181–188 (2024) | [10.1038/s41586-024-07441-w](https://doi.org/10.1038/s41586-024-07441-w) |
| `arch3.png` | Figure 1e | Jumper et al., "Highly accurate protein structure prediction with AlphaFold," *Nature* 596, 583–589 (2021) | [10.1038/s41586-021-03819-2](https://doi.org/10.1038/s41586-021-03819-2) |
| `arch4.png` | Figure 1 | Ruffolo et al., "Designing CRISPR–Cas systems with language models," *Nature* (2025) | [10.1038/s41586-025-08817-w](https://doi.org/10.1038/s41586-025-08817-w) |
| `f4p_assets_Dispersion_motivation.png` | Figure 1 | Liu, Sun, Xiao et al., "Dispersion Loss Counteracts Embedding Condensation," *arXiv* 2602.00217 (2026) | [arXiv:2602.00217](https://arxiv.org/abs/2602.00217) |
| `f4p_assets_ImmunoStruct_schematic.png` | Figure 1 | Givechian, Rocha, Liu et al., "ImmunoStruct enables multimodal deep learning for immunogenicity prediction," *Nature Machine Intelligence* 8, 70–83 (2026) | [10.1038/s42256-025-01163-y](https://doi.org/10.1038/s42256-025-01163-y) |
| `f4p_assets_ImmunoStruct_contrastive.png` | Figure 4 | (same as above) | [10.1038/s42256-025-01163-y](https://doi.org/10.1038/s42256-025-01163-y) |
| `f4p_assets_RNAGenScape_schematic.png` | Figure 1 | Liao, Liu, Sun et al., "RNAGenScape: Property-Guided, Optimized Generation of mRNA Sequences," *arXiv* 2510.24736 (2025) | [arXiv:2510.24736](https://arxiv.org/abs/2510.24736) |

Images prefixed with `f4p_` are from the [figures4papers](https://github.com/ChenLiu-1996/figures4papers) repository (Chen Liu, Yale University).

## Directory Structure

Each example is a pair of files with the same name (different extensions):

```
method_examples/
├── arch1.png          # Architecture diagram image
├── arch1.txt          # Description of what the diagram shows
├── ...
└── README.md          # This file
```

## How It Works

1. The pipeline discovers image + description pairs in this directory
2. Each pair is sent to the method proposer LLM as a few-shot example
3. The LLM analyzes the example diagrams and descriptions
4. It generates a method description that targets a similar visual style
5. Reference images are also passed to the image generator for style matching

## Custom Examples

Point to a different directory with `--architecture-examples-dir`:

```bash
python cli.py diagram --proposal paper.md --architecture-examples-dir /path/to/my/examples
```
