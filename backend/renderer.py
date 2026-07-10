"""PCT-compliant Matplotlib rendering: LaidFigure -> SVG string + PDF base64.

PLAN-THEN-DRAW architecture: every line is routed and every piece of text
(numerals, edge labels) is positioned BEFORE the canvas exists, so the canvas
is sized around the final artwork — nothing can clip at an edge — and text
placement is scored against ALL geometry, so words don't touch lines or
other words. A final audit pass machine-verifies those guarantees.

Style guarantees (uniform across every figure):
  * one stroke weight (1.5 pt) for boxes, connectors, lead lines, dividers
  * one label weight (regular); numerals italic; only FIG. captions bold
  * dashed lines appear ONLY for "signal" (wireless/optional) couplings
"""

import base64
import io
from typing import Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import (
    Ellipse, FancyArrowPatch, FancyBboxPatch, Polygon, Rectangle,
)
from matplotlib.path import Path

from layout_engine import (
    ENTITY_ROW_H, ENTITY_TITLE_H, TITLE_STRIP, LaidFigure, LaidNode,
)

LINE_W = 1.5                  # ~0.5mm — THE stroke weight, everywhere

plt.rcParams.update({
    "lines.linewidth": LINE_W,
    "lines.color": "black",
    "patch.edgecolor": "black",
    "patch.facecolor": "none",
    "patch.linewidth": LINE_W,
    "text.color": "black",
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.spines.left": False,
    "axes.spines.bottom": False,
    "xtick.bottom": False,
    "ytick.left": False,
    "svg.fonttype": "none",   # keep text as real <text>: crisp at any zoom
})

CAPTION_BAND = 26.0           # sheet number + "FIG. n" above the artwork
MARGIN_X = 14.0
MARGIN_BOTTOM = 12.0
MIN_SHEET_W = 120.0

LABEL_FONT = 9.0              # pt
NUMERAL_FONT = 10.0
EDGE_FONT = 7.5
FIG_FONT = 14.0
LINE_STEP = 5.0               # mm between wrapped label lines

NUMERAL_ZONE_W = 14.0
NUMERAL_ZONE_H = 10.0

# Most recent audit findings (one list per rendered figure); the QA suite
# asserts this is empty and the server logs any runtime violation.
LAST_AUDIT: List[str] = []


# ── geometry helpers ──────────────────────────────────────────────────────


