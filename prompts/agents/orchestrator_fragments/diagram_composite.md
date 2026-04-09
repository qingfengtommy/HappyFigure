### CODE — Composite Diagram

Services are already running for the build phase. Same as diagram, plus:

1. Spawn `@svg-builder` → generates raster, segments, builds SVG.
2. Spawn `@svg-refiner` → refines SVG.
3. If requested, spawn `@viz-composer` after refinement to replace raster visualizations with programmatic versions.
