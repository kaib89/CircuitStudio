"""Orthogonal wire routing (A* with obstacle avoidance).

Harvested from CircuitSVG's layout.py. Component *placement* is no longer done
here — positions come from the human-edited layout document.
"""
from __future__ import annotations
import heapq
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .symbols import Component

ROUTE_GRID = 5     # px per cell in the routing grid (A*)


# ── Routing ────────────────────────────────────────────────────────────────

Segment = tuple[tuple[float, float], tuple[float, float]]


def _overlap_length(seg1: Segment, seg2: Segment) -> float:
    """Length of collinear overlap between two orthogonal segments. 0 if none."""
    (x1a, y1a), (x1b, y1b) = seg1
    (x2a, y2a), (x2b, y2b) = seg2
    # both horizontal at same y
    if (abs(y1a - y1b) < 0.5 and abs(y2a - y2b) < 0.5
            and abs(y1a - y2a) < 0.5):
        l1, r1 = sorted([x1a, x1b])
        l2, r2 = sorted([x2a, x2b])
        return max(0.0, min(r1, r2) - max(l1, l2))
    # both vertical at same x
    if (abs(x1a - x1b) < 0.5 and abs(x2a - x2b) < 0.5
            and abs(x1a - x2a) < 0.5):
        l1, r1 = sorted([y1a, y1b])
        l2, r2 = sorted([y2a, y2b])
        return max(0.0, min(r1, r2) - max(l1, l2))
    return 0.0


def _total_overlap(candidates: list[Segment],
                   existing: list[Segment]) -> float:
    return sum(_overlap_length(c, e) for c in candidates for e in existing)


def _l_route_edge(p1: tuple[float, float], p2: tuple[float, float],
                  existing: list[Segment]) -> list[Segment]:
    """Two-segment L-route from p1 to p2; picks orientation that overlaps least."""
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 or y1 == y2:
        return [((x1, y1), (x2, y2))]
    opt_h = [((x1, y1), (x2, y1)), ((x2, y1), (x2, y2))]
    opt_v = [((x1, y1), (x1, y2)), ((x1, y2), (x2, y2))]
    ovl_h = _total_overlap(opt_h, existing)
    ovl_v = _total_overlap(opt_v, existing)
    return opt_h if ovl_h <= ovl_v else opt_v


def build_obstacle_grid(
    components: list["Component"],
    grid: int = ROUTE_GRID,
    inflate: int = 1,
) -> set[tuple[int, int]]:
    """Cells occupied by any component body. `inflate` adds a margin (in cells)
    so wires keep some breathing room around symbols."""
    obstacles: set[tuple[int, int]] = set()
    for c in components:
        bx0, by0, bx1, by1 = c.body_bbox()
        gx_min = int(math.floor(bx0 / grid)) - inflate
        gx_max = int(math.ceil(bx1 / grid)) + inflate
        gy_min = int(math.floor(by0 / grid)) - inflate
        gy_max = int(math.ceil(by1 / grid)) + inflate
        for gx in range(gx_min, gx_max + 1):
            for gy in range(gy_min, gy_max + 1):
                obstacles.add((gx, gy))
    return obstacles


def _astar_edge(
    start: tuple[float, float],
    end: tuple[float, float],
    obstacles: set[tuple[int, int]],
    overlap_cells: set[tuple[int, int]],
    grid: int = ROUTE_GRID,
    max_iter: int = 300000,
) -> list[Segment] | None:
    """A* orthogonal pathfinding from start to end. Returns segment list or None."""
    sx, sy = start
    ex, ey = end
    sgx = int(round(sx / grid))
    sgy = int(round(sy / grid))
    egx = int(round(ex / grid))
    egy = int(round(ey / grid))

    if sgx == egx and sgy == egy:
        return [(start, end)] if start != end else []

    # Heuristic: Manhattan distance with small turn-aware bias
    def H(gx: int, gy: int) -> int:
        return abs(gx - egx) + abs(gy - egy)

    NONE_D, H_D, V_D = 0, 1, 2
    start_state = (sgx, sgy, NONE_D)
    open_q: list = [(H(sgx, sgy), 0, 0, start_state)]
    g_scores: dict[tuple[int, int, int], int] = {start_state: 0}
    parents: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    closed: set[tuple[int, int, int]] = set()
    tie = 0

    while open_q and tie < max_iter:
        _, _, gv, state = heapq.heappop(open_q)
        if state in closed:
            continue
        closed.add(state)
        gx, gy, last_d = state

        if (gx, gy) == (egx, egy):
            cells = [(gx, gy)]
            cur = state
            while cur in parents:
                cur = parents[cur]
                cells.append((cur[0], cur[1]))
            cells.reverse()
            return _cells_to_segments(cells, grid, start, end)

        for ndx, ndy, nd in ((-1, 0, H_D), (1, 0, H_D), (0, -1, V_D), (0, 1, V_D)):
            ngx, ngy = gx + ndx, gy + ndy
            new_state = (ngx, ngy, nd)
            if new_state in closed:
                continue
            cell = (ngx, ngy)
            # Goal cell is always reachable, even if it overlaps an obstacle
            # (pin can be on the body edge after inflation).
            if cell in obstacles and cell != (egx, egy) and cell != (sgx, sgy):
                continue
            cost = 1
            if last_d != NONE_D and last_d != nd:
                cost += 10  # turn penalty — prefer straight runs
            if cell in overlap_cells:
                cost += 6   # other nets here — avoid if possible
            new_g = gv + cost
            if new_state not in g_scores or new_g < g_scores[new_state]:
                g_scores[new_state] = new_g
                parents[new_state] = state
                tie += 1
                f = new_g + H(ngx, ngy)
                heapq.heappush(open_q, (f, tie, new_g, new_state))

    return None  # no path


