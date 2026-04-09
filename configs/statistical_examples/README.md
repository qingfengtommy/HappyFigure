# Style Examples Directory

Place style reference images and their descriptions here for few-shot styling.

## Directory Structure

Each example is a pair of files with the same name (different extensions):

```
style_examples/
├── example1.png       # Example figure image
├── example1.txt       # Description of the style
├── nature_fig.png     # Another example
├── nature_fig.txt     # Its description
└── README.md          # This file
```

## Supported Image Formats

- `.png` (recommended)
- `.jpg` / `.jpeg`
- `.pdf`
- `.svg`

## Description Format (.txt or .md)

Write a plain-text description of the style you want the LLM to replicate. Focus on:

- Color palette (specific hex values if possible)
- Font choices and sizes
- Line weights and marker styles
- Grid and axis styling
- Layout proportions
- Overall aesthetic

### Example Description (example1.txt)

```
Nature-style figure with clean, minimal design.
- Colors: blue (#0072B2), orange (#E69F00), gray (#999999)
- Sans-serif font (Helvetica), title 10pt bold, labels 8pt
- Thin axis lines (0.5pt), no grid
- White background, tight margins
- 3.5 inches wide (single column), 2.5 inches tall
- Legend inside plot, top-right, no frame
```

## Usage

### Automatic (Default Directory)

Place files here and they'll be picked up automatically:

```bash
python cli.py --figure
```

### Custom Directory

Point to a different directory:

```bash
python cli.py --figure --style-examples /path/to/my/style_examples
```

## How It Works

1. The pipeline discovers image+description pairs in this directory
2. Each pair is sent to the stylist LLM as a few-shot example
3. The LLM analyzes the example images and descriptions
4. It generates a style spec that matches the references
5. The code agent uses this spec to generate publication-matching figures

## Tips

- Use 2-5 examples for best results (too many may slow down the API call)
- Include figures from the same journal/venue you're targeting
- Descriptions should be specific (hex colors, exact font sizes)
- If images are unclear, a detailed description helps more than the image
