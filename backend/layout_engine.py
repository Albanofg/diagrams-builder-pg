"""Deterministic layout: PatentGraph figure -> exact (x, y) coordinates in mm.

No force-directed layouts, no random seeds. The same AST always yields the
exact same coordinates:
- Flowcharts: topological depth -> vertical levels, rows centered.
- Block diagrams / schematics: recursive bounding-box packing on a grid
  (left-to-right, top-to-bottom), containers wrap their children.

Coordinate system: millimeters, origin top-left of the content area, y grows
downward. The renderer scales/centers content into the PCT usable area
(170 x 262 mm on A4).
"""

import math
import textwrap
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from models import Figure

# -- geometry constants (mm) -------------------------------------------------
NODE_HEIGHT = 16.0
LINE_HEIGHT = 5.0          # extra height per wrapped label line
MIN_WIDTH = 34.0
MAX_WIDTH = 64.0
CHAR_WIDTH = 2.05          # approx width of one character at label font size
H_GAP = 18.0               # horizontal gap between siblings (at scale 1.0)
V_GAP = 20.0               # vertical gap between flowchart levels

# Congestion-aware spacing: figures with more connectors than nodes get
# proportionally wider gaps so lines AND text have room to stay clean.
_GAP_SCALE = 1.0


def _hg() -> float:
    return H_GAP * _GAP_SCALE


def _vg() -> float:
    return V_GAP * _GAP_SCALE
PAD = 10.0                 # container inner padding
TITLE_STRIP = 8.0          # space reserved for a container's own label


@dataclass
class LaidNode:
    id: str
    label: str
    lines: List[str]
    numeral: str
    shape: str
    x: float = 0.0          # top-left
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    is_container: bool = False
    fields: List[str] = field(default_factory=list)

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0


@dataclass
class LaidEdge:
    source_id: str
    target_id: str
    label: Optional[str]
    style: str = "solid"
    arrow: str = "arrow"


@dataclass
class LaidFigure:
    number: int
    title: str
    figure_type: str
    nodes: Dict[str, LaidNode] = field(default_factory=dict)
    edges: List[LaidEdge] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0


def layout_figure(figure: Figure, gap_boost: float = 1.0) -> LaidFigure:
    """gap_boost > 1 re-lays the figure with wider spacing — used by the
    renderer's self-healing loop when the layout audit finds violations."""
    global _GAP_SCALE
    edge_count = len([e for e in figure.edges
                      if e.relationship_type != "contains"])
    base = min(2.2, max(1.0, 1.0 + 0.12 * (edge_count - len(figure.nodes))))
    _GAP_SCALE = min(3.2, base * gap_boost)
    laid = LaidFigure(
        number=figure.figure_number,
        title=figure.title,
        figure_type=figure.figure_type,
    )
    for node in figure.nodes:
        laid.nodes[node.id] = _size_node(node)
    laid.edges = [
        LaidEdge(e.source_id, e.target_id, e.label, e.style, e.arrow)
        for e in figure.edges
        if e.relationship_type != "contains"
    ]

    if figure.figure_type == "flowchart":
        _layout_flowchart(figure, laid)
    else:
        _layout_block(figure, laid)

    _normalize(laid)
    return laid


# -- node sizing --------------------------------------------------------------

ENTITY_TITLE_H = 8.0
ENTITY_ROW_H = 5.5


def _size_node(node) -> LaidNode:
    if node.shape in ("initial", "final"):
        return LaidNode(id=node.id, label="", lines=[], numeral="",
                        shape=node.shape, w=7.0, h=7.0)
    if node.shape == "entity":
        label = " ".join(node.label.split())
        fields = [" ".join(f.split()) for f in node.fields][:6]
        longest = max([len(label)] + [len(f) for f in fields] + [8])
        # No upper cap: the longest text DEFINES the width — text can
        # never be wider than its field (the canvas adapts to content).
        width = max(38.0, longest * CHAR_WIDTH + 14.0)
        height = ENTITY_TITLE_H + max(1, len(fields)) * ENTITY_ROW_H + 2.5
        return LaidNode(id=node.id, label=label, lines=[label],
                        numeral=node.reference_numeral or "",
                        shape="entity", w=width, h=height, fields=fields)
    label = " ".join(node.label.split())
    natural = len(label) * CHAR_WIDTH + 10.0
    width = max(MIN_WIDTH, min(MAX_WIDTH, natural))
    chars_per_line = max(8, int((width - 8.0) / CHAR_WIDTH))
    lines = textwrap.wrap(label, chars_per_line) or [label]
    height = NODE_HEIGHT + (len(lines) - 1) * LINE_HEIGHT
    if node.shape == "diamond":
        width += 14.0     # diamonds need slack: text sits in the inscribed box
        height += 6.0
    elif node.shape == "cylinder":
        height += 5.0     # room for the top ellipse
    elif node.shape == "parallelogram":
        width += 8.0      # skewed sides eat into the text area
    return LaidNode(
        id=node.id,
        label=label,
        lines=lines,
        numeral=node.reference_numeral or "",
        shape=node.shape,
        w=width,
        h=height,
    )


# -- flowchart: hierarchical levels -------------------------------------------