def _cells_to_segments(
    cells: list[tuple[int, int]],
    grid: int,
    exact_start: tuple[float, float],
    exact_end: tuple[float, float],
) -> list[Segment]:
    """Compress consecutive same-direction grid cells, snap endpoints to exact pin coords."""
    if len(cells) < 2:
        return [(exact_start, exact_end)] if exact_start != exact_end else []

    turn_pts = [cells[0]]
    for i in range(1, len(cells) - 1):
        prev_dx = cells[i][0] - cells[i - 1][0]
        prev_dy = cells[i][1] - cells[i - 1][1]
        next_dx = cells[i + 1][0] - cells[i][0]
        next_dy = cells[i + 1][1] - cells[i][1]
        if (prev_dx, prev_dy) != (next_dx, next_dy):
            turn_pts.append(cells[i])
    turn_pts.append(cells[-1])

    world = [(p[0] * grid, p[1] * grid) for p in turn_pts]
    world[0] = exact_start
    world[-1] = exact_end

    # Snap any non-axis-aligned segments (from snapping endpoints) by inserting
    # a corner so we keep orthogonality.
    segments: list[Segment] = []
    for i in range(len(world) - 1):
        a, b = world[i], world[i + 1]
        if a == b:
            continue
        if a[0] == b[0] or a[1] == b[1]:
            segments.append((a, b))
        else:
            # rare: insert a corner that matches the dominant direction of the next/prev seg
            mid = (b[0], a[1])
            segments.append((a, mid))
            segments.append((mid, b))
    return segments


def _segment_cells(seg: Segment, grid: int) -> set[tuple[int, int]]:
    (x1, y1), (x2, y2) = seg
    g1 = (int(round(x1 / grid)), int(round(y1 / grid)))
    g2 = (int(round(x2 / grid)), int(round(y2 / grid)))
    cells: set[tuple[int, int]] = set()
    if g1[0] == g2[0]:
        for gy in range(min(g1[1], g2[1]), max(g1[1], g2[1]) + 1):
            cells.add((g1[0], gy))
    elif g1[1] == g2[1]:
        for gx in range(min(g1[0], g2[0]), max(g1[0], g2[0]) + 1):
            cells.add((gx, g1[1]))
    return cells


def edge_key(a: str, b: str) -> str:
    """Stable identifier for the connection between two pins, order-independent."""
    return "|".join(sorted([a, b]))


