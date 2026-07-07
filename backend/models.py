"""Pydantic data contracts for PatentGeyser.

PatentGraph is the AST the LLM must return (OpenAI Structured Outputs).
GenerateResponse is the wire format the frontend consumes.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Node(BaseModel):
    id: str
    label: str
    reference_numeral: Optional[str] = None
    shape: Literal[
        "rectangle", "diamond", "cylinder", "rounded", "parallelogram",
        "predefined", "initial", "final", "entity",
    ] = "rectangle"
    fields: List[str] = Field(default_factory=list)


class Edge(BaseModel):
    source_id: str
    target_id: str
    label: Optional[str] = None
    relationship_type: Literal["connects_to", "contains", "flows_to"] = "connects_to"
    style: Literal["solid", "dashed"] = "solid"
    arrow: Literal["arrow", "none", "both"] = "arrow"


class Figure(BaseModel):
    figure_number: int
    title: str
    figure_type: Literal["flowchart", "block_diagram", "schematic"]
    nodes: List[Node]
    edges: List[Edge]


class PatentGraph(BaseModel):
    figures: List[Figure]


# ── Two-stage LEAP pipeline contracts (planner.leap.md / drafter.leap.md) ──

FigType = Literal[
    "system", "module", "flowchart", "dataflow", "sequence", "state",
    "hardware", "record",
]


class PlannedFigure(BaseModel):
    figNumber: int
    figType: FigType
    title: str
    briefDescription: str = ""
    illustrates: List[str] = Field(default_factory=list)
    outline: str
    numerals: List[str] = Field(default_factory=list)


class LedgerEntry(BaseModel):
    ref: str
    feature: str
    figures: List[int] = Field(default_factory=list)
    definedInSpec: bool = False


class CoverageEntry(BaseModel):
    element: str
    source: str = ""
    figures: List[int] = Field(default_factory=list)
    refs: List[str] = Field(default_factory=list)


class FigurePlan(BaseModel):
    figures: List[PlannedFigure]
    numerals: List[LedgerEntry] = Field(default_factory=list)
    coverage: List[CoverageEntry] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)


class DraftNode(BaseModel):
    id: str
    type: str
    label: str = ""
    ref: str = ""
    fields: List[str] = Field(default_factory=list)
    parent: Optional[str] = None
    numeralTarget: Literal["feature", "assembly"] = "feature"
    detail: Optional[str] = None


class DraftEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    kind: Literal[
        "control", "data", "structural", "signal", "branch", "message", "transition"
    ] = "control"
    direction: Literal["one-way", "both"] = "one-way"
    label: Optional[str] = None
    order: Optional[int] = None


class DraftFigure(BaseModel):
    figNumber: int
    figType: FigType
    title: str
    nodes: List[DraftNode]
    edges: List[DraftEdge]


class GenerateRequest(BaseModel):
    text: str = Field(..., description="Raw patent claims / disclosure text")


class GeneratedFigure(BaseModel):
    id: str
    title: str
    svgData: str
    pdfBase64: str


class GenerateResponse(BaseModel):
    figures: List[GeneratedFigure]


# ── Plan mode (POST /api/v1/draw) ──────────────────────────────────────────
# The consuming app runs its OWN planner and sends a finished plan; we skip our
# planner and run drafter -> layout -> render only. Pydantic ignores any extra
# fields the caller carries for its own document (briefDescription, etc.).


class PlanFigure(BaseModel):
    """One ready FIGURE_ASSIGNMENT from the caller's planner."""
    figNumber: int
    figType: FigType
    title: str
    outline: str
    numerals: List[str] = Field(default_factory=list)


class DrawPlan(BaseModel):
    figures: List[PlanFigure]
    numerals: List[LedgerEntry] = Field(default_factory=list)


class DrawRequest(BaseModel):
    plan: DrawPlan
    spec: Optional[str] = None  # optional fallback context; outline is authoritative


class DrawnFigure(BaseModel):
    figNumber: int              # echoes plan.figures[].figNumber, for caller matching
    id: str
    title: str
    svgData: str
    pdfBase64: str
    numerals: List[str] = Field(default_factory=list)  # numerals ACTUALLY drawn


class DrawResponse(BaseModel):
    figures: List[DrawnFigure]
