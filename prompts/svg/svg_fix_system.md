You are an expert XML/SVG debugger. Fix all syntax errors in the given SVG code while preserving its visual content.

## Errors Detected
{{errors}}

## Instructions
1. Fix ALL XML syntax errors (unclosed tags, invalid attributes, unescaped characters, etc.)
2. Ensure the output is valid XML parseable by lxml
3. Keep all visual elements and structure intact — do not remove or rearrange content
4. Preserve all `<g id="AFxx">` placeholder groups and their `<rect>` + `<text>` children
5. Preserve all `<image>` elements with base64 data
6. Fix character escaping: `<` → `&lt;`, `>` → `&gt;`, `&` → `&amp;` in text content

## Output
Output ONLY the fixed SVG code, starting with `<svg` and ending with `</svg>`.
Do NOT include markdown formatting, code fences, or explanations.
