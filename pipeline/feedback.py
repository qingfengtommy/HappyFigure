"""Human review feedback: template generation, parsing, preferences, and interactive CLI.

The ``--review`` flag controls template generation and resume parsing.
Style preferences (``configs/feedback/style_preferences.yaml``) are loaded
on every run when the file exists, regardless of ``--review``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.orchestrator import artifacts as art


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ExperimentFeedback:
    experiment: str
    style: list[str] = field(default_factory=list)
    data: list[str] = field(default_factory=list)
    code: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.style or self.data or self.code)


@dataclass
class ReviewFeedback:
    global_style: list[str] = field(default_factory=list)
    global_data: list[str] = field(default_factory=list)
    global_code: list[str] = field(default_factory=list)
    experiments: dict[str, ExperimentFeedback] = field(default_factory=dict)
    earliest_affected_stage: str = "generate"

    @property
    def empty(self) -> bool:
        if self.global_style or self.global_data or self.global_code:
            return False
        return all(ef.empty for ef in self.experiments.values())


# ---------------------------------------------------------------------------
# Review template generation
# ---------------------------------------------------------------------------


def _read_critic_result(run_dir: str, experiment: str) -> dict | None:
    path = os.path.join(art.experiment_dir(run_dir, experiment), art.CRITIC_RESULT)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _build_review_md(
    run_dir: str,
    experiments: list[str],
    global_lines: list[str] | None = None,
    per_exp_feedback: dict[str, list[str]] | None = None,
) -> str:
    """Build review.md content.  Shared by template generation and interactive review."""
    lines = [
        "# HappyFigure Review\n",
        "Instructions: Write feedback under each experiment. Tag lines to route:",
        "  [style] \u2192 re-runs style + code (chart type, colors, fonts, layout)",
        "  [data]  \u2192 re-runs explore + style + code (missing data, wrong columns)",
        "  [code]  \u2192 re-runs code only (axis scale, legend, labels)",
        "  Untagged lines default to [code].\n",
        "Leave a section empty to accept the figure as-is.\n",
        "## Global Feedback",
    ]
    if global_lines:
        lines.extend(global_lines)
    else:
        lines.append("<!-- Rules that apply to ALL figures in this run -->")
    lines.append("\n")

    for exp in sorted(experiments):
        cr = _read_critic_result(run_dir, exp)
        fig_path = os.path.join(art.OUTPUTS_DIR, exp, "figure.png")
        score = cr.get("score", "N/A") if cr else "N/A"
        verdict = cr.get("verdict", "N/A") if cr else "N/A"
        issues = cr.get("issues", []) if cr else []

        lines.append(f"## {exp}")
        lines.append(f"Figure: {fig_path}")
        lines.append(f"Score: {score} / 10 \u2014 {verdict}")
        if issues:
            lines.append("Issues:")
            for issue in issues:
                lines.append(f"- {issue}")
        else:
            lines.append("Issues: (none)")
        lines.append("")
        lines.append("### Feedback:")
        fb = (per_exp_feedback or {}).get(exp, [])
        if fb:
            lines.extend(fb)
        lines.append("\n")

    return "\n".join(lines)


def generate_review_template(run_dir: str, experiments: list[str]) -> str:
    """Write ``review.md`` template to *run_dir*.  Returns the path written."""
    content = _build_review_md(run_dir, experiments)
    path = art.review_template_path(run_dir)
    Path(path).write_text(content)
    return path


# ---------------------------------------------------------------------------
# Review parsing
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"^\[(style|data|code)\]\s*", re.IGNORECASE)


def _classify_line(line: str) -> tuple[str, str]:
    """Return (tag, text) for a feedback line."""
    m = _TAG_RE.match(line)
    if m:
        return m.group(1).lower(), line[m.end() :].strip()
    return "code", line.strip()


def parse_review(run_dir: str) -> ReviewFeedback | None:
    """Parse ``review.md``, write per-stage feedback files to ``feedback/``.

    Side effect: creates ``run_dir/feedback/human_*`` files so agents can
    read stage-scoped feedback by path.

    Returns None if the file doesn't exist or contains no feedback.
    """
    review_path = art.review_template_path(run_dir)
    if not os.path.exists(review_path):
        return None

    text = Path(review_path).read_text()
    review = ReviewFeedback()

    # Split into sections by ## headers
    sections: list[tuple[str, str]] = []
    current_header = ""
    current_lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith("## ") and not line.startswith("### "):
            if current_header:
                sections.append((current_header, "\n".join(current_lines)))
            current_header = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_header:
        sections.append((current_header, "\n".join(current_lines)))

    for header, body in sections:
        # Extract feedback lines from ### Feedback: subsection or whole body
        feedback_start = body.find("### Feedback:")
        if feedback_start >= 0:
            feedback_text = body[feedback_start + len("### Feedback:") :]
        elif header.lower() == "global feedback":
            feedback_text = body
        else:
            feedback_text = body

        feedback_lines: list[tuple[str, str]] = []
        for raw_line in feedback_text.split("\n"):
            stripped = raw_line.strip()
            if (
                not stripped
                or stripped.startswith("<!--")
                or stripped.startswith("Figure:")
                or stripped.startswith("Score:")
                or stripped.startswith("Issues:")
            ):
                continue
            # Strip leading "- " (markdown bullet) before classifying
            if stripped.startswith("- "):
                stripped = stripped[2:].strip()
                if not stripped:
                    continue
            # Skip untagged issue lines from the template (no [tag] prefix)
            tag, text = _classify_line(stripped)
            if text:
                feedback_lines.append((tag, text))

        if not feedback_lines:
            continue

        if header.lower() == "global feedback":
            for tag, text in feedback_lines:
                if tag == "style":
                    review.global_style.append(text)
                elif tag == "data":
                    review.global_data.append(text)
                else:
                    review.global_code.append(text)
        else:
            exp_name = header.strip()
            ef = ExperimentFeedback(experiment=exp_name)
            for tag, text in feedback_lines:
                if tag == "style":
                    ef.style.append(text)
                elif tag == "data":
                    ef.data.append(text)
                else:
                    ef.code.append(text)
            review.experiments[exp_name] = ef

    if review.empty:
        return None

    # Determine earliest affected stage
    has_data = bool(review.global_data) or any(ef.data for ef in review.experiments.values())
    has_style = bool(review.global_style) or any(ef.style for ef in review.experiments.values())
    if has_data:
        review.earliest_affected_stage = "explore"
    elif has_style:
        review.earliest_affected_stage = "design"
    else:
        review.earliest_affected_stage = "generate"

    # Write per-stage feedback files
    _write_feedback_files(run_dir, review)
    return review


def _write_feedback_files(run_dir: str, review: ReviewFeedback) -> None:
    """Write per-stage and per-experiment feedback files to run_dir/feedback/."""
    fb_dir = art.feedback_dir(run_dir)
    os.makedirs(fb_dir, exist_ok=True)

    def _write(path: str, items: list[str], header: str = "") -> None:
        if not items:
            return
        content = header + "\n".join(f"- {item}" for item in items) + "\n"
        Path(path).write_text(content)

    # Style feedback
    style_lines = list(review.global_style)
    for exp, ef in sorted(review.experiments.items()):
        for line in ef.style:
            style_lines.append(f"({exp}) {line}")
    _write(os.path.join(run_dir, art.HUMAN_STYLE_FEEDBACK), style_lines, "# Human Style Feedback\n\n")

    # Data feedback
    data_lines = list(review.global_data)
    for exp, ef in sorted(review.experiments.items()):
        for line in ef.data:
            data_lines.append(f"({exp}) {line}")
    _write(os.path.join(run_dir, art.HUMAN_DATA_FEEDBACK), data_lines, "# Human Data Feedback\n\n")

    # Code feedback
    code_lines = list(review.global_code)
    for exp, ef in sorted(review.experiments.items()):
        for line in ef.code:
            code_lines.append(f"({exp}) {line}")
    _write(os.path.join(run_dir, art.HUMAN_CODE_FEEDBACK), code_lines, "# Human Code Feedback\n\n")

    # Per-experiment combined feedback
    for exp, ef in review.experiments.items():
        all_lines = (
            [f"[style] {line}" for line in ef.style]
            + [f"[data] {line}" for line in ef.data]
            + [f"[code] {line}" for line in ef.code]
        )
        _write(art.human_experiment_feedback_path(run_dir, exp), all_lines, f"# Human Feedback: {exp}\n\n")


# ---------------------------------------------------------------------------
# Stage invalidation
# ---------------------------------------------------------------------------

_STAGE_ORDER = ["explore", "design", "generate", "assemble"]


def invalidate_stages_from(run_dir: str, stage: str) -> None:
    """Clear manifest entries for *stage* and all later stages."""
    from pipeline.contracts import StageRecord, StageStatus
    from pipeline.run_state import write_manifest_stage

    start = _STAGE_ORDER.index(stage) if stage in _STAGE_ORDER else 0
    for s in _STAGE_ORDER[start:]:
        write_manifest_stage(
            run_dir,
            s,
            StageRecord(status=StageStatus.PENDING),
        )


# ---------------------------------------------------------------------------
# Feedback path collection
# ---------------------------------------------------------------------------


def collect_feedback_paths(
    run_dir: str,
    stage: str,
    experiment: str | None = None,
) -> list[str]:
    """Return existing feedback file paths relevant to *stage*/*experiment*.

    Returns an empty list when no feedback files exist.
    """
    candidates: list[str] = []

    prefs = art.style_preferences_path()
    if os.path.exists(prefs):
        candidates.append(prefs)

    if stage in ("design", "all"):
        candidates.append(os.path.join(run_dir, art.HUMAN_STYLE_FEEDBACK))
    if stage in ("explore", "all"):
        candidates.append(os.path.join(run_dir, art.HUMAN_DATA_FEEDBACK))
    if stage in ("generate", "all"):
        candidates.append(os.path.join(run_dir, art.HUMAN_CODE_FEEDBACK))
        if experiment:
            candidates.append(art.human_experiment_feedback_path(run_dir, experiment))

    if stage == "all":
        # Also add per-experiment files for all experiments
        fb_dir = art.feedback_dir(run_dir)
        if os.path.isdir(fb_dir):
            seen = set(candidates)
            for f in sorted(os.listdir(fb_dir)):
                full = os.path.join(fb_dir, f)
                if full not in seen and f.startswith("human_feedback_"):
                    candidates.append(full)
                    seen.add(full)

    return [p for p in candidates if os.path.exists(p)]


# ---------------------------------------------------------------------------
# Style preferences
# ---------------------------------------------------------------------------


def _load_preferences() -> list[str]:
    """Load existing style preferences list.

    Parses the simple YAML format written by ``_save_preferences`` without
    requiring the ``yaml`` library — each rule is ``  - "text"`` on its own line.
    """
    path = art.style_preferences_path()
    if not os.path.exists(path):
        return []
    rules: list[str] = []
    for line in Path(path).read_text().split("\n"):
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        value = stripped[2:].strip()
        # Remove surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1].replace('\\"', '"')
        if value:
            rules.append(value)
    return rules


def _save_preferences(rules: list[str]) -> None:
    """Write style preferences file atomically."""
    path = art.style_preferences_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    parts = [
        "# Oldest first. New rules append to bottom. Evicted from top when full.\n"
        "# Human-editable: add, remove, or reorder lines freely.\n"
        "preferences:\n"
    ]
    for rule in rules:
        escaped = rule.replace('"', '\\"')
        parts.append(f'  - "{escaped}"\n')
    content = "".join(parts)

    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".yaml")
    closed = False
    try:
        os.write(tmp_fd, content.encode())
        os.close(tmp_fd)
        closed = True
        os.replace(tmp_path, path)
    except Exception:
        if not closed:
            os.close(tmp_fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def update_style_preferences(review: ReviewFeedback) -> int:
    """Extract [style] feedback and append to project preferences.

    Returns the number of new rules added.
    """
    from graphs.svg_utils import load_pipeline_config

    cfg = load_pipeline_config()
    max_rules = cfg.get("feedback", {}).get("max_style_preferences", 30)

    new_rules = list(review.global_style)
    for ef in review.experiments.values():
        new_rules.extend(ef.style)

    if not new_rules:
        return 0

    existing = _load_preferences()
    existing_set = set(existing)
    added = 0
    for rule in new_rules:
        if rule not in existing_set:
            existing.append(rule)
            existing_set.add(rule)
            added += 1

    # Evict from top if over cap
    if len(existing) > max_rules:
        existing = existing[-max_rules:]

    if added:
        _save_preferences(existing)
    return added


# ---------------------------------------------------------------------------
# Interactive review CLI
# ---------------------------------------------------------------------------


def _open_image(path: str) -> bool:
    """Open an image in the system viewer.  Returns True on success."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except FileNotFoundError:
        import ui as _ui

        _ui.warn(f"No image viewer found — open {path} manually")
        return False
    except Exception:
        return False


def _discover_experiments(run_dir: str) -> list[str]:
    """Find experiment list from manifest or directory scan."""
    from pipeline.run_state import read_manifest

    manifest = read_manifest(run_dir)
    stages = manifest.get("stages", {})
    for stage_name in ("generate", "design", "explore"):
        stage_data = stages.get(stage_name, {})
        exps = stage_data.get("experiments", [])
        if exps:
            return exps
    # Fallback: scan experiments/ directory
    exp_dir = os.path.join(run_dir, art.EXPERIMENTS_DIR)
    if os.path.isdir(exp_dir):
        return sorted(
            d for d in os.listdir(exp_dir) if os.path.isdir(os.path.join(exp_dir, d)) and not d.startswith(".")
        )
    return []


def run_interactive_review(run_dir: str) -> None:
    """Guided interactive review of figures from a completed run.

    Uses the shared ``ui`` module for consistent styling with the main pipeline.
    """
    import ui

    run_dir = os.path.abspath(run_dir)
    if not os.path.isdir(run_dir):
        ui.error(f"Run directory not found: {run_dir}")
        sys.exit(1)

    from pipeline.context import PROJECT_ROOT

    ui.set_project_root(str(PROJECT_ROOT))

    experiments = _discover_experiments(run_dir)
    if not experiments:
        ui.error("No experiments found in run directory.")
        sys.exit(1)

    # Banner — same style as main pipeline entry
    run_name = os.path.basename(run_dir)
    ui.info(f"[orch.title]HappyFigure Review[/]  [orch.cyan]{run_name}[/]")
    ui.dim(f"  {len(experiments)} experiment(s)")

    per_exp_feedback: dict[str, list[str]] = {}

    for exp in experiments:
        cr = _read_critic_result(run_dir, exp)
        fig_rel = f"{art.OUTPUTS_DIR}/{exp}/figure.png"
        fig_path = os.path.join(run_dir, art.OUTPUTS_DIR, exp, "figure.png")
        fig_exists = os.path.exists(fig_path)

        # Section divider — matches ui.section() style
        ui.section(exp)

        # Figure info
        status = "[orch.green]exists[/]" if fig_exists else "[orch.red]missing[/]"
        ui.dim(f"  Figure: {fig_rel} ({status})")
        if cr:
            score = cr.get("score", "N/A")
            verdict = cr.get("verdict", "N/A")
            v_style = "orch.green" if verdict == "ACCEPT" else "orch.yellow"
            ui.info(f"  Score: {score} / 10 — [{v_style}]{verdict}[/]")
            issues = cr.get("issues", [])
            if issues:
                for issue in issues:
                    ui.dim(f"    - {issue}")
        else:
            ui.dim("  Score: N/A (no critic result)")

        valid_choices = {"a", "f", "s"}
        if fig_exists:
            valid_choices.add("o")
        while True:
            options = "[A]ccept  [F]eedback"
            if fig_exists:
                options += "  [O]pen image"
            options += "  [S]kip"
            try:
                choice = input(f"  {options}\n  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return

            if choice not in valid_choices:
                ui.warn(f"  Invalid choice '{choice}'. Enter one of: {', '.join(sorted(valid_choices))}")
                continue

            if choice == "a":
                ui.success(f"Accepted {exp}")
                break
            elif choice == "o":
                _open_image(fig_path)
                continue
            elif choice == "f":
                ui.dim("  Feedback (press Enter on empty line to finish):")
                fb_lines: list[str] = []
                while True:
                    try:
                        line = input("  > ")
                    except (EOFError, KeyboardInterrupt):
                        break
                    if not line.strip():
                        break
                    fb_lines.append(line.strip())
                if fb_lines:
                    per_exp_feedback[exp] = fb_lines
                    ui.success(f"Saved feedback for {exp}")
                else:
                    ui.success(f"Accepted {exp} (no feedback entered)")
                break
            elif choice == "s":
                ui.dim(f"  Skipped {exp}")
                break

    # Global feedback
    ui.section("Global feedback")
    ui.dim("  Any feedback that applies to ALL figures? (empty to skip)")
    global_lines: list[str] = []
    while True:
        try:
            line = input("  > ")
        except (EOFError, KeyboardInterrupt):
            break
        if not line.strip():
            break
        global_lines.append(line.strip())

    # Write review.md using shared builder
    content = _build_review_md(run_dir, experiments, global_lines, per_exp_feedback)
    Path(art.review_template_path(run_dir)).write_text(content)

    review_path = ui.short_path(art.review_template_path(run_dir))
    ui.success(f"Review saved to {review_path}")
    ui.dim(f"  Apply with: python cli.py plot --proposal <proposal> --resume {ui.short_path(run_dir)} --review")
