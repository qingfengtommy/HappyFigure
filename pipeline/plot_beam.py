"""Beam search: style variants, ranking, refinement, and beam execution."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import ui
from pipeline.agent_runtime import spawn_subagent
from pipeline.orchestrator import artifacts as orch_art
from pipeline.plot_execution import run_code_agent
from pipeline.plot_planning import (
    build_planner_stylist_prompt,
    planner_base_write_instruction,
    planner_variant_write_instruction,
    validate_plot_specs,
)
from pipeline.run_state import (
    finalize_plot_experiment,
    get_experiments,
    persist_plot_execution_state,
    persist_plot_plan_state,
    plot_debug_dir,
)


# ── Beam-search style variation hints for planner-stylist ──────────────
# Intent-based directions — the LLM chooses palette, plot types, layout,
# spacing, and font sizes as a coherent package.

BEAM_STYLE_VARIATION_HINTS = [
    # Variant 0: no constraint — LLM's best judgment
    "",
    # Variant 1: prioritize accessibility and clarity
    (
        "\n\nSTYLE DIRECTION: Prioritize accessibility and clarity. "
        "Use a colorblind-safe palette. Maximize data-ink ratio. "
        "Prefer simple, standard plot types. Minimize decoration."
    ),
    # Variant 2: compact and information-dense
    (
        "\n\nSTYLE DIRECTION: Make figures compact and information-dense. "
        "Use tighter spacing, smaller figure sizes, and denser layouts. "
        "Consider alternative plot types that show more information per pixel "
        "(e.g., dot plots over bar charts, violin+swarm over box plots)."
    ),
    # Variant 3: presentation-ready with generous whitespace
    (
        "\n\nSTYLE DIRECTION: Optimize for slide presentations and posters. "
        "Use larger fonts, generous whitespace, spacious layouts, and bold colors. "
        "Prefer plot types that read well from a distance."
    ),
]

# Human-readable names for each style variant index, printed in the legend.
_STYLE_VARIANT_NAMES = [
    "default (no style constraint, palette A)",
    "accessible (colorblind-safe, palette B / Okabe-Ito)",
    "compact (muted professional, palette C)",
    "presentation (earth-safe spacious, palette D)",
]


# ── Palette definitions for beam style variants ─────────────────────────
# Each palette has 8+ distinct colors to handle any method count.
# All colors are tested for contrast against white backgrounds.

BEAM_PALETTES = {
    "A": {  # Warm emphasis — high contrast, print-safe
        "label": "A",
        "colors": ["#E64B35", "#4DBBD5", "#00A087", "#3C5488", "#F39B7F", "#8491B4", "#B09C85", "#7E6148"],
    },
    "B": {  # Okabe-Ito — colorblind-safe
        "label": "B",
        "colors": ["#E69F00", "#56B4E9", "#009E73", "#0072B2", "#D55E00", "#CC79A7", "#F0E442", "#000000"],
    },
    "C": {  # Muted professional — 70% saturation
        "label": "C",
        "colors": ["#C1443E", "#3DA4B8", "#008C74", "#2B4A78", "#D88570", "#6F7DA0", "#9A8672", "#5E4A38"],
    },
    "D": {  # Tableau10 earth-safe
        "label": "D",
        "colors": ["#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F", "#EDC948", "#B07AA1", "#FF9DA7"],
    },
}


def validate_variant_specs(run_dir: str, experiments: list[str],
                           variant_idx: int) -> bool:
    """Check that a beam variant produced valid specs for all experiments."""
    return validate_plot_specs(run_dir, experiments, variant_idx=variant_idx)


def parse_style_enforcement_block(content: str) -> tuple[str, dict | None, str]:
    """Parse a styled spec into (before, block_fields, after).

    Returns the block fields as a dict if found, else None.
    The before/after strings allow clean reconstruction.
    """
    pattern = r'(.*?)(=== STYLE ENFORCEMENT ===\s*\n)(.*?)(=== END STYLE ENFORCEMENT ===)(.*)'
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return content, None, ""

    before = m.group(1) + m.group(2)
    block_body = m.group(3)
    after = m.group(4) + m.group(5)

    # Parse fields from the block body
    fields = {}
    # Extract simple key: value fields
    for key in ("PALETTE", "PALETTE_COLORS", "FIGURE_SIZE_TIER", "FIGURE_SIZE_INCHES",
                "LAYOUT_GRID", "COLOR_MAP", "FONT_BASE_SIZE", "DPI",
                "PANEL_ASPECT", "TIGHT_LAYOUT_PAD"):
        pat = rf'^{key}:\s*(.+)$'
        km = re.search(pat, block_body, re.MULTILINE)
        if km:
            fields[key] = km.group(1).strip()

    # Extract COLOR_MAP_PYTHON block (python code block after COLOR_MAP_PYTHON:)
    cmp_pat = r'COLOR_MAP_PYTHON:\s*\n```python\s*\n(.*?)```'
    cmp_m = re.search(cmp_pat, block_body, re.DOTALL)
    if cmp_m:
        fields["COLOR_MAP_PYTHON_CODE"] = cmp_m.group(1).strip()

    # Extract layout spacing if present anywhere in the block body
    for sp_key in ("wspace", "hspace"):
        sp_pat = rf'{sp_key}\s*=\s*([\d.]+)'
        sp_m = re.search(sp_pat, block_body)
        if sp_m:
            fields[sp_key] = sp_m.group(1)

    fields["_raw_body"] = block_body
    return before, fields, after


def rebuild_style_enforcement_block(fields: dict, palette: dict,
                                    categories: list[str],
                                    wspace: str | None = None,
                                    hspace: str | None = None) -> str:
    """Rebuild the style enforcement block body with updated palette fields.

    Updates PALETTE, PALETTE_COLORS, COLOR_MAP, COLOR_MAP_PYTHON consistently.
    """
    body = fields["_raw_body"]

    # 1. Update PALETTE label
    old_pal = fields.get("PALETTE", "")
    if old_pal:
        body = re.sub(r'^(PALETTE:)\s*.+$', rf'\1 {palette["label"]}', body, count=1, flags=re.MULTILINE)

    # 2. Update PALETTE_COLORS
    new_colors_str = ", ".join(palette["colors"])
    old_colors = fields.get("PALETTE_COLORS", "")
    if old_colors:
        body = re.sub(r'^(PALETTE_COLORS:)\s*.+$', rf'\1 {new_colors_str}', body, count=1, flags=re.MULTILINE)

    # 3. Update COLOR_MAP — assign palette colors to categories round-robin
    color_map_entries = []
    py_map_entries = []
    for i, cat in enumerate(categories):
        color = palette["colors"][i % len(palette["colors"])]
        color_map_entries.append(f"{cat}={color}")
        py_map_entries.append(f'    "{cat}": "{color}"')

    old_cm = fields.get("COLOR_MAP", "")
    if old_cm:
        new_cm = ", ".join(color_map_entries)
        body = re.sub(r'^(COLOR_MAP:)\s*.+$', rf'\1 {new_cm}', body, count=1, flags=re.MULTILINE)

    # 4. Update COLOR_MAP_PYTHON code block
    if fields.get("COLOR_MAP_PYTHON_CODE"):
        new_py_code = "color_map = {\n" + ",\n".join(py_map_entries) + ",\n}"
        old_py_block_pat = r'(COLOR_MAP_PYTHON:\s*\n```python\s*\n).*?(```)'
        body = re.sub(old_py_block_pat, rf'\1{new_py_code}\n\2', body, count=1, flags=re.DOTALL)

    # 5. Update layout spacing if requested
    if wspace is not None:
        body = re.sub(r'wspace\s*=\s*[\d.]+', f'wspace = {wspace}', body)
    if hspace is not None:
        body = re.sub(r'hspace\s*=\s*[\d.]+', f'hspace = {hspace}', body)

    return body


def extract_categories_from_color_map(fields: dict) -> list[str]:
    """Extract category names from the existing COLOR_MAP or COLOR_MAP_PYTHON."""
    # Try COLOR_MAP_PYTHON first (more reliable)
    py_code = fields.get("COLOR_MAP_PYTHON_CODE", "")
    if py_code:
        cats = re.findall(r'"([^"]+)":\s*"#', py_code)
        if cats:
            return cats

    # Fallback: parse COLOR_MAP (format: cat1=#hex, cat2=#hex, ...)
    cm = fields.get("COLOR_MAP", "")
    if cm:
        cats = re.findall(r'(\S+?)=#', cm)
        if cats:
            return cats

    return []


def create_style_variant(base_spec_path: str, variant_idx: int,
                         run_dir: str, experiment: str) -> str:
    """Create a style variant by structurally rewriting the Style Enforcement Block.

    Updates PALETTE, PALETTE_COLORS, COLOR_MAP, and COLOR_MAP_PYTHON together
    so all palette sources of truth stay consistent. Also adjusts layout spacing
    for layout-variant palettes.

    Returns path to the variant spec file.
    """
    with open(base_spec_path) as f:
        content = f.read()

    before, fields, after = parse_style_enforcement_block(content)

    if fields is None:
        # No enforcement block found — fall back to writing unmodified content
        ui.warn(f"No STYLE ENFORCEMENT block in {base_spec_path}; variant {variant_idx} identical to base")
        exp_dir = orch_art.experiment_dir(run_dir, experiment)
        os.makedirs(exp_dir, exist_ok=True)
        variant_path = orch_art.beam_styled_spec_path(run_dir, experiment, variant_idx)
        with open(variant_path, "w") as f:
            f.write(content)
        return variant_path

    categories = extract_categories_from_color_map(fields)

    # Define variant mutations
    variant_configs = {
        1: {"palette": "B", "wspace": None, "hspace": None},           # colorblind-safe
        2: {"palette": "C", "wspace": "0.25", "hspace": "0.30"},       # muted compact
        3: {"palette": "D", "wspace": "0.45", "hspace": "0.50"},       # earth-safe spacious
    }

    config = variant_configs.get(variant_idx)
    if config is None:
        # Unknown variant index — write unmodified
        exp_dir = orch_art.experiment_dir(run_dir, experiment)
        os.makedirs(exp_dir, exist_ok=True)
        variant_path = orch_art.beam_styled_spec_path(run_dir, experiment, variant_idx)
        with open(variant_path, "w") as f:
            f.write(content)
        return variant_path

    palette = BEAM_PALETTES[config["palette"]]
    new_body = rebuild_style_enforcement_block(
        fields, palette, categories,
        wspace=config["wspace"], hspace=config["hspace"],
    )
    content = before + new_body + after

    exp_dir = orch_art.experiment_dir(run_dir, experiment)
    os.makedirs(exp_dir, exist_ok=True)
    variant_path = orch_art.beam_styled_spec_path(run_dir, experiment, variant_idx)
    with open(variant_path, "w") as f:
        f.write(content)

    # Optional debug mirror under debug/<exp>/beam/sN/ (same basename as v2 experiment spec)
    variant_dir = os.path.join(plot_debug_dir(run_dir, experiment), "beam", f"s{variant_idx}")
    os.makedirs(variant_dir, exist_ok=True)
    debug_copy = os.path.join(variant_dir, orch_art.STYLED_SPEC)
    with open(debug_copy, "w") as f:
        f.write(content)

    return variant_path


def beam_rank_key(candidate: dict) -> tuple:
    """Rank candidates by (score, ACCEPT tie-break) — matches LangGraph beam ranking."""
    score = candidate.get("score", 0.0)
    verdict = (candidate.get("result", {}).get("verdict") or "").upper()
    accept_bonus = 1 if "ACCEPT" in verdict else 0
    return (score, accept_bonus)


def format_prior_feedback(result: dict) -> str:
    """Format a single critic result into a feedback string."""
    parts = []
    score = result.get("score", "N/A")
    verdict = result.get("verdict", "N/A")
    parts.append(f"Score: {score}, Verdict: {verdict}")
    for key in ("feedback", "critique", "suggestions", "dimension_scores"):
        val = result.get(key)
        if val:
            parts.append(f"{key}: {json.dumps(val) if isinstance(val, (dict, list)) else val}")
    return "\n".join(parts)


def build_feedback_history(candidate: dict, max_chars: int | None = None) -> str:
    """Build cumulative feedback history from all prior beam iterations.

    Each candidate carries a ``feedback_history`` list of ``(tag, feedback)``
    tuples from previous rounds.  Keeps the last 2 rounds in full and
    summarises older rounds to a one-line score/verdict to stay within
    *max_chars* (default from ``pipeline.yaml`` ``context.max_feedback_chars``
    or 3000).
    """
    if max_chars is None:
        from graphs.svg_utils import load_pipeline_config
        cfg = load_pipeline_config()
        max_chars = cfg.get("context", {}).get("max_feedback_chars", 3000)

    history = candidate.get("feedback_history", [])
    current = format_prior_feedback(candidate["result"])

    # Partition: older rounds get summarised, recent rounds kept full.
    full_rounds: list[str] = []
    summary_rounds: list[str] = []
    for i, (tag, fb) in enumerate(history):
        if i >= len(history) - 1:
            # Keep last prior round in full
            full_rounds.append(f"--- Round {i + 1} ({tag}) ---\n{fb}")
        else:
            score_m = re.search(r"Score:\s*([\d.]+)", fb)
            score = score_m.group(1) if score_m else "?"
            verdict_m = re.search(r"Verdict:\s*(\w+)", fb)
            verdict = verdict_m.group(1) if verdict_m else "?"
            summary_rounds.append(f"Round {i + 1} ({tag}): score={score}, verdict={verdict}")

    parts: list[str] = []
    if summary_rounds:
        parts.append("Prior rounds (summarised): " + "; ".join(summary_rounds))
    parts.extend(full_rounds)
    parts.append(
        f"--- Round {len(history) + 1} ({candidate['tag']}) [current] ---\n{current}"
    )
    result = "\n\n".join(parts)

    # Hard cap — truncate from the front (oldest summaries) if still over budget.
    if len(result) > max_chars:
        result = result[-max_chars:]
        # Re-align to a line boundary
        nl = result.find("\n")
        if nl != -1 and nl < 200:
            result = "[...truncated]\n" + result[nl + 1:]
    return result


def beam_run_initial_candidates(
    beam_dir: str,
    exp_variant_specs: list[str],
    code_variants: int,
    run_dir: str,
    exp: str,
    experiments_dir: str,
    args: argparse.Namespace,
) -> list[dict]:
    """Run initial beam candidates (all style variants x code variants) in parallel.

    Returns a list of candidate dicts with keys: score, result, spec, tag,
    work_dir, feedback_history.
    """
    candidate_args = []
    for sv, spec in enumerate(exp_variant_specs):
        for cv in range(code_variants):
            tag = f"s{sv}_c{cv}"
            work_dir = os.path.join(beam_dir, tag)
            os.makedirs(work_dir, exist_ok=True)
            candidate_args.append((tag, spec, sv, cv, work_dir))

    all_tags = [ca[0] for ca in candidate_args]
    dashboard = ui.ProgressDashboard(all_tags)
    dashboard.start()

    candidates = []
    try:
        with ThreadPoolExecutor(max_workers=min(len(candidate_args), 4)) as pool:
            futures = {
                pool.submit(
                    run_code_agent, run_dir, exp, experiments_dir,
                    spec_path=ca[1], verbose=args.verbose,
                    work_dir=ca[4], label=ca[0],
                ): ca
                for ca in candidate_args
            }
            for ca in candidate_args:
                dashboard.update(ca[0], "running")

            for future in as_completed(futures):
                tag, spec, sv, cv, work_dir = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"score": 0, "verdict": "FAILED", "error": str(exc)}
                    dashboard.update(tag, "failed")
                score = result.get("score", 0.0)
                candidates.append({
                    "score": score,
                    "result": result,
                    "spec": spec,
                    "tag": tag,
                    "work_dir": work_dir,
                    "feedback_history": [],
                })
                dashboard.update(tag, f"done ({score:.1f})")
                ui.result(tag, score, result.get("verdict", "?"))
    finally:
        dashboard.stop()

    return candidates


def beam_run_refinement(
    survivors: list[dict],
    code_variants: int,
    beam_dir: str,
    run_dir: str,
    exp: str,
    experiments_dir: str,
    args: argparse.Namespace,
    iteration: int,
) -> list[dict]:
    """Refine beam survivors with prior feedback, running new candidates in parallel.

    Returns a list of new candidate dicts.
    """
    # Check for cross-session doom loops — inject corrective guidance if detected.
    # Scoped to "code-agent" so failures from other agents don't leak in.
    from pipeline.agent_runtime import get_doom_loop_guidance
    doom_guidance = get_doom_loop_guidance("code-agent")

    refine_args = []
    for survivor in survivors:
        # Build cumulative feedback history
        full_feedback = build_feedback_history(survivor)
        if doom_guidance:
            full_feedback = doom_guidance + "\n\n" + full_feedback
        # Carry forward history for the next round
        prev_history = list(survivor.get("feedback_history", []))
        prev_history.append((
            survivor["tag"],
            format_prior_feedback(survivor["result"]),
        ))
        for cv in range(code_variants):
            # Nested under beam/: {parent_tag}_r{iteration}_c{cv}
            refine_tag = f"{survivor['tag']}_r{iteration}_c{cv}"
            refine_work_dir = os.path.join(beam_dir, refine_tag)
            os.makedirs(refine_work_dir, exist_ok=True)
            refine_args.append((
                refine_tag, survivor["spec"], full_feedback,
                refine_work_dir, prev_history,
            ))

    total_cands = len(refine_args)
    ui.info(f"Refining {total_cands} candidates in parallel...")

    all_tags = [ra[0] for ra in refine_args]
    dashboard = ui.ProgressDashboard(all_tags)
    dashboard.start()

    new_candidates = []
    try:
        with ThreadPoolExecutor(max_workers=min(total_cands, 4)) as pool:
            futures = {
                pool.submit(
                    run_code_agent, run_dir, exp, experiments_dir,
                    spec_path=ra[1], verbose=args.verbose,
                    prior_feedback=ra[2],
                    work_dir=ra[3], label=ra[0],
                ): ra
                for ra in refine_args
            }
            for ra in refine_args:
                dashboard.update(ra[0], "running")

            for future in as_completed(futures):
                ra = futures[future]
                refine_tag, spec, _, refine_work_dir, prev_history = ra
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"score": 0, "verdict": "FAILED", "error": str(exc)}
                    dashboard.update(refine_tag, "failed")
                score = result.get("score", 0.0)
                new_candidates.append({
                    "score": score,
                    "result": result,
                    "spec": spec,
                    "tag": refine_tag,
                    "work_dir": refine_work_dir,
                    "feedback_history": prev_history,
                })
                dashboard.update(refine_tag, f"done ({score:.1f})")
                ui.result(refine_tag, score, result.get("verdict", "?"))
    finally:
        dashboard.stop()

    return new_candidates


def beam_log_iteration(
    beam_log_lines: list[str],
    iteration: int,
    candidates: list[dict],
    survivors: list[dict],
) -> None:
    """Log iteration rankings and survivors to beam_log_lines (mutates in place)."""
    beam_log_lines.append(f"## Iteration {iteration} — {len(candidates)} candidates")
    beam_log_lines.append("")
    for i, c in enumerate(candidates):
        verdict = c.get("result", {}).get("verdict", "?")
        beam_log_lines.append(
            f"  {i+1}. score={c['score']:.1f}  verdict={verdict}  tag={c['tag']}"
        )

    survivor_summary = ", ".join(f"{s['tag']}({s['score']:.1f})" for s in survivors)
    beam_log_lines.append("")
    beam_log_lines.append(f"  -> Survivors: {survivor_summary}")
    beam_log_lines.append("")


def step_plan_and_style_beam(run_dir: str, args: argparse.Namespace
                             ) -> tuple[list[str], dict[str, list[str]]]:
    """Step 2 (beam mode): Run planner-stylist S times with variation hints.

    Each run produces a complete, internally consistent set of specs where
    palette, layout, plot types, prose, and enforcement block all align.

    Variant 0 (base) lives at ``<run_dir>/experiments/<exp>/styled_spec.md``, copied to
    ``styled_spec_s0.md``. Variants 1+ write to ``experiments/<exp>/styled_spec_sN.md``.

    Returns:
        (experiments, variant_specs) where variant_specs maps experiment name
        to a list of spec paths [s0_path, s1_path, ...].
    """
    experiments_dir = os.path.abspath(args.experiments_dir) if args.experiments_dir else ""
    n_style_variants = getattr(args, "style_variants", 2) or 2
    n_style_variants = min(n_style_variants, len(BEAM_STYLE_VARIATION_HINTS))

    ui.info(f"Beam planning: {n_style_variants} style variants via planner-stylist")

    # ── Variant 0 (base) — writes to standard locations ──────────────
    from pipeline.feedback import collect_feedback_paths
    fb_paths = collect_feedback_paths(run_dir, "design")

    base_prompt = build_planner_stylist_prompt(
        run_dir,
        experiments_dir,
        write_instruction=planner_base_write_instruction(run_dir),
        variation_hint=BEAM_STYLE_VARIATION_HINTS[0],
        feedback_paths=fb_paths,
    )
    ui.info("Planner-stylist variant s0 (base)...")
    spawn_subagent(
        "planner-stylist",
        base_prompt,
        verbose=args.verbose,
        log_dir=os.path.join(run_dir, "logs"),
        log_name="planner-stylist_s0",
    )

    experiments = get_experiments(run_dir)
    if not experiments:
        ui.error("Variant s0 produced no experiments.")
        sys.exit(1)

    # Validate base specs
    for exp in experiments:
        spec = orch_art.experiment_styled_spec_path(run_dir, exp)
        if not os.path.exists(spec):
            ui.error(f"Missing base spec for {exp}")
            sys.exit(1)

    for exp in experiments:
        base = orch_art.experiment_styled_spec_path(run_dir, exp)
        s0 = orch_art.beam_styled_spec_path(run_dir, exp, 0)
        os.makedirs(orch_art.experiment_dir(run_dir, exp), exist_ok=True)
        shutil.copy2(base, s0)

    variant_specs: dict[str, list[str]] = {
        exp: [orch_art.beam_styled_spec_path(run_dir, exp, 0)]
        for exp in experiments
    }

    # ── Variants 1+ — styled_spec_sN.md per experiment ───────────────
    for sv in range(1, n_style_variants):
        hint = BEAM_STYLE_VARIATION_HINTS[sv]
        ui.info(f"Planner-stylist variant s{sv}...")

        variant_prompt = build_planner_stylist_prompt(
            run_dir,
            experiments_dir,
            write_instruction=planner_variant_write_instruction(run_dir, sv),
            variation_hint=hint,
            include_style_directive=False,
            experiments=experiments,
            feedback_paths=fb_paths,
        )
        spawn_subagent(
            "planner-stylist",
            variant_prompt,
            verbose=args.verbose,
            log_dir=os.path.join(run_dir, "logs"),
            log_name=f"planner-stylist_s{sv}",
        )

        if validate_variant_specs(run_dir, experiments, sv):
            for exp in experiments:
                variant_specs[exp].append(orch_art.beam_styled_spec_path(run_dir, exp, sv))
            ui.success(f"Variant s{sv}: all specs valid")
        else:
            # Fallback: structural rewrite from base spec
            ui.warn(f"Variant s{sv} incomplete, falling back to structural rewrite from base")
            for exp in experiments:
                base_spec = orch_art.experiment_styled_spec_path(run_dir, exp)
                fallback_path = create_style_variant(base_spec, sv, run_dir, exp)
                variant_specs[exp].append(fallback_path)

    # Report
    for exp in experiments:
        ui.info(f"{exp}: {len(variant_specs[exp])} variants")
        for i, sp in enumerate(variant_specs[exp]):
            with open(sp) as f:
                lines = len(f.readlines())
            ui.dim(f"    s{i}: {lines} lines")

    persist_plot_plan_state(run_dir, experiments, execution="beam")

    return experiments, variant_specs


def _select_winning_style(
    all_candidates: dict[str, list[dict]],
    n_style_variants: int,
) -> int:
    """Pick the style variant with the highest aggregate score across experiments.

    For each style variant sN, take the best candidate score per experiment,
    then average across experiments.  Returns the winning variant index.
    """
    style_scores: dict[int, list[float]] = {sv: [] for sv in range(n_style_variants)}
    for exp, candidates in all_candidates.items():
        # Group candidates by style variant index (parsed from tag "s{N}_c{M}")
        best_per_style: dict[int, float] = {}
        for c in candidates:
            tag = c["tag"]
            sv_match = re.match(r"s(\d+)", tag)
            if not sv_match:
                continue
            sv = int(sv_match.group(1))
            score = c.get("score", 0.0)
            if sv not in best_per_style or score > best_per_style[sv]:
                best_per_style[sv] = score
        for sv, score in best_per_style.items():
            if sv in style_scores:
                style_scores[sv].append(score)

    # Average score per style across experiments; styles missing from some
    # experiments get a 0 for those experiments.
    n_exp = len(all_candidates)
    style_avg = {}
    for sv, scores in style_scores.items():
        # Pad with zeros for experiments where this style had no candidates
        style_avg[sv] = sum(scores) / n_exp if n_exp > 0 else 0.0

    winning = max(style_avg, key=lambda sv: style_avg[sv])
    return winning


def step_execute_beam(run_dir: str, experiments: list[str],
                      args: argparse.Namespace,
                      variant_specs: dict[str, list[str]] | None = None) -> None:
    """Step 3: Beam search with cross-experiment style consistency.

    Iteration 1 runs ALL experiments × ALL style variants to select a winning
    style across experiments.  Subsequent iterations refine only pending
    experiments within the locked style, with per-experiment early-stop on
    ACCEPT.

    Args:
        variant_specs: Map of experiment -> list of spec paths (from planner-stylist
            beam). If None, falls back to structural rewrite from base spec.
    """
    experiments_dir = os.path.abspath(args.experiments_dir) if args.experiments_dir else ""
    beam_width = getattr(args, "beam_width", 2) or 2
    n_style_variants = getattr(args, "style_variants", 2) or 2
    code_variants = getattr(args, "code_variants", 2) or 2
    beam_iterations = getattr(args, "beam_iterations", 2) or 2

    ui.info(f"Beam search: width={beam_width}, style_variants={n_style_variants}, "
           f"code_variants={code_variants}, iterations={beam_iterations}")

    # Print candidate tag legend.
    style_names = _STYLE_VARIANT_NAMES[:n_style_variants]
    legend_lines = ["Candidate tag legend:"]
    for i, name in enumerate(style_names):
        legend_lines.append(f"  s{i} = {name}")
    legend_lines.append(f"  c{{N}} = code variant N (0–{code_variants - 1})")
    if beam_iterations > 1:
        legend_lines.append("  r{{N}} = refinement round N")
    ui.dim("\n".join(legend_lines))

    # ── Prepare variant specs per experiment ─────────────────────────────
    all_variant_specs: dict[str, list[str]] = {}
    beam_dirs: dict[str, str] = {}
    beam_logs: dict[str, list[str]] = {}

    for exp in experiments:
        if variant_specs and exp in variant_specs:
            exp_variant_specs = variant_specs[exp]
        else:
            base_spec = orch_art.experiment_styled_spec_path(run_dir, exp)
            exp_variant_specs = [base_spec]
            for sv in range(1, n_style_variants):
                exp_variant_specs.append(create_style_variant(base_spec, sv, run_dir, exp))
        all_variant_specs[exp] = exp_variant_specs

        beam_dir = os.path.join(plot_debug_dir(run_dir, exp), "beam")
        os.makedirs(beam_dir, exist_ok=True)
        beam_dirs[exp] = beam_dir

        beam_logs[exp] = [
            f"# Beam Search: {exp}",
            "",
            f"Parameters: B={beam_width}, S={n_style_variants}, C={code_variants}, iters={beam_iterations}",
            "",
        ]

        # Dedup check
        seen_color_hashes: dict[str, int] = {}
        for sv, spec_path in enumerate(exp_variant_specs):
            with open(spec_path) as f:
                spec_content = f.read()
            _, fields, _ = parse_style_enforcement_block(spec_content)
            cm_key = (fields or {}).get("COLOR_MAP_PYTHON_CODE", "")
            if cm_key in seen_color_hashes and cm_key:
                ui.warn(f"{exp}: variant s{sv} has identical palette as s{seen_color_hashes[cm_key]}")
                beam_logs[exp].append(
                    f"**WARNING**: s{sv} identical palette to s{seen_color_hashes[cm_key]}"
                )
            if cm_key:
                seen_color_hashes[cm_key] = sv

    # ── Iteration 1: run ALL experiments × ALL variants ──────────────────
    ui.section("Beam iteration 1: all experiments × all variants")
    all_candidates: dict[str, list[dict]] = {}
    for exp in experiments:
        total = len(all_variant_specs[exp]) * code_variants
        ui.info(f"{exp}: {total} candidates")
        candidates = beam_run_initial_candidates(
            beam_dirs[exp], all_variant_specs[exp], code_variants,
            run_dir, exp, experiments_dir, args,
        )
        candidates.sort(key=beam_rank_key, reverse=True)
        all_candidates[exp] = candidates
        beam_log_iteration(beam_logs[exp], 1, candidates, candidates[:beam_width])

    # ── Style selection: pick winning style across experiments ────────────
    winning_style = _select_winning_style(all_candidates, n_style_variants)
    ui.success(f"Locked winning style: s{winning_style} "
              f"({_STYLE_VARIANT_NAMES[winning_style] if winning_style < len(_STYLE_VARIANT_NAMES) else '?'})")

    # Filter to winning-style candidates only, then keep top beam_width per experiment.
    # Also identify experiments that are already ACCEPTED.
    pending: dict[str, list[dict]] = {}  # exp -> survivors (for refinement)
    results: list[tuple[str, dict]] = []
    summaries: list[dict] = []

    for exp in experiments:
        style_candidates = [
            c for c in all_candidates[exp]
            if c["tag"].startswith(f"s{winning_style}_")
        ]
        style_candidates.sort(key=beam_rank_key, reverse=True)
        survivors = style_candidates[:beam_width]

        if not survivors:
            # No candidates matched the winning style — fall back to best overall candidate
            ui.warn(f"{exp}: no candidates for winning style s{winning_style}, using best overall")
            fallback = sorted(all_candidates[exp], key=beam_rank_key, reverse=True)
            if fallback:
                survivors = fallback[:beam_width]
            else:
                ui.error(f"{exp}: no candidates at all, skipping")
                continue

        best_verdict = (survivors[0].get("result", {}).get("verdict") or "").upper()
        if "ACCEPT" in best_verdict:
            # Early stop: this experiment is done
            best = survivors[0]
            ui.success(f"{exp}: ACCEPTED (score {best['score']:.1f}) — early stop")
            beam_logs[exp].append(
                f"**Early stop** — ACCEPTED with score {best['score']:.1f} (style s{winning_style})"
            )
            summary = finalize_plot_experiment(run_dir, exp, best["result"], work_dir=best["work_dir"])
            results.append((exp, summary))
            summaries.append(summary)
        else:
            pending[exp] = survivors
            scores_str = ", ".join(f"{s['score']:.1f}" for s in survivors)
            ui.info(f"{exp}: pending (survivors: [{scores_str}])")

    # ── Iterations 2+: refine only pending experiments ───────────────────
    for iteration in range(2, beam_iterations + 1):
        if not pending:
            break
        ui.section(f"Beam iteration {iteration}: {len(pending)} pending experiments")

        newly_accepted: list[str] = []
        for exp, survivors in list(pending.items()):
            ui.info(f"Refining {exp}...")
            new_candidates = beam_run_refinement(
                survivors, code_variants, beam_dirs[exp],
                run_dir, exp, experiments_dir, args, iteration,
            )
            new_candidates.sort(key=beam_rank_key, reverse=True)
            new_survivors = new_candidates[:beam_width]

            beam_log_iteration(beam_logs[exp], iteration, new_candidates, new_survivors)

            best_verdict = (new_survivors[0].get("result", {}).get("verdict") or "").upper() if new_survivors else ""
            if "ACCEPT" in best_verdict:
                best = new_survivors[0]
                ui.success(f"{exp}: ACCEPTED at iteration {iteration} (score {best['score']:.1f})")
                beam_logs[exp].append(
                    f"**Early stop at iteration {iteration}** — ACCEPTED with score {best['score']:.1f}"
                )
                summary = finalize_plot_experiment(run_dir, exp, best["result"], work_dir=best["work_dir"])
                results.append((exp, summary))
                summaries.append(summary)
                newly_accepted.append(exp)
            else:
                pending[exp] = new_survivors
                scores_str = ", ".join(f"{s['score']:.1f}" for s in new_survivors)
                ui.info(f"{exp}: still pending (survivors: [{scores_str}])")

        for exp in newly_accepted:
            del pending[exp]

    # ── Finalize any remaining pending experiments with best candidate ────
    for exp, survivors in pending.items():
        best = survivors[0]
        ui.info(f"{exp}: promoting best candidate (score {best['score']:.1f}, not accepted)")
        summary = finalize_plot_experiment(run_dir, exp, best["result"], work_dir=best["work_dir"])
        results.append((exp, summary))
        summaries.append(summary)

    # ── Write beam summary logs ──────────────────────────────────────────
    for exp in experiments:
        # Find this experiment's final result
        exp_results = [r for r in results if r[0] == exp]
        if exp_results:
            _, summary = exp_results[0]
            best_figure = summary.get("figure_path", "")
            beam_logs[exp].append("## Final Winner")
            beam_logs[exp].append("")
            score = summary.get("score", "?")
            verdict = summary.get("verdict", "?")
            beam_logs[exp].append(
                f"  style=s{winning_style} | score={score} | verdict={verdict} | figure={best_figure}"
            )
            beam_logs[exp].append("")

        beam_summary_path = os.path.join(plot_debug_dir(run_dir, exp), "beam_summary.md")
        with open(beam_summary_path, "w") as f:
            f.write("\n".join(beam_logs[exp]))

    persist_plot_execution_state(run_dir, "beam", summaries)
    ui.summary_table(results)
