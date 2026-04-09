## Method Description

{{method_description}}

## Document Type

{{doc_type}} (quality threshold: {{threshold}}/12)

## Review Context

This is iteration {{iteration}} of {{max_iterations}} in the diagram refinement loop.

## Image Reference

The attached image shows two views side by side:
- **LEFT**: The original target figure (ground truth)
- **RIGHT**: The current SVG rendering

Your job is to evaluate how well the RIGHT image reproduces the LEFT image, while also ensuring structural correctness per the method description.

## Task

Examine the attached composite image carefully. Compare the SVG rendering (RIGHT) against the original figure (LEFT) and the method description above. Evaluate using all six dimensions in your rubric.

Be thorough but fair:
- On early iterations, focus on structural correctness and visual fidelity to the original.
- On later iterations, focus on layout polish and grouping clarity.
- If the diagram is fundamentally correct but has minor layout issues, acknowledge that.
- Do NOT suggest adding elements (legends, borders, stage markers) that aren't in the original figure.

The SVG source code is also provided below. When reporting issues, reference specific SVG elements (tag names, id attributes, coordinates) in the `code_comments` field so the refinement agent can make precise edits.

Return your evaluation as a single JSON object (no markdown fencing, no explanation outside the JSON).
