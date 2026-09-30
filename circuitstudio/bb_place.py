"""Breadboard auto-placement: a first plan to drag around, not an optimum.

Same spirit as the schematic's auto-arrange — the result only has to be
correct and readable enough to start from. Correct means it passes the
check in breadboard.py; everything else is heuristics:

1. Chips and boards go in a row along the centre channel, boards first,
   then in the order of their 'group' and of the circuit, with some room
   between them for the parts that hang off their pins.
2. Ground goes on the − rails, the supply on the + rails (with two
   supplies: one on the top + rail, one on the bottom). Chip pins on those
   nets get a short wire to the rail.
3. Every other net lives in half columns: the ones its chip pins sit in,
   or free ones picked near the parts that need them. A two-lead part
   bridges the two columns of its nets, as short as possible.
4. Whatever is still in pieces gets jumper wires, nearest pieces first.

Mode "rest" keeps everything already on the board and only adds; mode
"all" starts from an empty board.
"""
from __future__ import annotations

import copy
from typing import Any

from .breadboard import HUMAN_EDIT, ROW_Y, Breadboard, Hole, covered_holes, parse_hole
from .document import is_ground_net, is_supply_net
from .footprints import RIGID, footprint, is_physical

# Parts that do not sit on a breadboard; only their leads end in a hole.
# Kept in step with BB_OFFBOARD in web/app.js.
OFFBOARD_TYPES = {"speaker", "battery", "source_dc", "source_ac", "connector",
                  "esp32", "rpi", "arduino_uno", "arduino_nano"}
MAX_BRIDGE = 10     # columns a lead may span before a jumper is the better idea
# Lead spacing a part gets when its column is picked fresh — as in web/app.js.
SPAN = {"resistor": 4, "inductor": 4, "diode": 4, "zener": 4, "fuse": 4,
        "capacitor": 2, "capacitor_pol": 2, "crystal": 2, "switch": 2, "led": 1}

# Row preference inside a half column: leads near the chip, wires outside.
_LEAD_ROWS = {"lo": "dcbae", "hi": "ghijf"}
_WIRE_ROWS = {"lo": "abcde", "hi": "jihgf"}


def _half(row: str) -> str:
    return "lo" if row in "abcde" else "hi"


def link_cost(a: str, b: str, ca: int, cb: int) -> float:
    """How bad a lead or wire between two nodes is. Length counts, but most
    of all it must not cross the board: from the lower half to a top rail
    (or across the channel) it would run over the chips in between."""
    cost = abs(ca - cb)
    for x, y in ((a, b), (b, a)):
        if x[-2:] not in ("lo", "hi") and y[-2:] in ("lo", "hi"):
            same_side = (x[0] == "T") == (y[-2:] == "hi")
            cost += 1 if same_side else 40
    if a[-2:] in ("lo", "hi") and b[-2:] in ("lo", "hi") and a[-2:] != b[-2:]:
        cost += 8
    return cost


