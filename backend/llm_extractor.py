"""NLP extraction: raw patent text -> PatentGraph, via the two-stage LEAP pipeline.

Stage 1 (prompts/planner.leap.md): the whole disclosure -> a rule-compliant
figure plan + binding numeral ledger (37 CFR 1.83/1.84, PCT Rule 11).
Stage 2 (prompts/drafter.leap.md): one gated drafting call PER figure, run in
parallel, each seeing only its outline + the ledger + the spec.

The LLM acts ONLY as a parser/planner. Structured Outputs guarantees the
shape; determinism of the geometry comes from everything downstream being
pure Python. THE LEDGER IS LAW: numerals come from the planner, never invented
downstream.

Set MOCK_LLM=1 to bypass OpenAI entirely (local dev / QA without a key).
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Set

from models import (
    DraftFigure, Edge, Figure, FigurePlan, LedgerEntry, Node, PatentGraph,
    PlannedFigure,
)

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4").strip()
PROMPTS_DIR = Path(__file__).with_name("prompts")
MAX_FIGURES = 8
MAX_PARALLEL_DRAFTERS = 4

# drafter node type -> renderer shape
SHAPE_MAP = {
    "terminator": "rounded",
    "process": "rectangle",
    "decision": "diamond",
    "io": "parallelogram",
    "predefined": "predefined",
    "onpage-connector": "rounded",
    "offpage-connector": "rounded",
    "component": "rectangle",
    "container": "rectangle",
    "external": "rectangle",
    "datastore": "cylinder",
    "interface": "rectangle",
    "actor": "rectangle",
    "state": "rounded",
    "initial": "initial",
    "final": "final",
    "entity": "entity",
}

FLOW_LIKE = {"flowchart", "dataflow", "sequence", "state"}


class ExtractionError(Exception):
    """Raised when the text cannot be resolved into a valid PatentGraph."""


def extract_graph(text: str) -> PatentGraph:
    if os.environ.get("MOCK_LLM") == "1":
        return ensure_numerals(_mock_graph())

    plan = _run_planner(text)
    planned = plan.figures[:MAX_FIGURES]
    ledger_json = FigurePlan(
        figures=[], numerals=plan.numerals
    ).model_dump_json(include={"numerals"})

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_DRAFTERS) as pool:
        drafts = list(pool.map(
            lambda figure: _run_drafter(figure, ledger_json, text), planned,
        ))

    figures = [
        _convert_figure(draft) for draft in drafts if draft is not None
    ]
    figures = [f for f in figures if f.nodes]
    if not figures:
        raise ExtractionError("No drawable figures could be drafted.")
    for index, figure in enumerate(figures, start=1):
        figure.figure_number = index

    stated = {e.ref for e in plan.numerals if e.definedInSpec}
    stated |= set(re.findall(r"\((\d{1,4})\)", text))
    return _harmonize(PatentGraph(figures=figures), stated)


# -- plan mode: draw a caller-supplied plan (skip the planner) -----------------


def draw_from_plan(
    planned: List[PlannedFigure],
    ledger: List[LedgerEntry],
    spec: str = "",
) -> PatentGraph:
    """Plan mode entry point. The caller already ran the planner, so we SKIP it
    and run only drafter -> convert -> harmonize on the supplied plan.

    THE LEDGER IS LAW: every ledger numeral is bound as `stated` so _harmonize
    preserves the caller's numerals instead of reissuing a fresh 100,102,...
    sequence. Mirrors extract_graph()'s tail, minus _run_planner."""
    planned = planned[:MAX_FIGURES]
    ledger_json = FigurePlan(figures=[], numerals=ledger).model_dump_json(
        include={"numerals"}
    )

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_DRAFTERS) as pool:
        drafts = list(pool.map(
            lambda figure: _run_drafter(figure, ledger_json, spec), planned,
        ))

    figures: List[Figure] = []
    for planned_figure, draft in zip(planned, drafts):
        if draft is None:
            continue  # a drafter call failed — skip that figure, keep the rest
        figure = _convert_figure(draft)
        if not figure.nodes:
            continue
        # Trust the plan's figNumber, never the LLM's echo, so a dropped figure
        # doesn't renumber the rest.
        figure.figure_number = planned_figure.figNumber
        figures.append(figure)

    if not figures:
        raise ExtractionError("No drawable figures could be drafted from the plan.")

    figures.sort(key=lambda figure: figure.figure_number)
    stated = {row.ref for row in ledger}
    return _harmonize(PatentGraph(figures=figures), stated)


