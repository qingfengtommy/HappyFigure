"""Typed stage contracts and manifest records for the HappyFigure pipeline.

These dataclasses define the interfaces between pipeline stages (explore,
design, generate, assemble).  All artifact paths are **relative to run_dir**
— callers join with the concrete run_dir at runtime.

The run manifest (`run_manifest.json`) is a stage-oriented index that
augments (not replaces) `state.json`.  It tracks what completed, when, and
where the key outputs live.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Reserved artifact keys
# ---------------------------------------------------------------------------


class ArtifactKeys:
    """Centralised keys used in artifact dicts.

    Using constants prevents unstructured key sprawl across the codebase.
    """

    # Exploration stage
    REPORT = "report"
    SUMMARY_JSON = "summary_json"
    METHOD_DESC = "method_desc"
    IMAGE = "image"

    # Design stage
    GLOBAL_STYLE = "global_style"
    PLAN = "plan"
    PRIMARY_DESIGN = "primary_design"

    # Paper composite (unified mode)
    FIGURE_CLASSIFICATION = "figure_classification"
    ASSEMBLY_SPEC = "assembly_spec"
    PAPER_FIGURE_PLAN = "paper_figure_plan"
    DATA_DISTRIBUTION = "data_distribution"
    COLOR_REGISTRY = "color_registry"

    # Per-experiment helpers (keys contain a slash separator)
    @staticmethod
    def spec(exp: str) -> str:
        return f"spec/{exp}"

    @staticmethod
    def figure(exp: str) -> str:
        return f"figure/{exp}"

    @staticmethod
    def critic(exp: str) -> str:
        return f"critic/{exp}"

    @staticmethod
    def panel(figure_id: str, panel_id: str) -> str:
        return f"panel/{figure_id}/{panel_id}"


# ---------------------------------------------------------------------------
# Stage result contracts
# ---------------------------------------------------------------------------


@dataclass
class ExplorationResult:
    """Output of the explore stage."""

    run_dir: str
    mode: str  # "exp_plot" | "composite" | "agent_svg"
    artifacts: dict[str, str]  # ArtifactKeys → relative path
    experiments: list[str]


@dataclass
class DesignResult:
    """Output of the design stage."""

    mode: str
    artifacts: dict[str, str]  # ArtifactKeys → relative path
    experiments: list[str]
    variant_specs: dict[str, list[str]] | None = None
    # Paper composite extensions
    figure_classification: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Panel and figure types for paper composite mode
# ---------------------------------------------------------------------------


class PanelType(str, Enum):
    """Type classification for individual figure panels."""

    STATISTICAL = "statistical"
    DIAGRAM = "diagram"
    HYBRID = "hybrid"
    PLACEHOLDER = "placeholder"


@dataclass
class PanelEntry:
    """A single panel within a paper figure."""

    figure_id: str  # e.g. "Figure_1"
    panel_id: str  # e.g. "a"
    slug: str  # canonical: "figure_1__a"
    panel_type: PanelType
    generatable: bool
    description: str = ""
    data_source: str | None = None  # file path or sheet ref
    placeholder_strategy: str | None = None  # "labeled_gray", "source_image"
    source_image: str | None = None  # path to existing image
    services_needed: list[str] = field(default_factory=list)

    @staticmethod
    def make_slug(figure_id: str, panel_id: str) -> str:
        return f"{figure_id.lower().replace(' ', '_')}__{panel_id}"


@dataclass
class FigureEntry:
    """A complete paper figure containing multiple panels."""

    figure_id: str
    title: str
    panels: dict[str, PanelEntry] = field(default_factory=dict)  # panel_id → entry


@dataclass
class FigureClassification:
    """Classification of all figures for a paper composite run."""

    schema_version: int = 1
    source: str = "auto"  # "proposal", "auto", "user-tagged", "hybrid"
    figures: dict[str, FigureEntry] = field(default_factory=dict)  # figure_id → entry

    @property
    def needs_services(self) -> bool:
        """Whether any panel requires SAM3/OCR/BEN2 services."""
        return any(panel.services_needed for fig in self.figures.values() for panel in fig.panels.values())

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "needs_services": self.needs_services,
            "figures": {
                fig_id: {
                    "figure_id": fig.figure_id,
                    "title": fig.title,
                    "panels": {
                        pid: {
                            "figure_id": p.figure_id,
                            "panel_id": p.panel_id,
                            "slug": p.slug,
                            "panel_type": p.panel_type.value,
                            "generatable": p.generatable,
                            "description": p.description,
                            "data_source": p.data_source,
                            "placeholder_strategy": p.placeholder_strategy,
                            "source_image": p.source_image,
                            "services_needed": p.services_needed,
                        }
                        for pid, p in fig.panels.items()
                    },
                }
                for fig_id, fig in self.figures.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> FigureClassification:
        figures: dict[str, FigureEntry] = {}
        for fig_id, fig_data in d.get("figures", {}).items():
            panels: dict[str, PanelEntry] = {}
            for pid, p in fig_data.get("panels", {}).items():
                # Backward compat: "sketch" panels are now routed as diagrams
                raw_type = p["panel_type"]
                if raw_type == "sketch":
                    raw_type = "diagram"
                panels[pid] = PanelEntry(
                    figure_id=p["figure_id"],
                    panel_id=p["panel_id"],
                    slug=p.get("slug", PanelEntry.make_slug(p["figure_id"], p["panel_id"])),
                    panel_type=PanelType(raw_type),
                    generatable=p.get("generatable", True),
                    description=p.get("description", ""),
                    data_source=p.get("data_source"),
                    placeholder_strategy=p.get("placeholder_strategy"),
                    source_image=p.get("source_image"),
                    services_needed=p.get("services_needed", []),
                )
            figures[fig_id] = FigureEntry(
                figure_id=fig_data.get("figure_id", fig_id),
                title=fig_data.get("title", ""),
                panels=panels,
            )
        return cls(
            schema_version=d.get("schema_version", 1),
            source=d.get("source", "auto"),
            figures=figures,
        )


@dataclass
class AssemblyResult:
    """Result of assembling one paper figure from its panels."""

    figure_id: str
    total_panels: int
    generated_panels: int
    placeholder_panels: int
    source_image_panels: int = 0
    assembly_score: float | None = None
    iterations_used: int = 0
    output_path: str = ""
    deterministic_checks_passed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Manifest stage record
# ---------------------------------------------------------------------------


class StageStatus(Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StageRecord:
    """One stage entry in run_manifest.json."""

    status: StageStatus
    completed_at: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    experiments: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> StageRecord:
        d = dict(d)  # shallow copy
        d["status"] = StageStatus(d.get("status", "pending"))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
