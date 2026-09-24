"""Orthogonal wire routing (A* with obstacle avoidance).

Harvested from CircuitSVG's layout.py. Component *placement* is no longer done
here — positions come from the human-edited layout document.
"""
from __future__ import annotations
import heapq
import math
from typing import TYPE_CHECKING, Any

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


# Cells are packed into one int: (gx + _OFF) * _STRIDE + (gy + _OFF). Search
# states add the direction of travel on top: cell * 4 + dir. Ints hash and
# compare much faster than tuples, and A* spends nearly all its time on that.
_OFF = 1 << 20
_STRIDE = 1 << 21
_NONE_D, _H_D, _V_D = 0, 1, 2


def _enc(gx: int, gy: int) -> int:
    return (gx + _OFF) * _STRIDE + (gy + _OFF)


def _astar_edge(
    start: tuple[float, float],
    end: tuple[float, float],
    obstacles: set[int],
    overlap_cells: set[int],
    grid: int = ROUTE_GRID,
    max_iter: int = 300000,
) -> list[Segment] | None:
    """A* orthogonal pathfinding from start to end. Returns segment list or None.

    `obstacles` and `overlap_cells` hold cells packed with _enc().
    """
    sx, sy = start
    ex, ey = end
    sgx = int(round(sx / grid))
    sgy = int(round(sy / grid))
    egx = int(round(ex / grid))
    egy = int(round(ey / grid))

    if sgx == egx and sgy == egy:
        return [(start, end)] if start != end else []

    s_cell = _enc(sgx, sgy)
    e_cell = _enc(egx, egy)
    S = _STRIDE
    # (cell delta, dx, dy, direction) — same order as the original tuple version,
    # which fixes the tie-breaking and therefore the exact routes produced.
    moves = ((-S, -1, 0, _H_D), (S, 1, 0, _H_D), (-1, 0, -1, _V_D), (1, 0, 1, _V_D))

    start_state = s_cell * 4 + _NONE_D
    h0 = abs(sgx - egx) + abs(sgy - egy)
    open_q: list = [(h0, 0, 0, start_state, sgx, sgy)]
    g_scores: dict[int, int] = {start_state: 0}
    parents: dict[int, int] = {}
    closed: set[int] = set()
    tie = 0
    push = heapq.heappush
    pop = heapq.heappop

    while open_q and tie < max_iter:
        _, _, gv, state, gx, gy = pop(open_q)
        if state in closed:
            continue
        closed.add(state)
        cell = state >> 2
        last_d = state & 3

        if cell == e_cell:
            cells = [(gx, gy)]
            cur = state
            while cur in parents:
                cur = parents[cur]
                c = cur >> 2
                cells.append((c // S - _OFF, c % S - _OFF))
            cells.reverse()
            return _cells_to_segments(cells, grid, start, end)

        for dc, ddx, ddy, nd in moves:
            ncell = cell + dc
            new_state = ncell * 4 + nd
            if new_state in closed:
                continue
            # Goal cell is always reachable, even if it overlaps an obstacle
            # (pin can be on the body edge after inflation).
            if ncell in obstacles and ncell != e_cell and ncell != s_cell:
                continue
            cost = 1
            if last_d != _NONE_D and last_d != nd:
                cost += 10  # turn penalty — prefer straight runs
            if ncell in overlap_cells:
                cost += 6   # other nets here — avoid if possible
            new_g = gv + cost
            old = g_scores.get(new_state)
            if old is None or new_g < old:
                g_scores[new_state] = new_g
                parents[new_state] = state
                tie += 1
                ngx = gx + ddx
                ngy = gy + ddy
                push(open_q, (new_g + abs(ngx - egx) + abs(ngy - egy),
                              tie, new_g, new_state, ngx, ngy))

    return None  # no path


class FreeSpace:
    """Connected regions of obstacle-free cells, labelled once per scene.

    A pin boxed in by overlapping parts cannot be reached at all, but A* only
    finds that out after exhausting its iteration budget on the unbounded
    plane — over a second per wire. Two cells in different regions are known
    to be unconnected up front, so such a wire goes straight to the fallback,
    with exactly the result the exhausted search would have produced.
    """

    def __init__(self, obstacles: set[tuple[int, int]]):
        self.obstacles = obstacles
        self.labels: dict[tuple[int, int], int] = {}
        if not obstacles:
            self.box = (0, 0, -1, -1)
            return
        xs = [c[0] for c in obstacles]
        ys = [c[1] for c in obstacles]
        # One free ring around everything: outside the box the plane is empty,
        # so the ring (label 0) stands for the whole outside world.
        x0, y0, x1, y1 = min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1
        self.box = (x0, y0, x1, y1)
        label = 0
        for sx in range(x0, x1 + 1):
            for sy in range(y0, y1 + 1):
                seed = (sx, sy)
                if seed in obstacles or seed in self.labels:
                    continue
                self.labels[seed] = label
                stack = [seed]
                while stack:
                    cx, cy = stack.pop()
                    for n in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                        if (x0 <= n[0] <= x1 and y0 <= n[1] <= y1
                                and n not in obstacles and n not in self.labels):
                            self.labels[n] = label
                            stack.append(n)
                label += 1

    def _label(self, cell: tuple[int, int]) -> int | None:
        x0, y0, x1, y1 = self.box
        if not (x0 <= cell[0] <= x1 and y0 <= cell[1] <= y1):
            return 0
        return self.labels.get(cell)

    def _entry_labels(self, cell: tuple[int, int]) -> set[int]:
        # A* may start or end on an obstacle cell but never step through one,
        # so an obstacle endpoint connects via its free neighbours only.
        if cell not in self.obstacles:
            return {self._label(cell)}  # type: ignore[arg-type]
        cx, cy = cell
        out = set()
        for n in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
            if n not in self.obstacles:
                out.add(self._label(n))
        out.discard(None)
        return out  # type: ignore[return-value]

    def connected(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        if abs(a[0] - b[0]) + abs(a[1] - b[1]) <= 1:
            return True
        return bool(self._entry_labels(a) & self._entry_labels(b))


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


def _mst_edges(pts: list[tuple[float, float]]) -> list[tuple[int, int]]:
    """Prim's MST on Manhattan distance, O(n^2).

    Picks the same edges as the plain triple loop it replaced: among equally
    short candidates the one with the lowest tree index i, then lowest j.
    """
    n = len(pts)
    in_tree = [False] * n
    in_tree[0] = True
    best_d = [float("inf")] * n
    best_i = [0] * n

    def relax(k: int) -> None:
        kx, ky = pts[k]
        for j in range(n):
            if in_tree[j]:
                continue
            d = abs(kx - pts[j][0]) + abs(ky - pts[j][1])
            if d < best_d[j] or (d == best_d[j] and k < best_i[j]):
                best_d[j] = d
                best_i[j] = k

    relax(0)
    edges: list[tuple[int, int]] = []
    for _ in range(n - 1):
        j = min((j for j in range(n) if not in_tree[j]),
                key=lambda j: (best_d[j], best_i[j], j))
        edges.append((best_i[j], j))
        in_tree[j] = True
        relax(j)
    return edges


class Router:
    """Routes the nets of one scene, one after another.

    Later nets see earlier ones: they avoid running along their wires (overlap
    penalty) and the L-shaped fallback picks the orientation that overlaps
    least. So the order in which nets are routed matters, and the caller keeps
    it stable.
    """

    def __init__(self, components: list["Component"], grid: int = ROUTE_GRID,
                 inflate: int = 1):
        self.grid = grid
        self.obstacle_cells = build_obstacle_grid(components, grid=grid, inflate=inflate)
        self._obstacles = {_enc(gx, gy) for gx, gy in self.obstacle_cells}
        self._free: FreeSpace | None = None
        self.existing: list[Segment] = []
        self._overlap: set[int] = set()
        self._fresh: list[Segment] = []    # wires routed anew in this run
        self._net_fresh = 0          # index in `_fresh` where this net began
        self.reused = 0              # edges taken over from stored routes
        self.routed = 0              # edges that needed a fresh search
        self.routes: dict[str, dict] = {}   # what to store for next time

    @property
    def free_space(self) -> FreeSpace:
        if self._free is None:
            self._free = FreeSpace(self.obstacle_cells)
        return self._free

    def _add(self, leg: list[Segment]) -> None:
        self.existing.extend(leg)
        for seg in leg:
            self._overlap.update(_enc(gx, gy) for gx, gy in _segment_cells(seg, self.grid))

    def _leg(self, a: tuple[float, float], b: tuple[float, float]) -> list[Segment]:
        g = self.grid
        ca = (int(round(a[0] / g)), int(round(a[1] / g)))
        cb = (int(round(b[0] / g)), int(round(b[1] / g)))
        leg = None
        if ca == cb or self.free_space.connected(ca, cb):
            leg = _astar_edge(a, b, self._obstacles, self._overlap, g)
        if leg is None:
            leg = _l_route_edge(a, b, self.existing)
        return leg

    def _reusable(self, legs: list[list[Segment]]) -> bool:
        """A stored wire survives unless the world changed underneath it: it
        must not run through a body, nor lie on top of another net's wire that
        was only just routed. (Overlaps it already had when it was stored are
        fine — the router accepts those at a price, and re-checking them would
        reroute the same wires on every single change.)"""
        g = self.grid
        other = self._fresh[:self._net_fresh]
        for leg in legs:
            if not leg:
                continue
            ca = (int(round(leg[0][0][0] / g)), int(round(leg[0][0][1] / g)))
            cb = (int(round(leg[-1][1][0] / g)), int(round(leg[-1][1][1] / g)))
            ends = {_enc(*ca), _enc(*cb)}
            # A fallback wire to a boxed-in pin crosses bodies by necessity;
            # it stays valid for as long as a search would still fail.
            boxed_in = None
            for seg in leg:
                for gx, gy in _segment_cells(seg, g):
                    c = _enc(gx, gy)
                    if c in self._obstacles and c not in ends:
                        if boxed_in is None:
                            boxed_in = not self.free_space.connected(ca, cb)
                        if not boxed_in:
                            return False
                if any(_overlap_length(seg, o) > 0 for o in other):
                    return False
        return True

    def _stored(self, stored: dict[str, Any], key: str,
                chain: list[tuple[float, float]]) -> list[list[Segment]] | None:
        entry = stored.get(key)
        if not isinstance(entry, dict):
            return None
        try:
            pts = [tuple(map(float, p)) for p in entry["points"]]
            legs = [[((float(a[0]), float(a[1])), (float(b[0]), float(b[1])))
                     for a, b in leg] for leg in entry["legs"]]
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        if len(legs) != len(chain) - 1 or len(pts) != len(chain):
            return None
        if not _same_points(pts, chain):
            # The MST may list the pins the other way round this time.
            if not _same_points(pts[::-1], chain):
                return None
            legs = [[(b, a) for a, b in reversed(leg)] for leg in reversed(legs)]
        # Snap the ends to the exact current pin coordinates (float noise).
        for leg, a, b in zip(legs, chain, chain[1:]):
            if leg:
                leg[0] = (a, leg[0][1])
                leg[-1] = (leg[-1][0], b)
        return legs if self._reusable(legs) else None

    def route_net(
        self,
        pin_positions: list[tuple[float, float]],
        pin_keys: list[str],
        waypoints: dict[str, list[tuple[float, float]]] | None = None,
        stored: dict[str, Any] | None = None,
    ) -> list[dict]:
        """Connect the pins of one net with orthogonal wires.

        Returns one entry per tree edge:
            {"key": "R1.2|U1.DIS", "waypoints": [...], "legs": [[seg, ...], ...]}

        A tree edge is split into legs by its user waypoints, so the wire is
        forced through the points the human dragged it to. Each leg is
        A*-routed on its own and therefore still avoids component bodies.

        `stored` holds the wires of an earlier run (see `routes`). An edge whose
        pins and waypoints have not moved keeps its old wire if that is still
        valid, so dragging one part only reroutes the wires attached to it —
        both much faster and calmer than redrawing the whole sheet.
        """
        if len(pin_positions) < 2:
            return []
        waypoints = waypoints or {}
        stored = stored or {}
        pts = list(pin_positions)
        self._net_fresh = len(self._fresh)

        out: list[dict] = []
        for i, j in _mst_edges(pts):
            key = edge_key(pin_keys[i], pin_keys[j])
            wps = [tuple(w) for w in waypoints.get(key, [])]
            chain = [pts[i], *wps, pts[j]]
            legs = self._stored(stored, key, chain)
            if legs is not None:
                self.reused += 1
                for leg in legs:
                    self._add(leg)
            else:
                self.routed += 1
                legs = []
                for a, b in zip(chain, chain[1:]):
                    leg = self._leg(a, b)
                    legs.append(leg)
                    self._add(leg)
                    self._fresh.extend(leg)
            self.routes[key] = {
                "points": [[round(x, 2), round(y, 2)] for x, y in chain],
                "legs": [[[[round(x, 2), round(y, 2)] for x, y in seg] for seg in leg]
                         for leg in legs],
            }
            out.append({"key": key, "waypoints": [list(w) for w in wps], "legs": legs})
        return out


def _same_points(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> bool:
    return len(a) == len(b) and all(
        abs(p[0] - q[0]) < 0.05 and abs(p[1] - q[1]) < 0.05 for p, q in zip(a, b))


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