def route_net_edges(
    pin_positions: list[tuple[float, float]],
    pin_keys: list[str],
    waypoints: dict[str, list[tuple[float, float]]] | None = None,
    existing_segments: list[Segment] | None = None,
    obstacles: set[tuple[int, int]] | None = None,
    grid: int = ROUTE_GRID,
) -> list[dict]:
    """Connect the pins of one net with orthogonal wires.

    Returns one entry per tree edge:
        {"key": "R1.2|U1.DIS", "waypoints": [...], "legs": [[seg, ...], ...]}

    A tree edge is split into legs by its user waypoints, so the wire is forced
    through the points the human dragged it to. Each leg is A*-routed on its own
    and therefore still avoids component bodies.
    """
    if len(pin_positions) < 2:
        return []

    existing = list(existing_segments) if existing_segments else []
    waypoints = waypoints or {}

    pts = list(pin_positions)
    n = len(pts)
    # Prim MST on Manhattan
    in_tree = [False] * n
    in_tree[0] = True
    edges: list[tuple[int, int]] = []
    for _ in range(n - 1):
        best_d = float("inf")
        best_i, best_j = 0, 1
        for i in range(n):
            if not in_tree[i]:
                continue
            for j in range(n):
                if in_tree[j]:
                    continue
                d = abs(pts[i][0] - pts[j][0]) + abs(pts[i][1] - pts[j][1])
                if d < best_d:
                    best_d, best_i, best_j = d, i, j
        edges.append((best_i, best_j))
        in_tree[best_j] = True

    # Precompute overlap cells from existing routes (other nets)
    overlap_cells: set[tuple[int, int]] = set()
    if obstacles is not None:
        for s in existing:
            overlap_cells.update(_segment_cells(s, grid))

    out: list[dict] = []
    for i, j in edges:
        key = edge_key(pin_keys[i], pin_keys[j])
        wps = [tuple(w) for w in waypoints.get(key, [])]
        chain = [pts[i], *wps, pts[j]]

        legs: list[list[Segment]] = []
        for a, b in zip(chain, chain[1:]):
            leg: list[Segment] | None = None
            if obstacles is not None:
                leg = _astar_edge(a, b, obstacles, overlap_cells, grid)
            if leg is None:
                leg = _l_route_edge(a, b, existing)
            legs.append(leg)
            existing.extend(leg)
            if obstacles is not None:
                for s in leg:
                    overlap_cells.update(_segment_cells(s, grid))

        out.append({"key": key, "waypoints": [list(w) for w in wps], "legs": legs})

    return out


def _pt_key(p: tuple[float, float]) -> tuple[float, float]:
    return (round(p[0], 1), round(p[1], 1))


def _on_interior(p: tuple[float, float], seg: Segment, eps: float = 0.5) -> bool:
    """True if p lies strictly between the endpoints of the orthogonal seg."""
    (x1, y1), (x2, y2) = seg
    px, py = p
    if abs(y1 - y2) <= eps:                      # horizontal
        if abs(py - y1) > eps:
            return False
        return min(x1, x2) + eps < px < max(x1, x2) - eps
    if abs(x1 - x2) <= eps:                      # vertical
        if abs(px - x1) > eps:
            return False
        return min(y1, y2) + eps < py < max(y1, y2) - eps
    return False


def _direction(frm: tuple[float, float], to: tuple[float, float]) -> str:
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    if abs(dx) >= abs(dy):
        return "E" if dx > 0 else "W"
    return "S" if dy > 0 else "N"


def find_junctions(
    all_segments: list[list[Segment]],
    all_pins: list[list[tuple[float, float]]] | None = None,
) -> set[tuple[float, float]]:
    """Points where three or more conductors of the SAME net meet.

    Crossings between different nets are deliberately ignored — those are just
    visual overlaps with no electrical meaning.

    The wires of a net are first split at every node (segment endpoint or pin)
    that falls inside another segment, so a wire ending in the middle of another
    wire really produces two separate sub-segments.

    A dot is then drawn where conductors leave a node in three or more different
    directions. Counting *directions* rather than segments matters: same-net
    wires that overlap collinearly would otherwise score three connections while
    visually being a single straight line.

    A component pin counts as one conductor (the symbol's own lead runs into it),
    so a pin with a wire passing straight through gets a dot, while a pin that
    merely terminates one wire does not.
    """
    junctions: set[tuple[float, float]] = set()

    for idx, segs in enumerate(all_segments):
        if not segs:
            continue
        pins = all_pins[idx] if all_pins and idx < len(all_pins) else []

        nodes: set[tuple[float, float]] = set()
        for a, b in segs:
            nodes.add(_pt_key(a))
            nodes.add(_pt_key(b))
        for p in pins:
            nodes.add(_pt_key(p))

        # Split each segment at every node lying strictly inside it.
        sub: set[tuple[tuple[float, float], tuple[float, float]]] = set()
        for seg in segs:
            a, b = _pt_key(seg[0]), _pt_key(seg[1])
            if a == b:
                continue
            inner = [n for n in nodes if _on_interior(n, seg)]
            horizontal = abs(seg[0][1] - seg[1][1]) <= 0.5
            chain = sorted([a, b, *inner],
                           key=lambda q: q[0] if horizontal else q[1])
            for u, v in zip(chain, chain[1:]):
                if u != v:
                    sub.add((u, v) if u <= v else (v, u))

        dirs: dict[tuple[float, float], set[str]] = {}
        for u, v in sub:
            dirs.setdefault(u, set()).add(_direction(u, v))
            dirs.setdefault(v, set()).add(_direction(v, u))
        for p in pins:
            k = _pt_key(p)
            if k in dirs:
                dirs[k].add("PIN")

        for node, d in dirs.items():
            if len(d) >= 3:
                junctions.add(node)

    return junctions


