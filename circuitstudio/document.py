"""The two-document model.

`<name>.circuit.json`  — logic only: components + nets. Owned by the LLM.
`<name>.layout.json`   — looks only: positions, rotation, view. Owned by the human.
`<name>.breadboard.json` — optional: which lead sits in which breadboard hole
                          (see breadboard.py). Written by the LLM for now.

Keeping them apart is what makes the workflow work: the LLM can rewrite the
circuit without destroying a hand-tuned layout, and dragging things around never
touches the electrical description.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import ties
from .blocks import INNER_GAP, Block, Pin, arrange as arrange_blocks
from .registry import build_component
from .symbols import Component

DEFAULT_GRID = 20
AUTO_STEP = 60   # cell size of the auto-placement grid; big parts take several
AUTO_GAP = 25    # breathing room around a symbol, in px
DEFAULT_NOTE_W = 220
REHOME_MIN = 100  # a pin this far from its GND/VCC/label deserves its own

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Net names that are treated as power rails
_VCC_NAMES = {"vcc", "vdd", "v+", "5v", "3v3", "3.3v", "9v", "12v", "24v", "vin", "vbat"}
_GND_NAMES = {"gnd", "vss", "v-", "0v", "agnd", "dgnd", "pgnd"}


# "3.3V", "1V8", "+12V", "5V_USB" — a bare voltage is a rail.
_VOLTAGE_NAME = re.compile(r"^\+?\d+([.,]\d+)?v\d*([_-].*)?$")


def is_supply_net(name: str) -> bool:
    n = name.lower().strip()
    return (n in _VCC_NAMES
            or n.startswith(("vcc", "vdd", "v+", "+", "5v", "3v", "vbus", "vsys"))
            or bool(_VOLTAGE_NAME.match(n)))


def is_ground_net(name: str) -> bool:
    n = name.lower().strip()
    return n in _GND_NAMES or n.startswith(("gnd", "vss"))


def net_color(name: str) -> str:
    if is_supply_net(name):
        return "#CC0000"
    if is_ground_net(name):
        return "#000000"
    return "#333333"


def net_width(name: str) -> float:
    if is_ground_net(name):
        return 2.5
    if is_supply_net(name):
        return 2.0
    return 1.5


def is_safe_name(name: str) -> bool:
    return bool(_SAFE_NAME.match(name))


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a JSON object at top level")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


class Project:
    def __init__(self, folder: Path, name: str):
        if not is_safe_name(name):
            raise ValueError(f"Invalid project name: {name!r}")
        self.folder = folder
        self.name = name
        self.circuit: dict[str, Any] = {"title": name, "components": [], "nets": []}
        self.layout: dict[str, Any] = {"grid": DEFAULT_GRID, "positions": {},
                                       "wires": {}, "view": None}
        self.version = 0
        self._circuit_mtime: float = 0.0
        self.breadboard: dict[str, Any] | None = None
        self.breadboard_error: str | None = None
        self._bb_mtime: float = 0.0
        self._backed_up = False
        self.routes_dirty = False    # set by Scene when it produced new wires

    # ── Paths ────────────────────────────────────────────────────────────────

    @property
    def circuit_path(self) -> Path:
        return self.folder / f"{self.name}.circuit.json"

    @property
    def layout_path(self) -> Path:
        return self.folder / f"{self.name}.layout.json"

    @property
    def layout_backup_path(self) -> Path:
        return self.folder / f"{self.name}.layout.bak.json"

    @property
    def breadboard_path(self) -> Path:
        return self.folder / f"{self.name}.breadboard.json"

    @property
    def breadboard_svg_path(self) -> Path:
        return self.folder / f"{self.name}.breadboard.svg"

    @property
    def svg_path(self) -> Path:
        return self.folder / f"{self.name}.svg"

    @property
    def png_path(self) -> Path:
        return self.folder / f"{self.name}.png"

    # ── Review ───────────────────────────────────────────────────────────────

    def layout_fingerprint(self) -> str:
        """Short digest of everything the human arranges.

        Lets `review` state say not just *that* the layout was signed off, but
        whether it has been touched since — a stale picture is worse than none.
        """
        payload = json.dumps(
            {"positions": self.layout.get("positions", {}),
             "wires": self.layout.get("wires", {}),
             "notes": self.layout.get("notes", {})},
            sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def mark_reviewed(self) -> dict[str, Any]:
        """Record that the arrangement is finished and ready to be handed back."""
        info = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "layout": self.layout_fingerprint(),
            "parts": len(self.circuit.get("components", [])),
        }
        self.layout["review"] = info
        self.version += 1
        return info

    def review_state(self) -> dict[str, Any]:
        info = self.layout.get("review")
        if not isinstance(info, dict):
            return {"reviewed": False, "current": False}
        return {
            "reviewed": True,
            "at": info.get("at", ""),
            "current": info.get("layout") == self.layout_fingerprint(),
        }

    # ── Load / save ──────────────────────────────────────────────────────────

    def load(self) -> "Project":
        circuit = _read_json(self.circuit_path)
        if circuit is not None:
            self.circuit = circuit
            self._circuit_mtime = self.circuit_path.stat().st_mtime
        self.circuit.setdefault("title", self.name)
        self.circuit.setdefault("components", [])
        self.circuit.setdefault("nets", [])
        self.circuit.setdefault("notes", [])
        self.circuit.setdefault("nc", [])
        self.load_breadboard()
        return self.load_layout()

    def load_breadboard(self) -> bool:
        """(Re)read breadboard.json. A broken file is reported, not fatal."""
        path = self.breadboard_path
        if not path.exists():
            changed = self.breadboard is not None or self.breadboard_error is not None
            self.breadboard, self.breadboard_error, self._bb_mtime = None, None, 0.0
            return changed
        mtime = path.stat().st_mtime
        if mtime == self._bb_mtime:
            return False
        try:
            self.breadboard = _read_json(path)
            self.breadboard_error = None
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            self.breadboard = None
            self.breadboard_error = f"{path.name} is not valid JSON: {exc}"
        self._bb_mtime = mtime
        return True

    def reload_breadboard_if_changed(self) -> bool:
        """Pick up a breadboard plan the LLM (re)wrote while the editor runs."""
        if self.load_breadboard():
            self.version += 1
            return True
        return False

    def save_breadboard(self, data: dict[str, Any]) -> None:
        _write_json(self.breadboard_path, data)
        self.breadboard = data
        self.breadboard_error = None
        self._bb_mtime = self.breadboard_path.stat().st_mtime

    def load_layout(self) -> "Project":
        """Load only layout.json (plus auto-placement for the current circuit)."""
        layout = _read_json(self.layout_path)
        if layout is not None:
            self.layout = layout
        self.layout.setdefault("grid", DEFAULT_GRID)
        self.layout.setdefault("showGrid", True)
        self.layout.setdefault("positions", {})
        self.layout.setdefault("wires", {})
        self.layout.setdefault("notes", {})
        self.layout.setdefault("routes", {})
        self.layout.setdefault("ties", {})
        self.layout.setdefault("view", None)

        self.autoplace()
        self.autoplace_notes()
        self.version += 1
        return self

    def reload_circuit_if_changed(self) -> bool:
        """Pick up out-of-band edits (LLM writing circuit.json). Layout survives."""
        if not self.circuit_path.exists():
            return False
        mtime = self.circuit_path.stat().st_mtime
        if mtime == self._circuit_mtime:
            return False
        try:
            circuit = _read_json(self.circuit_path)
        except (json.JSONDecodeError, ValueError):
            return False  # half-written file; try again next poll
        if circuit is None:
            return False
        self._circuit_mtime = mtime
        self.circuit = circuit
        self.circuit.setdefault("title", self.name)
        self.circuit.setdefault("components", [])
        self.circuit.setdefault("nets", [])
        self.circuit.setdefault("notes", [])
        self.circuit.setdefault("nc", [])
        self.autoplace()
        self.autoplace_notes()
        self.version += 1
        return True

    def save_layout(self, backup: bool = False) -> None:
        """Write layout.json, keeping a recoverable copy of an older state.

        The backup is taken on the first save of this session (i.e. the layout
        as it was when the editor was opened, or before the assistant wrote the
        circuit) and again whenever `backup` is set, e.g. right before an
        auto-arrange. Backing up on *every* save would be useless: panning
        alone saves the view, so the copy would be overwritten within a second
        of the mistake it is meant to undo.
        """
        if (backup or not self._backed_up) and self.layout_path.exists():
            try:
                self.layout_backup_path.write_bytes(self.layout_path.read_bytes())
                self._backed_up = True
            except OSError:
                pass
        _write_json(self.layout_path, self.layout)

    def save_routes(self) -> None:
        """Persist wires a Scene just (re)computed, so they are reused next time."""
        if self.routes_dirty:
            self.routes_dirty = False
            self.save_layout()

    def clear_routes(self) -> None:
        """Forget all stored wires; the next Scene routes everything afresh."""
        self.layout["routes"] = {}
        self._route_cache = None
        self.version += 1

    def save_circuit(self) -> None:
        _write_json(self.circuit_path, self.circuit)
        self._circuit_mtime = self.circuit_path.stat().st_mtime

    # ── Placement ────────────────────────────────────────────────────────────

    def _symbol_ids(self) -> set[str]:
        return {str(s.get("id", "")) for s in self.circuit.get("components", [])
                if str(s.get("type", "")).lower() in ties.TIE_TYPES}

    def _adjacency(self) -> dict[str, set[str]]:
        """Which real parts should sit near each other.

        Net symbols are left out (they get hung onto their pins afterwards),
        and so are the power rails: in GND every part touches every other one,
        which pulled the whole sheet into one clump. A part that only hangs on
        rails — a decoupling cap — still gets its rail neighbours as a fallback.
        """
        symbols = self._symbol_ids()
        signal: dict[str, set[str]] = {}
        power: dict[str, set[str]] = {}
        for net in self.circuit.get("nets", []):
            name = str(net.get("name", ""))
            target = power if is_ground_net(name) or is_supply_net(name) else signal
            comp_ids = [str(p).split(".")[0] for p in net.get("pins", [])]
            comp_ids = [c for c in comp_ids if c not in symbols]
            for i, a in enumerate(comp_ids):
                for b in comp_ids[i + 1:]:
                    if a != b:
                        target.setdefault(a, set()).add(b)
                        target.setdefault(b, set()).add(a)
        for cid, nbrs in power.items():
            if cid not in signal:
                signal[cid] = nbrs
        return signal

    def autoplace(self) -> list[str]:
        """Give every component without a stored position a rough spot.

        Not a real placer — the whole point of CircuitStudio is that a human
        does the arranging. It only has to produce something readable enough to
        start dragging from, which means two things above all: nothing overlaps,
        and connected parts end up near each other.

        Stale entries for deleted components are deliberately kept — if the LLM
        re-adds the same ID later it lands back where you put it.
        """
        placed = self._place_new()
        self.rehome_ties()
        return placed

    def _place_new(self) -> list[str]:
        positions: dict[str, Any] = self.layout["positions"]
        specs = {str(s.get("id", "")): s for s in self.circuit.get("components", [])
                 if str(s.get("id", ""))}
        ids = list(specs)
        unplaced = [i for i in ids if i not in positions]
        if not unplaced:
            return []

        footprints = {cid: self._footprint_cells(specs[cid]) for cid in ids}
        occupied: set[tuple[int, int]] = set()
        for cid in ids:
            p = positions.get(cid)
            if p:
                self._occupy(occupied, self._to_cell(p), footprints[cid])

        adj = self._adjacency()
        symbols = self._symbol_ids()

        parts = [c for c in unplaced if c not in symbols]
        blocks = self._group_blocks(parts, specs)
        if blocks:
            self._place_blocks(blocks, specs, adj, footprints)
            for cid in (c for members in blocks for c in members):
                self._occupy(occupied, self._to_cell(positions[cid]), footprints[cid])
            parts = [c for c in parts if c not in positions]
        self._grid_place(parts, adj, positions, occupied, footprints)
        # Symbols that found no pin to sit on (more symbols than pins).
        leftovers = self._attach_symbols([c for c in unplaced if c in symbols])
        self._grid_place(leftovers, adj, positions, occupied, footprints,
                         home=self._tie_homes(specs))
        return unplaced

    def _grid_place(self, ids: list[str], adj: dict[str, set[str]],
                    positions: dict[str, Any], occupied: set[tuple[int, int]],
                    footprints: dict[str, tuple[int, int]],
                    home: Any = None) -> None:
        """Put each part at the centre of its placed neighbours (or at
        `home(cid, positions)` if that names a spot), else the nearest free
        cell to it."""
        for cid in self._placement_order(ids, adj, positions):
            anchors = [positions[n] for n in adj.get(cid, ()) if n in positions]
            spot = home(cid, positions) if home else None
            if spot is not None:
                anchors = [spot]
            if anchors:
                ax = sum(a.get("x", 0) for a in anchors) / len(anchors)
                ay = sum(a.get("y", 0) for a in anchors) / len(anchors)
                cell = (round(ax / AUTO_STEP), round(ay / AUTO_STEP))
            else:
                cell = (0, 0)
            cell = self._free_cell(occupied, cell, footprints[cid])
            self._occupy(occupied, cell, footprints[cid])
            positions[cid] = {
                "x": cell[0] * AUTO_STEP,
                "y": cell[1] * AUTO_STEP,
                "rotation": 0,
                "auto": True,
            }

    @classmethod
    def _free_cell(cls, occupied: set[tuple[int, int]], cell: tuple[int, int],
                   size: tuple[int, int]) -> tuple[int, int]:
        if not cls._collides(occupied, cell, size):
            return cell
        for radius in range(1, 60):
            for dr in range(-radius, radius + 1):
                for dc in range(-radius, radius + 1):
                    if abs(dr) != radius and abs(dc) != radius:
                        continue
                    cand = (cell[0] + dc, cell[1] + dr)
                    if not cls._collides(occupied, cand, size):
                        return cand
        return (cell[0] + 2 * len(occupied) + 1, cell[1])

    # ── Groups ───────────────────────────────────────────────────────────────

    def _group_blocks(self, parts: list[str],
                      specs: dict[str, dict[str, Any]]) -> list[list[str]]:
        """The groups to lay out as blocks, plus every loose part as a block
        of its own.

        A group of which some part already has a position is not a block: its
        new parts simply join the others via the normal placement. A circuit
        without any groups that is arranged from scratch counts as one group —
        the block layout beats the grid placer there too. Empty when neither
        applies (a few new parts next to placed ones).
        """
        symbols = self._symbol_ids()
        groups: dict[str, list[str]] = {}
        for cid, spec in specs.items():
            name = spec.get("group")
            if isinstance(name, str) and name.strip() and cid not in symbols:
                groups.setdefault(name.strip(), []).append(cid)
        todo = set(parts)
        if not groups:
            real = [c for c in specs if c not in symbols]
            fresh = len(real) > 1 and all(c in todo for c in real)
            return [real] if fresh else []
        blocks = [m for m in groups.values() if all(c in todo for c in m)]
        if not blocks:
            return []
        grouped = {c for m in groups.values() for c in m}
        return blocks + [[c] for c in parts if c not in grouped]

    def _place_blocks(self, blocks: list[list[str]], specs: dict[str, dict[str, Any]],
                      adj: dict[str, set[str]],
                      footprints: dict[str, tuple[int, int]]) -> None:
        """Lay out each block on its own, then put the blocks side by side."""
        positions: dict[str, Any] = self.layout["positions"]
        symbols = self._symbol_ids()
        pin_net: dict[str, str] = {}
        rails: set[str] = set()
        for net in self.circuit.get("nets", []):
            name = str(net.get("name", ""))
            if is_ground_net(name) or is_supply_net(name):
                rails.add(name)
            for ref in net.get("pins", []):
                pin_net[str(ref)] = name

        # Room a net label will need once it is hung onto a pin: its tag plus
        # the stub. (Ground and supply symbols are small and find a gap of
        # their own; reserving room for them only spreads the blocks apart.)
        reserve: dict[str, float] = {}
        by_id = {str(s.get("id", "")): s for s in self.circuit.get("components", [])}
        for net in self.circuit.get("nets", []):
            name = str(net.get("name", ""))
            for ref in net.get("pins", []):
                spec = by_id.get(str(ref).split(".")[0], {})
                if str(spec.get("type", "")).lower() == "label":
                    text = str(spec.get("value") or spec.get("id", ""))
                    need = max(len(text) * 7 + 16, 44) + 50.0
                    reserve[name] = max(reserve.get(name, 0.0), need)

        def drawn_box(comp: Component) -> tuple[float, float, float, float]:
            boxes = [comp.extent_bbox(), *comp.label_boxes()]
            for p in comp.pins:
                need = reserve.get(pin_net.get(f"{comp.comp_id}.{p.name}", ""))
                if need:
                    x, y = comp.abs_pin_pos(p.name)
                    dx, dy = _outward(comp, x, y)
                    boxes.append((min(x, x + dx * need), min(y, y + dy * need),
                                  max(x, x + dx * need), max(y, y + dy * need)))
            return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes))

        def pins_of(comp: Component) -> list[Pin]:
            out = []
            for p in comp.pins:
                net = pin_net.get(f"{comp.comp_id}.{p.name}")
                if net is not None:
                    x, y = comp.abs_pin_pos(p.name)
                    out.append(Pin(net, x, y, *_outward(comp, x, y)))
            return out

        fixed, _ = self.instantiate()
        fixed = [c for c in fixed if c.comp_id in positions]
        fixed_boxes = [drawn_box(c) for c in fixed]
        fixed_pins = [pin for c in fixed if c.comp_id not in symbols
                      for pin in pins_of(c)]

        local_layouts: list[dict[str, Any]] = []
        shapes: list[Block] = []
        for members in blocks:
            # Inside the block every part is a little block of its own.
            comps: list[Component] = []
            for cid in members:
                try:
                    comps.append(build_component(specs[cid]))
                except (ValueError, KeyError, TypeError):
                    pass
            parts = [Block(box=drawn_box(c), pins=pins_of(c)) for c in comps]
            inner = arrange_blocks(parts, [], [], rails, gap=INNER_GAP)
            local: dict[str, Any] = {cid: {"x": 0.0, "y": 0.0, "rotation": 0,
                                           "auto": True} for cid in members}
            boxes, pins = [], []
            for comp, (ox, oy) in zip(comps, inner):
                comp.x, comp.y = ox, oy
                local[comp.comp_id].update(x=ox, y=oy)
                boxes.append(drawn_box(comp))
                pins += pins_of(comp)
            box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                   max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes \
                else (-30.0, -30.0, 30.0, 30.0)
            local_layouts.append(local)
            shapes.append(Block(box=box, pins=pins))

        offsets = arrange_blocks(shapes, fixed_boxes, fixed_pins, rails)
        for local, (ox, oy) in zip(local_layouts, offsets):
            for cid, p in local.items():
                p["x"] = round(p["x"] + ox, 1)
                p["y"] = round(p["y"] + oy, 1)
                positions[cid] = p

    def _attach_symbols(self, todo: list[str]) -> list[str]:
        """Hang each new ground/supply/label symbol right onto one of its pins.

        Symbols connect by name, so their job is to save wires. Per net, the
        next symbol goes to the pin furthest from the symbols already there —
        the pin that would otherwise need the longest wire. Pins that already
        have a symbol right next to them are skipped.

        Returns the symbols that found no pin.
        """
        if not todo:
            return []
        positions: dict[str, Any] = self.layout["positions"]
        comps, _ = self.instantiate()
        by_id = {c.comp_id: c for c in comps}
        symbols = self._symbol_ids()
        pending = set(todo)
        # What a symbol must not land on: every part that already has a spot.
        placed = [c for c in comps if c.comp_id in positions and c.comp_id not in pending]

        for net in self.circuit.get("nets", []):
            refs = [str(r) for r in net.get("pins", [])]
            new_syms = [r.split(".")[0] for r in refs if r.split(".")[0] in pending]
            if not new_syms:
                continue
            anchors: list[tuple[float, float]] = []   # pins that have a symbol
            pins: list[tuple[str, tuple[float, float]]] = []
            for ref in refs:
                cid, _, pin = ref.partition(".")
                comp = by_id.get(cid)
                if comp is None or cid in pending or cid not in positions:
                    continue
                try:
                    pos = comp.abs_pin_pos(pin)
                except KeyError:
                    continue
                if cid in symbols:
                    anchors.append(pos)
                else:
                    pins.append((ref, pos))
            free = [(r, q) for r, q in pins
                    if all(abs(q[0] - a[0]) + abs(q[1] - a[1]) > 80 for a in anchors)]
            for sid in new_syms:
                if not free:
                    break
                pick = max(free, key=lambda f: min(
                    (abs(f[1][0] - a[0]) + abs(f[1][1] - a[1]) for a in anchors),
                    default=0.0))
                free.remove(pick)
                ref, pin_pos = pick
                sym = by_id[sid]
                self._park_tie(sym, by_id[ref.split(".")[0]], pin_pos, placed)
                positions[sid] = {"x": sym.x, "y": sym.y, "rotation": sym.rotation,
                                  "flip": sym.flip, "auto": True}
                placed.append(sym)
                anchors.append(pin_pos)
                pending.discard(sid)
        return [c for c in todo if c in pending]

    # ── Auto-placement helpers ───────────────────────────────────────────────

    def rehome_ties(self) -> list[str]:
        """Move idle GND/VCC/label symbols to the pins that need them.

        A symbol serves only the pins nearest to it, so one that is nobody's
        nearest hangs in the air while some pin runs a long wire to a symbol
        far away. Symbols the human has never touched (still "auto") are moved
        next to the worst-served pin until no pin is far from its symbol or no
        idle symbol is left. Anything the human placed stays where it is.
        """
        positions: dict[str, Any] = self.layout["positions"]
        overrides = self.layout.get("ties") or {}
        comps, _ = self.instantiate()
        by_id = {c.comp_id: c for c in comps}

        def type_of(ref: str) -> str:
            return by_id[ref.split(".", 1)[0]].comp_type

        moved: list[str] = []
        for net in self.circuit.get("nets", []):
            refs: list[str] = []
            pts: list[tuple[float, float]] = []
            for ref in dict.fromkeys(str(r) for r in net.get("pins", [])):
                cid, _, pin = ref.partition(".")
                comp = by_id.get(cid)
                try:
                    pts.append(comp.abs_pin_pos(pin))  # type: ignore[union-attr]
                except (AttributeError, KeyError):
                    continue
                refs.append(ref)

            for _ in range(len(refs)):
                groups = ties.assign(refs, pts, type_of, overrides)
                if groups is None:
                    break
                def movable(t: int) -> bool:
                    return bool(positions.get(refs[t].split(".", 1)[0], {}).get("auto"))

                far = [(ties.distance(pts[t], pts[k]), k, t)
                       for t, members in groups.items() for k in members[1:]
                       if refs[k] not in overrides]
                if not far:
                    break
                gap, k, served_by = max(far)
                if gap <= REHOME_MIN:
                    break
                # An idle symbol first; failing that, the far pin's own symbol
                # if everything it serves is far away (moving it strands nobody).
                idle = [t for t, members in groups.items()
                        if len(members) == 1 and movable(t)]
                if idle:
                    t = idle[0]
                elif movable(served_by) and all(
                        ties.distance(pts[served_by], pts[m]) > REHOME_MIN
                        for m in groups[served_by][1:]):
                    t = served_by
                else:
                    break
                sym = by_id[refs[t].split(".", 1)[0]]
                owner = by_id[refs[k].split(".", 1)[0]]
                self._park_tie(sym, owner, pts[k], comps)
                pts[t] = sym.abs_pin_pos(refs[t].split(".", 1)[1])
                positions[sym.comp_id] = {
                    "x": sym.x, "y": sym.y, "rotation": sym.rotation,
                    "flip": sym.flip, "auto": True,
                }
                moved.append(sym.comp_id)
        if moved:
            self.version += 1
        return moved

    @staticmethod
    def _park_tie(sym: Component, owner: Component, pin: tuple[float, float],
                  comps: list[Component]) -> None:
        """Put `sym` just outside `pin`, on the side the pin points to, where
        it overlaps no other part if possible."""
        px, py = pin
        dx, dy = px - owner.x, py - owner.y
        side = ("left" if dx < 0 else "right") if abs(dx) >= abs(dy) else \
               ("top" if dy < 0 else "bottom")
        sx = {"left": -1, "right": 1}.get(side, 0)

        def spot(g: float) -> tuple[float, float, int, bool]:
            if sym.comp_type == "ground":     # pin 25 above the centre
                if side == "top":
                    return (px, py - g - 25, 180, False)
                if side == "bottom":
                    return (px, py + g + 25, 0, False)
                return (px + sx * g, py + 25, 0, False)
            if sym.comp_type in ("vcc", "vdd"):  # pin 25 below the centre
                if side == "bottom":
                    return (px, py + g + 25, 180, False)
                if side == "top":
                    return (px, py - g - 25, 0, False)
                return (px + sx * g, py - 25, 0, False)
            # label: pin at the tip, the tag points away from the part
            return {"left": (px - g, py, 0, True), "right": (px + g, py, 0, False),
                    "top": (px, py - g, 270, False),
                    "bottom": (px, py + g, 90, False)}[side]

        others = [c.body_bbox() for c in comps if c is not sym]
        choice = None
        for g in (30, 50, 70, 90):
            sym.x, sym.y, sym.rotation, sym.flip = spot(g)
            x0, y0, x1, y1 = sym.extent_bbox()
            if not any(x0 < bx1 and bx0 < x1 and y0 < by1 and by0 < y1
                       for bx0, by0, bx1, by1 in others):
                choice = (sym.x, sym.y, sym.rotation, sym.flip)
                break
        sym.x, sym.y, sym.rotation, sym.flip = choice or spot(30)
        sym.x, sym.y = round(sym.x, 1), round(sym.y, 1)

    def _tie_homes(self, specs: dict[str, Any]):
        """Where a GND/VCC/label symbol should go when its net has several.

        Each such symbol only gets wired to the pins nearest to it, so piling
        all of them onto the middle of the net would serve nobody. Instead
        every symbol goes next to the part of its net that is furthest from
        the symbols already placed — spreading them out over the parts.
        """
        nets: dict[str, tuple[list[str], list[str]]] = {}
        for net in self.circuit.get("nets", []):
            ids = list(dict.fromkeys(str(p).split(".")[0]
                                     for p in net.get("pins", [])))
            symbols = [c for c in ids
                       if str(specs.get(c, {}).get("type", "")).lower() in ties.TIE_TYPES]
            if len(symbols) < 2:
                continue
            parts = [c for c in ids if c not in symbols and c in specs]
            for t in symbols:
                nets[t] = (symbols, parts)

        def home(cid: str, positions: dict[str, Any]) -> dict[str, Any] | None:
            if cid not in nets:
                return None
            symbols, parts = nets[cid]
            placed_parts = [positions[c] for c in parts if c in positions]
            if not placed_parts:
                return None
            siblings = [positions[t] for t in symbols if t != cid and t in positions]

            def spare(p: dict[str, Any]) -> float:
                if not siblings:
                    return 0.0
                return min(abs(p.get("x", 0) - s.get("x", 0))
                           + abs(p.get("y", 0) - s.get("y", 0)) for s in siblings)
            return max(placed_parts, key=spare)
        return home


    @staticmethod
    def _to_cell(pos: dict[str, Any]) -> tuple[int, int]:
        return (round(float(pos.get("x", 0)) / AUTO_STEP),
                round(float(pos.get("y", 0)) / AUTO_STEP))

    def _footprint_cells(self, spec: dict[str, Any]) -> tuple[int, int]:
        """How many cells a symbol needs, labels included.

        The old placer used one fixed cell for everything, so an ESP32 board
        (144 px tall) landed inside a 140 px slot and overlapped its neighbour.
        """
        try:
            comp = build_component(spec)
        except (ValueError, KeyError, TypeError):
            return (1, 1)
        boxes = [comp.body_bbox(), *comp.label_boxes()]
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        # Symbols sit at their centre, so the half-extent decides the span.
        half_w = max(abs(x0), abs(x1)) + AUTO_GAP
        half_h = max(abs(y0), abs(y1)) + AUTO_GAP
        return (max(1, int(math.ceil(2 * half_w / AUTO_STEP))),
                max(1, int(math.ceil(2 * half_h / AUTO_STEP))))

    @staticmethod
    def _cells_of(cell: tuple[int, int], size: tuple[int, int]):
        cw, ch = size
        x0 = cell[0] - (cw - 1) // 2
        y0 = cell[1] - (ch - 1) // 2
        for dx in range(cw):
            for dy in range(ch):
                yield (x0 + dx, y0 + dy)

    @classmethod
    def _occupy(cls, occupied: set[tuple[int, int]], cell: tuple[int, int],
                size: tuple[int, int]) -> None:
        occupied.update(cls._cells_of(cell, size))

    @classmethod
    def _collides(cls, occupied: set[tuple[int, int]], cell: tuple[int, int],
                  size: tuple[int, int]) -> bool:
        return any(c in occupied for c in cls._cells_of(cell, size))

    @staticmethod
    def _placement_order(unplaced: list[str], adj: dict[str, set[str]],
                         positions: dict[str, Any]) -> list[str]:
        """Place well-connected parts first, then walk outwards along the nets.

        In document order a part is usually placed before any of its neighbours,
        so the anchor average has nothing to work with and everything piles up
        around the origin.
        """
        todo = set(unplaced)
        order: list[str] = []
        # Seeds: parts next to something already positioned, else the hub.
        while todo:
            seeds = sorted(
                (c for c in todo if any(n in positions or n in order
                                        for n in adj.get(c, ()))),
                key=lambda c: -len(adj.get(c, ())),
            )
            start = seeds[0] if seeds else max(
                sorted(todo), key=lambda c: len(adj.get(c, ())))
            queue = [start]
            while queue:
                cid = queue.pop(0)
                if cid not in todo:
                    continue
                todo.discard(cid)
                order.append(cid)
                queue.extend(sorted(n for n in adj.get(cid, ()) if n in todo))
        return order

    def set_positions(self, updates: dict[str, Any]) -> None:
        """Store positions as given.

        Grid snapping happens in the editor, not here: pin pitches differ per
        symbol (ICs 40 px, two-terminal parts 50 px), so forcing every component
        onto the grid would make some pins impossible to line up. The editor
        decides; this only guards against float noise.
        """
        positions: dict[str, Any] = self.layout["positions"]
        for cid, p in updates.items():
            if not isinstance(p, dict):
                continue
            entry = positions.setdefault(cid, {})
            entry["x"] = round(float(p.get("x", entry.get("x", 0))), 1)
            entry["y"] = round(float(p.get("y", entry.get("y", 0))), 1)
            entry["rotation"] = int(p.get("rotation", entry.get("rotation", 0))) % 360
            entry["flip"] = bool(p.get("flip", entry.get("flip", False)))
            entry["locked"] = bool(p.get("locked", entry.get("locked", False)))
            entry.pop("auto", None)  # touched by a human => no longer auto
        self.version += 1

    def set_waypoints(self, edge: str, points: list[Any]) -> None:
        """Guide one wire (a pin-to-pin connection) through the given points.

        `edge` is the key produced by router.edge_key, e.g. "R1.2|U1.DIS".
        An empty list removes the guidance and returns the wire to auto-routing.

        Snapping is the editor's job (grid, or a pin's x/y when one is close):
        pins are often off the grid, and forcing waypoints onto it would put a
        jog into every wire that should run straight into such a pin.
        """
        wires: dict[str, Any] = self.layout.setdefault("wires", {})
        cleaned: list[list[float]] = []
        for p in points:
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                continue
            try:
                cleaned.append([round(float(p[0]), 1), round(float(p[1]), 1)])
            except (TypeError, ValueError):
                continue
        if cleaned:
            wires[edge] = cleaned
        else:
            wires.pop(edge, None)
        self.version += 1

    def set_tie(self, part: str, symbol: str | None) -> list[str]:
        """Wire every pin of `part` that shares a net with the GND/VCC/label
        `symbol` to that symbol, overriding the nearest-symbol rule.
        symbol=None hands all of `part`'s pins back to that rule.

        Returns the pin references that changed hands.
        """
        ties: dict[str, str] = self.layout.setdefault("ties", {})
        prefix = f"{part}."
        if symbol is None:
            dropped = [ref for ref in ties if ref.startswith(prefix)]
            for ref in dropped:
                del ties[ref]
            self.version += 1
            return dropped
        changed: list[str] = []
        for net in self.circuit.get("nets", []):
            pins = [str(p) for p in net.get("pins", [])]
            if not any(p.split(".", 1)[0] == symbol for p in pins):
                continue
            for ref in pins:
                if ref.startswith(prefix):
                    ties[ref] = symbol
                    changed.append(ref)
        self.version += 1
        return changed

    # ── Notes ────────────────────────────────────────────────────────────────

    def notes(self) -> list[dict[str, Any]]:
        """Annotations authored in the circuit document (text + anchor)."""
        return [n for n in self.circuit.get("notes", []) if isinstance(n, dict)]

    def autoplace_notes(self) -> list[str]:
        """Give new notes a spot next to whatever they are anchored to.

        Notes anchored to nearby parts would otherwise land on top of each
        other, so an occupied slot pushes the next note further down.
        """
        placed: dict[str, Any] = self.layout.setdefault("notes", {})
        positions = self.layout["positions"]
        step = 95.0
        taken = [(float(v.get("x", 0)), float(v.get("y", 0)))
                 for v in placed.values() if isinstance(v, dict)]
        added: list[str] = []
        for n in self.notes():
            nid = str(n.get("id", ""))
            if not nid or nid in placed:
                continue
            anchor = str(n.get("anchor", "")).split(".")[0]
            base = positions.get(anchor)
            x = float(base.get("x", 0)) + 150 if base else 0.0
            y = float(base.get("y", 0)) - 110 if base else 0.0
            while any(abs(px - x) < DEFAULT_NOTE_W and abs(py - y) < step
                      for px, py in taken):
                y += step
            taken.append((x, y))
            placed[nid] = {"x": x, "y": y, "w": DEFAULT_NOTE_W, "hidden": False}
            added.append(nid)
        return added

    def set_note(self, note_id: str, x: Any = None, y: Any = None,
                 w: Any = None, hidden: Any = None) -> None:
        notes: dict[str, Any] = self.layout.setdefault("notes", {})
        entry = notes.setdefault(
            note_id, {"x": 0.0, "y": 0.0, "w": DEFAULT_NOTE_W, "hidden": False})
        if x is not None:
            entry["x"] = round(float(x), 1)
        if y is not None:
            entry["y"] = round(float(y), 1)
        if w is not None:
            entry["w"] = max(90.0, round(float(w), 1))
        if hidden is not None:
            entry["hidden"] = bool(hidden)
        self.version += 1

    # ── Instantiation ────────────────────────────────────────────────────────

    def instantiate(self) -> tuple[list[Component], list[str]]:
        """Build positioned symbol objects. Returns (components, errors)."""
        comps: list[Component] = []
        errors: list[str] = []
        positions = self.layout["positions"]
        for spec in self.circuit.get("components", []):
            try:
                comp = build_component(spec)
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(str(exc))
                continue
            pos = positions.get(comp.comp_id, {})
            comp.x = float(pos.get("x", 0))
            comp.y = float(pos.get("y", 0))
            comp.rotation = int(pos.get("rotation", 0)) % 360
            comp.flip = bool(pos.get("flip", False))
            comps.append(comp)
        return comps, errors

    def auto_placed_ids(self) -> list[str]:
        return [cid for cid, p in self.layout["positions"].items() if p.get("auto")]


def _outward(comp: Component, px: float, py: float) -> tuple[int, int]:
    """Which way a pin at (px, py) points out of its part: one of the four
    unit directions."""
    ox, oy = px - comp.x, py - comp.y
    if abs(ox) >= abs(oy):
        return (1 if ox >= 0 else -1), 0
    return 0, (1 if oy >= 0 else -1)


def list_projects(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    names = [p.name[: -len(".circuit.json")]
             for p in folder.glob("*.circuit.json")]
    return sorted(n for n in names if is_safe_name(n))
