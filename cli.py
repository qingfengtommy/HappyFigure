"""
Entry point: HappyFigure figure generation pipeline.

Subcommands:
    plot / figure         — Generate statistical plots from experiment data
    diagram / method      — Generate method/architecture diagram (full SVG pipeline)
    sketch / method-svg   — Generate method diagram (lightweight, no services)
    composite / hybrid    — Diagram + programmatic visualization compositing
    review                — Interactively review figures from a completed run

Usage:
    # Statistical plots — positional paths (auto-detected)
    python run_once.py plot paper.md ./results
    python run_once.py plot ./data/dir1 ./data/dir2       # multiple results dirs
    python run_once.py plot paper.md                       # results-dir defaults to ./results

    # Explicit flags still work
    python run_once.py plot --proposal paper.md --results-dir ./results

    # Architecture diagram
    python run_once.py diagram paper.md

    # Quick architecture diagram (agent writes SVG directly, no services)
    python run_once.py sketch paper.md

    # Beam search for plots
    python run_once.py plot paper.md --execution beam

    # Switch agent platform (default from pipeline.yaml)
    python run_once.py plot paper.md --agent claude

    # Override LLM provider preset
    python run_once.py plot paper.md --llm-preset gemini

    # Human review: generate template, edit, resume with feedback
    python run_once.py plot paper.md --review
    python run_once.py review notes/figure_runs/run_20260406_220000
    python run_once.py plot --proposal paper.md --resume <run_dir> --review
"""
from __future__ import annotations

import argparse
import os
import sys

import ui

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

