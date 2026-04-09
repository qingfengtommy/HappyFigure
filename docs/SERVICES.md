# Microservices Guide

The `diagram` and `composite` commands require three microservices. The `plot` and `sketch` commands do **not** need these.

| Service | Port | Purpose |
|---------|------|---------|
| SAM3 | 8001 | Image segmentation |
| PaddleOCR | 8002 | Text detection |
| BEN2 | 8003 | Background removal |

## Quick Start

```bash
conda activate happyfigure

# Start all three
python scripts/pipeline_cli.py services start

# Verify health
python scripts/pipeline_cli.py services health

# Stop when done
python scripts/pipeline_cli.py services stop
```

`services start` launches all three services together, including PaddleOCR on port 8002. Use the `happyfigure` conda environment — OCR depends on packages installed there.

## GPU Requirements

| Service | VRAM (approx.) | Notes |
|---------|---------------|-------|
| SAM3 | ~3 GB | ViT-H model; CPU works but is slow |
| BEN2 | ~1 GB | Background removal; CPU fallback available |
| PaddleOCR | ~1 GB | Text detection; CPU fallback available |

All three can run on CPU (`--device cpu`) but GPU is strongly recommended. A single GPU with >=6 GB VRAM can run all three concurrently.

**First-run timing:** Expect 2-5 minutes on first launch while models download and load. Subsequent launches use cached weights (~30s for SAM3, ~10s for OCR/BEN2).

---

## SAM3 Model Weights

SAM3 uses a ViT-H checkpoint (~2.4 GB) hosted on HuggingFace. The model is **gated** — you must accept the license before downloading.

**Step 1: Request access** — visit the SAM3 model page on HuggingFace and accept the license agreement.

**Step 2: Authenticate**:

```bash
pip install huggingface-hub
huggingface-cli login
# Paste your HuggingFace access token (from https://huggingface.co/settings/tokens)
```

After that, the `sam3` package auto-downloads weights on first service launch.

| Setting | Where | Default |
|---------|-------|---------|
| Auto-download | `configs/services.yaml` — leave `checkpoint_path` unset | Enabled |
| HuggingFace cache dir | `HF_HOME` env var | `~/.cache/huggingface/` |
| Local checkpoint | `configs/services.yaml` — set `checkpoint_path` | Unset |

**Local checkpoint** (air-gapped machine):

```yaml
sam3:
  checkpoint_path: /path/to/sam3_vit_h.pth
```

**Pre-download**:

```bash
python -c "from sam3.model_builder import build_sam3_image_model; build_sam3_image_model(load_from_HF=True, device='cpu')"
```

> **Troubleshooting:** `401 Unauthorized` during SAM3 startup means you haven't accepted the model license or your HuggingFace token is missing. Run `huggingface-cli login`.

---

## BEN2 Model Weights

BEN2 uses a background-removal model (~500 MB). **No manual download needed** — `from_pretrained("PramaLLC/BEN2")` auto-downloads on first launch.

| Setting | Where | Default |
|---------|-------|---------|
| Auto-download | Built into `ben2` package | Enabled |
| HuggingFace cache dir | `HF_HOME` env var | `~/.cache/huggingface/` |
| Local model | `--model-path` CLI arg on `services.ben2.server` | Unset |

**Local model** (air-gapped machine):

```bash
git lfs install
git clone https://huggingface.co/PramaLLC/BEN2 /path/to/ben2-model
python -m services.ben2.server --model-path /path/to/ben2-model --device cuda
```

---

## PaddleOCR Model Weights

PaddleOCR uses the `PaddleOCR-VL-1.5` model (~300 MB). **Auto-downloaded** by the PaddleOCR library on first use.

| Setting | Where | Default |
|---------|-------|---------|
| Auto-download | Built into `paddleocr`/`paddlex` packages | Enabled |
| Model name | `configs/services.yaml` — `paddleocr_model` | `PaddlePaddle/PaddleOCR-VL-1.5` |
| Cache dir | PaddlePaddle internal | `~/.paddleocr/` |

**Note:** PaddleOCR requires `paddlepaddle-gpu==3.2.1` which only has wheels for Python <=3.11. For Python 3.12+, the CPU-only `paddlepaddle==3.2.1` is used instead.