# -- stage 1: planner ----------------------------------------------------------


def _client():
    from openai import OpenAI
    # Strip whitespace/newlines from the key — a stray trailing newline makes an
    # "Illegal header value" that the SDK surfaces only as a bare "Connection error."
    return OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "").strip())


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _run_planner(text: str) -> FigurePlan:
    from openai import APIError

    prompt = _load_prompt("planner.leap.md").replace("[SPECIFICATION_TEXT]", text)
    try:
        response = _client().responses.parse(
            model=OPENAI_MODEL, input=prompt, text_format=FigurePlan,
        )
    except APIError as exc:
        raise ExtractionError(f"Planner failed: {exc}") from exc
    plan = response.output_parsed
    if plan is None or not plan.figures:
        raise ExtractionError("Planner returned no figures.")
    return plan


# -- stage 2: drafters (one per figure, parallel) ------------------------------


def _run_drafter(figure: PlannedFigure, ledger_json: str,
                 text: str) -> Optional[DraftFigure]:
    from openai import APIError

    prompt = (
        _load_prompt("drafter.leap.md")
        .replace("[FIGURE_PLAN_JSON]", figure.model_dump_json())
        .replace("[NUMERAL_LEDGER_JSON]", ledger_json)
        .replace("[SPECIFICATION_TEXT]", text)
    )
    try:
        response = _client().responses.parse(
            model=OPENAI_MODEL, input=prompt, text_format=DraftFigure,
        )
        return response.output_parsed
    except APIError as exc:
        print(f"DRAFTER FAILED (figure skipped): {exc}", flush=True)
        return None  # degrade gracefully: skip this figure, keep the rest


# -- conversion: DraftFigure -> renderable Figure ------------------------------


def _convert_figure(draft: DraftFigure) -> Figure:
    nodes: List[Node] = []
    ids = set()
    for raw in draft.nodes:
        if raw.id in ids:
            continue
        ids.add(raw.id)
        nodes.append(Node(
            id=raw.id,
            label=raw.label.strip(),
            reference_numeral=raw.ref.strip() or None,
            shape=SHAPE_MAP.get(raw.type, "rectangle"),
            fields=[f.strip() for f in raw.fields if f.strip()][:6],
        ))

    edges: List[Edge] = []
    for raw in draft.nodes:
        if raw.parent and raw.parent in ids and raw.id != raw.parent:
            edges.append(Edge(
                source_id=raw.parent, target_id=raw.id,
                relationship_type="contains",
            ))
    for raw in draft.edges:
        if raw.from_ not in ids or raw.to not in ids:
            continue
        label = (raw.label or "").strip().upper() or None
        if raw.kind == "structural":
            label = None
        if raw.kind == "message" and raw.order is not None and label:
            label = f"{raw.order}. {label}"
        edges.append(Edge(
            source_id=raw.from_, target_id=raw.to,
            label=label,
            relationship_type="flows_to" if draft.figType in FLOW_LIKE
            else "connects_to",
            style="dashed" if raw.kind == "signal" else "solid",
            # ERD convention: relationship lines carry cardinality, no heads.
            arrow="none" if (raw.kind == "structural"
                             or draft.figType == "record")
            else ("both" if raw.direction == "both" else "arrow"),
        ))

    return Figure(
        figure_number=draft.figNumber,
        title=draft.title,
        figure_type="flowchart" if draft.figType in FLOW_LIKE
        else "block_diagram",
        nodes=nodes,
        edges=_merge_parallel_edges(edges),
    )


