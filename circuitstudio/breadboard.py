"""Breadboard plan: which lead sits in which hole — and does that match the netlist.

`<name>.breadboard.json` sits next to the circuit and layout:

    {
      "board": {"columns": 63, "split_rails": false},
      "parts": {
        "U1": {"anchor": "e30", "rotation": 0},        rigid: pin 1 + turn
        "R1": {"legs": {"1": "d31", "2": "d35"}},      leads one by one
        "ANT1": {"offboard": true, "legs": {"1": "a33"}}
      },
      "wires": [{"from": "j30", "to": "T+30", "color": "red"}],
      "edited_by_human": "2026-09-30T18:00:00+02:00"      set by the editor
    }

Holes are named like on the board: a1…j63 for the terminal strips, and
T+ / T- / B- / B+ plus a column number for the rails.

A breadboard is electrically simple — every half column is one node, every
rail is one node (or two, on boards with split rails) — so the netlist the
plan *actually* builds can be worked out and compared with circuit.json. That
comparison is the point of this module: a short, an open connection or an
unused pin sitting in a live column is found before anything is plugged in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .footprints import LEGS, RIGID, Footprint, footprint, is_physical
from .library import expand
from .registry import build_component
from .symbols import Component

DEFAULT_COLUMNS = 63            # full-size 830-hole board
MIN_COLUMNS, MAX_COLUMNS = 10, 100

# Vertical position of each terminal row, in pitches. The centre channel is
# 0.3" wide, so rows e and f are three pitches apart — exactly a DIP's width.
ROW_Y = {"j": 0, "i": 1, "h": 2, "g": 3, "f": 4,
         "e": 7, "d": 8, "c": 9, "b": 10, "a": 11}
Y_ROW = {y: r for r, y in ROW_Y.items()}
RAILS = ("T+", "T-", "B-", "B+")
# Top to bottom — moving a leaded part by "one row" steps through this list.
ROW_ORDER = ["T+", "T-", "j", "i", "h", "g", "f", "e", "d", "c", "b", "a", "B-", "B+"]
HUMAN_EDIT = "edited_by_human"   # set once the human changed the plan in the editor
_RAIL_TEXT = {"T+": "top + rail", "T-": "top − rail",
              "B-": "bottom − rail", "B+": "bottom + rail"}

_TERMINAL = re.compile(r"^([a-jA-J])(\d{1,3})$")
_RAIL = re.compile(r"^([TtBb])([+-])(\d{1,3})$")
_COLOR = re.compile(r"^#?[A-Za-z0-9]{1,20}$")

STRUCTURE = "structure"     # the plan itself is broken — MCP refuses to write it
SHORT = "short"
OPEN = "open"
STRAY = "stray"
UNPLACED = "unplaced"
_ORDER = {STRUCTURE: 0, SHORT: 1, OPEN: 2, STRAY: 3, UNPLACED: 4}


@dataclass(frozen=True)
class Hole:
    name: str
    col: int
    row: str        # "a".."j", or a rail name

    @property
    def rail(self) -> bool:
        return self.row in RAILS


def parse_hole(text: Any, columns: int) -> Hole | None:
    s = str(text).strip()
    m = _TERMINAL.match(s)
    if m:
        row, col = m.group(1).lower(), int(m.group(2))
        return Hole(f"{row}{col}", col, row) if 1 <= col <= columns else None
    m = _RAIL.match(s)
    if m:
        row, col = m.group(1).upper() + m.group(2), int(m.group(3))
        return Hole(f"{row}{col}", col, row) if 1 <= col <= columns else None
    return None


def _rot(dx: int, dy: int, rot: int) -> tuple[int, int]:
    """Clockwise on screen (y points down), like symbols._rotate."""
    for _ in range(rot // 90):
        dx, dy = -dy, dx
    return dx, dy


@dataclass
class PlacedPin:
    cid: str
    label: str              # "U1 pin 7 (GND)" / "R1.2"
    ref: str | None         # "U1.GND" when the schematic uses this pin
    hole: Hole
    tie: str | None = None  # pins with the same tie are joined on the part
    number: int | None = None   # physical pin number (rigid parts)


@dataclass
class Part:
    cid: str
    ctype: str
    value: str
    comp: Component
    fp: Footprint | None
    offboard: bool = False
    anchor: Hole | None = None
    rotation: int = 0
    pins: list[PlacedPin] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # names without a hole
    covered: list[Hole] = field(default_factory=list)  # under the part's body


@dataclass
class Wire:
    a: Hole
    b: Hole
    color: str | None = None
    index: int = 0          # position in the plan's "wires" list


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


class Breadboard:
    """A parsed plan plus everything the check found out about it."""

    def __init__(self, data: dict[str, Any] | None, circuit: dict[str, Any]):
        self.exists = isinstance(data, dict)
        data = data if isinstance(data, dict) else {}
        self.circuit = circuit
        self.columns = DEFAULT_COLUMNS
        self.split_rails = False
        self.parts: dict[str, Part] = {}
        self.wires: list[Wire] = []
        self.findings: list[dict[str, Any]] = []
        self._uf = _UnionFind()
        self.net_of: dict[str, str] = {}
        for net in circuit.get("nets", []) or []:
            if isinstance(net, dict):
                for ref in net.get("pins", []) or []:
                    self.net_of.setdefault(str(ref), str(net.get("name", "")))

        self._specs: dict[str, dict[str, Any]] = {}
        self._comps: dict[str, Component] = {}
        for spec in circuit.get("components", []) or []:
            if not isinstance(spec, dict):
                continue
            try:
                comp = build_component(spec)
            except (ValueError, TypeError, AttributeError):
                continue        # reported by the circuit validation already
            self._specs[comp.comp_id] = expand(spec)   # a library part's value etc.
            self._comps[comp.comp_id] = comp

        if self.exists:
            self._parse(data)
            self._check()
        self.findings.sort(key=lambda f: _ORDER.get(f["kind"], 9))

    # ── findings ─────────────────────────────────────────────────────────────

    def _add(self, kind: str, message: str, holes: list[str] | None = None) -> None:
        self.findings.append({"kind": kind, "message": message,
                              "holes": sorted(set(holes or []))})

    @property
    def structure_errors(self) -> list[str]:
        return [f["message"] for f in self.findings if f["kind"] == STRUCTURE]

    # ── nodes ────────────────────────────────────────────────────────────────

    def node_key(self, hole: Hole) -> str:
        if hole.rail:
            if self.split_rails:
                return f"{hole.row}:{'L' if hole.col <= self.columns // 2 else 'R'}"
            return hole.row
        return f"{hole.col}{'lo' if hole.row in 'abcde' else 'hi'}"

    def describe_node(self, hole: Hole) -> str:
        if hole.rail:
            text = _RAIL_TEXT[hole.row]
            if self.split_rails:
                text += (" (left half)" if hole.col <= self.columns // 2
                         else " (right half)")
            return text
        return f"column {hole.col} ({'a–e' if hole.row in 'abcde' else 'f–j'})"

    def root(self, hole: Hole) -> str:
        return self._uf.find(self.node_key(hole))

    # ── parsing ──────────────────────────────────────────────────────────────

    def _parse(self, data: dict[str, Any]) -> None:
        board = data.get("board") or {}
        if not isinstance(board, dict):
            self._add(STRUCTURE, "'board' must be an object")
            board = {}
        cols = board.get("columns", DEFAULT_COLUMNS)
        if isinstance(cols, bool) or not isinstance(cols, int) \
                or not MIN_COLUMNS <= cols <= MAX_COLUMNS:
            self._add(STRUCTURE, f"'board.columns' must be a whole number between "
                                 f"{MIN_COLUMNS} and {MAX_COLUMNS} (63 = full size, "
                                 f"30 = half size)")
        else:
            self.columns = cols
        self.split_rails = bool(board.get("split_rails", False))

        parts = data.get("parts") or {}
        if not isinstance(parts, dict):
            self._add(STRUCTURE, "'parts' must be an object {part ID: placement}")
            parts = {}
        for cid, entry in parts.items():
            self._parse_part(str(cid), entry)

        wires = data.get("wires") or []
        if not isinstance(wires, list):
            self._add(STRUCTURE, "'wires' must be a list")
            wires = []
        for i, w in enumerate(wires, 1):
            self._parse_wire(i, w)

        self._check_occupancy()

    def _parse_part(self, cid: str, entry: Any) -> None:
        if cid not in self._comps:
            self._add(STRUCTURE, f"'{cid}' is on the breadboard but not in the circuit")
            return
        if not isinstance(entry, dict):
            self._add(STRUCTURE, f"'{cid}': placement must be an object")
            return
        spec, comp = self._specs[cid], self._comps[cid]
        ctype = str(spec.get("type", "")).lower()
        if not is_physical(ctype):
            self._add(STRUCTURE, f"'{cid}' is a net symbol ({ctype}), not a part. "
                                 f"Put its net on a rail with a wire instead.")
            return
        fp, fp_errors = footprint(spec, comp)
        part = Part(cid=cid, ctype=ctype, value=str(spec.get("value", "") or ""),
                    comp=comp, fp=fp, offboard=bool(entry.get("offboard", False)))

        if part.offboard or (fp is not None and fp.kind == LEGS):
            if "anchor" in entry and not part.offboard:
                self._add(STRUCTURE, f"'{cid}' ({ctype}) is placed lead by lead: "
                                     f"use \"legs\": {{pin: hole}}, not 'anchor'")
                return
            self._place_legs(part, entry)
        elif fp is None:
            for e in fp_errors:
                self._add(STRUCTURE, e)
            return
        else:
            if "legs" in entry:
                self._add(STRUCTURE, f"'{cid}' ({fp.package}) has fixed pin spacing: "
                                     f"place it with \"anchor\" (hole of pin 1) and "
                                     f"\"rotation\", or mark it \"offboard\": true")
                return
            if not self._place_rigid(part, entry):
                return
        self.parts[cid] = part

    def _place_legs(self, part: Part, entry: dict[str, Any]) -> None:
        legs = entry.get("legs") or {}
        names = [p.name for p in part.comp.pins]
        if not isinstance(legs, dict):
            self._add(STRUCTURE, f"'{part.cid}': 'legs' must be an object {{pin: hole}}")
            return
        numbers = part.fp.numbers if part.fp and part.fp.kind == RIGID else {}
        ties = part.fp.ties if part.fp else []
        for name, where in legs.items():
            name = str(name)
            if name not in names:
                self._add(STRUCTURE, f"'{part.cid}' has no pin '{name}'. "
                                     f"Available: {', '.join(names)}")
                continue
            hole = parse_hole(where, self.columns)
            if hole is None:
                self._add(STRUCTURE, self._bad_hole(f"'{part.cid}.{name}'", where))
                continue
            tie = None
            num = numbers.get(name)
            for k, group in enumerate(ties):
                if num in group:
                    tie = f"{part.cid}#{k}"
            part.pins.append(PlacedPin(part.cid, f"{part.cid}.{name}",
                                       f"{part.cid}.{name}", hole, tie))
        part.missing = [n for n in names if n not in legs]

    def _place_rigid(self, part: Part, entry: dict[str, Any]) -> bool:
        fp = part.fp
        assert fp is not None
        anchor = parse_hole(entry.get("anchor", ""), self.columns)
        if anchor is None or anchor.rail:
            self._add(STRUCTURE, self._bad_hole(f"'{part.cid}' anchor",
                                                entry.get("anchor"), terminal=True))
            return False
        rot = entry.get("rotation", 0)
        if rot not in (0, 90, 180, 270):
            self._add(STRUCTURE, f"'{part.cid}': rotation must be 0, 90, 180 or 270")
            return False
        part.anchor, part.rotation = anchor, rot
        off_board: list[str] = []
        for num in sorted(fp.offsets):
            dx, dy = _rot(*fp.offsets[num], rot)
            col, row = anchor.col + dx, Y_ROW.get(ROW_Y[anchor.row] + dy)
            if row is None or not 1 <= col <= self.columns:
                off_board.append(str(num))
                continue
            name = fp.name_of(num)
            tie = next((f"{part.cid}#{k}" for k, g in enumerate(fp.ties) if num in g),
                       None)
            part.pins.append(PlacedPin(
                part.cid, f"{part.cid} {fp.describe(num)}",
                f"{part.cid}.{name}" if name else None,
                Hole(f"{row}{col}", col, row), tie, num))
        if off_board:
            self._add(STRUCTURE,
                      f"'{part.cid}' ({fp.package}) with pin 1 at {anchor.name}, "
                      f"rotation {rot}: pin(s) {', '.join(off_board)} land off the "
                      f"terminal strips. A DIP straddles the centre channel with "
                      f"pin 1 in row e (rotation 0) or row f (rotation 180).")
            return False
        part.covered = covered_holes([p.hole for p in part.pins])
        return True

    def _parse_wire(self, i: int, w: Any) -> None:
        if not isinstance(w, dict):
            self._add(STRUCTURE, f"Wire {i} must be an object {{from, to}}")
            return
        a = parse_hole(w.get("from", ""), self.columns)
        b = parse_hole(w.get("to", ""), self.columns)
        if a is None or b is None:
            bad = "from" if a is None else "to"
            self._add(STRUCTURE, self._bad_hole(f"Wire {i} '{bad}'", w.get(bad)))
            return
        if a == b:
            self._add(STRUCTURE, f"Wire {i} starts and ends in {a.name}")
            return
        color = w.get("color")
        if color is not None and not (isinstance(color, str) and _COLOR.match(color)):
            self._add(STRUCTURE, f"Wire {i}: color must be a name or #hex, "
                                 f"got {color!r}")
            color = None
        self.wires.append(Wire(a, b, color, i - 1))

    def _bad_hole(self, what: str, value: Any, terminal: bool = False) -> str:
        where = ("a terminal hole a1…j" if terminal
                 else "a hole a1…j") + f"{self.columns}"
        if not terminal:
            where += f" or a rail hole T+1…B-{self.columns}"
        return f"{what}: {value!r} is not {where}"

    def _check_occupancy(self) -> None:
        """One lead per hole — a second one does not fit, physically."""
        owner: dict[str, str] = {}
        for part in self.parts.values():
            for p in part.pins:     # off-board leads end in a hole, too
                self._claim(owner, p.hole, p.label)
        for part in self.parts.values():
            for h in part.covered:
                self._claim(owner, h, f"the body of {part.cid}")
        for i, w in enumerate(self.wires, 1):
            self._claim(owner, w.a, f"wire {i}")
            self._claim(owner, w.b, f"wire {i}")

    def _claim(self, owner: dict[str, str], hole: Hole, who: str) -> None:
        if hole.name in owner:
            first = owner[hole.name]
            if who.startswith("the body of") or first.startswith("the body of"):
                body, lead = (who, first) if who.startswith("the body") else (first, who)
                msg = (f"Hole {hole.name} is covered by {body[len('the body of '):]} "
                       f"— {lead} cannot go in there.")
            else:
                msg = (f"Hole {hole.name} is used twice: {first} and {who}. Use "
                       f"another hole of the same column.")
            self._add(STRUCTURE, msg, [hole.name])
        else:
            owner[hole.name] = who

    # ── the check ────────────────────────────────────────────────────────────

    def _check(self) -> None:
        uf = self._uf
        for w in self.wires:
            uf.union(self.node_key(w.a), self.node_key(w.b))
        by_tie: dict[str, list[PlacedPin]] = {}
        for part in self.parts.values():
            for p in part.pins:
                uf.find(self.node_key(p.hole))
                if p.tie:
                    by_tie.setdefault(p.tie, []).append(p)
        for group in by_tie.values():
            for p in group[1:]:
                uf.union(self.node_key(group[0].hole), self.node_key(p.hole))

        pins_at: dict[str, list[PlacedPin]] = {}
        for part in self.parts.values():
            for p in part.pins:
                pins_at.setdefault(self.root(p.hole), []).append(p)

        self._check_shorts(pins_at)
        self._check_opens()
        self._check_strays(pins_at, by_tie)
        self._check_unplaced()

    def nets_in(self, pins: list[PlacedPin]) -> list[str]:
        return sorted({self.net_of[p.ref] for p in pins
                       if p.ref and p.ref in self.net_of})

    def _check_shorts(self, pins_at: dict[str, list[PlacedPin]]) -> None:
        for pins in pins_at.values():
            nets = self.nets_in(pins)
            if len(nets) < 2:
                continue
            live = [p for p in pins if p.ref in self.net_of]
            # Where do the nets actually touch? Usually one column holds leads
            # of both — that is the spot to fix, not the whole GND net.
            by_key: dict[str, list[PlacedPin]] = {}
            for p in live:
                by_key.setdefault(self.node_key(p.hole), []).append(p)
            spots = [ps for ps in by_key.values() if len(self.nets_in(ps)) > 1]
            if spots:
                where = "; ".join(
                    f"{self.describe_node(ps[0].hole)}: " + ", ".join(
                        f"{p.label} ({p.hole.name}, {self.net_of[p.ref]})" for p in ps)
                    for ps in spots)
                marks = [p.hole.name for ps in spots for p in ps]
            else:
                # Joined through wires or rails: name a few pins of each net.
                where = "via wires/rails — " + " | ".join(
                    f"{net}: " + _few([p for p in live if self.net_of[p.ref] == net])
                    for net in nets)
                counts = {n: sum(self.net_of[p.ref] == n for p in live) for n in nets}
                smallest = min(nets, key=lambda n: counts[n])
                marks = [p.hole.name for p in live if self.net_of[p.ref] == smallest]
            self._add(SHORT, f"Short between {' and '.join(nets)} — {where}", marks)

    def _check_opens(self) -> None:
        groups: dict[str, dict[str, list[PlacedPin]]] = {}
        for part in self.parts.values():
            for p in part.pins:
                net = self.net_of.get(p.ref or "")
                if net:
                    groups.setdefault(net, {}).setdefault(self.root(p.hole), []).append(p)
        for net, by_root in groups.items():
            if len(by_root) < 2:
                continue
            islands = " | ".join(", ".join(f"{p.label} ({p.hole.name})" for p in pins)
                                 for pins in by_root.values())
            self._add(OPEN, f"Open: net '{net}' falls apart into {len(by_root)} "
                            f"pieces on the board — {islands}",
                      [p.hole.name for pins in by_root.values() for p in pins])

    def _check_strays(self, pins_at: dict[str, list[PlacedPin]],
                      by_tie: dict[str, list[PlacedPin]]) -> None:
        """An unused pin that shares a node with a live net."""
        nc = _nc(self.circuit)
        for pins in pins_at.values():
            nets = self.nets_in(pins)
            if not nets:
                continue
            for p in pins:
                if p.ref and p.ref in self.net_of:
                    continue
                if p.tie and any(q.ref in self.net_of for q in by_tie[p.tie]):
                    continue    # joined on the part to a pin that is in use
                what = "marked no-connect" if p.ref in nc else "not in any net"
                self._add(STRAY, f"{p.label} is {what}, but sits in "
                                 f"{self.describe_node(p.hole)} with net "
                                 f"{', '.join(nets)} — it gets connected anyway. "
                                 f"Move the part or the other leads.",
                          [p.hole.name])

    def _check_unplaced(self) -> None:
        for cid, spec in self._specs.items():
            if not is_physical(str(spec.get("type", "")).lower()):
                continue
            part = self.parts.get(cid)
            if part is None:
                self._add(UNPLACED, f"{cid} is not on the breadboard yet")
                continue
            loose = [n for n in part.missing if f"{cid}.{n}" in self.net_of]
            if loose:
                self._add(UNPLACED, f"{cid}: " + ", ".join(
                    f"{n} (net {self.net_of[f'{cid}.{n}']})" for n in loose)
                    + " not plugged in anywhere")

    # ── for the editor ───────────────────────────────────────────────────────

    def used_holes(self) -> dict[str, dict[str, Any]]:
        """Who sits in which hole — the editor refuses drops onto these."""
        used: dict[str, dict[str, Any]] = {}
        for part in self.parts.values():
            for p in part.pins:
                pin = p.ref.split(".", 1)[1] if p.ref else None
                used[p.hole.name] = {"part": part.cid, "pin": pin}
        for part in self.parts.values():
            for h in part.covered:
                used.setdefault(h.name, {"part": part.cid, "pin": None, "covered": True})
        for w in self.wires:
            used.setdefault(w.a.name, {"wire": w.index, "end": "from"})
            used.setdefault(w.b.name, {"wire": w.index, "end": "to"})
        return used

    def unplaced(self) -> list[dict[str, Any]]:
        """Physical parts the plan does not place (yet), with what the editor
        needs to put them on the board — or why it cannot."""
        out = []
        for cid, spec in self._specs.items():
            ctype = str(spec.get("type", "")).lower()
            if cid in self.parts or not is_physical(ctype):
                continue
            comp = self._comps[cid]
            fp, errors = footprint(spec, comp)
            entry: dict[str, Any] = {
                "id": cid, "type": ctype, "value": str(spec.get("value", "") or ""),
                "pins": [p.name for p in comp.pins], "kind": LEGS,
                "reason": errors[0] if errors else None,
            }
            if fp is not None and fp.kind == RIGID:
                entry["kind"] = RIGID
                entry["package"] = fp.package
                entry["offsets"] = {str(n): list(o) for n, o in fp.offsets.items()}
            out.append(entry)
        return out

    # ── summary ──────────────────────────────────────────────────────────────

    def report(self) -> str:
        if not self.exists:
            return "No breadboard plan yet."
        placed = len(self.parts)
        head = (f"Breadboard: {self.columns} columns, {placed} part(s) placed, "
                f"{len(self.wires)} wire(s).")
        if not self.findings:
            return head + "\nThe plan matches the netlist — no shorts, no open " \
                          "connections, no stray pins."
        lines = [head, f"{len(self.findings)} finding(s):"]
        lines += [f"- {f['message']}" for f in self.findings]
        return "\n".join(lines)


def covered_holes(holes: list[Hole]) -> list[Hole]:
    """Terminal holes under a wide board's body (the Pico spans rows c-h and
    hides d-g). A DIP's rows are one channel apart, so it covers none."""
    ys = {ROW_Y[h.row] for h in holes if not h.rail}
    if len(ys) < 2 or max(ys) - min(ys) <= 3:
        return []
    cols = {h.col for h in holes}
    return [Hole(f"{row}{c}", c, row) for c in sorted(cols)
            for row, y in ROW_Y.items() if min(ys) < y < max(ys)]


def _few(pins: list[PlacedPin], n: int = 3) -> str:
    text = ", ".join(f"{p.label} ({p.hole.name})" for p in pins[:n])
    return text + (f" … (+{len(pins) - n})" if len(pins) > n else "")


def _nc(circuit: dict[str, Any]) -> set[str]:
    return {str(r) for r in circuit.get("nc", []) or []}