def _layout_flowchart(figure: Figure, laid: LaidFigure) -> None:
    order = {node.id: index for index, node in enumerate(figure.nodes)}
    flow_edges = [e for e in figure.edges if e.relationship_type != "contains"]

    indegree = {nid: 0 for nid in laid.nodes}
    adjacency: Dict[str, List[str]] = {nid: [] for nid in laid.nodes}
    for edge in flow_edges:
        adjacency[edge.source_id].append(edge.target_id)
        indegree[edge.target_id] += 1

    # Kahn's algorithm, longest-path depth; ties broken by appearance order.
    # Cycles (loop-backs like a "NO" branch returning upstream) are broken by
    # force-processing the earliest-appearing blocked node, so the dominant
    # top-to-bottom spine survives and only the back-edge routes around it.
    depth = {nid: 0 for nid in laid.nodes}
    queue = sorted([n for n, d in indegree.items() if d == 0], key=lambda n: order[n])
    remaining = dict(indegree)
    visited: set = set()
    while len(visited) < len(laid.nodes):
        if not queue:
            pending = sorted(set(laid.nodes) - visited, key=lambda n: order[n])
            queue.append(pending[0])
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        for target in sorted(adjacency[current], key=lambda n: order[n]):
            if target in visited:
                continue  # back-edge: keeps its earlier depth
            depth[target] = max(depth[target], depth[current] + 1)
            remaining[target] -= 1
            if remaining[target] == 0:
                queue.append(target)
        queue.sort(key=lambda n: order[n])

    levels: Dict[int, List[str]] = {}
    for nid, level in depth.items():
        levels.setdefault(level, []).append(nid)
    for level in levels.values():
        level.sort(key=lambda n: order[n])

    row_widths = {
        lvl: sum(laid.nodes[n].w for n in nodes) + _hg() * (len(nodes) - 1)
        for lvl, nodes in levels.items()
    }
    total_width = max(row_widths.values())

    y = 0.0
    for level in sorted(levels):
        nodes = levels[level]
        row_height = max(laid.nodes[n].h for n in nodes)
        x = (total_width - row_widths[level]) / 2.0
        for nid in nodes:
            node = laid.nodes[nid]
            node.x = x
            node.y = y + (row_height - node.h) / 2.0
            x += node.w + _hg()
        y += row_height + _vg()


# -- block diagram / schematic: recursive grid packing ------------------------

def _layout_block(figure: Figure, laid: LaidFigure) -> None:
    order = {node.id: index for index, node in enumerate(figure.nodes)}
    children: Dict[str, List[str]] = {nid: [] for nid in laid.nodes}
    parent: Dict[str, str] = {}
    for edge in figure.edges:
        if edge.relationship_type != "contains":
            continue
        # first stated parent wins; ignore self/secondary containment
        if edge.target_id not in parent and edge.source_id != edge.target_id:
            parent[edge.target_id] = edge.source_id
            children[edge.source_id].append(edge.target_id)
    for sibling_list in children.values():
        sibling_list.sort(key=lambda n: order[n])

    roots = sorted([n for n in laid.nodes if n not in parent], key=lambda n: order[n])

    def measure(nid: str) -> None:
        node = laid.nodes[nid]
        kids = children[nid]
        if not kids:
            return
        for kid in kids:
            measure(kid)
        grid_w, grid_h, _ = _grid_metrics([laid.nodes[k] for k in kids])
        node.is_container = True
        node.w = max(node.w, grid_w + 2 * PAD)
        node.h = TITLE_STRIP + grid_h + 2 * PAD

    def place(nid: str) -> None:
        node = laid.nodes[nid]
        kids = children[nid]
        if not kids:
            return
        _grid_place(
            [laid.nodes[k] for k in kids],
            origin_x=node.x + PAD,
            origin_y=node.y + TITLE_STRIP + PAD,
            inner_w=node.w - 2 * PAD,
        )
        for kid in kids:
            place(kid)

    for root in roots:
        measure(root)
    _grid_place([laid.nodes[r] for r in roots], origin_x=0.0, origin_y=0.0,
                inner_w=None)
    for root in roots:
        place(root)


def _grid_metrics(nodes: List[LaidNode]):
    columns = max(1, math.ceil(math.sqrt(len(nodes))))
    rows = [nodes[i:i + columns] for i in range(0, len(nodes), columns)]
    width = max(
        sum(n.w for n in row) + _hg() * (len(row) - 1) for row in rows
    )
    height = sum(max(n.h for n in row) for row in rows) + _vg() * (len(rows) - 1)
    return width, height, rows


def _grid_place(nodes: List[LaidNode], origin_x: float, origin_y: float,
                inner_w: Optional[float]) -> None:
    width, _, rows = _grid_metrics(nodes)
    span = inner_w if inner_w is not None else width
    y = origin_y
    for row in rows:
        row_width = sum(n.w for n in row) + _hg() * (len(row) - 1)
        row_height = max(n.h for n in row)
        x = origin_x + (span - row_width) / 2.0
        for node in row:
            node.x = x
            node.y = y + (row_height - node.h) / 2.0
            x += node.w + _hg()
        y += row_height + _vg()


# -- normalization ------------------------------------------------------------

def _normalize(laid: LaidFigure) -> None:
    """Shift content to the origin. Geometry is NEVER scaled: font sizes are
    fixed, so any scaling would break the text-fits-its-box guarantee. The
    renderer sizes the canvas around the content; fitting onto A4 patent
    paper is a later export concern."""
    nodes = list(laid.nodes.values())
    min_x = min(n.x for n in nodes)
    min_y = min(n.y for n in nodes)
    for node in nodes:
        node.x -= min_x
        node.y -= min_y
    laid.width = max(n.x + n.w for n in nodes)
    laid.height = max(n.y + n.h for n in nodes)