def _merge_parallel_edges(edges: List[Edge]) -> List[Edge]:
    """Collapse duplicate and reciprocal connectors between the same pair.

    A->B plus B->A becomes ONE double-headed edge; exact duplicates drop.
    Prevents the 'ladder' of tightly parallel lines between two boxes."""
    merged: List[Edge] = []
    seen: dict = {}
    for edge in edges:
        if edge.relationship_type == "contains":
            merged.append(edge)
            continue
        key = (frozenset((edge.source_id, edge.target_id)),
               edge.label, edge.style)
        if key in seen:
            other = seen[key]
            if (other.source_id == edge.target_id
                    and other.arrow != "none" and edge.arrow != "none"):
                other.arrow = "both"
            continue  # duplicate (same or reciprocal) — draw once
        seen[key] = edge
        merged.append(edge)
    return merged


# -- harmonization: code-enforced drawing-set consistency ----------------------
# The prompts ask for these properties; this pass GUARANTEES them:
#   * same element (node id) -> same numeral, same shape, same label everywhere
#   * a numeral never labels two different elements
#   * every block (terminators and decisions included) carries a numeral;
#     only state-diagram initial/final markers stay bare
#   * non-stated numerals form one ascending even sequence (100, 102, ...)
#     assigned in reading order; numerals stated in the disclosure are binding


def _identity_key(label: str, node_id: str) -> str:
    """Cross-figure element identity. Drafters run independently and may
    slug the same element differently, so the NORMALIZED LABEL is the
    identity — patent practice already requires that the same name means
    the same element (and distinct elements carry distinct names)."""
    normalized = " ".join(label.split()).upper()
    return normalized if normalized else node_id


def _tokens_related(a: str, b: str) -> bool:
    """True when one label's words are a subset of the other's
    ("ORCHESTRATOR" vs "ORCHESTRATOR AGENT")."""
    ta, tb = set(a.split()), set(b.split())
    return bool(ta) and bool(tb) and (ta <= tb or tb <= ta)


def _harmonize(graph: PatentGraph, stated: Set[str]) -> PatentGraph:
    canon_shape: dict[str, str] = {}
    canon_fields: dict[str, List[str]] = {}
    canon_num: dict[str, str] = {}
    numeral_owner: dict[str, str] = {}
    alias: dict[str, str] = {}
    used: Set[str] = set()

    def resolve(key: str) -> str:
        while key in alias:
            key = alias[key]
        return key

    # Sweep A: bind stated numerals and detect label-variant aliases.
    # Two drafters naming one element "ORCHESTRATOR" and "ORCHESTRATOR
    # AGENT" while citing the SAME stated numeral are the same element.
    for figure in graph.figures:
        for node in figure.nodes:
            key = resolve(_identity_key(node.label, node.id))
            numeral = (node.reference_numeral or "").strip()
            if not numeral or numeral not in stated:
                continue
            if numeral in used:
                owner = numeral_owner[numeral]
                if owner != key and _tokens_related(owner, key):
                    alias[key] = owner
                continue
            if key not in canon_num:
                canon_num[key] = numeral
                numeral_owner[numeral] = key
                used.add(numeral)

    # Sweep B: canonical shape/fields per resolved identity.
    for figure in graph.figures:
        for node in figure.nodes:
            key = resolve(_identity_key(node.label, node.id))
            canon_shape.setdefault(key, node.shape)
            if node.fields and key not in canon_fields:
                canon_fields[key] = node.fields

    counter = 100

    def next_numeral() -> str:
        nonlocal counter
        while str(counter) in used or str(counter) in stated:
            counter += 2
        value = str(counter)
        used.add(value)
        counter += 2
        return value

    # Final sweep: enforce canon and number every block in reading order.
    for figure in graph.figures:
        for node in figure.nodes:
            key = resolve(_identity_key(node.label, node.id))
            node.shape = canon_shape[key]
            node.fields = canon_fields.get(key, node.fields)
            if node.shape in ("initial", "final"):
                node.reference_numeral = None
                continue
            if key not in canon_num:
                canon_num[key] = next_numeral()
            node.reference_numeral = canon_num[key]
    return graph


