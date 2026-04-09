# Architecture Examples Directory

Place architecture diagram reference images and their descriptions here for few-shot prompting in the method drawing pipeline.

## Directory Structure

Each example is a pair of files with the same name (different extensions):

```
architecture_examples/
├── arch1.png          # Example architecture diagram image
├── arch1.txt          # Description of what the diagram shows
├── arch2.png          # Another example
├── arch2.txt          # Its description
└── README.md          # This file
```

## Supported Image Formats

- `.png` (recommended)
- `.jpg` / `.jpeg`
- `.pdf`
- `.svg`

## Description Format (.txt or .md)

Write a plain-text description of the architecture diagram. Focus on:

- What model/method the diagram depicts
- Layout style (left-to-right flow, top-to-bottom, hierarchical)
- Key visual elements (blocks, arrows, skip connections, attention heads)
- Color usage and grouping conventions
- Level of detail (abstract overview vs. detailed internals)
- Text labeling style

### Example Description (arch1.txt)

```
Transformer encoder-decoder architecture diagram from "Attention Is All You Need".
- Left-to-right data flow: input embeddings → encoder stack → decoder stack → output
- Encoder: stacked blocks with multi-head attention + feed-forward sublayers
- Decoder: similar blocks with masked attention + cross-attention
- Skip connections shown as curved arrows bypassing each sublayer
- Color coding: blue for encoder blocks, orange for decoder blocks, green for attention
- Clean white background, labeled arrows for data flow
- Sans-serif font, component names inside rounded rectangles
```

## Usage

### Automatic (Default Directory)

Place files here and they'll be picked up automatically:

```bash
python cli.py --method-drawing --input-dir /path/to/markdown/files
```

### Custom Directory

Point to a different directory:

```bash
python cli.py --method-drawing --input-dir /path/to/md --architecture-examples /path/to/arch_examples
```

## How It Works

1. The pipeline discovers image+description pairs in this directory
2. Each pair is sent to the method proposer LLM as a few-shot example
3. The LLM analyzes the example diagrams and descriptions
4. It generates a method description that targets a similar visual style
5. Reference images are also passed to the AI image generator (Nano Banana Pro) for style matching