class Placer:
    def __init__(self, circuit: dict[str, Any], plan: dict[str, Any] | None,
                 mode: str = "rest"):
        plan = copy.deepcopy(plan) if isinstance(plan, dict) else {}
        if mode == "all":
            plan = {"board": plan.get("board") or {}, "parts": {}, "wires": []}
        plan.pop(HUMAN_EDIT, None)
        plan.setdefault("parts", {})
        plan.setdefault("wires", [])
        self.circuit = circuit
        self.plan = plan
        self.log: list[str] = []
        self.failed: list[str] = []
        bb = Breadboard(plan, circuit)
        self.cols = bb.columns
        self.split = bb.split_rails
        self.net_of = bb.net_of
        self.specs = bb._specs
        self.comps = bb._comps
        # Parts whose placement is broken count as not placed.
        for cid in list(plan["parts"]):
            if cid not in bb.parts:
                del plan["parts"][cid]
        self.used: set[str] = set()
        self.owner: dict[str, str | None] = {}     # node -> net (None: reserved)
        self.board_nodes: set[str] = set()         # columns a board's pins sit in
        self.chip_span: dict[str, tuple[int, int]] = {}   # node -> its chip's columns
        self.segments: dict[str, list[tuple[int, int]]] = {}  # row -> lead spans
        for part in bb.parts.values():
            for p in part.pins:
                self._take(p.hole.name, self.net_of.get(p.ref or ""))
                if part.ctype == "pico":
                    self.board_nodes.add(self.node(p.hole.name))
            if part.anchor is not None:
                self._note_chip([p.hole.name for p in part.pins])
            elif not part.offboard and len(part.pins) == 2 \
                    and part.pins[0].hole.row == part.pins[1].hole.row:
                cs = sorted(p.hole.col for p in part.pins)
                self.segments.setdefault(part.pins[0].hole.row, []).append((cs[0], cs[1]))
            for h in part.covered:
                self.used.add(h.name)
        for w in bb.wires:
            self.used.update((w.a.name, w.b.name))
        self.rails = self._rail_plan()

    # ── nodes and holes ──────────────────────────────────────────────────────

    def node(self, hole: str) -> str:
        h = parse_hole(hole, self.cols)
        if h.rail:
            if self.split:
                return f"{h.row}:{'L' if h.col <= self.cols // 2 else 'R'}"
            return h.row
        return f"{h.col}{_half(h.row)}"

    def _note_chip(self, holes: list[str]) -> None:
        cols = [parse_hole(h, self.cols).col for h in holes]
        for h in holes:
            self.chip_span[self.node(h)] = (min(cols), max(cols))

    def _outward(self, node: str | None) -> int:
        """Direction away from the middle of the chip this column belongs to,
        so a fresh column lands outside the chip, not among its pins."""
        if node is None or node not in self.chip_span:
            return 1
        lo, hi = self.chip_span[node]
        return -1 if int(node[:-2]) * 2 < lo + hi else 1

    def _take(self, hole: str, net: str | None) -> None:
        self.used.add(hole)
        n = self.node(hole)
        if n not in self.owner or self.owner[n] is None:
            self.owner[n] = net

    def free_holes(self, node: str, near: int, rows: dict[str, str]) -> list[str]:
        """Free holes of a node, best first."""
        if node[-2:] in ("lo", "hi"):
            col, half = int(node[:-2]), node[-2:]
            return [f"{r}{col}" for r in rows[half] if f"{r}{col}" not in self.used]
        rail = node.split(":")[0]
        lo, hi = 1, self.cols
        if ":" in node:
            lo, hi = ((1, self.cols // 2) if node.endswith("L")
                      else (self.cols // 2 + 1, self.cols))
        cands = [f"{rail}{c}" for c in range(lo, hi + 1) if f"{rail}{c}" not in self.used]
        return sorted(cands, key=lambda h: abs(parse_hole(h, self.cols).col - near))

    @staticmethod
    def col_of(node: str, near: int) -> int:
        return int(node[:-2]) if node[-2:] in ("lo", "hi") else near

    def nodes_of(self, net: str) -> list[str]:
        return [n for n, owner in self.owner.items() if owner == net]

    def free_column(self, near: int, half: str, avoid: set[str] = frozenset()) -> str | None:
        """An empty half column close to `near`."""
        for d in range(0, self.cols):
            for c in (near + d, near - d) if d else (near,):
                n = f"{c}{half}"
                if 1 <= c <= self.cols and n not in self.owner and n not in avoid \
                        and len(self.free_holes(n, c, _LEAD_ROWS)) >= 3:
                    return n
        return None

    # ── rails ────────────────────────────────────────────────────────────────

    def _rail_plan(self) -> dict[str, list[str]]:
        """Which rail rows each power net may use."""
        nets = []
        for net in self.circuit.get("nets", []) or []:
            name = str(net.get("name", ""))
            if any(self._physical(r) for r in net.get("pins", []) or []):
                nets.append(name)
        grounds = [n for n in nets if is_ground_net(n)][:1]
        supplies = [n for n in nets if is_supply_net(n) and n not in grounds][:2]
        plan = {g: ["T-", "B-"] for g in grounds}
        if len(supplies) == 1:
            plan[supplies[0]] = ["T+", "B+"]
        elif len(supplies) == 2:
            plan[supplies[0]], plan[supplies[1]] = ["T+"], ["B+"]
        return plan

    def _physical(self, ref: str) -> bool:
        spec = self.specs.get(str(ref).split(".", 1)[0])
        return bool(spec) and is_physical(str(spec.get("type", "")).lower())

    def rail_node(self, net: str, half: str, col: int) -> str | None:
        rows = self.rails.get(net)
        if not rows:
            return None
        side = "T" if half == "hi" else "B"
        row = next((r for r in rows if r.startswith(side)), rows[0])
        if self.split:
            return f"{row}:{'L' if col <= self.cols // 2 else 'R'}"
        return row

    # ── placing ──────────────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        todo = [cid for cid, spec in self.specs.items()
                if cid not in self.plan["parts"]
                and is_physical(str(spec.get("type", "")).lower())]
        rigid, legs = [], []
        for cid in todo:
            if str(self.specs[cid].get("type", "")).lower() in OFFBOARD_TYPES:
                legs.append(cid)
                continue
            fp, errors = footprint(self.specs[cid], self.comps[cid])
            if fp is None:
                self.failed.append(f"{cid}: {errors[0]}")
            elif fp.kind == RIGID:
                rigid.append(cid)
            else:
                legs.append(cid)
        self._place_rigid_parts(rigid)
        self._rail_wires()
        self._place_leg_parts(legs)
        self._join_islands()
        return self.plan

    def _order(self, ids: list[str]) -> list[str]:
        order = {cid: i for i, cid in enumerate(self.specs)}
        groups: dict[str, int] = {}
        for cid in self.specs:
            groups.setdefault(str(self.specs[cid].get("group", "")), len(groups))

        def key(cid: str) -> tuple:
            spec = self.specs[cid]
            board = str(spec.get("type", "")).lower() == "pico"
            return (not board, groups[str(spec.get("group", ""))], order[cid])
        return sorted(ids, key=key)

    def _attached(self, cid: str) -> int:
        """Leaded parts that share a (non-rail) net with this part."""
        nets = {self.net_of[f"{cid}.{p.name}"] for p in self.comps[cid].pins
                if f"{cid}.{p.name}" in self.net_of} - set(self.rails)
        others = set()
        for ref, net in self.net_of.items():
            if net in nets:
                other = ref.split(".", 1)[0]
                if other != cid:
                    others.add(other)
        return len(others)

    def _place_rigid_parts(self, ids: list[str]) -> None:
        taken = [parse_hole(h, self.cols).col for h in self.used
                 if parse_hole(h, self.cols) and not parse_hole(h, self.cols).rail]
        cursor = max(taken) + 3 if taken else None
        for cid in self._order(ids):
            fp, _ = footprint(self.specs[cid], self.comps[cid])
            row = "c" if fp.package == "Pico" else "e"
            width = max(dx for dx, _ in fp.offsets.values()) + 1
            gap = min(8, 2 + self._attached(cid) // 2)
            if cursor is None:
                # A board hangs its USB end over the edge; a chip gets room on
                # its left for the parts that go there.
                cursor = 2 if fp.package == "Pico" else 1 + min(gap, 5)
            placed = False
            # First try right of what is there, then any gap on the board.
            for start in list(range(cursor, self.cols + 1)) + list(range(1, cursor)):
                holes = self._rigid_holes(fp, row, start)
                if holes is None:
                    continue
                self.plan["parts"][cid] = {"anchor": f"{row}{start}", "rotation": 0}
                for num, h in holes.items():
                    name = fp.name_of(num)
                    self._take(h, self.net_of.get(f"{cid}.{name}") if name else None)
                    if fp.package == "Pico":
                        self.board_nodes.add(self.node(h))
                self._note_chip(list(holes.values()))
                for h in covered_holes([parse_hole(h, self.cols)
                                        for h in holes.values()]):
                    self.used.add(h.name)
                self.log.append(f"placed {cid} with pin 1 in {row}{start}")
                cursor = start + width + gap
                placed = True
                break
            if not placed:
                self.failed.append(f"{cid}: no room left on a {self.cols}-column board")

    def _rigid_holes(self, fp, row: str, start: int) -> dict[int, str] | None:
        out = {}
        y0 = ROW_Y[row]
        yrow = {y: r for r, y in ROW_Y.items()}
        for num, (dx, dy) in fp.offsets.items():
            r, c = yrow.get(y0 + dy), start + dx
            if r is None or not 1 <= c <= self.cols:
                return None
            h = f"{r}{c}"
            if h in self.used or f"{c}{_half(r)}" in self.owner:
                return None
            out[num] = h
        # Keep one empty column either side, so neighbours do not touch.
        cols = {parse_hole(h, self.cols).col for h in out.values()}
        halves = {_half(parse_hole(h, self.cols).row) for h in out.values()}
        for c in (min(cols) - 1, max(cols) + 1):
            if any(f"{c}{hf}" in self.owner for hf in halves):
                return None
        return out

    def _rail_wires(self) -> None:
        """Chip pins on a power net get a short wire to their rail."""
        for node, net in list(self.owner.items()):
            if net not in self.rails or node[-2:] not in ("lo", "hi"):
                continue
            col, half = int(node[:-2]), node[-2:]
            rail = self.rail_node(net, half, col)
            if rail is None or self._joined(node, rail):
                continue
            self._wire(node, rail, col)

    def _joined(self, a: str, b: str) -> bool:
        bb = Breadboard(self.plan, self.circuit)
        return bb._uf.find(a) == bb._uf.find(b)

    def _wire(self, a: str, b: str, near: int) -> bool:
        ha = self.free_holes(a, near, _WIRE_ROWS)
        if not ha:
            return False
        ca = parse_hole(ha[0], self.cols).col
        hb = self.free_holes(b, ca, _WIRE_ROWS)
        if not hb:
            return False
        self.plan["wires"].append({"from": ha[0], "to": hb[0]})
        self.used.update((ha[0], hb[0]))
        return True

    def _place_leg_parts(self, ids: list[str]) -> None:
        pending = list(self._order(ids))
        while pending:
            # Parts that already have somewhere to go first: keeps things local.
            pending.sort(key=lambda cid: -sum(
                1 for p in self.comps[cid].pins
                if self._net_nodes(self.net_of.get(f"{cid}.{p.name}"))))
            cid = pending.pop(0)
            ctype = str(self.specs[cid].get("type", "")).lower()
            names = [p.name for p in self.comps[cid].pins]
            if ctype in OFFBOARD_TYPES:
                self._place_offboard(cid, names)
            elif len(names) == 2:
                self._place_two(cid, names)
            else:
                self._place_row(cid, names)

    def _net_nodes(self, net: str | None) -> list[str]:
        if not net:
            return []
        nodes = self.nodes_of(net)
        if net in self.rails:
            for row in self.rails[net]:
                if self.split:
                    nodes += [f"{row}:L", f"{row}:R"]
                else:
                    nodes.append(row)
        return nodes

    def _anchor_col(self) -> int:
        cols = [parse_hole(h, self.cols).col for h in self.used
                if not parse_hole(h, self.cols).rail]
        return max(cols) + 2 if cols else 3

    def _place_two(self, cid: str, names: list[str]) -> None:
        nets = [self.net_of.get(f"{cid}.{n}") for n in names]
        nodes = [self._net_nodes(n) for n in nets]
        best = None
        for na in nodes[0]:
            for nb in nodes[1]:
                if na == nb:
                    continue
                ca = self.col_of(na, self.col_of(nb, 0))
                cb = self.col_of(nb, ca)
                if na[-2:] not in ("lo", "hi"):
                    ca = cb
                cost = link_cost(na, nb, ca, cb)
                # Parts gather round the chips rather than the board's pins.
                cost += 3 * ((na in self.board_nodes) + (nb in self.board_nodes))
                if na[-2:] not in ("lo", "hi") and nb[-2:] not in ("lo", "hi"):
                    continue    # rail to rail: a jumper does that better
                if best is None or cost < best[0]:
                    best = (cost, na, nb)
        if best and best[0] <= MAX_BRIDGE and self._legs(cid, names, [best[1], best[2]]):
            return
        # Give the net(s) without a good spot a fresh column next to the other.
        anchor = next((n for n in nodes[0] + nodes[1] if n[-2:] in ("lo", "hi")), None)
        if anchor is None:
            col, half = self._anchor_col(), "lo"
        else:
            col, half = int(anchor[:-2]), anchor[-2:]
        # The net that already has a column keeps it; the other one gets a
        # fresh column the part's own length away, so leads do not bunch up.
        span = SPAN.get(str(self.specs[cid].get("type", "")).lower(), 3)
        first = 0 if any(n[-2:] in ("lo", "hi") for n in nodes[0]) or not nodes[1] else 1
        a = self._pick(nets[first], nodes[first], col, half, set())
        acol = self.col_of(a, col) if a else col
        b = self._pick(nets[1 - first], nodes[1 - first],
                       acol + span * self._outward(a), half, {a} if a else set())
        if first:
            a, b = b, a
        if not (a and b and self._legs(cid, names, [a, b])):
            self.failed.append(f"{cid}: no free holes for it")

    def _pick(self, net: str | None, nodes: list[str], col: int, half: str,
              avoid: set[str]) -> str | None:
        """A node for one lead: the net's own nearby, else a fresh column."""
        near = [n for n in nodes if n not in avoid and n[-2:] == half
                and abs(int(n[:-2]) - col) <= 2
                and self.free_holes(n, col, _LEAD_ROWS)]
        if near:
            return near[0]
        rails = [n for n in nodes if n[-2:] not in ("lo", "hi") and n not in avoid]
        side = "T" if half == "hi" else "B"
        rails.sort(key=lambda n: not n.startswith(side))
        if rails:
            return rails[0]
        return self.free_column(col, half, avoid)

    def _legs(self, cid: str, names: list[str], nodes: list[str]) -> bool:
        cols = [self.col_of(n, 0) for n in nodes]
        ref_col = next((c for c in cols if c), self._anchor_col())
        holes: list[str] = []
        # Same row for both leads where possible: tidier.
        halves = [n[-2:] for n in nodes]
        if len(nodes) == 2 and halves[0] == halves[1] and halves[0] in ("lo", "hi"):
            lo, hi = sorted(cols)
            for r in _LEAD_ROWS[halves[0]]:
                pair = [f"{r}{cols[0]}", f"{r}{cols[1]}"]
                # Two parts lying in one row over the same columns would sit
                # on top of each other.
                clash = any(not (hi < a or lo > b) for a, b in self.segments.get(r, []))
                if not clash and not any(h in self.used for h in pair):
                    holes = pair
                    self.segments.setdefault(r, []).append((lo, hi))
                    break
        if not holes:
            for n, c in zip(nodes, cols):
                other = next((x for x in cols if x and x != c), ref_col)
                free = [h for h in self.free_holes(n, other if not c else c, _LEAD_ROWS)
                        if h not in holes]
                if not free:
                    return False
                holes.append(free[0])
        self.plan["parts"][cid] = {"legs": dict(zip(names, holes))}
        for name, h in zip(names, holes):
            self._take(h, self.net_of.get(f"{cid}.{name}"))
        self.log.append(f"placed {cid} in {', '.join(holes)}")
        return True

    def _place_row(self, cid: str, names: list[str]) -> None:
        """A part with 1 or 3+ bendable leads: side by side in free columns."""
        col = self._anchor_col()
        for start in range(col, self.cols + 1):
            nodes = [f"{start + i}lo" for i in range(len(names))]
            if all(n not in self.owner and start + i <= self.cols
                   for i, n in enumerate(nodes)):
                if self._legs(cid, names, nodes):
                    return
        self.failed.append(f"{cid}: no free columns for its {len(names)} leads")

    def _place_offboard(self, cid: str, names: list[str]) -> None:
        legs = {}
        for name in names:
            net = self.net_of.get(f"{cid}.{name}")
            nodes = [n for n in self._net_nodes(net) if n[-2:] in ("lo", "hi")]
            node = (nodes[0] if nodes else None) or self.free_column(self._anchor_col(), "lo")
            free = self.free_holes(node, 0, _WIRE_ROWS) if node else []
            if not free:
                self.failed.append(f"{cid}.{name}: no free hole for its lead")
                continue
            legs[name] = free[0]
            self._take(free[0], net)
        if legs:
            self.plan["parts"][cid] = {"offboard": True, "legs": legs}
            self.log.append(f"{cid} off the board, leads in {', '.join(legs.values())}")

    # ── joining ──────────────────────────────────────────────────────────────

    def _join_islands(self) -> None:
        """Jumpers for every net the board does not hold together yet."""
        for _ in range(200):       # each round adds one wire
            bb = Breadboard(self.plan, self.circuit)
            opens = [f for f in bb.findings if f["kind"] == "open"]
            if not opens:
                return
            if not self._bridge_one(bb):
                for f in opens:
                    self.failed.append(f["message"])
                return

    def _bridge_one(self, bb: Breadboard) -> bool:
        islands: dict[str, dict[str, set[str]]] = {}
        for part in bb.parts.values():
            for p in part.pins:
                net = self.net_of.get(p.ref or "")
                if net:
                    islands.setdefault(net, {}).setdefault(bb.root(p.hole), set()).add(
                        bb.node_key(p.hole))
        # A rail in use belongs to its net's islands too — an unused one does
        # not, or it would get a jumper for nothing.
        for h in {parse_hole(x, self.cols) for x in self.used}:
            net = next((n for n, rows in self.rails.items() if h.row in rows), None)
            if h.rail and net in islands:
                islands[net].setdefault(bb.root(h), set()).add(bb.node_key(h))
        for net, by_root in islands.items():
            if len(by_root) < 2:
                continue
            groups = list(by_root.values())
            best = None
            for i, a in enumerate(groups):
                for b in groups[i + 1:]:
                    for na in a:
                        for nb in b:
                            # Rails meet right next to the circuit, not at
                            # the far end of the board.
                            edge = min(self.cols, self._anchor_col())
                            ca, cb = self.col_of(na, self.col_of(nb, edge)), \
                                self.col_of(nb, self.col_of(na, edge))
                            cost = link_cost(na, nb, ca, cb)
                            if best is None or cost < best[0]:
                                best = (cost, na, nb, ca)
            if best and self._wire(best[1], best[2], best[3]):
                self.log.append(f"wire for net {net}")
                return True
        return False


def autoplace(circuit: dict[str, Any], plan: dict[str, Any] | None,
              mode: str = "rest") -> tuple[dict[str, Any], list[str], list[str]]:
    """Return the new plan, what was done, and what could not be done."""
    board = (plan or {}).get("board") or {}
    placer = Placer(circuit, plan, mode)
    new = placer.run()
    if mode == "all" and "columns" not in board:
        # No size chosen yet: laid out on a full board, a small circuit that
        # uses only the left part of it goes on a half-size board instead —
        # with room to spare, or it would be cramped.
        widest = max((parse_hole(h, placer.cols).col for h in placer.used), default=0)
        if widest <= 28:
            small = copy.deepcopy(new)
            small["board"] = {**small.get("board", {}), "columns": 30}
            same = [f["message"] for f in Breadboard(small, circuit).findings] == \
                [f["message"] for f in Breadboard(new, circuit).findings]
            if same:
                new = small
    if isinstance(plan, dict) and plan.get(HUMAN_EDIT) and mode == "rest":
        new[HUMAN_EDIT] = plan[HUMAN_EDIT]
    return new, placer.log, placer.failed