def ensure_numerals(graph: PatentGraph) -> PatentGraph:
    """Mock-path wrapper: numerals already present in the graph are binding."""
    present = {
        node.reference_numeral
        for figure in graph.figures for node in figure.nodes
        if node.reference_numeral
    }
    return _harmonize(graph, present)


def _mock_graph() -> PatentGraph:
    """Deterministic sample AST used by tests and MOCK_LLM dev mode."""
    return PatentGraph(
        figures=[
            Figure(
                figure_number=1,
                title="System Architecture Overview",
                figure_type="block_diagram",
                nodes=[
                    Node(id="computing_system", label="COMPUTING SYSTEM"),
                    Node(id="processor", label="PROCESSOR", reference_numeral="102"),
                    Node(id="memory", label="MEMORY MODULE"),
                    Node(id="extraction_engine", label="EXTRACTION ENGINE"),
                    Node(id="layout_engine", label="LAYOUT ENGINE"),
                    Node(id="datastore", label="GRAPH DATASTORE", shape="cylinder"),
                    Node(id="client_device", label="CLIENT DEVICE"),
                ],
                edges=[
                    Edge(source_id="computing_system", target_id="processor",
                         relationship_type="contains"),
                    Edge(source_id="computing_system", target_id="memory",
                         relationship_type="contains"),
                    Edge(source_id="computing_system", target_id="extraction_engine",
                         relationship_type="contains"),
                    Edge(source_id="computing_system", target_id="layout_engine",
                         relationship_type="contains"),
                    Edge(source_id="extraction_engine", target_id="datastore",
                         relationship_type="connects_to", label="stores"),
                    Edge(source_id="client_device", target_id="computing_system",
                         relationship_type="connects_to", label="network",
                         style="dashed"),
                ],
            ),
            Figure(
                figure_number=2,
                title="Method of Generating Vector Graphics",
                figure_type="flowchart",
                nodes=[
                    Node(id="start", label="START", shape="rounded"),
                    Node(id="receive_text", label="RECEIVE PATENT TEXT",
                         shape="parallelogram"),
                    Node(id="extract_ast", label="EXTRACT STRUCTURED AST"),
                    Node(id="valid", label="AST VALID?", shape="diamond"),
                    Node(id="layout", label="CALCULATE DETERMINISTIC LAYOUT"),
                    Node(id="render", label="RENDER PCT-COMPLIANT VECTORS"),
                    Node(id="reject", label="RETURN PARSE ERROR"),
                ],
                edges=[
                    Edge(source_id="start", target_id="receive_text",
                         relationship_type="flows_to"),
                    Edge(source_id="receive_text", target_id="extract_ast",
                         relationship_type="flows_to"),
                    Edge(source_id="extract_ast", target_id="valid",
                         relationship_type="flows_to"),
                    Edge(source_id="valid", target_id="layout",
                         relationship_type="flows_to", label="YES"),
                    Edge(source_id="valid", target_id="reject",
                         relationship_type="flows_to", label="NO"),
                    Edge(source_id="layout", target_id="render",
                         relationship_type="flows_to"),
                ],
            ),
            Figure(
                figure_number=3,
                title="Workflow Data Model",
                figure_type="block_diagram",
                nodes=[
                    Node(id="user_record", label="USER RECORD", shape="entity",
                         fields=["USER ID", "NAME", "ROLE"]),
                    Node(id="workflow_record", label="WORKFLOW RECORD",
                         shape="entity",
                         fields=["WORKFLOW ID", "OWNER ID", "STATUS"]),
                    Node(id="task_record", label="TASK RECORD", shape="entity",
                         fields=["TASK ID", "WORKFLOW ID", "WEIGHT"]),
                ],
                edges=[
                    Edge(source_id="user_record", target_id="workflow_record",
                         label="1:N", arrow="none"),
                    Edge(source_id="workflow_record", target_id="task_record",
                         label="1:N", arrow="none"),
                ],
            ),
        ]
    )