def _segment_hits_rect(p1, p2, left, top, right, bottom) -> bool:
    """Axis-aligned segment vs. rectangle interior (strict, grazing passes)."""
    x1, y1 = p1
    x2, y2 = p2
    if abs(y1 - y2) < 1e-9:  # horizontal
        return (top < y1 < bottom
                and min(x1, x2) < right - 0.1 and max(x1, x2) > left + 0.1)
    if abs(x1 - x2) < 1e-9:  # vertical
        return (left < x1 < right
                and min(y1, y2) < bottom - 0.1 and max(y1, y2) > top + 0.1)
    # Conservative fallback for slanted lead lines: bounding-box overlap.
    return _rects_overlap(
        (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
        (left, top, right, bottom),
    )


def _rects_overlap(a, b) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


# ── orthogonal A* grid router ─────────────────────────────────────────────
# Node boxes (inflated) are hard walls: a routed line can NEVER pass through
# a node when any orthogonal path around it exists. Turn penalties keep
# routes patent-clean (few bends); cells already used by earlier edges cost
# extra so parallel edges separate; numeral callout zones are expensive.

GRID = 4.0
INFLATE = 2.0
TURN_COST = 3
USED_COST = 3        # arrowed edges keep their distance from each other...
TRUNK_COST = 1       # ...structural (headless) lines SHARE lanes: bus + taps
CROSS_COST = 9       # crossing a perpendicular line: detour when possible
SOFT_NUMERAL = 20
MAX_POPS = 40000

_DIRS = {"right": (1, 0), "left": (-1, 0), "down": (0, 1), "up": (0, -1)}


class _GridRouter:
    def __init__(self, laid: LaidFigure, ox: float, oy: float):
        self.ox, self.oy = ox, oy
        margin = 16.0
        self.x0 = ox - margin
        self.y0 = oy - margin
        self.cols = int((laid.width + 2 * margin) / GRID) + 2
        self.rows = int((laid.height + 2 * margin) / GRID) + 2
        self.blocked: set = set()
        self.soft: dict = {}
        self.used: dict = {}
        self.used_axis: dict = {}   # cell -> {'h': count, 'v': count}
        self.trunk_used: dict = {}  # cells carrying structural bus lanes
        self.committed: dict = {}   # edge key -> (cells, axis pairs, cost)
        for node in laid.nodes.values():
            left, top = node.x + ox, node.y + oy
            height = TITLE_STRIP if node.is_container else node.h
            blocked_cells = self._cells(left - INFLATE, top - INFLATE,
                                        left + node.w + INFLATE,
                                        top + height + INFLATE)
            self.blocked |= blocked_cells
            # Soft band along every border so routes keep a respectful
            # distance instead of hugging box outlines (doubled-line look).
            full_h = node.h
            band = self._cells(left - INFLATE - GRID, top - INFLATE - GRID,
                               left + node.w + INFLATE + GRID,
                               top + full_h + INFLATE + GRID)
            if node.is_container:
                band -= self._cells(left + GRID, top + GRID,
                                    left + node.w - GRID,
                                    top + full_h - GRID)
            else:
                band -= blocked_cells
            for cell in band:
                self.soft[cell] = self.soft.get(cell, 0) + 2
            if node.numeral:
                for cell in self._cells(left + node.w - 2.0,
                                        top - NUMERAL_ZONE_H,
                                        left + node.w + NUMERAL_ZONE_W,
                                        top + 1.0):
                    self.soft[cell] = self.soft.get(cell, 0) + SOFT_NUMERAL

    def _cells(self, left, top, right, bottom) -> set:
        c0 = max(0, int((left - self.x0) // GRID))
        c1 = min(self.cols - 1, int((right - self.x0) // GRID))
        r0 = max(0, int((top - self.y0) // GRID))
        r1 = min(self.rows - 1, int((bottom - self.y0) // GRID))
        return {(c, r) for c in range(c0, c1 + 1) for r in range(r0, r1 + 1)}

    def _center(self, cell):
        return (self.x0 + cell[0] * GRID + GRID / 2.0,
                self.y0 + cell[1] * GRID + GRID / 2.0)

    def _snap(self, value, origin) -> float:
        index = round((value - origin - GRID / 2.0) / GRID)
        return origin + index * GRID + GRID / 2.0

    def _anchor(self, node: LaidNode, side: str):
        left, top = node.x + self.ox, node.y + self.oy
        right, bottom = left + node.w, top + node.h
        if side in ("left", "right"):
            y = min(max(self._snap(node.cy + self.oy, self.y0), top + 2.0),
                    bottom - 2.0)
            return (right if side == "right" else left, y)
        x = min(max(self._snap(node.cx + self.ox, self.x0), left + 2.0),
                right - 2.0)
        return (x, bottom if side == "down" else top)

    def _outside_cell(self, point, side):
        direction = _DIRS[side]
        reach = INFLATE + GRID / 2.0 + 0.6
        return (int((point[0] + direction[0] * reach - self.x0) // GRID),
                int((point[1] + direction[1] * reach - self.y0) // GRID))

    def route_edge(self, source: LaidNode, target: LaidNode,
                   trunk: bool = False, edge_key=None):
        sx, sy = source.cx + self.ox, source.cy + self.oy
        tx, ty = target.cx + self.ox, target.cy + self.oy
        dx, dy = tx - sx, ty - sy
        h_exit, h_entry = ("right", "left") if dx >= 0 else ("left", "right")
        v_exit, v_entry = ("down", "up") if dy >= 0 else ("up", "down")
        if abs(dx) >= abs(dy):
            exits, entries = (h_exit, v_exit), (h_entry, v_entry)
        else:
            exits, entries = (v_exit, h_exit), (v_entry, h_entry)

        best = self._try_anchor_pairs(source, target, exits, entries, trunk)
        if best is None:
            # widen the search to every side combination before giving up
            all_sides = ("up", "down", "left", "right")
            best = self._try_anchor_pairs(source, target, all_sides,
                                          all_sides, trunk)
        if best is None:
            return None
        _, cells, points = best
        self._commit(edge_key, cells, trunk)
        return points

    def _commit(self, edge_key, cells, trunk: bool) -> None:
        commit = TRUNK_COST if trunk else USED_COST
        axis_pairs = []
        prev = None
        for cell in cells:
            self.used[cell] = self.used.get(cell, 0) + commit
            if trunk:
                self.trunk_used[cell] = self.trunk_used.get(cell, 0) + 1
            if prev is not None:
                axis = "h" if cell[1] == prev[1] else "v"
                for c in (prev, cell):
                    counts = self.used_axis.setdefault(c, {"h": 0, "v": 0})
                    counts[axis] += 1
                    axis_pairs.append((c, axis))
            prev = cell
        if edge_key is not None:
            self.committed[edge_key] = (list(cells), axis_pairs, commit,
                                        trunk)

    def uncommit(self, edge_key) -> None:
        """Rip up a routed edge so it can be re-routed against full traffic."""
        entry = self.committed.pop(edge_key, None)
        if entry is None:
            return
        cells, axis_pairs, commit, trunk = entry
        for cell in cells:
            remaining = self.used.get(cell, 0) - commit
            if remaining > 0:
                self.used[cell] = remaining
            else:
                self.used.pop(cell, None)
            if trunk:
                count = self.trunk_used.get(cell, 0) - 1
                if count > 0:
                    self.trunk_used[cell] = count
                else:
                    self.trunk_used.pop(cell, None)
        for cell, axis in axis_pairs:
            counts = self.used_axis.get(cell)
            if counts:
                counts[axis] = max(0, counts[axis] - 1)

    def _try_anchor_pairs(self, source, target, exits, entries, trunk):
        best = None
        for exit_side in exits:
            for entry_side in entries:
                result = self._astar(source, exit_side, target, entry_side,
                                     trunk)
                if result and (best is None or result[0] < best[0]):
                    best = result
        return best

    def _astar(self, source, exit_side, target, entry_side, trunk=False):
        import heapq

        start_pt = self._anchor(source, exit_side)
        goal_pt = self._anchor(target, entry_side)
        start = self._outside_cell(start_pt, exit_side)
        goal = self._outside_cell(goal_pt, entry_side)
        allow = {start, goal}

        if start == goal:
            if (abs(start_pt[0] - goal_pt[0]) < 1e-6
                    or abs(start_pt[1] - goal_pt[1]) < 1e-6):
                return (1, [start], [start_pt, goal_pt])
            corner = ((start_pt[0], goal_pt[1])
                      if exit_side in ("up", "down")
                      else (goal_pt[0], start_pt[1]))
            return (2, [start], [start_pt, corner, goal_pt])

        start_dir = _DIRS[exit_side]
        heap = [(0, 0, 0, start, start_dir)]
        best_cost = {(start, start_dir): 0}
        parent = {}
        counter = 0
        pops = 0
        found = None
        while heap:
            pops += 1
            if pops > MAX_POPS:
                return None
            f, g, _, cell, direction = heapq.heappop(heap)
            if best_cost.get((cell, direction), float("inf")) < g:
                continue
            if cell == goal:
                found = (g, cell, direction)
                break
            for new_dir in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (cell[0] + new_dir[0], cell[1] + new_dir[1])
                if not (0 <= nxt[0] < self.cols and 0 <= nxt[1] < self.rows):
                    continue
                if nxt in self.blocked and nxt not in allow:
                    continue
                # Trunk (structural/headless) lines are ATTRACTED to lanes
                # other trunks already use: fresh cells cost double, shared
                # bus cells cost a single step — many couplings to one hub
                # merge into ONE visible spine with taps instead of a bundle
                # of parallel tracks. Crossing a perpendicular line stays
                # expensive for everyone: detour when possible.
                if trunk:
                    traffic = 0
                    step = 1 if nxt in self.trunk_used else 2
                else:
                    traffic = self.used.get(nxt, 0)
                    step = 1
                counts = self.used_axis.get(nxt)
                if counts:
                    my_axis = "h" if new_dir[1] == 0 else "v"
                    other = "v" if my_axis == "h" else "h"
                    if counts.get(other, 0) > 0:
                        traffic += CROSS_COST
                cost = (g + step + (TURN_COST if new_dir != direction else 0)
                        + self.soft.get(nxt, 0) + traffic)
                key = (nxt, new_dir)
                if cost < best_cost.get(key, float("inf")):
                    best_cost[key] = cost
                    parent[key] = (cell, direction)
                    counter += 1
                    h = abs(goal[0] - nxt[0]) + abs(goal[1] - nxt[1])
                    heapq.heappush(heap, (cost + h, cost, counter, nxt, new_dir))
        if found is None:
            return None

        g, cell, direction = found
        cells = [cell]
        key = (cell, direction)
        while key in parent:
            key = parent[key]
            cells.append(key[0])
        cells.reverse()

        points = [start_pt] + [self._center(c) for c in cells] + [goal_pt]
        return (g, cells, _simplify(points))


def _simplify(points: list) -> list:
    out = [points[0]]
    for point in points[1:]:
        if (abs(point[0] - out[-1][0]) < 1e-6
                and abs(point[1] - out[-1][1]) < 1e-6):
            continue
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            collinear_x = (abs(a[0] - b[0]) < 1e-6
                           and abs(b[0] - point[0]) < 1e-6)
            collinear_y = (abs(a[1] - b[1]) < 1e-6
                           and abs(b[1] - point[1]) < 1e-6)
            if collinear_x or collinear_y:
                out[-1] = point
                continue
        out.append(point)
    return out


# ── heuristic fallback routes (used only if A* finds no path) ─────────────


def _route_candidates(source: LaidNode, target: LaidNode, ox: float, oy: float,
                      stagger: float = 0.0):
    sx, sy = source.cx + ox, source.cy + oy
    tx, ty = target.cx + ox, target.cy + oy
    dx, dy = tx - sx, ty - sy

    side_out = (source.x + ox + source.w, sy) if dx >= 0 else (source.x + ox, sy)
    vert_out = (sx, source.y + oy + source.h) if dy >= 0 else (sx, source.y + oy)
    vert_in = (tx, target.y + oy) if dy >= 0 else (tx, target.y + oy + target.h)
    side_in = (target.x + ox, ty) if dx >= 0 else (target.x + ox + target.w, ty)

    near_v = abs(dx) < (source.w + target.w) / 4.0
    near_h = abs(dy) < (source.h + target.h) / 4.0
    if near_v and not near_h:
        if abs(dx) < 0.75:
            candidates = [[vert_out, vert_in]]
        else:
            mid_y = (vert_out[1] + vert_in[1]) / 2.0
            candidates = [[vert_out, (sx, mid_y), (tx, mid_y), vert_in]]
        base = (sx + tx) / 2.0
        for extra in (0.0, 16.0):
            offset = max(source.w, target.w) / 2.0 + 10.0 + stagger + extra
            for sign in (-1.0, 1.0):
                rail = base + sign * offset
                exit_pt = ((source.x + ox, sy) if sign < 0
                           else (source.x + ox + source.w, sy))
                entry_pt = ((target.x + ox, ty) if sign < 0
                            else (target.x + ox + target.w, ty))
                candidates.append([exit_pt, (rail, sy), (rail, ty), entry_pt])
        return candidates
    if near_h and not near_v:
        if abs(dy) < 0.75:
            candidates = [[side_out, side_in]]
        else:
            mid_x = (side_out[0] + side_in[0]) / 2.0
            candidates = [[side_out, (mid_x, sy), (mid_x, ty), side_in]]
        base = (sy + ty) / 2.0
        for extra in (0.0, 16.0):
            offset = max(source.h, target.h) / 2.0 + 10.0 + stagger + extra
            for sign in (-1.0, 1.0):
                rail = base + sign * offset
                exit_pt = ((sx, source.y + oy) if sign < 0
                           else (sx, source.y + oy + source.h))
                entry_pt = ((tx, target.y + oy) if sign < 0
                            else (tx, target.y + oy + target.h))
                candidates.append([exit_pt, (sx, rail), (tx, rail), entry_pt])
        return candidates

    candidates = []
    if abs(dx) > source.w / 2 + 0.5 and abs(dy) > target.h / 2 + 0.5:
        candidates.append([side_out, (tx, sy), vert_in])
    if abs(dy) > source.h / 2 + 0.5 and abs(dx) > target.w / 2 + 0.5:
        candidates.append([vert_out, (sx, ty), side_in])
    if abs(dy) > (source.h + target.h) / 2 + 1.0:
        mid_y = (vert_out[1] + vert_in[1]) / 2.0
        candidates.append([vert_out, (sx, mid_y), (tx, mid_y), vert_in])
    if abs(dx) > (source.w + target.w) / 2 + 1.0:
        mid_x = (side_out[0] + side_in[0]) / 2.0
        candidates.append([side_out, (mid_x, sy), (mid_x, ty), side_in])
    if not candidates:
        if abs(dy) >= abs(dx):
            mid_y = (vert_out[1] + vert_in[1]) / 2.0
            candidates.append([vert_out, (sx, mid_y), (tx, mid_y), vert_in])
        else:
            mid_x = (side_out[0] + side_in[0]) / 2.0
            candidates.append([side_out, (mid_x, sy), (mid_x, ty), side_in])
    return candidates


BOX_WEIGHT = 4
NUMERAL_WEIGHT = 2


def _obstacles(laid: LaidFigure, edge, ox: float, oy: float):
    rects = []
    for node in laid.nodes.values():
        left, top = node.x + ox, node.y + oy
        if node.id not in (edge.source_id, edge.target_id):
            if node.is_container:
                rects.append((left, top, left + node.w, top + TITLE_STRIP,
                              BOX_WEIGHT))
            else:
                rects.append((left, top, left + node.w, top + node.h,
                              BOX_WEIGHT))
        if node.numeral:
            rects.append((left + node.w - 2.0, top - NUMERAL_ZONE_H,
                          left + node.w + NUMERAL_ZONE_W, top + 1.0,
                          NUMERAL_WEIGHT))
    return rects


def _crossings(points, laid: LaidFigure, edge, ox: float, oy: float) -> int:
    score = 0
    for left, top, right, bottom, weight in _obstacles(laid, edge, ox, oy):
        for p1, p2 in zip(points, points[1:]):
            if _segment_hits_rect(p1, p2, left, top, right, bottom):
                score += weight
    return score


def _select_route(laid: LaidFigure, edge, index: int,
                  router: Optional[_GridRouter] = None) -> list:
    source = laid.nodes[edge.source_id]
    target = laid.nodes[edge.target_id]
    if router is not None:
        points = router.route_edge(source, target,
                                   trunk=(edge.arrow == "none"),
                                   edge_key=index)
        if points is not None:
            return points
    candidates = _route_candidates(source, target, 0.0, 0.0,
                                   stagger=(index % 3) * 3.0)
    return min(candidates, key=lambda c: _crossings(c, laid, edge, 0.0, 0.0))


# ── line jumps (hop-overs at unavoidable crossings) ───────────────────────
# The A* router MINIMIZES crossings, but some remain unavoidable. Where two
# connectors cross WITHOUT connecting, draw a small semicircular hop on the
# horizontal line so it visually passes OVER the vertical one — the standard
# "these paths cross, they do not join" convention (Visio "arc jump",
# draw.io "line jump"). A crossing earns a hop ONLY when the meeting point is
# STRICTLY INTERIOR to both segments; shared-node attaches, corners and trunk
# T-taps all land on a segment endpoint, so they fail the strict test and get
# no hop — correct, those are real connections.

JUMP_R = 1.4          # hop-over radius (~1/3 of GRID); the bulge over the line
JUMP_EPS = 0.5        # strict-interior margin; below it a "crossing" is a join


def _seg_orient(p1, p2) -> Optional[str]:
    (x1, y1), (x2, y2) = p1, p2
    if abs(y1 - y2) < 1e-6 and abs(x1 - x2) > 1e-6:
        return "h"
    if abs(x1 - x2) < 1e-6 and abs(y1 - y2) > 1e-6:
        return "v"
    return None            # zero-length or slanted (fallback lead): never hops


def _near_vertex(x: float, y: float, pts, r: float) -> bool:
    r2 = r * r
    return any((x - vx) ** 2 + (y - vy) ** 2 < r2 for vx, vy in pts)


def _plan_jumps(routes, r: float = JUMP_R, eps: float = JUMP_EPS) -> Dict[int, list]:
    """Map edge_index -> [(vx, hy, r), ...] hop points on that edge's H runs.

    Deterministic regardless of edge order: the rule keys off ORIENTATION,
    so at every genuine crossover exactly one line — the horizontal one —
    hops (never both; a double hop is wrong).
    """
    edge_segs = []
    for _, pts in routes:
        edge_segs.append([(o, p1, p2)
                          for p1, p2 in zip(pts, pts[1:])
                          if (o := _seg_orient(p1, p2))])
    polylines = [pts for _, pts in routes]

    jumps: Dict[int, list] = {}
    for i, segs_i in enumerate(edge_segs):
        for orient_i, a_i, b_i in segs_i:
            if orient_i != "h":
                continue
            hy = a_i[1]
            hx_lo, hx_hi = min(a_i[0], b_i[0]), max(a_i[0], b_i[0])
            for j, segs_j in enumerate(edge_segs):
                if j == i:
                    continue
                for orient_j, a_j, b_j in segs_j:
                    if orient_j != "v":
                        continue
                    vx = a_j[0]
                    vy_lo, vy_hi = min(a_j[1], b_j[1]), max(a_j[1], b_j[1])
                    # A real crossover == strictly interior to BOTH segments.
                    if not (hx_lo + eps < vx < hx_hi - eps
                            and vy_lo + eps < hy < vy_hi - eps):
                        continue
                    # Don't distort a bend: skip near any vertex of either line.
                    if (_near_vertex(vx, hy, polylines[i], r)
                            or _near_vertex(vx, hy, polylines[j], r)):
                        continue
                    bucket = jumps.setdefault(i, [])
                    # Merge hops clustered on one run (rare; router avoids it).
                    if any(abs(hx - vx) < 2 * r and abs(hyy - hy) < 1e-6
                           for hx, hyy, _ in bucket):
                        continue
                    bucket.append((vx, hy, r))
    return jumps


def _jump_avoid_segments(jumps: Dict[int, list]) -> list:
    """Inverted-U footprint (≈2r × r above each crossing) as avoidance segments,
    so text planners and the audit treat the bulge as part of the line."""
    segs = []
    for hops in jumps.values():
        for vx, hy, r in hops:
            top = hy - r
            segs.append(((vx - r, top), (vx + r, top)))    # crown
            segs.append(((vx - r, hy), (vx - r, top)))      # left shoulder
            segs.append(((vx + r, hy), (vx + r, top)))      # right shoulder
    return segs


def _path_with_jumps(points, jumps) -> Path:
    """Splice a semicircle bulging up (−y) into each horizontal run a jump sits
    on. `jumps`: (x, y, r) crossovers owned by THIS edge's horizontal segments.
    """
    verts = [points[0]]
    codes = [Path.MOVETO]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        horizontal = abs(y0 - y1) < 1e-6
        step = 1 if x1 >= x0 else -1
        hops = sorted(
            (j for j in jumps
             if horizontal and abs(j[1] - y0) < 1e-6
             and min(x0, x1) < j[0] < max(x0, x1)),
            key=lambda j: abs(j[0] - x0))          # in travel order along the run
        for jx, _jy, r in hops:
            enter = jx - step * r
            verts.append((enter, y0))
            codes.append(Path.LINETO)
            # unit top-half arc; abs(uy) forces the bulge up whichever half it is
            arc = Path.arc(180, 0) if step > 0 else Path.arc(0, 180)
            for (ux, uy), c in zip(arc.vertices[1:], arc.codes[1:]):  # drop MOVETO
                verts.append((jx + ux * r, y0 - abs(uy) * r))
                codes.append(c)
        verts.append((x1, y1))
        codes.append(Path.LINETO)
    return Path(verts, codes)


# ── planning: numerals and labels positioned against ALL geometry ─────────


def _numeral_corners(node: LaidNode) -> Tuple[Tuple[float, float], ...]:
    x, y, w, h = node.x, node.y, node.w, node.h
    if node.shape == "diamond":
        return ((x + w * 0.75, y + h * 0.25), (x + w * 0.25, y + h * 0.25))
    if node.shape == "cylinder":
        return ((x + w * 0.9, y + 3.0), (x + w * 0.1, y + 3.0))
    if node.shape == "rounded":
        return ((x + w - 3.0, y + 0.5), (x + 3.0, y + 0.5))
    return ((x + w, y), (x, y))


def _plan_numerals(laid: LaidFigure, route_segments, node_full_rects):
    plans: Dict[str, tuple] = {}
    rects: List[tuple] = []
    leads: List[tuple] = []
    for node in laid.nodes.values():
        if not node.numeral or node.shape in ("initial", "final"):
            continue
        num_w = len(node.numeral) * 2.4 + 0.5
        ne, nw = _numeral_corners(node)
        candidates = [
            (ne, 5.5, -5.0), (ne, 7.5, -9.5), (ne, 11.0, -5.0),
            (ne, 5.5, -14.0), (ne, 8.0, 6.0),
            (nw, -num_w - 6.5, -5.0), (nw, -num_w - 8.0, -9.5),
            (ne, 9.0, -18.0), (ne, 13.0, -9.5), (ne, 15.0, -14.0),
            (nw, -num_w - 6.5, -14.0), (nw, -num_w - 11.0, -18.0),
            (ne, 12.0, 8.0), (nw, -num_w - 10.0, 8.0),
        ]
        best = None
        for corner, off_x, off_y in candidates:
            nx, ny = corner[0] + off_x, corner[1] + off_y
            rect = (nx - 0.5, ny - 4.0, nx + num_w, ny + 0.5)
            if off_x > 0:
                lead = ((nx - 1.0, ny + 1.2), corner)
            else:
                lead = ((nx + num_w + 1.0, ny + 1.2), corner)
            score = 0
            for p1, p2 in route_segments:
                if _segment_hits_rect(p1, p2, *rect):
                    score += 3
                if _segment_hits_rect(
                        p1, p2,
                        min(lead[0][0], lead[1][0]),
                        min(lead[0][1], lead[1][1]),
                        max(lead[0][0], lead[1][0]),
                        max(lead[0][1], lead[1][1])):
                    score += 1   # lead crossing a line: avoid when possible
            for other in rects:
                if _rects_overlap(rect, other):
                    score += 4
            for full in node_full_rects:
                if _rects_overlap(rect, full):
                    score += 4
                if _segment_hits_rect(lead[0], lead[1], *full):
                    score += 4
            if best is None or score < best[0]:
                best = (score, nx, ny, rect, lead, corner)
            if score == 0:
                break
        _, nx, ny, rect, lead, corner = best
        plans[node.id] = (nx, ny, lead)
        rects.append(rect)
        leads.append(lead)
    return plans, rects, leads


def _plan_labels(routes, route_segments, node_obstacle_rects, numeral_rects):
    plans = []
    placed: List[tuple] = []
    # Longest labels claim space first — they have the fewest clean spots.
    labeled = sorted(
        (r for r in routes if r[0].label),
        key=lambda r: len(r[0].label), reverse=True)
    for edge, points in labeled:
        segments = list(zip(points, points[1:]))

        def length(seg):
            (x1, y1), (x2, y2) = seg
            return abs(x2 - x1) + abs(y2 - y1)

        label_w = len(edge.label) * 1.7 + 1.0
        label_h = 3.4

        def candidate(seg, t: float, side: float, gap: float):
            (x1, y1), (x2, y2) = seg
            seg_len = max(length(seg), 1e-6)
            ux, uy = (x2 - x1) / seg_len, (y2 - y1) / seg_len
            horizontal = abs(y1 - y2) < 0.75
            cx = x1 + ux * seg_len * t
            cy = y1 + uy * seg_len * t
            if horizontal:
                if side > 0:
                    rect = (cx - label_w / 2, cy - gap - label_h,
                            cx + label_w / 2, cy - gap)
                    return rect, (cx, cy - gap, "center", "bottom")
                rect = (cx - label_w / 2, cy + gap,
                        cx + label_w / 2, cy + gap + label_h)
                return rect, (cx, cy + gap, "center", "top")
            if side > 0:
                rect = (cx + gap, cy - label_h / 2,
                        cx + gap + label_w, cy + label_h / 2)
                return rect, (cx + gap, cy, "left", "center")
            rect = (cx - gap - label_w, cy - label_h / 2,
                    cx - gap, cy + label_h / 2)
            return rect, (cx - gap, cy, "right", "center")

        def score(rect) -> int:
            hits = 0
            for nrect in node_obstacle_rects:
                if _rects_overlap(rect, nrect):
                    hits += 6      # sitting on a box is the worst outcome
            for lrect in placed:
                if _rects_overlap(rect, lrect):
                    hits += 5
            for mrect in numeral_rects:
                if _rects_overlap(rect, mrect):
                    hits += 5
            for p1, p2 in route_segments:
                if _segment_hits_rect(p1, p2, *rect):
                    hits += 1
            return hits

        # Preferred segment first (near the exit), then every other
        # segment of the route, longest first — a congested first segment
        # never forces a collision elsewhere on the same line.
        preferred = segments[0] if length(segments[0]) > 12.0 else max(
            segments, key=length)
        ordered = [preferred] + sorted(
            (s for s in segments if s is not preferred),
            key=length, reverse=True)

        best = None
        done = False
        for seg in ordered:
            # The gap ladder reaches well clear of congested lanes: a label
            # a little further from its line but CLEAN beats one touching.
            for gap in (2.2, 4.5, 7.5, 11.0, 15.0):
                for t in (0.5, 0.32, 0.68, 0.18, 0.82, 0.08, 0.92):
                    for side in (1.0, -1.0):
                        rect, anchor = candidate(seg, t, side, gap)
                        value = score(rect)
                        if best is None or value < best[0]:
                            best = (value, rect, anchor)
                        if value == 0:
                            done = True
                            break
                    if done:
                        break
                if done:
                    break
            if done:
                break
        _, rect, anchor = best
        placed.append(rect)
        plans.append((edge.label, anchor, rect))
    return plans


# ── audit: machine-verify the legibility guarantees ───────────────────────


def _audit(figure_no: int, route_segments, text_rects, box_rects) -> List[str]:
    violations = []
    shrunk = [(r[0] + 0.3, r[1] + 0.3, r[2] - 0.3, r[3] - 0.3, name)
              for r, name in text_rects]
    for left, top, right, bottom, name in shrunk:
        for p1, p2 in route_segments:
            if _segment_hits_rect(p1, p2, left, top, right, bottom):
                violations.append(
                    f"FIG.{figure_no}: text '{name}' touches a line")
                break
        for box in box_rects:
            if _rects_overlap((left, top, right, bottom), box):
                violations.append(
                    f"FIG.{figure_no}: text '{name}' sits on a box")
                break
    for i in range(len(shrunk)):
        for j in range(i + 1, len(shrunk)):
            a, b = shrunk[i], shrunk[j]
            if _rects_overlap(a[:4], b[:4]):
                violations.append(
                    f"FIG.{figure_no}: text '{a[4]}' overlaps '{b[4]}'")
    return violations


# ── rendering ──────────────────────────────────────────────────────────────


def render_figure(laid: LaidFigure, total_sheets: int = 1) -> tuple[str, str]:
    """Return (svg_string, pdf_base64) for one content-sized sheet."""
    global LAST_AUDIT

    # 1. ROUTE all edges (content coordinates, origin at 0,0).
    # Structural trunk lines route first so they establish shared lanes;
    # arrowed edges then keep their distance from those trunks.
    router = _GridRouter(laid, 0.0, 0.0)
    route_order = sorted(
        range(len(laid.edges)),
        key=lambda i: (0 if laid.edges[i].arrow == "none" else 1, i),
    )
    routed: Dict[int, list] = {}
    for index in route_order:
        routed[index] = _select_route(laid, laid.edges[index], index, router)
    # Rip-up & re-route: each edge re-routes against the COMPLETE traffic
    # picture, fixing order asymmetry (early edges crossing late lines
    # they never saw). One refinement pass converges.
    for index in route_order:
        router.uncommit(index)
        routed[index] = _select_route(laid, laid.edges[index], index, router)
    routes = [(edge, routed[index]) for index, edge in enumerate(laid.edges)]
    route_segments = [seg for _, pts in routes for seg in zip(pts, pts[1:])]

    # Plan hop-overs for the crossings the router could not avoid, BEFORE any
    # text is placed, and fold each hop's footprint into the geometry text
    # must clear — so numerals/labels route around the bulge and the audit
    # (which sees the same avoid list) stays honest.
    jumps = _plan_jumps(routes)
    route_segments += _jump_avoid_segments(jumps)

    node_full_rects = []
    node_obstacle_rects = []
    for node in laid.nodes.values():
        full = (node.x, node.y, node.x + node.w, node.y + node.h)
        node_full_rects.append(full)
        height = TITLE_STRIP if node.is_container else node.h
        node_obstacle_rects.append((node.x, node.y, node.x + node.w,
                                    node.y + height))

    # 2. PLAN all text against all geometry.
    numeral_plans, numeral_rects, numeral_leads = _plan_numerals(
        laid, route_segments, node_full_rects)
    avoid_segments = route_segments + numeral_leads
    label_plans = _plan_labels(routes, avoid_segments, node_obstacle_rects,
                               numeral_rects)

    # 3. SIZE the canvas around everything — nothing can clip.
    xs: List[float] = []
    ys: List[float] = []
    for rect in node_full_rects + numeral_rects + [p[2] for p in label_plans]:
        xs += [rect[0], rect[2]]
        ys += [rect[1], rect[3]]
    for p1, p2 in route_segments + numeral_leads:
        xs += [p1[0], p2[0]]
        ys += [p1[1], p2[1]]
    min_x, max_x = min(xs) - MARGIN_X, max(xs) + MARGIN_X
    if max_x - min_x < MIN_SHEET_W:
        pad = (MIN_SHEET_W - (max_x - min_x)) / 2.0
        min_x -= pad
        max_x += pad
    min_y = min(ys) - CAPTION_BAND
    max_y = max(ys) + MARGIN_BOTTOM
    sheet_w, sheet_h = max_x - min_x, max_y - min_y

    fig = plt.figure(figsize=(sheet_w / 25.4, sheet_h / 25.4))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(min_x, max_x)
    ax.set_ylim(max_y, min_y)       # y grows downward, like the layout
    ax.set_aspect("equal")
    ax.axis("off")

    center_x = (min_x + max_x) / 2.0
    ax.text(center_x, min_y + 7.0, f"{laid.number} / {total_sheets}",
            ha="center", va="center", fontsize=10)
    ax.text(center_x, min_y + 17.5, f"FIG. {laid.number}",
            ha="center", va="center", fontsize=FIG_FONT, fontweight="bold")

    # 4. DRAW from the plans.
    for node in laid.nodes.values():
        _draw_node(ax, node, numeral_plans.get(node.id))
    for index, (edge, points) in enumerate(routes):
        _draw_arrow(ax, edge, points, jumps.get(index, ()))
    for text, (tx, ty, ha, va), _ in label_plans:
        ax.text(tx, ty, text, ha=ha, va=va, fontsize=EDGE_FONT)

    # 5. AUDIT the result; log violations (QA asserts none on the mock).
    text_rects = [(rect, laid.nodes[nid].numeral)
                  for nid, rect in zip(numeral_plans.keys(), numeral_rects)]
    text_rects += [(rect, text) for text, _, rect in label_plans]
    LAST_AUDIT = _audit(laid.number, avoid_segments, text_rects,
                        node_obstacle_rects)
    for violation in LAST_AUDIT:
        print(f"[layout-audit] {violation}")

    svg_buffer = io.BytesIO()
    fig.savefig(svg_buffer, format="svg")
    pdf_buffer = io.BytesIO()
    fig.savefig(pdf_buffer, format="pdf")
    plt.close(fig)

    svg = svg_buffer.getvalue().decode("utf-8")
    pdf = base64.b64encode(pdf_buffer.getvalue()).decode("ascii")
    return svg, pdf


# ── nodes ───────────────────────────────────────────────────────────────────


def _draw_node(ax, node: LaidNode, numeral_plan: Optional[tuple]) -> None:
    x, y, w, h = node.x, node.y, node.w, node.h

    if node.shape == "diamond":
        ax.add_patch(Polygon(
            [(x + w / 2, y), (x + w, y + h / 2), (x + w / 2, y + h), (x, y + h / 2)],
            closed=True, fill=False, edgecolor="black",
        ))
        text_cy = y + h / 2
    elif node.shape == "cylinder":
        ellipse_h = 6.0
        body_top = y + ellipse_h / 2
        body_bottom = y + h - ellipse_h / 2
        ax.plot([x, x], [body_top, body_bottom], color="black")
        ax.plot([x + w, x + w], [body_top, body_bottom], color="black")
        ax.add_patch(Ellipse((x + w / 2, body_top), w, ellipse_h,
                             fill=False, edgecolor="black"))
        ax.add_patch(Ellipse((x + w / 2, body_bottom), w, ellipse_h,
                             fill=False, edgecolor="black"))
        text_cy = y + h / 2 + ellipse_h / 4
    elif node.shape == "rounded":
        ax.add_patch(FancyBboxPatch(
            (x, y), w, h,
            boxstyle="round,pad=0,rounding_size=4.0",
            fill=False, edgecolor="black",
        ))
        text_cy = y + h / 2
    elif node.shape == "parallelogram":
        skew = 5.0
        ax.add_patch(Polygon(
            [(x + skew, y), (x + w, y), (x + w - skew, y + h), (x, y + h)],
            closed=True, fill=False, edgecolor="black",
        ))
        text_cy = y + h / 2
    elif node.shape == "predefined":
        ax.add_patch(Rectangle((x, y), w, h, fill=False, edgecolor="black"))
        ax.plot([x + 3.0, x + 3.0], [y, y + h], color="black")
        ax.plot([x + w - 3.0, x + w - 3.0], [y, y + h], color="black")
        text_cy = y + h / 2
    elif node.shape == "initial":
        ax.add_patch(Ellipse((x + w / 2, y + h / 2), 5.0, 5.0,
                             facecolor="black", edgecolor="black"))
        return
    elif node.shape == "final":
        ax.add_patch(Ellipse((x + w / 2, y + h / 2), 7.0, 7.0,
                             fill=False, edgecolor="black"))
        ax.add_patch(Ellipse((x + w / 2, y + h / 2), 3.6, 3.6,
                             facecolor="black", edgecolor="black"))
        return
    elif node.shape == "entity":
        ax.add_patch(Rectangle((x, y), w, h, fill=False, edgecolor="black"))
        ax.plot([x, x + w], [y + ENTITY_TITLE_H, y + ENTITY_TITLE_H],
                color="black")
        ax.text(x + w / 2, y + ENTITY_TITLE_H / 2, node.label,
                ha="center", va="center", fontsize=LABEL_FONT)
        for index, field_name in enumerate(node.fields):
            ax.text(x + 3.0, y + ENTITY_TITLE_H + 3.2 + index * ENTITY_ROW_H,
                    field_name, ha="left", va="center", fontsize=7.5)
        _draw_numeral(ax, node, numeral_plan)
        return
    else:
        ax.add_patch(Rectangle((x, y), w, h, fill=False, edgecolor="black"))
        text_cy = y + h / 2

    if node.is_container:
        ax.text(x + w / 2, y + 5.0, node.label, ha="center", va="center",
                fontsize=LABEL_FONT)
    else:
        start = text_cy - (len(node.lines) - 1) * LINE_STEP / 2.0
        for index, line in enumerate(node.lines):
            ax.text(x + w / 2, start + index * LINE_STEP, line,
                    ha="center", va="center", fontsize=LABEL_FONT)

    _draw_numeral(ax, node, numeral_plan)


def _draw_numeral(ax, node: LaidNode, plan: Optional[tuple]) -> None:
    if not node.numeral or plan is None:
        return
    nx, ny, lead = plan
    (lx, ly), corner = lead
    ax.plot([lx, corner[0]], [ly, corner[1]], color="black")
    ax.text(nx, ny, node.numeral, ha="left", va="bottom",
            fontsize=NUMERAL_FONT, fontstyle="italic")


# ── edges ───────────────────────────────────────────────────────────────────


def _draw_arrow(ax, edge, points: list, jumps=()) -> None:
    arrowstyle = {"none": "-", "both": "<|-|>"}.get(edge.arrow, "-|>")
    path = _path_with_jumps(points, jumps) if jumps else Path(points)
    ax.add_patch(FancyArrowPatch(
        path=path,
        arrowstyle=arrowstyle, mutation_scale=12,
        linewidth=LINE_W, edgecolor="black", facecolor="black", fill=True,
        linestyle="solid" if edge.style != "dashed" else (0, (4.0, 2.5)),
        shrinkA=0.0, shrinkB=0.0,
    ))