from pipeline.context import PROJECT_ROOT, RunnerContext, get_ctx, set_ctx
from pipeline.orchestrator.main import run_agent_pipeline
from pipeline.orchestrator.modes import resolve_mode


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Build the unified subcommand-based CLI parser."""
    global_parser = argparse.ArgumentParser(add_help=False)
    global_parser.add_argument("--agent", type=str, default=None,
                               choices=["opencode", "claude", "codex", "gemini", "copilot"],
                               help="Agent runner CLI (overrides pipeline.yaml agent.platform)")
    global_parser.add_argument("--llm-preset", type=str, default=None,
                               choices=["azure", "gemini", "mixed"],
                               help="Override LLM config from pipeline.yaml (optional)")
    global_parser.add_argument("--verbose", action="store_true",
                               help="Enable verbose logging")
    global_parser.add_argument("--resume", type=str, default=None, metavar="RUN_DIR",
                               help="Resume from a previous run directory (skips completed stages)")
    global_parser.add_argument("--orchestrator-mode", type=str, default=None,
                               choices=["agent-first", "python-stages"],
                               help="Internal orchestration mode (default from pipeline.yaml)")
    global_parser.add_argument("--review", action="store_true",
                               help="Enable human review: generate review.md after run, "
                                    "or consume existing review.md on --resume")

    _SUP = argparse.SUPPRESS
    global_parser_sub = argparse.ArgumentParser(add_help=False)
    global_parser_sub.add_argument("--agent", type=str, default=_SUP,
                                   choices=["opencode", "claude", "codex", "gemini", "copilot"],
                                   help=argparse.SUPPRESS)
    global_parser_sub.add_argument("--llm-preset", type=str, default=_SUP,
                                   choices=["azure", "gemini", "mixed"],
                                   help=argparse.SUPPRESS)
    global_parser_sub.add_argument("--verbose", action="store_true",
                                   default=_SUP, help=argparse.SUPPRESS)
    global_parser_sub.add_argument("--resume", type=str, default=_SUP,
                                   help=argparse.SUPPRESS)
    global_parser_sub.add_argument("--orchestrator-mode", type=str, default=_SUP,
                                   choices=["agent-first", "python-stages"],
                                   help=argparse.SUPPRESS)
    global_parser_sub.add_argument("--review", action="store_true",
                                   default=_SUP, help=argparse.SUPPRESS)

    class _HelpfulParser(argparse.ArgumentParser):
        _subparsers_map: dict | None = None

        def error(self, message: str):
            import sys as _sys
            _sys.stderr.write(f"error: {message}\n\n")
            sub_shown = False
            if self._subparsers_map:
                for arg in _sys.argv[1:]:
                    if arg in self._subparsers_map:
                        self._subparsers_map[arg].print_help(_sys.stderr)
                        sub_shown = True
                        break
            if not sub_shown:
                self.print_help(_sys.stderr)
            _sys.exit(2)

    parser = _HelpfulParser(
        prog="happyfigure",
        parents=[global_parser],
        description="HappyFigure — AI-powered scientific figure generation",
        epilog=(
            "examples:\n"
            "  %(prog)s plot paper.md                       # proposal file, results from ./results\n"
            "  %(prog)s plot paper.md ./exp1 ./exp2          # proposal + multiple results dirs\n"
            "  %(prog)s plot ./data/dir1 ./data/dir2         # results dirs only (no proposal)\n"
            "  %(prog)s plot --proposal paper.md             # explicit flag still works\n"
            "  %(prog)s plot paper.md --execution beam\n"
            "  %(prog)s diagram paper.md --quality-profile conference\n"
            "  %(prog)s composite paper.md ./results\n"
            "  %(prog)s sketch paper.md\n"
            "  %(prog)s plot paper.md --agent claude\n"
            "  %(prog)s plot paper.md --llm-preset gemini\n"
            "  %(prog)s review notes/figure_runs/run_20260406_220000\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", required=True,
                                       parser_class=_HelpfulParser,
                                       metavar="{plot|figure,diagram|method,sketch|method-svg,composite|hybrid}")

    # ── figure ──
    fig = subparsers.add_parser("plot", parents=[global_parser_sub], aliases=["figure"],
                                help="Generate statistical plots from experiment data (alias: figure)",
                                description="Generate statistical plots from experiment data. Alias: figure.",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    fig.add_argument("--proposal", type=str, default=None,
                     help="Path to proposal file (.md, .pdf, .tex) or directory containing proposal docs")
    fig.add_argument("--results-dir", "--results", type=str, default=None,
                     help="Path(s) to experiments/results directories, comma-separated for multiple (default: ./results)")
    fig.add_argument("paths", nargs="*", default=[],
                     help="Positional paths: files (.md/.pdf/.tex) → proposal, directories → results dirs")
    exec_group = fig.add_argument_group("execution strategy")
    exec_group.add_argument("--execution", type=str, default="sequential",
                            choices=["sequential", "parallel", "beam"],
                            help="sequential: one experiment at a time; "
                                 "parallel: all experiments concurrently; "
                                 "beam: style×code variant search ranked by critic (default: sequential)")

    beam_group = fig.add_argument_group("beam search parameters")
    beam_group.add_argument("--beam-width", type=int, default=2,
                            help="Candidates kept per round (default: 2)")
    beam_group.add_argument("--style-variants", type=int, default=2,
                            help="Style variants in first iteration (default: 2)")
    beam_group.add_argument("--code-variants", type=int, default=2,
                            help="Code variants per candidate (default: 2)")
    beam_group.add_argument("--beam-iterations", type=int, default=2,
                            help="Beam search iterations (default: 2)")

    # ── shared diagram arguments ──
    def _add_diagram_args(p):
        p.add_argument("--proposal", type=str, default=None,
                       help="Path to proposal file (.md, .pdf, .tex) or directory containing proposal docs")
        p.add_argument("--results-dir", "--results", type=str, default=None,
                       help="Path(s) to experiments/results directories, comma-separated for multiple")
        p.add_argument("paths", nargs="*", default=[],
                       help="Positional paths: files (.md/.pdf/.tex) → proposal, directories → results dirs")
        p.add_argument("--quality-profile", type=str, default="journal",
                       choices=["journal", "conference", "poster", "presentation",
                                "report", "grant", "thesis", "preprint", "default"],
                       help="Quality threshold profile (default: journal)")
        p.add_argument("--max-team-iterations", type=int, default=3,
                       help="Max review iterations (default: 3)")
        ig = p.add_argument_group("image source (mutually exclusive)")
        isrc = ig.add_mutually_exclusive_group()
        isrc.add_argument("--drawing-image", type=str, default=None,
                          help="Use existing image (skips proposer + image generation)")

    # ── method ──
    meth = subparsers.add_parser("diagram", parents=[global_parser_sub], aliases=["method"],
                                 help="Generate method/architecture diagram (full SVG pipeline; alias: method)",
                                 description="Generate method/architecture diagram via the full SVG pipeline. Alias: method.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_diagram_args(meth)
    meth.set_defaults(skip_viz_compose=True)

    # ── composite ──
    comp = subparsers.add_parser("composite", parents=[global_parser_sub], aliases=["hybrid"],
                                 help="Diagram + programmatic visualization compositing (alias: hybrid)",
                                 description="Generate a composite method diagram with programmatic visualization compositing. Alias: hybrid.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_diagram_args(comp)
    viz_group = comp.add_argument_group("viz-composer options")
    viz_group.add_argument("--skip-viz-compose", action="store_true",
                           help="Run diagram pipeline only, skip viz-composer step")

    parser._subparsers_map = {
        "plot": fig, "figure": fig,
        "diagram": meth, "method": meth,
        "composite": comp, "hybrid": comp,
    }

    # ── method-svg ──
    msvg = subparsers.add_parser("sketch", parents=[global_parser_sub], aliases=["method-svg"],
                                 help="Generate method diagram (lightweight, no services; alias: method-svg)",
                                 description="Generate a lightweight method diagram directly as SVG. Alias: method-svg.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    msvg.add_argument("--proposal", type=str, default=None,
                      help="Path to proposal file (.md, .pdf, .tex) or directory containing proposal docs")
    msvg.add_argument("--results-dir", "--results", type=str, default=None,
                      help="Path(s) to experiments/results directories, comma-separated for multiple")
    msvg.add_argument("paths", nargs="*", default=[],
                      help="Positional paths: files (.md/.pdf/.tex) → proposal, directories → results dirs")

    parser._subparsers_map["sketch"] = msvg
    parser._subparsers_map["method-svg"] = msvg

    # ── paper (unified composite) ──
    paper = subparsers.add_parser("paper", parents=[global_parser_sub], aliases=["paper-composite"],
                                   help="Generate all figures for a paper (plots + diagrams + assembly)",
                                   description="Paper-level pipeline: generate ALL figures — statistical plots, "
                                               "method diagrams, and hybrids — in one run with complex assembly.",
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    paper.add_argument("--proposal", type=str, default=None,
                       help="Path to proposal markdown file or directory (optional — agents discover from data)")
    paper.add_argument("--results-dir", "--results", type=str, default=None,
                       help="Path(s) to experiments/results directories, comma-separated for multiple")
    paper.add_argument("paths", nargs="*", default=[],
                       help="Positional paths: files (.md/.pdf/.tex) → proposal, directories → results dirs")
    paper.add_argument("--execution", choices=["sequential", "parallel", "beam"], default="parallel",
                       help="Execution strategy for statistical panels (default: parallel)")
    paper.add_argument("--figures", type=str, default=None,
                       help="Comma-separated figure IDs to generate (default: all)")
    paper.add_argument("--skip-assembly", action="store_true",
                       help="Generate panels only, skip assembly step")
    paper.add_argument("--skip-diagrams", action="store_true",
                       help="Skip diagram panel generation")
    paper.add_argument("--skip-plots", action="store_true",
                       help="Skip statistical plot generation")
    parser._subparsers_map["paper"] = paper
    parser._subparsers_map["paper-composite"] = paper

    # ── review ──
    rev = subparsers.add_parser("review", parents=[global_parser_sub],
                                help="Interactively review figures from a completed run",
                                description="Walk through each figure, view scores, provide feedback. "
                                            "Writes review.md for use with --resume --review.",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    rev.add_argument("run_dir", type=str,
                     help="Path to completed run directory")
    parser._subparsers_map["review"] = rev

    return parser


_PROPOSAL_EXTENSIONS = {".md", ".pdf", ".tex", ".txt", ".rst"}


def _looks_like_path(s: str) -> bool:
    """Return True if *s* looks like a filesystem path rather than natural language.

    Multi-word strings (3+ words) are treated as natural language instructions
    unless the entire string resolves to an existing file or directory.
    """
    s = s.strip()
    if not s:
        return False
    # Exists on disk — always a path, regardless of spaces
    if os.path.exists(s):
        return True
    # Multi-word strings that don't exist on disk → natural language
    # (even if they contain embedded paths like "explore /tmp/data")
    words = s.split()
    if len(words) >= 3:
        return False
    # Two-word strings: NL if neither word looks path-like
    if len(words) == 2:
        # e.g. "my results" → NL; "./results data" → could be two paths
        return any(
            w.startswith(("/", "./", "../", "~")) or os.path.splitext(w)[1]
            for w in words
        )
    # Single token — check if it looks like a path
    if s.startswith(("/", "./", "../", "~")):
        return True
    _, ext = os.path.splitext(s)
    if ext:
        return True
    # Single token, no extension, doesn't exist — still treat as relative path
    return True


def _resolve_positional_paths(args: argparse.Namespace) -> None:
    """Classify positional paths into --proposal / --results-dir when not set explicitly."""
    paths = getattr(args, "paths", None) or []
    if not paths:
        return

    proposal_candidates: list[str] = []
    dir_candidates: list[str] = []

    for p in paths:
        p = p.strip()
        if not p:
            continue
        resolved = os.path.abspath(p)
        if os.path.isdir(resolved):
            dir_candidates.append(resolved)
        elif os.path.isfile(resolved):
            _, ext = os.path.splitext(resolved)
            if ext.lower() in _PROPOSAL_EXTENSIONS:
                proposal_candidates.append(resolved)
            else:
                # Unknown file type — treat as results dir parent
                dir_candidates.append(os.path.dirname(resolved))
        else:
            # Path doesn't exist yet — guess by extension
            _, ext = os.path.splitext(p)
            if ext.lower() in _PROPOSAL_EXTENSIONS:
                proposal_candidates.append(resolved)
            else:
                dir_candidates.append(resolved)

    # Fill in --proposal from positional if not already set
    if not getattr(args, "proposal", None) and proposal_candidates:
        args.proposal = proposal_candidates[0]
        # Remaining proposal files treated as dirs (unlikely but safe)
        dir_candidates.extend(proposal_candidates[1:])

    # Fill in --results-dir from positional if not already set
    if not getattr(args, "results_dir", None) and dir_candidates:
        args.results_dir = ",".join(dir_candidates)


def _ensure_compat_attrs(args: argparse.Namespace) -> None:
    # Resolve positional paths into --proposal / --results-dir first.
    _resolve_positional_paths(args)

    # Detect natural-language instructions vs real paths.
    # When the user passes a description string instead of a file path, store
    # it as an instruction for the agent and clear the path field.
    args._proposal_instruction = None
    args._results_instruction = None

    proposal = getattr(args, "proposal", None)
    if proposal and not _looks_like_path(proposal):
        args._proposal_instruction = proposal
        args.proposal = None  # not a real path

    raw_results = getattr(args, "results_dir", None)
    if raw_results and not all(_looks_like_path(d.strip()) for d in raw_results.split(",") if d.strip()):
        args._results_instruction = raw_results
        args.results_dir = None  # not real paths

    # Default --results-dir for plot subcommand when nothing was provided.
    if getattr(args, "command", None) in ("plot", "figure") and not getattr(args, "results_dir", None) and not args._results_instruction:
        args.results_dir = "./results"

    # --results-dir: resolve all paths to absolute (handles comma-separated).
    raw_results = getattr(args, "results_dir", None)
    if not hasattr(args, "experiments_dir"):
        if raw_results:
            dirs = [os.path.abspath(d.strip()) for d in raw_results.split(",") if d.strip()]
            args.experiments_dir = ",".join(dirs)
        else:
            args.experiments_dir = raw_results

    # --proposal: accept file or directory.  When a directory is given, store
    # the resolved dir path on args._proposal_dir so downstream can tell the
    # agent to explore it.  args.proposal stays as-is (resolved later by
    # _ensure_session_proposal which handles the dir→file conversion).
    proposal = getattr(args, "proposal", None)
    if proposal and os.path.isdir(proposal):
        args._proposal_dir = os.path.abspath(proposal)
    else:
        args._proposal_dir = None

    if not hasattr(args, "mode") or args.mode is None:
        args.mode = resolve_mode(args)


def _load_pipeline_config() -> dict:
    from graphs.svg_utils import load_pipeline_config
    return load_pipeline_config()


def _resolve_llm_roles(config: dict, llm_preset: str | None) -> dict[str, str]:
    llm_cfg = config.get("llm", {})
    roles = llm_cfg.get("roles", {})
    summary: dict[str, str] = {}
    for role, role_def in roles.items():
        if isinstance(role_def, dict):
            provider = role_def.get("provider", "?")
            model = role_def.get("model", "?")
            summary[role] = f"{provider}/{model}"
    if llm_preset:
        presets = llm_cfg.get("presets", {})
        preset = presets.get(llm_preset, {})
        for role, role_def in preset.items():
            if isinstance(role_def, dict):
                provider = role_def.get("provider", "?")
                model = role_def.get("model", "?")
                summary[role] = f"{provider}/{model}"
    return summary


def _resolve_agent_platform(args: argparse.Namespace, config: dict) -> str:
    cli_agent = getattr(args, "agent", None)
    if cli_agent:
        return cli_agent
    return config.get("agent", {}).get("platform", "opencode")


def _preflight_auth_checks(config: dict, llm_preset: str | None) -> None:
    ctx = get_ctx()

    platform_result = ctx.orchestrator.check_auth()
    if platform_result["ok"]:
        ui.dim(f"  Agent: {platform_result['message']}")
    else:
        msg = f"  Agent: {platform_result['message']}"
        if platform_result.get("error"):
            msg += f" — {platform_result['error']}"
        ui.error(msg)
        sys.exit(1)

    import llm
    llm.init_from_config()
    if llm_preset:
        llm.apply_preset(llm_preset)

    llm_results = llm.check_connections()
    any_llm_ok = False
    for result in llm_results:
        if result["ok"]:
            ui.dim(f"  LLM: {result['message']}")
            any_llm_ok = True
        else:
            msg = f"  LLM: {result['message']}"
            if result.get("error"):
                msg += f" — {result['error']}"
            ui.warn(msg)

    # If no LLM provider is reachable, the figure-critic subagent won't be
    # able to score figures. Mark critic as unavailable so agents skip scoring
    # instead of all failing independently at runtime.
    if not any_llm_ok and llm_results:
        ui.warn("  Critic: no LLM providers available — scoring will be skipped")
        ctx.critic_available = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """CLI entry point for HappyFigure."""
    parser = _build_parser()
    args = parser.parse_args()
    _ensure_compat_attrs(args)

    config = _load_pipeline_config()
    agent_platform = _resolve_agent_platform(args, config)
    llm_preset = getattr(args, "llm_preset", None)

    # Set project root early so all UI output can shorten paths
    ui.set_project_root(str(PROJECT_ROOT))

    from agents import create_orchestrator
    orch = create_orchestrator(agent_platform, config)

    ctx = RunnerContext(
        orchestrator=orch,
        config=config,
        verbose=getattr(args, "verbose", False),
        llm_preset=llm_preset,
    )
    set_ctx(ctx)

    command = getattr(args, "command", None)

    # review subcommand: no pipeline setup needed
    if command == "review":
        from pipeline.feedback import run_interactive_review
        run_interactive_review(getattr(args, "run_dir", ""))
        return

    execution = getattr(args, "execution", "sequential")
    llm_roles = _resolve_llm_roles(config, llm_preset)
    orch_mode = (
        getattr(args, "orchestrator_mode", None)
        or config.get("orchestrator", {}).get("mode", "python-stages")
    )

    ui.banner(
        command, agent_platform, orch.model_display, llm_preset,
        execution=execution, llm_roles=llm_roles,
        orchestrator_mode=orch_mode,
    )

    _preflight_auth_checks(config, llm_preset)

    try:
        run_agent_pipeline(args)
    finally:
        orch.cleanup()


if __name__ == "__main__":
    main()
